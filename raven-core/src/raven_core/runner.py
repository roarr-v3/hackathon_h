"""Spec runner: setup apps on each worker, start one agent per spec agent, wait, set run status."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shlex
from typing import Any, Awaitable, Callable

from raven_core.events import Agent, AgentStatus, CreateAgentRequest, Event, ExecutionProfile, Placement, Platform, Run, RunStatus
from raven_core.specs import Spec, SpecAgent
from raven_core.workers import FakeWorker, get_worker

log = logging.getLogger(__name__)

TERMINAL = {AgentStatus.passed, AgentStatus.failed, AgentStatus.stopped, AgentStatus.error}
SENDER_DELAY_S = float(os.environ.get("RAVEN_SENDER_DELAY_S", "20"))

StartAgent = Callable[[CreateAgentRequest, str], Awaitable[Agent]]
StopAgent = Callable[..., Awaitable[None]]  # (agent_id, reason=None)


class SetupTimeoutError(TimeoutError):
    """Application setup timed out before H computer use started."""


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


async def setup_platform(
    platform: Platform, cmds: list[str], placement: Placement = Placement.auto,
    *, worker: Any = None, execution: ExecutionProfile | None = None, timeout_s: float = 60.0,
) -> Any:
    """Share one finite budget across preparation, commands and window waits."""
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("setup timeout_s must be finite and positive")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    phase = "selecting the setup worker"
    try:
        async with asyncio.timeout_at(deadline):
            if worker is None:
                worker = get_worker(platform, placement=placement, execution=execution)
                if execution is not None:
                    from raven_core.workers import prepare_worker

                    phase = "preparing the setup worker"
                    worker = await prepare_worker(worker, execution=execution)
            if execution is None and getattr(worker, "target", None) in ("apple-container", "lume"):
                from raven_core import machines

                phase = "preparing the setup worker"
                await machines.ensure(platform)
            for index, raw in enumerate(cmds, 1):
                cmd, background = launch_command(platform, raw)
                phase = f"running setup command {index}/{len(cmds)}"
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                rc, out, err = await worker.shell(cmd, timeout=remaining)
                if rc == 124:
                    raise TimeoutError
                if rc != 0:
                    raise RuntimeError(f"setup command {index}/{len(cmds)} exited {rc}: {(err or out)[-300:]}")
                if background:
                    phase = f"waiting for the window after setup command {index}/{len(cmds)}"
                    await asyncio.sleep(2)  # let the window appear before the agent's first screenshot
    except TimeoutError:
        raise SetupTimeoutError(
            f"{platform.value} setup timed out while {phase} (setup budget {timeout_s:g}s; before H computer use)"
        ) from None
    return worker


def agent_request(spec: Spec, a: SpecAgent, placement: Placement = Placement.auto) -> CreateAgentRequest:
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
    task = (
        f"Setup: Use the assigned {a.platform.value} desktop and verify the starting screen.\n\n"
        f"Task and expected result:\n{a.task.strip()}{context}\n\n"
        "Verification: Base the verdict on what you see on screen. Stop and describe the blocker if "
        "you cannot run the test. After two unchanged observations, change approach or stop."
    )
    # The agent gets the suite's deadline so it ends itself (naming the phase it was in) instead
    # of being stopped from the outside; the runner's own timeout below is only a backstop.
    return CreateAgentRequest(platform=a.platform, task=task, title=title, placement=placement, timeout_s=spec.timeout_s)


async def run_spec(
    spec: Spec,
    platforms: list[Platform] | None,
    start_agent: StartAgent,
    run: Run,
    *,
    emit: Callable[[Event], Awaitable[None]] | None = None,
    stop_agent: StopAgent | None = None,
    reload_spec: Callable[[], Spec] | None = None,
    wait_for_agents: Callable[[], Awaitable[None]] | None = None,
    prepare_platform: Callable[[Platform, list[str]], Awaitable[Any]] | None = None,
    failure_context: Callable[[list[Agent]], dict[str, Any]] | None = None,
    placement: Placement = Placement.auto,
    poll_s: float = 1.0,
    grace_s: float = 60.0,
) -> None:
    """Retry repeatable suites on execution errors, keeping each attempt's evidence."""
    async def set_status(status: RunStatus) -> None:
        run.status = status
        if emit:
            await emit(Event(type="run.status", run_id=run.id, data={"status": status.value}))

    async def progress(text: str) -> None:
        log.info("%s: %s", run.id, text)
        if emit:
            await emit(Event(type="agent.message", run_id=run.id, data={"role": "system", "text": text}))

    async def stop_pending(agents: list[Agent], reason: str | None = None) -> None:
        if stop_agent:
            await asyncio.gather(*(stop_agent(a.id, reason) for a in agents if a.status not in TERMINAL))

    async def settle() -> None:
        """Give agents that were just stopped a moment to record their final reason."""
        if wait_for_agents:
            try:
                await wait_for_agents()
            except TimeoutError:
                pass

    await set_status(RunStatus.running)
    wanted = set(platforms or spec.platforms)
    attempts = spec.attempts
    previous_agents: list[Agent] = []
    for attempt in range(1, attempts + 1):
        if run.status == RunStatus.stopped:
            return
        if attempt > 1 and reload_spec:
            spec = reload_spec()
        chosen = [a for a in spec.agents if a.platform in wanted]
        if not chosen:
            await progress("No test steps matched the selected platforms.")
            break
        if attempts > 1:
            await progress(f"Starting QA attempt {attempt}/{attempts}.")

        setup = list(dict.fromkeys(a.platform for a in chosen if prepare_platform or spec.setup.get(a.platform)))
        results = await asyncio.gather(
            *(prepare_platform(p, spec.setup.get(p, [])) if prepare_platform
              else setup_platform(p, spec.setup[p], placement, **({"execution": run.execution} if run.execution else {}))
              for p in setup), return_exceptions=True,
        )
        errors = [f"{p.value} setup: {r}" for p, r in zip(setup, results) if isinstance(r, BaseException)]
        agents: list[Agent] = []

        async def start(a: SpecAgent) -> None:
            delay = a.start_delay_s
            if delay is None:
                delay = SENDER_DELAY_S if a.role == "sender" and len(chosen) > 1 else 0
            if delay:
                await asyncio.sleep(delay)
            if run.status == RunStatus.stopped:
                return
            request = agent_request(spec, a, placement)
            request.execution = run.execution.model_copy(deep=True) if run.execution else None
            if attempt > 1:
                request.task += (
                    "\n\nThe previous attempt had an execution error. Verify the current screen and app state "
                    "from scratch. If an action leaves the screen unchanged twice, use a different route or "
                    "stop and explain the blocker. Require fresh evidence for this attempt."
                )
                if failure_context:
                    context = failure_context([previous for previous in previous_agents if previous.platform == a.platform])
                    failures = [
                        {key: value for key, value in failure.items() if key not in ("task", "task_id")}
                        for failure in context.get("failures", [])
                    ]
                    request.task += (
                        "\n\nHistorical failure evidence follows. Keep the current suite's assertions and inputs; "
                        "use this evidence only to recover from the execution error:\n"
                        + json.dumps({"failures": failures}, ensure_ascii=False)
                    )
            agent = await start_agent(request, run.id)
            agents.append(agent)
            if agent.id not in run.agent_ids:
                run.agent_ids.append(agent.id)

        if not errors:
            max_delay = max(
                (a.start_delay_s if a.start_delay_s is not None else
                 SENDER_DELAY_S if a.role == "sender" and len(chosen) > 1 else 0)
                for a in chosen
            )
            attempt_limit = spec.timeout_s + grace_s + max_delay
            try:
                async with asyncio.timeout(attempt_limit):
                    started = await asyncio.gather(*(start(a) for a in chosen), return_exceptions=True)
                    errors.extend(f"{a.platform.value} startup: {r}" for a, r in zip(chosen, started)
                                  if isinstance(r, BaseException))
                    while run.status != RunStatus.stopped and any(a.status not in TERMINAL for a in agents):
                        await asyncio.sleep(poll_s)
            except TimeoutError:
                # Agents normally end on their own spec.timeout_s deadline; reaching this means one did not.
                reason = f"QA attempt exceeded {attempt_limit:.0f}s while waiting for computer use"
                errors.append(reason + ".")
                await stop_pending(agents, reason)
                await settle()
            except asyncio.CancelledError:
                await stop_pending(agents, "Run cancelled")
                raise

        if run.status == RunStatus.stopped:
            return
        # Agents still pending at this point were stopped by the attempt timeout above (a user stop
        # returns earlier); their reason says what they were waiting for.
        errors.extend(f"{a.platform.value}: {a.verdict_reason or 'agent execution error'}"
                      for a in agents if a.status in (AgentStatus.error, AgentStatus.stopped))
        if not errors and len(agents) == len(chosen) and all(a.status == AgentStatus.passed for a in agents):
            await set_status(RunStatus.passed)
            return
        if errors:
            await progress(f"Attempt {attempt}/{attempts}: " + " | ".join(errors))
        if not errors or any(a.status == AgentStatus.failed for a in agents) or attempt == attempts:
            break
        if wait_for_agents:
            await wait_for_agents()
        elif any(a.status not in TERMINAL for a in agents):
            break
        await progress("Retrying the suite after an execution error.")
        previous_agents = agents
        await asyncio.sleep(poll_s)

    if run.status != RunStatus.stopped:
        await set_status(RunStatus.failed)
