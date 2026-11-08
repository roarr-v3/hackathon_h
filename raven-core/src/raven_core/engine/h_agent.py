"""Run one Raven agent with H's desktop agent (`h/holo-desktop`) inside a test machine.

raven-core spawns `raven_host.py` on the host, reads its JSON lines, and turns them into
Raven events. Steering messages from the inbox go to the host's stdin; `stop` terminates it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from raven_core.engine.holo import (
    AGENT_TIMEOUT_S,
    StopReason,
    _Deadline,
    _Stopped,
    _bounded_int,
    _cancel_and_drain,
    _env_float,
    _get_stop_reason,
    _model_label,
    _positive_float,
)
from raven_core.events import Agent, AgentStatus, Event
from raven_core.hosts import Host, host_args
from raven_core.workers.base import png_size

log = logging.getLogger(__name__)
Emit = Callable[[Event], Awaitable[None]]
VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(PASSED|FAILED)\b", re.I)

QA_SUFFIX = (
    "\n\nWhen you are done, answer with a report that starts with exactly `VERDICT: PASSED` "
    "or `VERDICT: FAILED`, then one short paragraph with the visible evidence. "
    "Inspect the starting screen before acting. Check the screen after each action. "
    "If the same action leaves the screen unchanged twice, take a different route or stop and describe "
    "the blocker. If you cannot reach the app or observe the test because the environment or tooling "
    "is blocked, start with `VERDICT: BLOCKED` and name the last verified state."
)


def verdict_from(answer: Any, outcome: Any, status: Any) -> tuple[bool, str]:
    text = answer if isinstance(answer, str) else json.dumps(answer, default=str) if answer else ""
    if m := VERDICT_RE.match(text or ""):
        return m.group(1).upper() == "PASSED", text.strip()
    if str(status) not in ("completed", "idle"):
        return False, f"H session ended {status}: {text or 'no answer'}"
    return False, "H session completed without an explicit test verdict."


async def run_h_agent(
    agent: Agent,
    host: Host,
    emit: Emit,
    inbox: asyncio.Queue[str],
    stop: asyncio.Event,
    artifacts_dir: Path,
    *,
    timeout_s: float | None = None,
    model: str | None = None,
    mode: str | None = None,
    max_steps: int = 60,
    request_timeout_s: float | None = None,
    request_attempts: int | None = None,
    get_stop_reason: StopReason | None = None,
) -> None:
    """Never raises (except CancelledError, after emitting a verdict)."""
    shot_dir = Path(artifacts_dir) / agent.id
    shot_dir.mkdir(parents=True, exist_ok=True)
    env_mode = os.environ.get("RAVEN_H_MODE")
    mode = mode or (env_mode if env_mode and env_mode != "models-loop" else None) or getattr(host, "default_mode", "local-runtime")
    model = model or os.environ.get("HAI_MODEL") or None
    timeout_s = _positive_float(timeout_s, _env_float("RAVEN_AGENT_TIMEOUT_S", AGENT_TIMEOUT_S))
    max_steps = _bounded_int(max_steps, 60, 10000)
    cleanup_s = min(10.0, timeout_s * 0.1)
    started = time.monotonic()
    deadline = started + timeout_s - cleanup_s
    phase, phase_since = "starting H host", started
    n_shot = 0
    n_actions = 0
    stderr_bytes = 0
    proc = None
    tasks: list[asyncio.Task] = []
    session = stopper = None
    cancelled = False

    def enter(value: str) -> None:
        nonlocal phase, phase_since
        phase, phase_since = value, time.monotonic()

    def where() -> str:
        return f"{phase} ({time.monotonic() - phase_since:.2f}s in phase, {time.monotonic() - started:.2f}s elapsed)"

    def cancellation_reason() -> str:
        cause = _get_stop_reason(stop, get_stop_reason) or ("Stopped by user" if stop.is_set() else "H session task cancelled")
        return f"{cause} while {where()}"

    def check() -> None:
        if stop.is_set():
            raise _Stopped(f"{_get_stop_reason(stop, get_stop_reason) or 'Stopped by user'} while {where()}")
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        if time.monotonic() >= deadline:
            raise _Deadline(
                f"H session timed out within its {timeout_s:g}s agent budget while {where()}; "
                f"{cleanup_s:g}s reserved for host cleanup"
            )

    async def send(type_: str, data: dict) -> None:
        try:
            await emit(Event(type=type_, run_id=agent.run_id, agent_id=agent.id, data=data))  # type: ignore[arg-type]
        except Exception as error:
            log.warning("H event emit failed (%s)", type(error).__name__)

    async def lifecycle(state: str, text: str, **fields: Any) -> None:
        logger = log.warning if state == "failed" else log.info
        logger("%s h_session %s: %s", agent.id, state, text)
        await send("agent.message", {
            "role": "system", "text": text, "phase": "h_session", "state": state,
            "activity": phase, "elapsed_s": time.monotonic() - started,
            "phase_elapsed_s": time.monotonic() - phase_since,
            "timeout_s": timeout_s, "session_limit_s": timeout_s - cleanup_s,
            "cleanup_s": cleanup_s, "max_steps": max_steps, "actions": n_actions,
            "model_requests_observable": False, **fields,
        })

    async def set_status(s: AgentStatus) -> None:
        if agent.status != s:
            agent.status = s
            await send("agent.status", {"status": s.value})

    async def finish(status: AgentStatus, passed: bool, reason: str) -> None:
        agent.verdict_reason = reason
        await send("agent.verdict", {"passed": passed, "reason": reason})
        await set_status(status)

    async def on_line(msg: dict) -> tuple[AgentStatus, str] | None:
        nonlocal n_shot, n_actions
        check()
        kind = msg.get("kind")
        if kind == "started":
            enter("waiting for H session events")
            session_id = str(msg.get("session_id") or "")
            session_id = session_id if re.fullmatch(r"ses_[A-Za-z0-9_-]{1,96}", session_id) else None
            await lifecycle("started", f"H session connected ({mode}, {_model_label(model)}).", session_id=session_id)
        elif kind == "action":
            n_actions += 1
            enter("waiting for H after an action")
            await send("agent.action", {"action": msg.get("tool"), "args": msg.get("args") or {}, "thought": msg.get("thought")})
        elif kind == "screenshot" and msg.get("path"):
            enter("reading H screenshot")
            path = str(msg["path"])
            if path.startswith("http"):
                await send("agent.screenshot", {"url": path, "width": 0, "height": 0})
            else:
                data = await host.read_file(path)
                check()
                if data:
                    ext = Path(path).suffix or ".png"
                    local = shot_dir / f"{n_shot:03d}{ext}"
                    local.write_bytes(data)
                    w, h = png_size(data) if ext == ".png" else (0, 0)
                    url = f"/artifacts/{agent.id}/{local.name}"
                    agent.last_screenshot = url
                    await send("agent.screenshot", {"url": url, "width": w, "height": h})
                    n_shot += 1
            enter("waiting for H session events")
        elif kind in ("message", "observation"):
            await send("agent.message", {"role": "agent", "text": msg.get("text") or ""})
        elif kind == "live_view":
            await send("agent.message", {"role": "system", "text": f"Live view: {msg.get('url')}"})
        elif kind == "error":
            await lifecycle("failed", "H reported a session error; untrusted error details are omitted.")
        elif kind == "result":
            answer = msg.get("answer")
            status = str(msg.get("status"))
            if status not in ("completed", "idle"):
                status = status if status in ("timed_out", "interrupted", "failed", "error", "cancelled") else "without completion"
                return AgentStatus.error, f"H session ended {status} while {where()}"
            text = answer.strip() if isinstance(answer, str) else ""
            if re.match(r"VERDICT:\s*BLOCKED\b", text, re.I):
                return AgentStatus.error, text
            if not VERDICT_RE.match(text):
                return AgentStatus.error, "H session completed without an explicit test verdict."
            passed, reason = verdict_from(answer, msg.get("outcome"), status)
            return (AgentStatus.passed if passed else AgentStatus.failed), reason
        return None

    async def pump_inbox() -> None:
        while True:
            text = await inbox.get()
            check()
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.write((text.replace("\n", " ") + "\n").encode())
                await proc.stdin.drain()

    async def read_stdout() -> tuple[AgentStatus, str] | None:
        assert proc.stdout
        async for raw in proc.stdout:
            check()
            line = raw.decode(errors="replace").strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and (result := await on_line(msg)) is not None:
                return result
        return None

    async def drain_stderr() -> None:
        nonlocal stderr_bytes
        if proc.stderr:
            while chunk := await proc.stderr.read(4096):
                stderr_bytes += len(chunk)

    async def run_session() -> tuple[AgentStatus, str] | None:
        nonlocal proc
        check()
        proc = await host.spawn(host_args(
            agent.task + QA_SUFFIX, mode, model, f"/tmp/raven-shots/{agent.id}",
            max_steps, max(0.0, deadline - time.monotonic()),
        ))
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        check()
        enter("waiting for H session events")
        tasks.extend((asyncio.create_task(pump_inbox()), asyncio.create_task(drain_stderr())))
        result = await read_stdout()
        if result is None:
            enter("waiting for H host process exit")
            await proc.wait()
        return result

    status, reason = AgentStatus.error, "H session ended without a result"
    try:
        await set_status(AgentStatus.running)
        check()
        if mode not in ("local-runtime", "agents-api"):
            raise ValueError("Unsupported H runtime mode")
        request_limits = (
            " Requested per-request timeout/retry overrides cannot be enforced by this adapter."
            if request_timeout_s is not None or request_attempts is not None else ""
        )
        await lifecycle(
            "started",
            f"H session starting ({mode}, {_model_label(model)}, {agent.platform.value}): "
            f"{timeout_s:g}s agent budget, {timeout_s - cleanup_s:g}s session limit, {max_steps} steps max. "
            f"H internal model calls and per-request timing/retries are not observable here.{request_limits}",
        )
        check()
        session = asyncio.create_task(run_session())
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait(
            {session, stopper}, timeout=max(0.0, deadline - time.monotonic()),
            return_when=asyncio.FIRST_COMPLETED,
        )
        check()
        if session not in done:
            raise _Deadline(f"H session timed out while {where()}")
        res = session.result()
        if res is None:
            reason = f"H host exited ({proc.returncode}) without a result; {stderr_bytes} stderr bytes omitted"
        else:
            status, reason = res
    except _Stopped as error:
        status, reason = AgentStatus.stopped, error.reason
    except _Deadline as error:
        reason = str(error)
    except asyncio.CancelledError:
        cancelled = True
        status = AgentStatus.stopped
        reason = cancellation_reason()
    except Exception as e:  # noqa: BLE001
        reason = f"H session failed with {type(e).__name__} while {where()}"
    finally:
        last_activity = where()
        enter("cleaning up H host")

        async def cleanup() -> bool:
            await _cancel_and_drain(session, stopper, *tasks)
            return await _shutdown(proc, grace_s=cleanup_s)

        cleaning = asyncio.create_task(cleanup())
        while not cleaning.done():
            try:
                await asyncio.shield(cleaning)
            except asyncio.CancelledError:
                cancelled = True
                status = AgentStatus.stopped
                reason = cancellation_reason()
        try:
            reaped = cleaning.result()
        except Exception as error:
            reaped = False
            log.warning("H host cleanup failed (%s)", type(error).__name__)
        if not reaped:
            reason += "; host shutdown could not be confirmed"
            if status != AgentStatus.stopped:
                status = AgentStatus.error

    state = "cancelled" if status == AgentStatus.stopped else "succeeded" if status in (AgentStatus.passed, AgentStatus.failed) else "failed"
    remote_note = (
        " Host exit does not independently confirm hosted H cancellation."
        if mode == "agents-api" and status in (AgentStatus.stopped, AgentStatus.error) else ""
    )
    await lifecycle(
        state, f"H session {state} after {time.monotonic() - started:.2f}s; last activity: {last_activity}; "
        f"result={status.value}.{remote_note}",
        host_reaped=reaped,
    )
    await finish(status, status == AgentStatus.passed, reason)
    if cancelled:
        raise asyncio.CancelledError


async def _shutdown(proc: asyncio.subprocess.Process | None, grace_s: float = 10.0) -> bool:
    """Close stdin to request H cancellation, then bound process exit with terminate/kill."""
    if proc is None or proc.returncode is not None:
        return True
    if proc.stdin and not proc.stdin.is_closing():
        proc.stdin.close()
    grace_s = _positive_float(grace_s, 10.0)

    async def discard(stream) -> None:
        if stream:
            while await stream.read(4096):
                pass

    waiter = asyncio.create_task(proc.wait())
    drains = [asyncio.create_task(discard(stream)) for stream in (proc.stdout, proc.stderr)]
    try:
        for fraction, action in ((0.6, proc.terminate), (0.2, proc.kill), (0.2, None)):
            try:
                await asyncio.wait_for(asyncio.shield(waiter), grace_s * fraction)
                return True
            except TimeoutError:
                if proc.returncode is not None:
                    return True
                if action:
                    try:
                        action()
                    except ProcessLookupError:
                        pass
        return proc.returncode is not None
    finally:
        await _cancel_and_drain(waiter, *drains)
