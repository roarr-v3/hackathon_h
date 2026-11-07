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

from raven_core.events import Agent, AgentStatus, Event
from raven_core.hosts import Host, host_args
from raven_core.workers.base import png_size

log = logging.getLogger(__name__)
Emit = Callable[[Event], Awaitable[None]]
VERDICT_RE = re.compile(r"VERDICT:\s*(PASSED|FAILED)", re.I)

QA_SUFFIX = (
    "\n\nWhen you are done, answer with a report that starts with exactly `VERDICT: PASSED` "
    "or `VERDICT: FAILED`, then one short paragraph. If it failed, quote the visible "
    "on-screen text that proves it."
)


def verdict_from(answer: Any, outcome: Any, status: Any) -> tuple[bool, str]:
    text = answer if isinstance(answer, str) else json.dumps(answer, default=str) if answer else ""
    if m := VERDICT_RE.search(text or ""):
        return m.group(1).upper() == "PASSED", text.strip()
    if str(status) not in ("completed", "idle"):
        return False, f"H session ended {status}: {text or 'no answer'}"
    return str(outcome) == "success", text.strip() or f"outcome={outcome}"


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
) -> None:
    """Never raises (except CancelledError, after emitting a verdict)."""
    shot_dir = Path(artifacts_dir) / agent.id
    shot_dir.mkdir(parents=True, exist_ok=True)
    mode = mode or os.environ.get("RAVEN_H_MODE", "local-runtime")
    model = model or os.environ.get("HAI_MODEL") or None
    timeout_s = timeout_s or float(os.environ.get("RAVEN_AGENT_TIMEOUT_S", 600))
    n_shot = 0

    async def send(type_: str, data: dict) -> None:
        try:
            await emit(Event(type=type_, run_id=agent.run_id, agent_id=agent.id, data=data))  # type: ignore[arg-type]
        except Exception:
            log.exception("emit failed")

    async def set_status(s: AgentStatus) -> None:
        if agent.status != s:
            agent.status = s
            await send("agent.status", {"status": s.value})

    async def finish(status: AgentStatus, passed: bool, reason: str) -> None:
        agent.verdict_reason = reason
        await send("agent.verdict", {"passed": passed, "reason": reason})
        await set_status(status)

    async def on_line(msg: dict) -> tuple[bool, str] | None:
        nonlocal n_shot
        kind = msg.get("kind")
        if kind == "started":
            await send("agent.message", {"role": "system", "text": f"H {msg.get('agent')} session {msg.get('session_id')} ({msg.get('mode')}, {msg.get('model') or 'default model'}) on {host!r}"})
        elif kind == "action":
            await send("agent.action", {"action": msg.get("tool"), "args": msg.get("args") or {}, "thought": msg.get("thought")})
        elif kind == "screenshot" and msg.get("path"):
            path = str(msg["path"])
            if path.startswith("http"):
                await send("agent.screenshot", {"url": path, "width": 0, "height": 0})
            else:
                data = await host.read_file(path)
                if data:
                    ext = Path(path).suffix or ".png"
                    local = shot_dir / f"{n_shot:03d}{ext}"
                    local.write_bytes(data)
                    w, h = png_size(data) if ext == ".png" else (0, 0)
                    url = f"/artifacts/{agent.id}/{local.name}"
                    agent.last_screenshot = url
                    await send("agent.screenshot", {"url": url, "width": w, "height": h})
                    n_shot += 1
        elif kind in ("message", "observation"):
            await send("agent.message", {"role": "agent", "text": msg.get("text") or ""})
        elif kind == "live_view":
            await send("agent.message", {"role": "system", "text": f"Live view: {msg.get('url')}"})
        elif kind == "error":
            await send("agent.message", {"role": "system", "text": f"H error: {msg.get('error')}"})
        elif kind == "result":
            return verdict_from(msg.get("answer"), msg.get("outcome"), msg.get("status"))
        return None

    await set_status(AgentStatus.running)
    proc = None
    try:
        proc = await host.spawn(host_args(agent.task + QA_SUFFIX, mode, model, f"/tmp/raven-shots/{agent.id}", max_steps, timeout_s))

        async def pump_inbox() -> None:
            while True:
                text = await inbox.get()
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.write((text.replace("\n", " ") + "\n").encode())
                    await proc.stdin.drain()

        async def read_stdout() -> tuple[bool, str] | None:
            result = None
            assert proc.stdout
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                result = await on_line(msg) or result
            return result

        inbox_task = asyncio.create_task(pump_inbox())
        reader = asyncio.create_task(read_stdout())
        stopper = asyncio.create_task(stop.wait())
        deadline = time.monotonic() + timeout_s + 60
        done, _ = await asyncio.wait({reader, stopper}, timeout=max(1.0, deadline - time.monotonic()), return_when=asyncio.FIRST_COMPLETED)
        inbox_task.cancel()
        if reader in done:
            stopper.cancel()
            await proc.wait()
            res = reader.result()
            if res is None:
                err = (await proc.stderr.read()).decode(errors="replace")[-600:] if proc.stderr else ""
                await finish(AgentStatus.error, False, f"host exited ({proc.returncode}) without a result. {err}")
            else:
                passed, reason = res
                await finish(AgentStatus.passed if passed else AgentStatus.failed, passed, reason)
        else:
            reader.cancel()
            proc.terminate()
            await finish(AgentStatus.stopped, False, "stopped by user" if stopper in done else f"timed out after {timeout_s:.0f}s")
    except asyncio.CancelledError:
        if proc and proc.returncode is None:
            proc.terminate()
        await finish(AgentStatus.stopped, False, "cancelled")
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("h agent failed")
        if proc and proc.returncode is None:
            proc.terminate()
        await finish(AgentStatus.error, False, f"{type(e).__name__}: {e}")
