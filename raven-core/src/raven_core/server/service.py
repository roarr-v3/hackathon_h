"""Raven orchestration service: owns the Store, starts agents/runs, reports.

The engine modules (workers/engine/specs/runner) are imported lazily so the
server keeps working (with fallbacks) while they are being built. Tests can
override `get_worker`, `run_agent`, `run_spec` on the instance."""

from __future__ import annotations

import asyncio
import inspect
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
    Placement,
    Platform,
    Run,
    RunStatus,
)
from raven_core.placement import describe_host, requested
from raven_core.server.config import Settings
from raven_core.server.intents import HELP, parse_command, parse_platforms
from raven_core.store import (
    TERMINAL_AGENT,
    TERMINAL_RUN,
    AgentHandle,
    Store,
    coerce_event,
    default_persist_path,
)

log = logging.getLogger("raven.service")

STOP_GRACE_S = 8.0


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
        # Engine hooks; None -> import from raven_core.* lazily.
        self.get_worker: Callable[..., Any] | None = None
        self.run_agent: Callable[..., Any] | None = None
        self.run_spec: Callable[..., Any] | None = None
        self._run_tasks: dict[str, asyncio.Task[Any]] = {}
        self._stopped_runs: set[str] = set()
        self._bg: set[asyncio.Task[Any]] = set()
        self._screen_lock = asyncio.Lock()

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
        return self.store.emit(coerce_event(event))

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
                self.store.emit(ev)
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
        worker: Any = None
        pending: Any = None
        error: Exception | None = None
        worker_name = req.platform.value
        try:
            get_worker = self._get_worker_fn()
            if _accepts(get_worker, "placement"):
                w = get_worker(req.platform, placement=req.placement)
            else:
                w = get_worker(req.platform)
            if inspect.isawaitable(w):
                pending = w
            else:
                worker = w
                worker_name = _worker_name(w, req.platform)
        except Exception as e:  # missing module, no sandbox, ...
            error = e
        agent = StartedAgent(
            run_id=run_id,
            platform=req.platform,
            worker=worker_name,
            title=req.title or _title_from_task(req.task),
            task=req.task,
            placement=_placement_of(worker),
        )
        self.store.add_agent(agent)
        self.emit(Event(type="agent.created", agent_id=agent.id, run_id=run_id, data={"agent": agent.model_dump(mode="json")}))
        handle = AgentHandle(inbox=asyncio.Queue(), stop=asyncio.Event())
        if run_id is not None and run_id in self._stopped_runs:
            handle.stop.set()  # run was stopped before this agent was spawned
        self.store.handles[agent.id] = handle
        handle.task = asyncio.get_running_loop().create_task(
            self._agent_task(agent, worker, pending, handle, error), name=f"agent-{agent.id}"
        )
        return agent

    async def _agent_task(
        self,
        agent: Agent,
        worker: Any,
        pending: Any,
        handle: AgentHandle,
        error: Exception | None,
    ) -> None:
        final: AgentStatus | None = None
        try:
            if error is None and pending is not None:
                try:
                    worker = await pending
                    agent.worker = _worker_name(worker, agent.platform)
                    agent.placement = _placement_of(worker)
                except Exception as e:
                    error = e
            bare_metal = error is None and _is_bare_metal(worker)
            if error is None and not _is_fake(worker):
                from raven_core import machines

                async def say(text: str) -> None:
                    self._sysmsg(agent, text)

                try:
                    if bare_metal:
                        preflight = getattr(worker, "preflight", None)
                        if preflight is not None:
                            await preflight()
                    else:
                        await machines.ensure(agent.platform, say)
                except Exception as e:  # noqa: BLE001 - surfaced to the user as a worker error
                    error = e
                if error is None and not bare_metal:
                    try:
                        await machines.show(agent.platform, say)
                    except Exception as e:  # noqa: BLE001 - the test runs anyway, just unseen
                        self._sysmsg(agent, f"Couldn't open the live view of the {agent.platform.value} machine: {e}")
            if error is not None:
                self._sysmsg(agent, f"Worker unavailable for {agent.platform.value}: {type(error).__name__}: {error}")
                agent.verdict_reason = f"worker unavailable: {error}"
                final = AgentStatus.error
                return
            if bare_metal:
                if self._screen_lock.locked():
                    self._sysmsg(agent, f"Waiting for {describe_host()}: another agent is using its screen…")
                await self._screen_lock.acquire()
            try:
                if bare_metal:
                    self._sysmsg(
                        agent,
                        f"Running on {describe_host()}, bare metal: Raven controls the mouse and keyboard "
                        "until this agent finishes. Press Stop to take them back.",
                    )
                    await self._call_optional(worker, "begin")
                self._status(agent, AgentStatus.running)
                run_agent = self._run_agent_fn()
                res = run_agent(
                    agent,
                    worker,
                    self.make_emit(agent),
                    handle.inbox,
                    handle.stop,
                    self.settings.artifacts_dir,
                )
                if inspect.isawaitable(res):
                    await res
            finally:
                if bare_metal:
                    try:
                        await asyncio.shield(self._call_optional(worker, "finish"))
                    finally:
                        self._screen_lock.release()
            if agent.status in (AgentStatus.passed, AgentStatus.failed):
                final = agent.status
            elif handle.stop.is_set():
                final = AgentStatus.stopped
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
                final = AgentStatus.failed
        except asyncio.CancelledError:
            final = AgentStatus.stopped
        except Exception as e:
            log.exception("agent %s crashed", agent.id)
            self._sysmsg(agent, f"Agent crashed: {type(e).__name__}: {e}")
            agent.verdict_reason = agent.verdict_reason or f"crash: {type(e).__name__}: {e}"
            final = AgentStatus.error
        finally:
            if final is not None:
                self._status(agent, final)

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

    def send_message(self, agent_id: str, text: str) -> bool:
        agent = self.get_agent(agent_id)
        self.emit(Event(type="agent.message", agent_id=agent.id, run_id=agent.run_id, data={"role": "user", "text": text}))
        h = self.store.handles.get(agent_id)
        if h is None or (h.task is not None and h.task.done()):
            return False
        h.inbox.put_nowait(text)
        return True

    def stop_agent(self, agent_id: str) -> Agent:
        agent = self.get_agent(agent_id)
        h = self.store.handles.get(agent_id)
        if h is not None:
            h.stop.set()
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
        run = Run(spec=name, platforms=platforms)
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

    async def _stop_agent_async(self, agent_id: str) -> None:
        try:
            self.stop_agent(agent_id)
        except NotFound:
            pass

    async def _run_task(
        self, run: Run, spec: Any, platforms: list[Platform], placement: Placement = Placement.auto
    ) -> None:
        self._set_run_status(run, RunStatus.running)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.run_timeout_s
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
                if "placement" in params:
                    kwargs["placement"] = placement
                res = run_spec(spec, platforms, self.start_agent, run, **kwargs)
                if inspect.isawaitable(res):
                    await asyncio.wait_for(res, timeout=self.settings.run_timeout_s)
            else:
                self._fallback_run_spec(spec, platforms, run, placement)
            await self._wait_agents(run.id, max(1.0, deadline - loop.time()))
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("run %s timed out", run.id)
            for a in self.store.agents_for_run(run.id):
                if a.status not in TERMINAL_AGENT:
                    self.stop_agent(a.id)
            try:
                await self._wait_agents(run.id, STOP_GRACE_S + 2)
            except (TimeoutError, asyncio.TimeoutError):
                pass
        except asyncio.CancelledError:
            self._stopped_runs.add(run.id)
        except Exception as e:
            log.exception("run %s failed", run.id)
            self.emit(
                Event(
                    type="agent.message",
                    run_id=run.id,
                    data={"role": "system", "text": f"Runner error: {type(e).__name__}: {e}"},
                )
            )
        final = self._compute_run_status(run)
        self._set_run_status(run, final)
        if final == RunStatus.failed and self.settings.reports:
            try:
                await self.report(run.id, devin=self.settings.auto_devin)
            except Exception:
                log.exception("report failed for run %s", run.id)

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
            delivered = self.send_message(agent_id, text)
            return {"action": "message", "agent_id": agent_id, "delivered": delivered,
                    "detail": f"Sent to agent {agent_id}"}
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
            delivered = self.send_message(running[0].id, text)
            return {"action": "message", "agent_id": running[0].id, "delivered": delivered,
                    "detail": f"Sent to {running[0].platform.value} agent"}
        plats = parse_platforms(text)
        if len(plats) == 1:
            agent = self.start_agent(CreateAgentRequest(platform=plats[0], task=text, placement=requested(text)))
            return {"action": "agent", "agent": agent.model_dump(mode="json"),
                    "detail": f"Started {plats[0].value} agent"}
        return {"action": "none", "detail": "Sorry, I didn't understand. " + HELP}

    async def shutdown(self) -> None:
        for h in self.store.handles.values():
            h.stop.set()
        tasks = [h.task for h in self.store.handles.values() if h.task and not h.task.done()]
        tasks += [t for t in self._run_tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=3)
