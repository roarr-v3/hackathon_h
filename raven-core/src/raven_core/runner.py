"""Spec runner: setup apps on each worker, start one agent per spec agent, wait, set run status."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from typing import Awaitable, Callable

from raven_core.events import Agent, AgentStatus, CreateAgentRequest, Event, Platform, Run, RunStatus
from raven_core.specs import Spec, SpecAgent
from raven_core.workers import get_worker

log = logging.getLogger(__name__)

TERMINAL = {AgentStatus.passed, AgentStatus.failed, AgentStatus.stopped, AgentStatus.error}
SENDER_DELAY_S = float(os.environ.get("RAVEN_SENDER_DELAY_S", "20"))

StartAgent = Callable[[CreateAgentRequest, str], Awaitable[Agent]]


def launch_command(platform: Platform, cmd: str) -> tuple[str, bool]:
    """Turn a spec setup entry into (shell command, background?).

    - "sh:<cmd>"  -> run <cmd> as-is and wait for it.
    - linux       -> open it in a maximized xfce4-terminal on the sandbox desktop (DISPLAY=:1).
    - windows     -> `start` a new console window running it.
    - macos       -> "open ..." is passed through; anything else runs in Terminal.app.
    """
    if cmd.startswith("sh:"):
        return cmd[3:].strip(), False
    if platform == Platform.linux:
        inner = f"{cmd}; exec bash"
        return (
            "export DISPLAY=${DISPLAY:-:1}; "
            f"setsid -f xfce4-terminal --maximize -T raven-app -x bash -lc {shlex.quote(inner)} >/dev/null 2>&1",
            True,
        )
    if platform == Platform.windows:
        # works whether spacesd's shell is cmd.exe or PowerShell
        return f"cmd /c start cmd /k {cmd}", True
    if platform == Platform.macos:
        if cmd.startswith("open "):
            return cmd, True
        script = cmd.replace("\\", "\\\\").replace('"', '\\"')
        return f"osascript -e 'tell application \"Terminal\" to do script \"{script}\"' -e 'tell application \"Terminal\" to activate'", True
    return cmd, False


async def setup_platform(platform: Platform, cmds: list[str]) -> None:
    worker = get_worker(platform)
    for raw in cmds:
        cmd, background = launch_command(platform, raw)
        try:
            rc, out, err = await worker.shell(cmd, timeout=60)
            if rc != 0:
                log.warning("setup %s rc=%s: %s %s", platform.value, rc, out[-300:], err[-300:])
        except Exception as e:  # noqa: BLE001
            log.warning("setup %s failed: %s", platform.value, e)
        if background:
            await asyncio.sleep(2)  # let the window appear before the agent's first screenshot


def agent_request(spec: Spec, a: SpecAgent) -> CreateAgentRequest:
    others = [x.platform.value for x in spec.agents if x is not a]
    context = ""
    if others:
        context = (
            f"\n\nContext: this is part of the multi-device test '{spec.name}'. Testers on "
            f"{', '.join(others)} are doing their part at the same time."
        )
    if a.title:
        title = a.title
    elif a.role:
        title = f"{a.platform.value} · {a.role}"
    else:
        title = f"{spec.name} on {a.platform.value}"
    return CreateAgentRequest(platform=a.platform, task=a.task.strip() + context, title=title)


async def run_spec(
    spec: Spec,
    platforms: list[Platform] | None,
    start_agent: StartAgent,
    run: Run,
    *,
    emit: Callable[[Event], Awaitable[None]] | None = None,
    stop_agent: Callable[[str], Awaitable[None]] | None = None,
    poll_s: float = 1.0,
    grace_s: float = 60.0,
) -> None:
    """Run a spec to completion. Mutates run.status / run.agent_ids.

    Optional: `emit` (to publish run.status), `stop_agent(agent_id)` (used on overall timeout).
    """

    async def set_status(s: RunStatus) -> None:
        run.status = s
        if emit:
            try:
                await emit(Event(type="run.status", run_id=run.id, data={"status": s.value}))
            except Exception:  # noqa: BLE001
                log.exception("emit failed")

    wanted = [Platform(p) for p in platforms] if platforms else spec.platforms
    chosen = [a for a in spec.agents if a.platform in wanted]
    if not chosen:
        await set_status(RunStatus.failed)
        return
    await set_status(RunStatus.running)

    try:
        # 1. setup apps on every selected platform, concurrently
        await asyncio.gather(
            *(setup_platform(p, spec.setup[p]) for p in {a.platform for a in chosen} if spec.setup.get(p)),
            return_exceptions=True,
        )

        # 2. start agents (senders get a head-start delay so receivers are subscribed first:
        #    Nostr geohash messages are ephemeral and are not replayed to late joiners)
        agents: list[Agent] = []

        async def start(a: SpecAgent) -> None:
            delay = a.start_delay_s
            if delay is None:
                delay = SENDER_DELAY_S if (a.role == "sender" and len(chosen) > 1) else 0
            if delay:
                await asyncio.sleep(delay)
            if run.status == RunStatus.stopped:
                return
            agent = await start_agent(agent_request(spec, a), run.id)
            agents.append(agent)
            if agent.id not in run.agent_ids:
                run.agent_ids.append(agent.id)

        starters = asyncio.gather(*(start(a) for a in chosen), return_exceptions=True)

        # 3. wait for all agents to be terminal
        max_delay = max([SENDER_DELAY_S] + [a.start_delay_s or 0 for a in chosen])
        deadline = time.monotonic() + spec.timeout_s + grace_s + max_delay
        results = None
        while True:
            if starters.done() and results is None:
                results = starters.result()
                for r in results:
                    if isinstance(r, BaseException):
                        log.error("start_agent failed: %r", r)
            if run.status == RunStatus.stopped:
                break
            if results is not None and all(a.status in TERMINAL for a in agents):
                break
            if time.monotonic() > deadline:
                log.warning("run %s timed out", run.id)
                for a in agents:
                    if a.status not in TERMINAL and stop_agent:
                        try:
                            await stop_agent(a.id)
                        except Exception:  # noqa: BLE001
                            pass
                break
            await asyncio.sleep(poll_s)

        if run.status == RunStatus.stopped:
            starters.cancel()
            return
        ok = (
            results is not None
            and not any(isinstance(r, BaseException) for r in results)
            and len(agents) == len(chosen)
            and all(a.status == AgentStatus.passed for a in agents)
        )
        await set_status(RunStatus.passed if ok else RunStatus.failed)
    except asyncio.CancelledError:
        await set_status(RunStatus.stopped)
        raise
    except Exception:  # noqa: BLE001
        log.exception("run %s crashed", run.id)
        await set_status(RunStatus.failed)
