"""Raven orchestration service: owns the Store, starts agents/runs, reports.

The engine modules (workers/engine/specs/runner) are imported lazily so the
server keeps working (with fallbacks) while they are being built. Tests can
override `get_worker`, `run_agent`, `run_spec` on the instance."""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
import logging
import random
import re
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

from raven_core.events import (
    Agent,
    AgentStatus,
    CreateAgentRequest,
    CreateRunRequest,
    Event,
    ExecutionProfile,
    Placement,
    Platform,
    Run,
    RunStatus,
    StopCause,
)
from raven_core.execution import ExecutionProfiles
from raven_core.placement import describe_host, requested
from raven_core.server.config import Settings
from raven_core.server.intents import HELP, follow_up_task, parse_command, task_platforms
from raven_core.store import (
    TERMINAL_AGENT,
    TERMINAL_RUN,
    AgentHandle,
    StopSignal,
    Store,
    coerce_event,
    default_persist_path,
)

log = logging.getLogger("raven.service")

STOP_GRACE_S = 8.0
STOPPED_BY_USER = "Stopped by user"


class StartedAgent(Agent):
    """An Agent that can also be awaited (returns itself), so `start_agent` works
    whether the runner calls it as `start_agent(...)` or `await start_agent(...)`."""

    def __await__(self):  # type: ignore[override]
        async def _self() -> "StartedAgent":
            return self

        return _self().__await__()


class _Done:
    """Return value of `emit`: harmless if ignored, also awaitable."""

    def __await__(self):
        return iter(())


_DONE = _Done()


class NotFound(KeyError):
    pass


def _title_from_task(task: str) -> str:
    t = " ".join(task.split())
    return t if len(t) <= 60 else t[:57] + "..."


def _is_fake(w: Any) -> bool:
    from raven_core.workers.fake import FakeWorker

    return isinstance(w, FakeWorker) or type(w).__name__.startswith("Fake")


def _is_bare_metal(w: Any) -> bool:
    return bool(getattr(w, "bare_metal", False))


def _placement_of(w: Any) -> Placement | None:
    if w is None or _is_fake(w):
        return None
    return Placement.bare_metal if _is_bare_metal(w) else Placement.vm


def _accepts(fn: Callable[..., Any], name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind == inspect.Parameter.VAR_KEYWORD for p in params)


def _worker_name(w: Any, platform: Platform) -> str:
    for attr in ("name", "id", "worker_id"):
        v = getattr(w, attr, None)
        if isinstance(v, str) and v:
            return v
    return f"{platform.value}:{type(w).__name__}"


def _spec_dict(spec: Any) -> dict[str, Any]:
    if isinstance(spec, dict):
        return spec
    if hasattr(spec, "model_dump"):
        return spec.model_dump(mode="json")
    if hasattr(spec, "__dict__"):
        return dict(vars(spec))
    return {}


def _plat(v: Any) -> Platform | None:
    try:
        return Platform(getattr(v, "value", v))
    except ValueError:
        return None


def spec_platforms(spec: Any) -> list[Platform]:
    plats = getattr(spec, "platforms", None)
    if plats is None and isinstance(spec, dict):
        plats = spec.get("platforms")
    if callable(plats):
        plats = plats()
    out: list[Platform] = []
    if plats:
        for p in plats:
            pp = _plat(p)
            if pp and pp not in out:
                out.append(pp)
        if out:
            return out
    agents = getattr(spec, "agents", None)
    if agents is None and isinstance(spec, dict):
        agents = spec.get("agents")
    for a in agents or []:
        p = a.get("platform") if isinstance(a, dict) else getattr(a, "platform", None)
        pp = _plat(p)
        if pp and pp not in out:
            out.append(pp)
    return out


_WORDS = ["falcon", "maple", "comet", "ember", "quartz", "harbor", "nova", "tundra", "cobalt", "lumen"]


class Raven:
    def __init__(self, settings: Settings | None = None, store: Store | None = None) -> None:
        self.settings = settings or Settings()
        self.store = store or Store(persist_path=default_persist_path(self.settings.artifacts_dir))
        self.execution_profiles = ExecutionProfiles(self.settings.artifacts_dir / "execution.json")
        # Engine hooks; None -> import from raven_core.* lazily.
        self.get_worker: Callable[..., Any] | None = None
        self.run_agent: Callable[..., Any] | None = None
        self.run_spec: Callable[..., Any] | None = None
        self.plan_task: Callable[..., Any] | None = None
        self.fix_task: Callable[..., Any] | None = None
        self.prepare_worker: Callable[..., Any] | None = None
        self._run_tasks: dict[str, asyncio.Task[Any]] = {}
        self._run_deadlines: dict[str, float] = {}
        self._stopped_runs: set[str] = set()
        self._bg: set[asyncio.Task[Any]] = set()
        self._screen_lock = asyncio.Lock()
        self._target_locks: dict[str, asyncio.Lock] = {}
        self._prepared_workers: dict[tuple[str, Platform], Any] = {}
        self._owned_workers: dict[int, Any] = {}
        self._agent_workers: dict[str, Any] = {}
        self._approvals: dict[str, tuple[str, dict[str, Any], asyncio.Future[str | None]]] = {}

    async def execution_state(self, *, check: bool = False) -> dict[str, Any]:
        profile = self.execution_profiles.snapshot()
        if check:
            from raven_core.workers import inspect_targets

            targets = await asyncio.wait_for(inspect_targets(profile), timeout=45)
        else:
            targets = [
                {"platform": Platform.macos.value, "target": "This Mac"},
                {"platform": Platform.ios.value, "target": f"Device Hub · {profile.ios_device_id or profile.ios_target}"},
                {"platform": Platform.linux.value, "target": profile.linux_space if profile.linux_target == "apple-container" else "Devin Linux VM"},
                {"platform": Platform.windows.value, "target": "Devin Windows VM"},
            ]
            targets = [{**target, "status": "unchecked", "detail": "Check targets before starting computer use."} for target in targets]
        return {"profile": profile.model_dump(mode="json"), "targets": targets}

    async def save_execution(self, profile: ExecutionProfile) -> dict[str, Any]:
        self.execution_profiles.save(profile)
        return await self.execution_state()

    # ------------------------------------------------------------------ engine resolution
    def _get_worker_fn(self) -> Callable[..., Any]:
        if self.get_worker is not None:
            return self.get_worker
        from raven_core.workers import get_worker

        return get_worker

    def _run_agent_fn(self) -> Callable[..., Any]:
        if self.run_agent is not None:
            return self.run_agent
        from raven_core.engine import run_agent

        return run_agent

    def _run_spec_fn(self) -> Callable[..., Any] | None:
        if self.run_spec is not None:
            return self.run_spec
        try:
            from raven_core.runner import run_spec
        except ImportError:
            log.warning("raven_core.runner not available; using built-in fallback runner")
            return None
        return run_spec

    # ------------------------------------------------------------------ events
    def emit(self, event: Event) -> Event:
        event = coerce_event(event)
        handle = self.store.handles.get(event.agent_id or "")
        if event.type == "agent.status" and event.data.get("status") == AgentStatus.stopped:
            if handle and handle.stop.cause == StopCause.deadline:
                event = event.model_copy(update={"data": {**event.data, "status": AgentStatus.error.value}})
        return self.store.emit(event)

    def _status(self, agent: Agent, status: AgentStatus) -> None:
        self.emit(Event(type="agent.status", agent_id=agent.id, run_id=agent.run_id, data={"status": status.value}))

    def _sysmsg(self, agent: Agent, text: str) -> None:
        self.emit(
            Event(
                type="agent.message",
                agent_id=agent.id,
                run_id=agent.run_id,
                data={"role": "system", "text": text},
            )
        )

    def make_emit(self, agent: Agent) -> Callable[..., Any]:
        def emit(event_or_type: Any = None, data: dict[str, Any] | None = None, **kw: Any) -> _Done:
            try:
                if event_or_type is None and "type" in kw:
                    event_or_type = kw.pop("type")
                kw.setdefault("agent_id", agent.id)
                kw.setdefault("run_id", agent.run_id)
                ev = coerce_event(event_or_type, data, **kw)
                self.emit(ev)
            except Exception:
                log.exception("bad event from engine")
            return _DONE

        return emit

    # ------------------------------------------------------------------ specs
    def list_specs(self) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] | None = None
        try:
            from raven_core.specs import list_specs

            specs = list_specs(self.settings.specs_dir)
        except Exception as e:
            log.debug("specs.list_specs unavailable (%s); using fallback", type(e).__name__)
        if specs is None:
            specs = self._fallback_list_specs()
        out = []
        for s in specs:
            d = s if isinstance(s, dict) else _spec_dict(s)
            out.append(
                {
                    "name": d.get("name"),
                    "description": (d.get("description") or "").strip(),
                    "platforms": [p.value for p in spec_platforms(d)],
                }
            )
        return out

    def spec_names(self) -> list[str]:
        return [s["name"] for s in self.list_specs() if s.get("name")]

    def _spec_files(self) -> list[Path]:
        d = self.settings.specs_dir
        if not d.exists():
            return []
        return sorted([*d.glob("*.yaml"), *d.glob("*.yml")])

    def _fallback_list_specs(self) -> list[dict[str, Any]]:
        out = []
        for f in self._spec_files():
            try:
                d = yaml.safe_load(f.read_text()) or {}
                d.setdefault("name", f.stem)
                out.append(d)
            except Exception:
                log.warning("bad spec file %s", f.name)
        return out

    def load_spec(self, name: str) -> Any:
        try:
            from raven_core.specs import load_spec
        except ImportError:
            load_spec = None
        if load_spec is not None:
            try:
                return load_spec(name, self.settings.specs_dir)
            except (FileNotFoundError, KeyError) as e:
                raise NotFound(f"spec not found: {name}") from e
            except Exception:
                log.exception("specs.load_spec failed; trying fallback")
        for d in self._fallback_list_specs():
            if d.get("name") == name or name in (str(d.get("name")),):
                return d
        for f in self._spec_files():
            if f.stem == name:
                d = yaml.safe_load(f.read_text()) or {}
                d.setdefault("name", f.stem)
                return d
        raise NotFound(f"spec not found: {name}")

    # ------------------------------------------------------------------ agents
    def start_agent(self, req: CreateAgentRequest, run_id: str | None = None) -> StartedAgent:
        """Create an agent, pick its worker and spawn the engine task."""
        if isinstance(req, dict):
            req = CreateAgentRequest.model_validate(req)
        run = self.store.runs.get(run_id) if run_id else None
        selected = run.execution if run else req.execution
        if req.repair and req.previous_agent_id and selected is None:
            selected = self.get_agent(req.previous_agent_id).execution
        execution = self.execution_profiles.snapshot(selected, model=None if run else req.model)
        worker, pending, error, worker_name = self._select_worker(req, execution, run_id)
        agent = StartedAgent(
            run_id=run_id,
            platform=req.platform,
            worker=worker_name,
            title=req.title or _title_from_task(req.task),
            task=req.task,
            placement=_placement_of(worker),
            execution=execution,
            original_task=req.task,
            repo_path=req.repo_path,
        )
        self.store.add_agent(agent)
        self.emit(Event(type="agent.created", agent_id=agent.id, run_id=run_id, data={"agent": agent.model_dump(mode="json")}))
        handle = AgentHandle(inbox=asyncio.Queue(), stop=StopSignal())
        if run_id is not None and run_id in self._stopped_runs:
            handle.stop.set("Run was stopped before this agent started")
        self.store.handles[agent.id] = handle
        if worker is not None:
            self._agent_workers[agent.id] = worker
            self._owned_workers[id(worker)] = worker
        handle.task = asyncio.get_running_loop().create_task(
            self._agent_task(agent, worker, pending, handle, error, timeout_s=req.timeout_s, request=req), name=f"agent-{agent.id}"
        )
        return agent

    def _select_worker(
        self, req: CreateAgentRequest, execution: ExecutionProfile, run_id: str | None = None,
    ) -> tuple[Any, Any, Exception | None, str]:
        worker: Any = None
        pending: Any = None
        error: Exception | None = None
        worker_name = req.platform.value
        try:
            get_worker = self._get_worker_fn()
            worker_options: dict[str, Any] = {}
            if _accepts(get_worker, "placement"):
                worker_options["placement"] = req.placement
            if _accepts(get_worker, "execution"):
                worker_options["execution"] = execution
            w = self._prepared_workers.get((run_id, req.platform)) if run_id else None
            if w is None:
                w = get_worker(req.platform, **worker_options)
            if inspect.isawaitable(w):
                pending = w
            else:
                worker = w
                worker_name = _worker_name(w, req.platform)
        except Exception as e:  # missing module, no sandbox, ...
            error = e
        return worker, pending, error, worker_name

    def _resume_agent(self, agent: Agent, text: str) -> str | None:
        if agent.run_id is not None:
            return None
        plats = task_platforms(text)
        task = follow_up_task(text, agent.original_task or agent.task)
        req = CreateAgentRequest(
            platform=plats[0] if len(plats) == 1 else agent.platform, task=task, placement=requested(text),
            model=agent.execution.model if agent.execution else None, repo_path=agent.repo_path,
        )
        execution = self.execution_profiles.snapshot(None, model=req.model)
        worker, pending, error, worker_name = self._select_worker(req, execution)
        previous = self.store.handles.get(agent.id)
        handle = AgentHandle(inbox=asyncio.Queue(), stop=StopSignal())
        self.store.handles[agent.id] = handle
        self._sysmsg(agent, f"Continuing in this thread on {req.platform.value}.")

        async def attempt() -> None:
            if previous is not None and previous.task is not None:
                await asyncio.wait({previous.task})
            agent.platform, agent.worker, agent.placement = req.platform, worker_name, _placement_of(worker)
            agent.task = agent.original_task = task
            agent.execution, agent.verdict_reason = execution, None
            self._status(agent, AgentStatus.queued)
            if worker is not None:
                self._agent_workers[agent.id] = worker
                self._owned_workers[id(worker)] = worker
            handle.first_event = len(self.store.agent_events.get(agent.id, []))
            await self._agent_task(agent, worker, pending, handle, error, request=req)

        handle.task = asyncio.get_running_loop().create_task(attempt(), name=f"agent-{agent.id}")
        return agent.id

    async def _agent_task(
        self,
        agent: Agent,
        worker: Any,
        pending: Any,
        handle: AgentHandle,
        error: Exception | None,
        timeout_s: float | None = None,
        request: CreateAgentRequest | None = None,
    ) -> None:
        final: AgentStatus | None = None
        held_lock: asyncio.Lock | None = None
        began = False
        phase = "selecting target"
        try:
            if error is None and pending is not None:
                try:
                    worker = await pending
                    agent.worker = _worker_name(worker, agent.platform)
                    agent.placement = _placement_of(worker)
                except Exception as e:
                    error = e
            if error is None and agent.run_id is None:
                phase = "fixing" if request and request.repair else "planning"
                await self._plan_agent(agent, handle, request)
            if error is not None:
                raise error
            phase = "waiting for target"
            lock = self._lock_for_worker(worker)
            if lock.locked():
                self._sysmsg(agent, f"Waiting for `{_worker_name(worker, agent.platform)}`: another test owns its screen.")
            await self._acquire_lock(lock, handle)
            held_lock = lock
            phase = "preparing target"
            async def say(text: str) -> None:
                self._sysmsg(agent, text)
            original = worker
            worker = await self._stage(
                self._prepare_target(worker, agent.platform, agent.execution, say),
                handle, self.settings.prepare_timeout_s,
            )
            self._agent_workers[agent.id] = worker
            self._owned_workers[id(worker)] = worker
            if original is not worker:
                await self._close_unused(original)
            resolved_lock = self._lock_for_worker(worker)
            if resolved_lock is not held_lock:
                held_lock.release()
                held_lock = None
                await self._acquire_lock(resolved_lock, handle)
                held_lock = resolved_lock
            if handle.stop.is_set():
                raise asyncio.CancelledError
            agent.worker = _worker_name(worker, agent.platform)
            agent.placement = _placement_of(worker)
            self.emit(Event(type="agent.message", agent_id=agent.id, run_id=agent.run_id, data={
                "role": "system", "phase": "target", "state": "ready",
                "worker": agent.worker, "placement": agent.placement,
                "text": f"Resolved {agent.platform.value} target `{agent.worker}` "
                        f"with {agent.execution.mode_for(agent.platform).value if agent.execution else 'configured H mode'}.",
            }))
            if _is_bare_metal(worker):
                self._sysmsg(
                    agent,
                    f"Running on {describe_host()}, bare metal: Raven controls the mouse and keyboard "
                    "until this agent finishes. Press Stop to take them back.",
                )
                began = True
                await self._call_optional(worker, "begin")
            phase = "executing QA"
            if agent.run_id in self._run_deadlines:
                remaining = self._run_deadlines[agent.run_id] - asyncio.get_running_loop().time() - STOP_GRACE_S
                if remaining <= 0:
                    handle.stop.set("Run deadline exhausted before computer use", cause=StopCause.deadline)
                    raise TimeoutError
                timeout_s = min(timeout_s, remaining) if timeout_s is not None else remaining
            self._status(agent, AgentStatus.running)
            run_agent = self._run_agent_fn()
            kwargs: dict[str, Any] = {}
            if timeout_s and _accepts(run_agent, "timeout_s"):
                kwargs["timeout_s"] = timeout_s
            if agent.execution and _accepts(run_agent, "model"):
                kwargs["model"] = agent.execution.model
            from raven_core.hosts import Host

            if agent.execution and isinstance(worker, Host) and _accepts(run_agent, "mode"):
                kwargs["mode"] = agent.execution.mode_for(agent.platform).value
            res = run_agent(
                agent, worker, self.make_emit(agent), handle.inbox, handle.stop,
                self.settings.artifacts_dir, **kwargs,
            )
            if inspect.isawaitable(res):
                await res
            if agent.status in (AgentStatus.passed, AgentStatus.failed):
                final = agent.status
            elif handle.stop.is_set():
                final = self._stop_status(handle)
            elif agent.status in (AgentStatus.stopped, AgentStatus.error):
                final = agent.status
            else:
                self.emit(
                    Event(
                        type="agent.verdict",
                        agent_id=agent.id,
                        run_id=agent.run_id,
                        data={"passed": False, "reason": "agent ended without a verdict"},
                    )
                )
                final = AgentStatus.error
        except asyncio.CancelledError:
            final = self._stop_status(handle)
            agent.verdict_reason = getattr(handle.stop, "reason", None) or f"Agent task cancelled while {phase}"
        except Exception as e:
            from raven_core.integrations.report import _safe

            log.warning("agent %s failed while %s (%s)", agent.id, phase, type(e).__name__)
            reason = str(_safe(str(e))) or type(e).__name__
            self._sysmsg(agent, f"Agent blocked while {phase}: {reason}")
            agent.verdict_reason = agent.verdict_reason or f"Failed while {phase}: {reason}"
            final = AgentStatus.error
        finally:
            try:
                cleanup = asyncio.create_task(self._cleanup_agent(agent, worker, began))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                    except Exception as error:
                        log.warning("Agent cleanup failed (%s)", type(error).__name__)
                await asyncio.gather(cleanup, return_exceptions=True)
            finally:
                if held_lock is not None:
                    held_lock.release()
                if final is not None:
                    self._status(agent, final)
                    attempt_events = self.store.agent_events.get(agent.id, [])[handle.first_event:]
                    if not any(event.type == "agent.verdict" for event in attempt_events):
                        self.emit(Event(type="agent.verdict", agent_id=agent.id, run_id=agent.run_id, data={
                            "passed": final == AgentStatus.passed,
                            "reason": agent.verdict_reason or "Agent ended without a verification verdict",
                            "category": "timeout" if handle.stop.cause == StopCause.deadline else phase,
                        }))
                    if final in (AgentStatus.failed, AgentStatus.error):
                        self._write_agent_failure(agent)

    async def _cleanup_agent(self, agent: Agent, worker: Any, began: bool) -> None:
        try:
            async with asyncio.timeout(20):
                if began:
                    await self._call_optional(worker, "finish")
                if worker is not None:
                    self._agent_workers.pop(agent.id, None)
                    await self._close_unused(worker, agent)
        finally:
            self._agent_workers.pop(agent.id, None)

    @staticmethod
    def _stop_status(handle: AgentHandle) -> AgentStatus:
        return AgentStatus.error if handle.stop.cause == StopCause.deadline else AgentStatus.stopped

    def _lock_for_worker(self, worker: Any) -> asyncio.Lock:
        if _is_bare_metal(worker):
            return self._screen_lock
        key = f"{getattr(worker, 'platform', '')}:{getattr(worker, 'name', id(worker))}"
        return self._target_locks.setdefault(key, asyncio.Lock())

    async def _acquire_lock(self, lock: asyncio.Lock, handle: AgentHandle) -> None:
        acquired = False

        async def acquire() -> None:
            nonlocal acquired
            await lock.acquire()
            acquired = True

        try:
            await self._stage(acquire(), handle, self.settings.run_timeout_s)
        except BaseException:
            if acquired:
                lock.release()
            raise

    async def _prepare_target(self, worker: Any, platform: Platform, execution: ExecutionProfile, say: Any) -> Any:
        if _is_fake(worker):
            return worker
        from raven_core import machines

        if self.prepare_worker is not None:
            return await self.prepare_worker(worker, execution=execution, say=say)
        if self.get_worker is not None:
            if _is_bare_metal(worker):
                preflight = getattr(worker, "preflight", None)
                if preflight is not None:
                    await preflight()
            elif getattr(worker, "target", None) in ("apple-container", "lume"):
                await machines.ensure(platform, say)
                await machines.show(platform, say)
            return worker
        from raven_core.workers import prepare_worker

        cached = getattr(worker, "_raven_resolved_worker", None)
        resolved = await prepare_worker(worker, execution=execution, say=say)
        if platform == Platform.ios and cached is not None:
            from raven_core.workers.device_hub import prepare_device

            # Fresh preparation already binds the exact device. Cached preflight
            # does not: revalidate once per setup/execution, under the screen lock.
            resolved.device = await prepare_device(execution)
        if getattr(resolved, "target", None) == "apple-container":
            try:
                await machines.show(platform, say, execution=execution, name=resolved.name)
            except Exception as error:
                await say(f"Live viewer unavailable ({type(error).__name__}); selected target remains ready.")
        return resolved

    async def _close_unused(self, worker: Any, agent: Agent | None = None) -> None:
        active = [*self._agent_workers.values(), *self._prepared_workers.values()]
        if any(candidate is worker or getattr(candidate, "_raven_resolved_worker", None) is worker for candidate in active):
            return
        close = getattr(worker, "close", None)
        try:
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=15)
            worker._closed = True
            self._owned_workers.pop(id(worker), None)
        except Exception as error:
            log.warning("Worker cleanup failed (%s)", type(error).__name__)
            if agent is not None:
                self._sysmsg(agent, "Worker cleanup could not be confirmed; check the target's session before starting another run.")

    def _write_agent_failure(self, agent: Agent) -> None:
        from raven_core.integrations.report import build_failure_context
        from raven_core.planning import task_identity

        context = build_failure_context([agent], self.store.agent_events.get(agent.id, []), platform=agent.platform)
        context.update(task_id=task_identity(agent.original_task or agent.task, agent.platform), platform=agent.platform.value)
        directory = self.settings.artifacts_dir / agent.id
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "failure.json").write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")
        except OSError:
            log.warning("Could not persist failure context for %s", agent.id)

    async def _stage(self, awaitable: Any, handle: AgentHandle, timeout: float) -> Any:
        if handle.stop.is_set():
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise asyncio.CancelledError
        task = asyncio.ensure_future(awaitable)
        stopper = asyncio.create_task(handle.stop.wait())
        try:
            done, _ = await asyncio.wait({task, stopper}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if stopper in done:
                raise asyncio.CancelledError
            if task not in done:
                raise TimeoutError("Execution preparation exceeded its deadline")
            return task.result()
        finally:
            for pending in (task, stopper):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, stopper, return_exceptions=True)

    def _previous_failure(self, agent: Agent, request: CreateAgentRequest | None) -> Agent | None:
        candidates = list(self.store.agents.values())
        if request and request.previous_agent_id:
            candidates = [self.get_agent(request.previous_agent_id)]
        matching = [
            previous for previous in candidates
            if previous.id != agent.id
            and previous.platform == agent.platform
            and previous.repo_path == agent.repo_path
            and (previous.original_task or previous.task).strip() == (agent.original_task or agent.task).strip()
            and previous.status in (AgentStatus.error, AgentStatus.failed)
        ]
        return max(matching, key=lambda previous: previous.created_at, default=None)

    async def _plan_agent(self, agent: Agent, handle: AgentHandle, request: CreateAgentRequest | None) -> None:
        from raven_core.planning import PlanResult, PlanningError, build_failure_context, fix_task, plan_task, task_identity

        previous = self._previous_failure(agent, request)
        failure = None
        if previous is not None:
            failure = build_failure_context([previous], self.store.agent_events.get(previous.id, []), platform=agent.platform)
            failure.update(task=agent.original_task or agent.task, task_id=task_identity(agent.original_task or agent.task, agent.platform))
        planner = self.plan_task or plan_task
        try:
            original_plan = None
            if previous and previous.planning_session_id:
                path = self.settings.artifacts_dir / previous.id / "plan.json"
                if path.is_file():
                    original_plan = PlanResult.model_validate_json(path.read_text(encoding="utf-8"))
                    if original_plan.session_id != previous.planning_session_id:
                        raise PlanningError("The saved plan belongs to a different Devin session.")
            if request and request.repair:
                if previous is None or previous.run_id or original_plan is None or failure is None:
                    raise PlanningError("A repair needs a failed free-form test and its saved Devin plan.")
                categories = [item.get("verdict", {}).get("category") for item in failure.get("failures", [])]
                if previous.status != AgentStatus.failed or any(
                    category in ("timeout", "execution", "blocked", "cancelled", "planning") for category in categories
                ):
                    raise PlanningError("Resolve the reported infrastructure or planning blocker before requesting an application code repair.")
                if not previous.execution or previous.execution.planner != agent.execution.planner:
                    raise PlanningError("A repair must use the original planner mode and session.")
                fixer = self.fix_task or fix_task

                async def repair():
                    from raven_core.integrations import linkup

                    deadline = asyncio.get_running_loop().time() + self.settings.fix_timeout_s
                    self._sysmsg(agent, "Looking up public platform references before the Devin repair.")
                    research = await linkup.search(linkup.failure_query(agent.platform.value, categories[0] or "assertion"))
                    if research is None:
                        self._sysmsg(agent, "No external research is available; the repair will use the recorded failure evidence.")
                    return await fixer(
                        agent.original_task or agent.task, agent.platform, plan=original_plan,
                        failure_context=failure, mode=agent.execution.planner, repo_path=agent.repo_path,
                        timeout_s=max(0.01, deadline - asyncio.get_running_loop().time()),
                        external_context=research, emit=self.make_emit(agent),
                        approve=lambda operation: self._request_approval(agent, operation),
                    )

                fixed = await self._stage(
                    repair(),
                    handle, self.settings.fix_timeout_s + STOP_GRACE_S,
                )
                if fixed.session_id != original_plan.session_id or fixed.task != original_plan.task:
                    raise PlanningError("Devin changed the approved QA task or repair session; H will not run it.")
                if fixed.report.outcome != "changes_applied":
                    raise PlanningError(f"Devin did not complete a repair: {fixed.report.summary}")
                result = original_plan
                directory = self.settings.artifacts_dir / agent.id
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "fix.json").write_text(fixed.model_dump_json(indent=2) + "\n", encoding="utf-8")
                handle.inbox.put_nowait(
                    "Setup after a code repair: rebuild and relaunch the target application using its documented "
                    "project setup, then execute the unchanged QA assertions. If the repaired source/build is not "
                    "available on this target, report that as a blocker. Do not claim a fix was verified without "
                    "running the original checks against the updated application."
                )
            else:
                result = await self._stage(
                    planner(
                        agent.original_task or agent.task, agent.platform,
                        mode=agent.execution.planner if agent.execution else "local",
                        execution=agent.execution,
                        repo_path=agent.repo_path, failure_context=failure,
                        session_id=previous.planning_session_id if previous and (
                            previous.execution is None or (agent.execution is not None and previous.execution.planner == agent.execution.planner)
                        ) else None,
                        timeout_s=self.settings.planning_timeout_s,
                        emit=self.make_emit(agent),
                    ),
                    handle, self.settings.planning_timeout_s + STOP_GRACE_S,
                )
                if original_plan and result.plan is not None:
                    plan = result.plan.model_copy(update={
                        "expected_result": original_plan.plan.expected_result,
                        "verification": original_plan.plan.verification,
                    })
                    result = PlanResult(task=plan.render(), plan=plan, session_id=result.session_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            agent.planning_session_id = getattr(error, "session_id", None)
            reason = str(error) if isinstance(error, PlanningError) else f"Devin planning failed ({type(error).__name__})"
            self.emit(Event(type="agent.verdict", agent_id=agent.id, data={
                "passed": False, "reason": reason, "category": "planning",
            }))
            raise PlanningError(reason, session_id=agent.planning_session_id) from error
        agent.task = result.task
        agent.planning_session_id = result.session_id
        plan = result.plan.model_dump(mode="json") if hasattr(result.plan, "model_dump") else result.plan
        directory = self.settings.artifacts_dir / agent.id
        directory.mkdir(parents=True, exist_ok=True)
        data = {
            "task_id": task_identity(agent.original_task or agent.task, agent.platform),
            "task": result.task, "plan": plan, "session_id": result.session_id,
            "execution": agent.execution.model_dump(mode="json") if agent.execution else None,
        }
        (directory / "plan.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self.emit(Event(type="agent.message", agent_id=agent.id, data={
            "role": "system", "phase": "planning", "state": "ready",
            "text": "Devin plan validated. H will execute the planned QA task.",
            "task": agent.task, "planning_session_id": agent.planning_session_id,
        }))

    async def _request_approval(self, agent: Agent, operation: dict[str, Any]) -> str | None:
        operation = {**operation, "options": [
            option for option in operation.get("options", [])
            if isinstance(option, dict) and option.get("kind") in ("allow_once", "reject_once")
        ]}
        approval_id = uuid.uuid4().hex
        answer: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        self._approvals[approval_id] = (agent.id, operation, answer)
        self.emit(Event(type="agent.message", agent_id=agent.id, data={
            "role": "system", "phase": "approval", "state": "pending", "approval_id": approval_id,
            "text": "Devin requested permission for a repair operation. Review its command or file changes before approving.",
        }))
        try:
            return await answer
        finally:
            self._approvals.pop(approval_id, None)
            self.emit(Event(type="agent.message", agent_id=agent.id, data={
                "role": "system", "phase": "approval", "state": "closed", "approval_id": approval_id,
                "text": "The repair permission request is no longer pending.",
            }))

    def get_approval(self, agent_id: str, approval_id: str) -> dict[str, Any]:
        pending = self._approvals.get(approval_id)
        if pending is None or pending[0] != agent_id or pending[2].done():
            raise NotFound(approval_id)
        return pending[1]

    def decide_approval(self, agent_id: str, approval_id: str, option_id: str | None) -> None:
        operation = self.get_approval(agent_id, approval_id)
        allowed = {
            option.get("optionId") for option in operation.get("options", [])
            if isinstance(option, dict) and option.get("kind") == "allow_once"
        }
        if option_id is not None and option_id not in allowed:
            rejected = {
                option.get("optionId") for option in operation.get("options", [])
                if isinstance(option, dict) and option.get("kind") == "reject_once"
            }
            if option_id not in rejected:
                raise ValueError("Choose an offered allow-once option or decline the request.")
            option_id = None
        self._approvals[approval_id][2].set_result(option_id)

    @staticmethod
    async def _call_optional(worker: Any, method: str) -> None:
        fn = getattr(worker, method, None)
        if fn is None:
            return
        try:
            await fn()
        except Exception:  # noqa: BLE001 - cosmetics (pointer, hiding Raven) never fail the agent
            log.exception("%r.%s failed", worker, method)

    def get_agent(self, agent_id: str) -> Agent:
        a = self.store.agents.get(agent_id)
        if a is None:
            raise NotFound(agent_id)
        return a

    def send_message(self, agent_id: str, text: str) -> str | None:
        agent = self.get_agent(agent_id)
        self.emit(Event(type="agent.message", agent_id=agent.id, run_id=agent.run_id, data={"role": "user", "text": text}))
        if agent.status in TERMINAL_AGENT:
            return self._resume_agent(agent, text)
        h = self.store.handles.get(agent_id)
        if h is None or (h.task is not None and h.task.done()):
            return None
        h.inbox.put_nowait(text)
        return agent.id

    def stop_agent(self, agent_id: str, reason: str | None = None, *, cause: StopCause = StopCause.user) -> Agent:
        """Ask the agent to stop; `reason` ends up in its verdict (default: stopped by the user)."""
        agent = self.get_agent(agent_id)
        h = self.store.handles.get(agent_id)
        if h is not None:
            h.stop.set(reason or STOPPED_BY_USER, cause=cause)
            task = h.task
            if task is not None and not task.done():
                asyncio.get_running_loop().call_later(
                    STOP_GRACE_S, lambda: task.cancel() if not task.done() else None
                )
        return agent

    def active_agents(self) -> list[Agent]:
        return [a for a in self.store.agents.values() if a.status not in TERMINAL_AGENT]

    def platform_busy(self, platform: Platform) -> bool:
        return any(a.platform == platform for a in self.active_agents())

    def start_background(self) -> None:
        """Server-only background jobs (not started for in-process test/CLI uses)."""
        from raven_core import machines

        task = asyncio.get_running_loop().create_task(machines.idle_reaper(self.platform_busy), name="idle-reaper")
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        if sys.platform == "darwin":
            from raven_core.workers.local_mac import log_permission_state

            check = asyncio.get_running_loop().create_task(asyncio.to_thread(log_permission_state), name="mac-permissions")
            self._bg.add(check)
            check.add_done_callback(self._bg.discard)

    def stop_all(self) -> list[str]:
        ids = [a.id for a in self.active_agents()]
        for r in list(self.store.runs.values()):
            if r.status not in TERMINAL_RUN:
                self._stopped_runs.add(r.id)
                self._set_run_status(r, RunStatus.stopped)  # runner polls run.status
        for i in ids:
            self.stop_agent(i)
        return ids

    def stop_run(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        self._stopped_runs.add(run_id)
        if run.status not in TERMINAL_RUN:
            self._set_run_status(run, RunStatus.stopped)  # runner polls run.status
        for a in self.store.agents_for_run(run_id):
            if a.status not in TERMINAL_AGENT:
                self.stop_agent(a.id)
        task = self._run_tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        return run

    # ------------------------------------------------------------------ runs
    def get_run(self, run_id: str) -> Run:
        r = self.store.runs.get(run_id)
        if r is None:
            raise NotFound(run_id)
        return r

    def create_run(self, req: CreateRunRequest) -> Run:
        spec = self.load_spec(req.spec)
        sd = _spec_dict(spec)
        name = getattr(spec, "name", None) or sd.get("name") or req.spec
        platforms = list(req.platforms or spec_platforms(spec))
        run = Run(spec=name, platforms=platforms, execution=self.execution_profiles.snapshot(req.execution, model=req.model))
        self.store.add_run(run)
        self.emit(Event(type="run.created", run_id=run.id, data={"run": run.model_dump(mode="json")}))
        task = asyncio.get_running_loop().create_task(
            self._run_task(run, spec, platforms, req.placement), name=f"run-{run.id}"
        )
        self._run_tasks[run.id] = task
        return run

    def _set_run_status(self, run: Run, status: RunStatus) -> None:
        last = next(
            (e.data.get("status") for e in reversed(self.store.run_events.get(run.id, [])) if e.type == "run.status"),
            None,
        )
        if last == status.value:
            return  # already announced (e.g. by the runner)
        self.emit(Event(type="run.status", run_id=run.id, data={"status": status.value}))

    async def _emit_async(self, event: Event) -> None:
        self.emit(event)

    async def _stop_agent_async(self, agent_id: str, reason: str | None = None) -> None:
        try:
            cause = StopCause.deadline if reason and reason.startswith("QA attempt exceeded") else StopCause.cancelled
            self.stop_agent(agent_id, reason, cause=cause)
        except NotFound:
            pass

    async def _prepare_run_platform(
        self, run: Run, platform: Platform, commands: list[str], placement: Placement,
    ) -> Any:
        from raven_core.runner import SetupTimeoutError, setup_platform
        from raven_core.workers import WorkerUnavailable

        async def say(text: str, *, phase: str = "preparing") -> None:
            self.emit(Event(type="agent.message", run_id=run.id, data={
                "role": "system", "phase": phase, "platform": platform.value, "text": text,
            }))

        key = (run.id, platform)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.prepare_timeout_s
        run_deadline = self._run_deadlines.get(run.id)
        if run_deadline is not None:
            deadline = min(deadline, run_deadline)
        held: asyncio.Lock | None = None
        worker = None
        phase = "selecting target"
        try:
            async with asyncio.timeout_at(deadline):
                if run.id in self._stopped_runs:
                    raise asyncio.CancelledError
                worker = self._prepared_workers.get(key)
                if worker is None:
                    factory = self._get_worker_fn()
                    options: dict[str, Any] = {}
                    if _accepts(factory, "placement"):
                        options["placement"] = placement
                    if _accepts(factory, "execution"):
                        options["execution"] = run.execution
                    worker = factory(platform, **options)
                    if inspect.isawaitable(worker):
                        worker = await worker
                    self._prepared_workers[key] = worker
                    self._owned_workers[id(worker)] = worker
                lock = self._lock_for_worker(worker)
                phase = "waiting for the target screen"
                await lock.acquire()
                held = lock
                original = worker
                phase = "preparing target"
                worker = await self._prepare_target(worker, platform, run.execution, say)
                self._prepared_workers[key] = worker
                self._owned_workers[id(worker)] = worker
                lock = self._lock_for_worker(worker)
                if lock is not held:
                    held.release()
                    held = None
                    phase = "waiting for the resolved target screen"
                    await lock.acquire()
                    held = lock
                if original is not worker:
                    phase = "releasing the original target"
                    await self._close_unused(original)
            # Setup owns the remaining deadline so its timeout retains the command
            # or window-wait phase, rather than the worker's last "desktop ready".
            phase = "running application setup"
            if commands:
                await say(f"Running {platform.value} application setup ({len(commands)} commands).", phase="setup")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            await setup_platform(
                platform, commands, placement, worker=worker, execution=run.execution, timeout_s=remaining,
            )
            return worker
        except SetupTimeoutError as error:
            detail = str(error)
            if run_deadline is not None and deadline == run_deadline:
                detail += " Limited by the remaining run deadline."
            # Emit here as well as in the runner's summary: the run backstop may
            # cancel that summary immediately after this stage finishes.
            await say(detail, phase="setup")
            raise WorkerUnavailable(detail) from None
        except asyncio.CancelledError:
            # The whole-run backstop can win the race with setup's own deadline.
            # Keep the active preparation phase visible without swallowing a stop.
            if run_deadline is not None and loop.time() >= run_deadline and run.id not in self._stopped_runs:
                await say(
                    f"{platform.value} preparation timed out while {phase} (run deadline; before H computer use).",
                    phase="setup" if phase == "running application setup" else "preparing",
                )
            raise
        except TimeoutError:
            if phase == "preparing target":
                phase = getattr(worker, "preparation_phase", phase)
            limit = (
                "the remaining run deadline" if run_deadline is not None and deadline == run_deadline
                else f"its {self.settings.prepare_timeout_s:g}s preparation budget"
            )
            raise WorkerUnavailable(
                f"{platform.value} preparation timed out while {phase} (limited by {limit})"
            ) from None
        finally:
            if held is not None:
                held.release()

    async def _release_run_workers(self, run_id: str) -> None:
        workers = {}
        for key in [key for key in self._prepared_workers if key[0] == run_id]:
            worker = self._prepared_workers.pop(key)
            workers[id(worker)] = worker
        await asyncio.gather(*(self._close_unused(worker) for worker in workers.values()))

    def _attempt_context(self, agents: list[Agent]) -> dict[str, Any]:
        from raven_core.integrations.report import build_failure_context

        events = [event for agent in agents for event in self.store.agent_events.get(agent.id, [])]
        return build_failure_context(agents, events)

    async def _run_task(
        self, run: Run, spec: Any, platforms: list[Platform], placement: Placement = Placement.auto
    ) -> None:
        self._set_run_status(run, RunStatus.running)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.run_timeout_s
        self._run_deadlines[run.id] = deadline
        runner_task: asyncio.Task[Any] | None = None
        try:
            run_spec = self._run_spec_fn()
            if run_spec is not None:
                kwargs: dict[str, Any] = {}
                try:
                    params = inspect.signature(run_spec).parameters
                except (TypeError, ValueError):
                    params = {}
                if "emit" in params:
                    kwargs["emit"] = self._emit_async
                if "stop_agent" in params:
                    kwargs["stop_agent"] = self._stop_agent_async
                if "reload_spec" in params:
                    kwargs["reload_spec"] = getattr(spec, "rerender", lambda: spec)
                if "wait_for_agents" in params:
                    kwargs["wait_for_agents"] = lambda: self._wait_agents(run.id, STOP_GRACE_S + 2)
                if "placement" in params:
                    kwargs["placement"] = placement
                if "prepare_platform" in params:
                    kwargs["prepare_platform"] = lambda platform, commands: self._prepare_run_platform(
                        run, platform, commands, placement,
                    )
                if "failure_context" in params:
                    kwargs["failure_context"] = self._attempt_context
                res = run_spec(spec, platforms, self.start_agent, run, **kwargs)
                if inspect.isawaitable(res):
                    runner_task = asyncio.create_task(res)
                    done, _ = await asyncio.wait({runner_task}, timeout=max(0, deadline - loop.time()))
                    if not done:
                        raise TimeoutError
                    runner_task.result()
            else:
                self._fallback_run_spec(spec, platforms, run, placement)
            await self._wait_agents(run.id, max(1.0, deadline - loop.time()))
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("run %s timed out", run.id)
            reason = f"Run exceeded its {self.settings.run_timeout_s:.0f}s time limit"
            self.emit(Event(type="agent.message", run_id=run.id, data={"role": "system", "text": reason + "."}))
            for a in self.store.agents_for_run(run.id):
                if a.status not in TERMINAL_AGENT:
                    self.stop_agent(a.id, reason, cause=StopCause.deadline)
            try:
                await self._wait_agents(run.id, STOP_GRACE_S + 2)
            except (TimeoutError, asyncio.TimeoutError):
                pass
        except asyncio.CancelledError:
            self._stopped_runs.add(run.id)
            for agent in self.store.agents_for_run(run.id):
                if agent.status not in TERMINAL_AGENT:
                    self.stop_agent(agent.id, "Run cancelled", cause=StopCause.cancelled)
        except Exception as e:
            log.exception("run %s failed", run.id)
            self.emit(
                Event(
                    type="agent.message",
                    run_id=run.id,
                    data={"role": "system", "text": f"Runner error: {type(e).__name__}: {e}"},
                )
            )
        finally:
            if runner_task is not None and not runner_task.done():
                runner_task.cancel()
                await asyncio.gather(runner_task, return_exceptions=True)
            await self._release_run_workers(run.id)
            self._run_deadlines.pop(run.id, None)
        final = self._compute_run_status(run)
        self._set_run_status(run, final)
        if final == RunStatus.failed:
            try:
                self._write_failure_notes(run)
            except OSError:
                log.exception("could not write failure notes for run %s", run.id)
        if final == RunStatus.failed and self.settings.reports:
            try:
                await self.report(run.id, devin=self.settings.auto_devin)
            except Exception:
                log.exception("report failed for run %s", run.id)

    def _write_failure_notes(self, run: Run) -> None:
        from raven_core.integrations.report import failure_notes

        directory = self.settings.artifacts_dir / run.id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "failure.md"
        notes = failure_notes(run, self.store.agents_for_run(run.id), self.store.run_events.get(run.id, []))
        temp = path.with_suffix(".tmp")
        temp.write_text(notes, encoding="utf-8")
        temp.replace(path)

    async def _wait_agents(self, run_id: str, timeout: float) -> None:
        tasks = [
            h.task
            for a in self.store.agents_for_run(run_id)
            if (h := self.store.handles.get(a.id)) is not None and h.task is not None
        ]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:
                raise TimeoutError

    def _compute_run_status(self, run: Run) -> RunStatus:
        agents = self.store.agents_for_run(run.id)
        current = RunStatus(getattr(run.status, "value", run.status))
        if run.id in self._stopped_runs:
            return RunStatus.stopped
        if current in TERMINAL_RUN:  # runner decided
            return current
        if not agents:
            return RunStatus.failed
        sts = {a.status for a in agents}
        if sts == {AgentStatus.passed}:
            return RunStatus.passed
        if sts & {AgentStatus.failed, AgentStatus.error}:
            return RunStatus.failed
        if AgentStatus.stopped in sts:
            return RunStatus.stopped
        return RunStatus.failed

    def _fallback_run_spec(
        self, spec: Any, platforms: list[Platform], run: Run, placement: Placement = Placement.auto
    ) -> None:
        d = _spec_dict(spec)
        vars_: dict[str, str] = {}
        for k, v in (d.get("vars") or {}).items():
            vars_[k] = str(v).replace("{{random_word}}", f"{random.choice(_WORDS)}{random.randint(10, 99)}")

        def render(s: str) -> str:
            return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: vars_.get(m.group(1), m.group(0)), s)

        try:
            timeout_s = float(d.get("timeout_s") or 0) or None
        except (TypeError, ValueError):
            timeout_s = None
        for a in d.get("agents") or []:
            a = a if isinstance(a, dict) else _spec_dict(a)
            p = _plat(a.get("platform"))
            if p is None or p not in platforms:
                continue
            role = a.get("role") or ""
            self.start_agent(
                CreateAgentRequest(
                    platform=p,
                    task=render(str(a.get("task") or "")).strip(),
                    title=f"{d.get('name', run.spec)} · {p.value} {role}".strip(),
                    placement=placement,
                    timeout_s=timeout_s,
                    execution=run.execution,
                ),
                run.id,
            )

    async def report(self, run_id: str, devin: bool = False) -> dict[str, Any]:
        from raven_core.integrations.report import build_failure_report

        run = self.get_run(run_id)
        agents = self.store.agents_for_run(run_id)
        data = await build_failure_report(
            run,
            agents,
            {a.id: self.store.agent_events.get(a.id, []) for a in agents},
            create_devin=devin,
        )
        self.emit(Event(type="report.ready", run_id=run_id, data=data))
        return data

    # ------------------------------------------------------------------ voice / text commands
    def command(self, text: str, agent_id: str | None = None) -> dict[str, Any]:
        if agent_id:
            target = self.send_message(agent_id, text)
            return {"action": "message", "agent_id": target or agent_id, "delivered": target is not None,
                    "detail": f"Sent to agent {target or agent_id}"}
        intent = parse_command(text, self.spec_names())
        if intent.kind == "run":
            run = self.create_run(
                CreateRunRequest(spec=intent.spec or "", platforms=intent.platforms, placement=intent.placement)
            )
            plats = ", ".join(p.value for p in run.platforms)
            return {"action": "run", "run": run.model_dump(mode="json"),
                    "detail": f"Running {run.spec} on {plats}"}
        if intent.kind == "stop_all":
            ids = self.stop_all()
            return {"action": "stop_all", "stopped": ids, "detail": f"Stopping {len(ids)} agent(s)"}
        if intent.kind == "agent" and intent.platform and intent.task:
            agent = self.start_agent(
                CreateAgentRequest(platform=intent.platform, task=intent.task, placement=intent.placement)
            )
            return {"action": "agent", "agent": agent.model_dump(mode="json"),
                    "detail": f"Started {intent.platform.value} agent: {agent.title}"}
        if intent.kind == "list":
            return {"action": "list", "agents": [a.model_dump(mode="json") for a in self.store.agents.values()],
                    "detail": f"{len(self.active_agents())} active agent(s)"}
        if intent.kind == "specs":
            specs = self.list_specs()
            return {"action": "specs", "specs": specs, "detail": ", ".join(s["name"] for s in specs)}
        if intent.kind == "help":
            return {"action": "help", "detail": HELP}
        running = [a for a in self.active_agents()]
        if len(running) == 1:
            target = self.send_message(running[0].id, text)
            return {"action": "message", "agent_id": running[0].id, "delivered": target is not None,
                    "detail": f"Sent to {running[0].platform.value} agent"}
        plats = task_platforms(text)
        if len(plats) == 1:
            agent = self.start_agent(CreateAgentRequest(platform=plats[0], task=text, placement=requested(text)))
            return {"action": "agent", "agent": agent.model_dump(mode="json"),
                    "detail": f"Started {plats[0].value} agent"}
        return {"action": "none", "detail": "Sorry, I didn't understand. " + HELP}

    async def shutdown(self) -> None:
        for h in self.store.handles.values():
            h.stop.set("Raven is shutting down", cause=StopCause.shutdown)
        tasks = [h.task for h in self.store.handles.values() if h.task and not h.task.done()]
        tasks += [t for t in self._run_tasks.values() if not t.done()]
        tasks += [task for task in self._bg if not task.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=STOP_GRACE_S + 20)
        self._prepared_workers.clear()
        await asyncio.gather(*(self._close_unused(worker) for worker in list(self._owned_workers.values())))
