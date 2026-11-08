"""Holo computer-use agent loop (OpenAI-compatible function calling).

Follows https://hub.hcompany.ai/models-api/build-an-agent/{core-concepts,function-calling}:
- tools declared in `tools`, tool_choice="required", temperature 0.8
- each step the screenshot is a `user` message wrapped in <observation> tags
- only the last N screenshots stay in context (max 5 images/request; we keep 2)
- coordinates are integers in [0, 1000] normalized to the image, origin top-left
- each assistant tool_call gets a `role: tool` result with its tool_call_id

Timeouts are layered so a stall is attributable: every Holo request has its own deadline
(HAI_REQUEST_TIMEOUT_S, clamped to what is left of the agent's budget) and is retried visibly
(HAI_REQUEST_ATTEMPTS); the agent's budget (timeout_s, RAVEN_AGENT_TIMEOUT_S) ends the loop with
a verdict that names the phase it was in. The loop logs every request it sends, how long it
waited, and every retry, both to the Python log and as `agent.message` system events.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from raven_core.env import load_env
from raven_core.events import Agent, AgentStatus, Event
from raven_core.workers.base import Worker, png_size

log = logging.getLogger(__name__)

DEFAULT_MODEL = "holo4-27b"
FALLBACK_MODEL = "holo3-1-35b-a3b"
DEFAULT_BASE_URL = "https://api.hcompany.ai/v1"
MAX_STEPS = 40
KEEP_IMAGES = 2
SETTLE_S = 0.8
AGENT_TIMEOUT_S = 600.0  # default agent budget (RAVEN_AGENT_TIMEOUT_S) when no suite deadline is given
REQUEST_TIMEOUT_S = 45.0  # one Holo request (HAI_REQUEST_TIMEOUT_S); always shorter than the agent budget
REQUEST_ATTEMPTS = 3  # requests per step before the agent gives up (HAI_REQUEST_ATTEMPTS)
SLOW_REQUEST_S = 15.0  # announce a request that is still pending after this long (HAI_SLOW_REQUEST_S)
MIN_REQUEST_S = 5.0  # don't send a request the agent budget can no longer accommodate
REQUEST_GRACE_S = 2.0  # our own cut-off runs this long after the SDK's timeout, in case it never fires
RETRY_BACKOFF_S = 1.5  # × attempt number
CONNECT_TIMEOUT_S = 10.0
RETRY_STATUSES = {408, 409, 429}
REQUEST_HEADROOM_S = 5.0
MAX_REQUEST_ATTEMPTS = 10

Emit = Callable[[Event], Awaitable[None]]
StopReason = Callable[[], str | None]


def make_client() -> Any:
    """AsyncOpenAI client for the H Models API. No SDK retries: `_Loop.complete` retries
    itself so every attempt is logged and bounded by the agent's deadline."""
    load_env()
    from openai import AsyncOpenAI

    key = os.environ.get("HAI_API_KEY")
    if not key:
        raise RuntimeError("HAI_API_KEY is not set (Raven Agent Settings, or .env for development)")
    return AsyncOpenAI(
        base_url=os.environ.get("HAI_BASE_URL", DEFAULT_BASE_URL),
        api_key=key,
        timeout=_env_float("HAI_TIMEOUT_S", REQUEST_TIMEOUT_S),
        max_retries=0,
    )


def _positive_float(value: Any, default: float, *, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) and (number >= 0 if allow_zero else number > 0) else default


def _env_float(name: str, default: float, *, allow_zero: bool = False) -> float:
    return _positive_float(os.environ.get(name), default, allow_zero=allow_zero)


def _bounded_int(value: Any, default: int, maximum: int) -> int:
    return max(1, int(min(maximum, _positive_float(value, default))))


def _get_stop_reason(stop: asyncio.Event, get_stop_reason: StopReason | None = None) -> str | None:
    reason = None
    if get_stop_reason is not None:
        try:
            reason = get_stop_reason()
        except Exception as error:
            log.warning("stop reason unavailable (%s)", type(error).__name__)
    if not isinstance(reason, str) or not reason.strip():
        reason = getattr(stop, "reason", None)
    return reason.strip() if isinstance(reason, str) and reason.strip() else None


def _model_label(model: str | None) -> str:
    if not model:
        return "default model"
    return model if re.fullmatch(r"holo[0-9a-z.-]{1,80}", model) else "custom model"


def _http_status(error: Exception) -> int | None:
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) and 100 <= status <= 599 else None


async def _cancel_and_drain(*tasks: asyncio.Future | None) -> None:
    pending = [task for task in tasks if task is not None]
    for task in pending:
        if not task.done():
            task.cancel()
    if pending:
        drained = asyncio.gather(*pending, return_exceptions=True)
        interrupted = False
        while not drained.done():
            try:
                await asyncio.shield(drained)
            except asyncio.CancelledError:
                interrupted = True
        if interrupted:
            raise asyncio.CancelledError


# ---------------------------------------------------------------- prompt + tools


def system_prompt(agent: Agent, screen: tuple[int, int], allow_shell: bool) -> str:
    os_name = {"macos": "macOS", "linux": "Linux (Ubuntu, XFCE desktop)", "windows": "Windows", "ios": "iOS"}.get(
        agent.platform.value, agent.platform.value
    )
    shell_line = (
        "- You may use `shell` only for read-only verification (e.g. checking a process is running); "
        "never use it to perform the task itself, the test must go through the UI.\n"
        if allow_shell
        else ""
    )
    return (
        f"You are Raven, a meticulous QA tester operating a {os_name} computer through screenshots. "
        f"The screen is {screen[0]}x{screen[1]}. You are one of several testers running the same app on "
        "different operating systems at the same time; other testers may post messages you need to see.\n"
        "Rules:\n"
        "- Every turn you get the current screenshot inside <observation> tags. Act with exactly one tool call per turn.\n"
        "- Coordinates are integers in [0, 1000] relative to the screenshot (0,0 top-left, 1000,1000 bottom-right).\n"
        "- Before typing, make sure the right input field has focus (click it first). Terminal apps (TUIs) take "
        "keyboard input directly; press Enter with the `key` tool to submit.\n"
        "- When you need to wait for something to appear (a message from another device, a page load), use `wait`.\n"
        f"{shell_line}"
        "- Messages wrapped in <operator> tags come from the human supervising the test; follow them.\n"
        "- When the test goal is verified on screen, call `done` with passed=true and a short factual reason. "
        "If the app visibly fails an assertion, call `done` with passed=false and quote what you observed. "
        "If setup or tooling prevents verification, call `done` with passed=false and blocked=true. "
        "If the same action leaves the screen unchanged twice, try a different route or report the blocker. "
        "Messages over the network can take up to a minute to arrive."
    )


def _xy(desc: str) -> dict:
    return {
        "element": {"type": "string", "description": desc},
        "x": {"type": "integer", "description": "X coordinate as integer in [0, 1000]"},
        "y": {"type": "integer", "description": "Y coordinate as integer in [0, 1000]"},
    }


def build_tools(allow_shell: bool = False) -> list[dict]:
    def fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": properties, "required": required},
            },
        }

    tools = [
        fn("click", "Click at (x, y) coordinates", _xy("Detailed description of the target UI element"), ["element", "x", "y"]),
        fn(
            "double_click",
            "Double-click at (x, y) coordinates",
            _xy("Detailed description of the target UI element"),
            ["element", "x", "y"],
        ),
        fn(
            "type_text",
            "Type text with the keyboard into the focused element",
            {
                "text": {"type": "string", "description": "Text to type"},
                "press_enter": {"type": "boolean", "description": "Press Enter after typing"},
            },
            ["text"],
        ),
        fn(
            "key",
            "Press a key or key combination, e.g. 'enter', 'escape', 'tab', 'ctrl+c', 'cmd+space', 'alt+f4'",
            {"keys": {"type": "string", "description": "Key or '+'-joined combination"}},
            ["keys"],
        ),
        fn(
            "scroll",
            "Scroll the mouse wheel with the pointer at (x, y)",
            {
                **_xy("Description of the area to scroll"),
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "amount": {"type": "integer", "description": "Number of wheel notches (1-10)"},
            },
            ["x", "y", "direction"],
        ),
        fn(
            "wait",
            "Wait for the screen to change (e.g. for a message to arrive)",
            {"seconds": {"type": "number", "description": "Seconds to wait (1-30)"}},
            ["seconds"],
        ),
        fn(
            "done",
            "Finish the test with a verdict",
            {
                "passed": {"type": "boolean", "description": "true if the test goal was verified on screen"},
                "blocked": {"type": "boolean", "description": "true if setup or tooling prevented verification"},
                "reason": {"type": "string", "description": "Short factual explanation of what was observed"},
            },
            ["passed", "reason"],
        ),
    ]
    if allow_shell:
        tools.insert(
            -1,
            fn(
                "shell",
                "Run a read-only shell command on the machine for verification; returns exit code and output",
                {"command": {"type": "string"}},
                ["command"],
            ),
        )
    return tools


# ---------------------------------------------------------------- message helpers


def observation(png: bytes, note: str | None = None) -> dict:
    b64 = base64.b64encode(png).decode()
    content: list[dict] = [
        {"type": "text", "text": "<observation>\n"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        {"type": "text", "text": "\n</observation>"},
    ]
    if note:
        content.append({"type": "text", "text": note})
    return {"role": "user", "content": content}


def trim_to_last_n_images(messages: list[dict], n: int = KEEP_IMAGES) -> None:
    seen = 0
    for msg in reversed(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        new_content = []
        for part in msg["content"]:
            if part.get("type") == "image_url":
                seen += 1
                if seen > n:
                    new_content.append({"type": "text", "text": "[screenshot omitted]"})
                    continue
            new_content.append(part)
        msg["content"] = new_content


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {"value": v}
    except (json.JSONDecodeError, TypeError):
        return {"_raw": str(raw)}


def _clip(s: str | None, n: int = 600) -> str | None:
    if not s:
        return None
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- the loop


class _Verdict(Exception):
    def __init__(self, passed: bool, reason: str):
        self.passed, self.reason = passed, reason


async def run_agent(
    agent: Agent,
    worker: Worker,
    emit: Emit,
    inbox: asyncio.Queue[str],
    stop: asyncio.Event,
    artifacts_dir: Path,
    *,
    timeout_s: float | None = None,
    max_steps: int = MAX_STEPS,
    client: Any = None,
    model: str | None = None,
    allow_shell: bool | None = None,
    request_timeout_s: float | None = None,
    request_attempts: int | None = None,
    get_stop_reason: StopReason | None = None,
) -> None:
    """Run one agent to a verdict. Never raises (except CancelledError, after emitting).

    `timeout_s` is the agent's whole budget (the suite's deadline when run from a spec);
    `request_timeout_s` bounds each Holo request and is always clamped below what is left of it.
    """
    artifacts_dir = Path(artifacts_dir)
    shot_dir = artifacts_dir / agent.id
    shot_dir.mkdir(parents=True, exist_ok=True)

    async def send(type_: str, data: dict) -> None:
        try:
            await emit(Event(type=type_, run_id=agent.run_id, agent_id=agent.id, data=data))  # type: ignore[arg-type]
        except Exception as error:
            log.warning("agent event emit failed (%s)", type(error).__name__)

    async def set_status(s: AgentStatus) -> None:
        if agent.status != s:
            agent.status = s
            await send("agent.status", {"status": s.value})

    async def finish(status: AgentStatus, passed: bool, reason: str) -> None:
        agent.verdict_reason = reason
        if status in (AgentStatus.error, AgentStatus.stopped):
            state = "cancelled" if status == AgentStatus.stopped else "failed"
            where = (loop.interrupted_at or loop.where()) if loop else "starting"
            text = f"Holo loop {state} while {where}."
            log.info("%s agent %s: %s", agent.id, state, text)
            await send("agent.message", {"role": "system", "text": text, "phase": "agent", "state": state})
        await send("agent.verdict", {"passed": passed, "reason": reason})
        await set_status(status)

    if allow_shell is None:
        allow_shell = os.environ.get("RAVEN_AGENT_SHELL", "").lower() in ("1", "true", "yes")
    timeout_s = _positive_float(timeout_s, _env_float("RAVEN_AGENT_TIMEOUT_S", AGENT_TIMEOUT_S))
    deadline = time.monotonic() + timeout_s

    loop: _Loop | None = None
    try:
        await set_status(AgentStatus.running)
        if stop.is_set():
            raise _Stopped(f"{_get_stop_reason(stop, get_stop_reason) or 'Stopped by user'} while starting")
        if client is None:
            client = make_client()
        model = model or os.environ.get("HAI_MODEL") or DEFAULT_MODEL
        loop = _Loop(
            agent, worker, send, inbox, stop, shot_dir, client, model, allow_shell, deadline, max_steps,
            timeout_s=timeout_s, request_timeout_s=request_timeout_s, request_attempts=request_attempts,
            get_stop_reason=get_stop_reason,
        )
        await loop.run()
        await finish(AgentStatus.error, False, f"Step budget exhausted ({max_steps} steps) without a verdict")
    except _Verdict as v:
        await finish(AgentStatus.passed if v.passed else AgentStatus.failed, v.passed, v.reason)
    except (TimeoutError, _ExecutionError) as e:
        reason = str(e) if isinstance(e, (_Deadline, _ExecutionError)) else (
            f"Worker operation timed out while {loop.where()}" if loop else "Timed out before reaching a verdict"
        )
        log.warning("agent %s failed while %s", agent.id, loop.where() if loop else "starting")
        await finish(AgentStatus.error, False, reason)
    except _Stopped as s:
        await finish(AgentStatus.stopped, False, s.reason)
    except asyncio.CancelledError:
        reason = _get_stop_reason(stop, get_stop_reason) or ("Stopped by user" if stop.is_set() else "Agent task cancelled")
        where = (loop.interrupted_at or loop.where()) if loop else "starting"
        log.info("agent %s cancelled while %s", agent.id, where)
        await finish(AgentStatus.stopped, False, f"{reason} while {where}")
        raise
    except Exception as e:  # noqa: BLE001
        log.error("agent %s failed (%s)", agent.id, type(e).__name__)
        await send("agent.message", {"role": "system", "text": f"Agent error: {type(e).__name__}"})
        await finish(AgentStatus.error, False, f"Agent error: {type(e).__name__}")


class _ExecutionError(Exception):
    pass


class _Stopped(Exception):
    def __init__(self, reason: str):
        self.reason = reason


class _Deadline(TimeoutError):
    """The agent's own budget ran out (distinct from a worker call that merely timed out)."""


def _retryable(e: Exception) -> bool:
    """Timeouts, connection errors, throttling and server errors are worth another request."""
    status = _http_status(e)
    if status is not None:
        return status in RETRY_STATUSES or status >= 500
    return True  # APITimeoutError / APIConnectionError / our own asyncio timeout


class _Loop:
    def __init__(
        self, agent, worker, send, inbox, stop, shot_dir, client, model, allow_shell, deadline, max_steps,
        *, timeout_s: float | None = None, request_timeout_s: float | None = None, request_attempts: int | None = None,
        get_stop_reason: StopReason | None = None,
    ):
        self.agent: Agent = agent
        self.worker: Worker = worker
        self.send = send
        self.inbox: asyncio.Queue[str] = inbox
        self.stop: asyncio.Event = stop
        self.get_stop_reason = get_stop_reason
        self.shot_dir: Path = shot_dir
        self.client = client
        self.model: str = model
        self.allow_shell: bool = allow_shell
        self.deadline: float | None = deadline
        self.timeout_s = timeout_s
        self.max_steps = _bounded_int(max_steps, MAX_STEPS, 10000)
        self.request_timeout_s = _positive_float(request_timeout_s, _env_float("HAI_REQUEST_TIMEOUT_S", REQUEST_TIMEOUT_S))
        self.request_attempts = _bounded_int(
            request_attempts if request_attempts is not None else os.environ.get("HAI_REQUEST_ATTEMPTS"),
            REQUEST_ATTEMPTS, MAX_REQUEST_ATTEMPTS,
        )
        self.slow_request_s = _env_float("HAI_SLOW_REQUEST_S", SLOW_REQUEST_S)
        self.settle_s = _env_float("RAVEN_SETTLE_S", SETTLE_S, allow_zero=True)
        self.headroom_s = min(REQUEST_HEADROOM_S, (timeout_s or AGENT_TIMEOUT_S) * 0.1)
        self.min_request_s = min(MIN_REQUEST_S, (timeout_s or AGENT_TIMEOUT_S) * 0.05)
        self.reasoning_disabled = False
        self.tools = build_tools(allow_shell)
        self.messages: list[dict] = []
        self.screen = (0, 0)
        # where the loop is, so a stop/timeout can say what it interrupted
        self.step = 0
        self.actions_done = 0
        self.phase = "starting"
        self.phase_since = time.monotonic()
        self.inflight: dict[str, Any] | None = None  # the Holo request we are waiting on
        self.n_requests = 0
        self.n_retries = 0
        self.wait_s = 0.0  # total time spent waiting for Holo
        self.interrupted_at: str | None = None

    # ------------------------------------------------------------ progress bookkeeping

    def enter(self, phase: str) -> None:
        self.phase, self.phase_since = phase, time.monotonic()

    def where(self) -> str:
        """What the loop is doing right now, e.g. for the reason of a stop or timeout."""
        if self.inflight:
            c = self.inflight
            return (
                f"waiting for Holo request #{c['n']} (step {c['step']}, attempt {c['attempt']}/{self.request_attempts}, "
                f"{time.monotonic() - c['sent']:.2f}s of its {c['budget']:g}s limit, {c['hard_budget']:g}s hard deadline)"
            )
        return f"{self.phase} (step {self.step}, {time.monotonic() - self.phase_since:.2f}s)"

    def stats(self) -> str:
        return (
            f"{self.n_requests} Holo request(s) sent, {self.n_retries} retried, "
            f"{self.wait_s:.2f}s waiting for Holo, {self.actions_done} action(s) executed"
        )

    def timed_out(self) -> str:
        budget = f" after {self.timeout_s:g}s" if self.timeout_s else ""
        return f"Timed out{budget} before reaching a verdict while {self.where()}. {self.stats()}."

    def stopped(self) -> str:
        reason = _get_stop_reason(self.stop, self.get_stop_reason) or "Stopped by user"
        return f"{reason} while {self.where()}"

    def check(self) -> None:
        if self.stop.is_set():
            raise _Stopped(self.stopped())
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise _Deadline(self.timed_out())

    async def guarded(self, aw: Awaitable[Any]) -> Any:
        """Await `aw` but abort promptly on stop / deadline."""
        task = asyncio.ensure_future(aw)
        stopper = asyncio.ensure_future(self.stop.wait())
        try:
            self.check()
            timeout = max(0.0, self.deadline - time.monotonic()) if self.deadline is not None else None
            done, _ = await asyncio.wait({task, stopper}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            self.check()
            if task in done:
                return task.result()
            raise _Deadline(self.timed_out())
        finally:
            await _cancel_and_drain(task, stopper)

    def drain_inbox(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except asyncio.QueueEmpty:
                return out

    async def run(self) -> None:
        self.enter("measuring the screen")
        self.screen = await self.guarded(self.worker.size())
        self.messages = [
            {"role": "system", "content": system_prompt(self.agent, self.screen, self.allow_shell)},
            {"role": "user", "content": f"Test task:\n{self.agent.task}"},
        ]
        budget = f"{self.timeout_s:g}s budget" if self.timeout_s else "no deadline"
        await self.send(
            "agent.message",
            {
                "role": "system",
                "text": f"Holo loop on {_model_label(self.model)}: {budget}, {self.request_timeout_s:g}s per request, "
                f"up to {self.request_attempts} attempts per step, {self.max_steps} steps max.",
            },
        )
        for step in range(self.max_steps):
            self.step = step + 1
            self.check()
            for text in self.drain_inbox():
                self.messages.append({"role": "user", "content": f"<operator>\n{text}\n</operator>"})

            self.enter("taking a screenshot")
            png = await self.guarded(self.worker.screenshot())
            w, h = png_size(png)
            name = f"{step:03d}.png"
            (self.shot_dir / name).write_bytes(png)
            url = f"/artifacts/{self.agent.id}/{name}"
            self.agent.last_screenshot = url
            await self.send("agent.screenshot", {"url": url, "width": w, "height": h})

            self.messages.append(observation(png, f"Step {step + 1}/{self.max_steps}."))
            trim_to_last_n_images(self.messages, KEEP_IMAGES)

            msg = await self.guarded(self.complete())
            content = _get(msg, "content")
            reasoning = _get(msg, "reasoning_content") or _get(msg, "reasoning")
            tool_calls = _get(msg, "tool_calls") or []
            thought = _clip(content) or _clip(reasoning)

            calls = []
            for i, tc in enumerate(tool_calls):
                fnc = _get(tc, "function")
                raw_args = _get(fnc, "arguments") or "{}"
                if not isinstance(raw_args, str):
                    raw_args = json.dumps(raw_args)
                calls.append(
                    {
                        "id": _get(tc, "id") or f"call_{step}_{i}",
                        "type": "function",
                        "function": {"name": _get(fnc, "name"), "arguments": raw_args},
                    }
                )
            assistant: dict[str, Any] = {"role": "assistant", "content": content or ""}
            if calls:
                assistant["tool_calls"] = calls
            self.messages.append(assistant)

            if not calls:
                if content:
                    await self.send("agent.message", {"role": "agent", "text": _clip(content, 2000)})
                continue

            for call in calls:
                name_ = call["function"]["name"] or ""
                args = _parse_args(call["function"]["arguments"])
                result = await self.execute(name_, args, thought)
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                thought = None  # only attach the thought to the first action
            self.enter("settling after the action")
            await self.guarded(asyncio.sleep(self.settle_s))

    # ------------------------------------------------------------ Holo requests

    def request_budget(self, delay: float = 0.0) -> tuple[float, float]:
        """SDK and hard deadlines leave time for verification and cancellation cleanup."""
        self.check()
        grace = _positive_float(REQUEST_GRACE_S, 2.0, allow_zero=True)
        available = self.request_timeout_s + grace
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            available = left - self.headroom_s - delay
            if available <= max(self.min_request_s, 0.0):
                raise _Deadline(
                    f"Timed out before reaching a verdict: {max(0.0, left):.2f}s of the agent budget left, "
                    f"too little for Holo request #{self.n_requests + 1} (step {self.step})"
                    f"{' after retry backoff' if delay else ''}; reserving {self.headroom_s:g}s for verification "
                    f"and cleanup while {self.where()}. {self.stats()}."
                )
        grace = min(grace, available * 0.1)
        budget = min(self.request_timeout_s, available - grace)
        return budget, min(available, budget + grace)

    def request_kwargs(self, budget: float) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=self.messages,
            tools=self.tools,
            tool_choice="required",
            temperature=0.8,
            max_tokens=_bounded_int(os.environ.get("HAI_MAX_TOKENS"), 4096, 32768),
            timeout=httpx.Timeout(budget, connect=min(CONNECT_TIMEOUT_S, budget)),
        )
        effort = os.environ.get("HAI_REASONING_EFFORT", "low" if self.model == "holo4-27b" else "")
        if effort and not self.reasoning_disabled:
            kwargs["extra_body"] = {"reasoning_effort": effort}
        return kwargs

    async def request_event(self, state: str, text: str, **fields: Any) -> None:
        c = self.inflight or {}
        data = {
            "role": "system", "text": text, "phase": "model_request", "state": state,
            "request": c.get("n", self.n_requests), "step": self.step,
            "attempt": c.get("attempt"), "max_attempts": self.request_attempts,
            "model": _model_label(self.model),
            **fields,
        }
        logger = log.warning if state in ("failed", "retrying", "pending") else log.info
        logger("%s model_request %s: %s", self.agent.id, state, text)
        await self.send("agent.message", data)

    async def _slow_notice(self, n: int, budget: float) -> None:
        if self.slow_request_s >= budget:
            return
        await asyncio.sleep(self.slow_request_s)
        if not self.inflight or self.inflight["n"] != n:
            return
        self.check()
        elapsed = time.monotonic() - self.inflight["sent"]
        await self.request_event(
            "pending", f"Holo request {n} still pending after {elapsed:.2f} s (hard limit {budget:g} s).",
            elapsed_s=elapsed, hard_timeout_s=budget,
        )

    async def request(self, attempt: int, budget: float, hard_budget: float, kwargs: dict) -> Any:
        self.n_requests += 1
        if attempt > 1:
            self.n_retries += 1
        n = self.n_requests
        images = sum(
            1 for m in self.messages if isinstance(m.get("content"), list)
            for p in m["content"] if p.get("type") == "image_url"
        )
        sent = time.monotonic()
        hard_deadline = sent + hard_budget
        if self.deadline is not None:
            hard_deadline = min(hard_deadline, self.deadline - self.headroom_s)
        hard_budget = hard_deadline - sent
        self.enter(f"waiting for Holo request #{n} (attempt {attempt}/{self.request_attempts})")
        self.inflight = {
            "n": n, "step": self.step, "attempt": attempt, "sent": sent,
            "budget": budget, "hard_budget": hard_budget,
        }
        notice = None
        try:
            await self.request_event(
                "started",
                f"Holo request {n} started: step {self.step}, attempt {attempt}/{self.request_attempts}, "
                f"model={_model_label(self.model)}, images={images}, messages={len(self.messages)}, "
                f"SDK limit {budget:g}s, hard limit {hard_budget:.2f}s.",
                elapsed_s=0.0, timeout_s=budget, hard_timeout_s=hard_budget,
                remaining_s=self.deadline - sent if self.deadline is not None else None,
                images=images, messages=len(self.messages),
            )
            self.check()
            if time.monotonic() >= hard_deadline:
                raise TimeoutError
            notice = asyncio.create_task(self._slow_notice(n, hard_budget))
            try:
                try:
                    async with asyncio.timeout_at(hard_deadline) as timer:
                        resp = await self.client.chat.completions.create(**kwargs)
                    if timer.expired():
                        raise TimeoutError
                except Exception:
                    self.check()
                    raise
                self.check()
            finally:
                await _cancel_and_drain(notice)
                notice = None
            msg = resp.choices[0].message
            names = {t["function"]["name"] for t in self.tools} | {"answer"}
            tools = ", ".join(
                name if (name := _get(_get(tc, "function"), "name")) in names else "unknown tool"
                for tc in (_get(msg, "tool_calls") or [])
            ) or "no tool call"
            elapsed = time.monotonic() - sent
            retried = f" after {attempt} attempts" if attempt > 1 else ""
            self.enter(f"processing Holo request #{n} response")
            await self.request_event(
                "succeeded", f"Holo answered request {n} in {elapsed:.2f} s{retried}: {tools}.",
                elapsed_s=elapsed,
            )
            return msg
        except (asyncio.CancelledError, _Stopped, _Deadline):
            self.interrupted_at = self.where()
            await self.request_event(
                "cancelled", f"Holo request {n} cancelled after {time.monotonic() - sent:.2f} s; no response will be acted on.",
                elapsed_s=time.monotonic() - sent,
            )
            raise
        except Exception as error:
            elapsed = time.monotonic() - sent
            self.enter(f"handling Holo request #{n} failure")
            await self.request_event(
                "failed", f"Holo request {n} {self.failure_label(error, elapsed)}.",
                elapsed_s=elapsed, http_status=_http_status(error), error_type=type(error).__name__,
            )
            raise
        finally:
            self.wait_s += time.monotonic() - sent
            self.inflight = None
            await _cancel_and_drain(notice)

    def failure_label(self, error: Exception, elapsed: float) -> str:
        if isinstance(error, (TimeoutError, httpx.TimeoutException)) or type(error).__name__ == "APITimeoutError":
            return f"timed out after {elapsed:.2f}s ({type(error).__name__})"
        status = _http_status(error)
        return f"failed after {elapsed:.2f}s with {f'HTTP {status}' if status else type(error).__name__}"

    async def complete(self) -> Any:
        """One step's chat completion: every transport call, including configuration fixes,
        consumes an attempt and has a bounded deadline."""
        for attempt in range(1, self.request_attempts + 1):
            budget, hard_budget = self.request_budget()
            kwargs = self.request_kwargs(budget)
            sent = time.monotonic()
            try:
                return await self.request(attempt, budget, hard_budget, kwargs)
            except (_Stopped, _Deadline):
                raise
            except Exception as error:
                self.check()
                label = self.failure_label(error, time.monotonic() - sent)
                status = _http_status(error)
                retry = _retryable(error)
                adjustment = ""
                if attempt < self.request_attempts:
                    if status in (403, 404) and self.model != FALLBACK_MODEL:
                        self.model = FALLBACK_MODEL
                        adjustment = f"switching to {FALLBACK_MODEL}"
                    elif (
                        status == 400 and "reasoning_effort" in str(error)
                        and "extra_body" in kwargs and not self.reasoning_disabled
                    ):
                        self.reasoning_disabled = True
                        adjustment = "disabling unsupported reasoning_effort for this agent"
                if attempt >= self.request_attempts or not (retry or adjustment):
                    raise _ExecutionError(
                        f"Holo did not answer: request #{self.n_requests} {label}; "
                        f"gave up after {attempt} attempts in step {self.step}. {self.stats()}."
                    ) from error
                delay = 0.0 if adjustment else _positive_float(RETRY_BACKOFF_S, 1.5, allow_zero=True) * attempt
                self.enter(f"retry backoff after Holo request #{self.n_requests} ({delay:g}s)")
                self.request_budget(delay)
                await self.request_event(
                    "retrying",
                    f"Holo request {self.n_requests} retrying in {delay:g} s "
                    f"(attempt {attempt + 1}/{self.request_attempts})"
                    f"{': ' + adjustment if adjustment else ''}.",
                    attempt=attempt, next_attempt=attempt + 1, delay_s=delay,
                    elapsed_s=time.monotonic() - sent,
                )
                await self.guarded(asyncio.sleep(delay))

    def px(self, args: dict) -> tuple[int, int]:
        w, h = self.screen
        x = max(0, min(1000, int(round(float(args.get("x", 500))))))
        y = max(0, min(1000, int(round(float(args.get("y", 500))))))
        return min(w - 1, int(x / 1000 * w)), min(h - 1, int(y / 1000 * h))

    async def execute(self, name: str, args: dict, thought: str | None) -> str:
        self.check()
        w = self.worker
        result = "ok"
        if name == "done":
            passed = args.get("passed")
            if isinstance(passed, str):
                passed = passed.strip().lower() in ("true", "yes", "1", "pass", "passed")
            reason = str(args.get("reason") or args.get("_raw") or "")
            await self.send("agent.action", {"action": "done", "args": args, "thought": thought})
            self.check()
            if args.get("blocked") is True:
                raise _ExecutionError(reason or "Computer use was blocked before verification")
            raise _Verdict(bool(passed), reason or ("passed" if passed else "failed"))
        if name == "answer":  # Holo's native terminator, in case it slips through
            text = str(args.get("content", ""))
            await self.send("agent.action", {"action": "answer", "args": args, "thought": thought})
            self.check()
            low = text.lower()
            raise _Verdict(not any(k in low for k in ("fail", "not ", "unable", "error", "could not")), text)

        tool_name = name if name in {t["function"]["name"] for t in self.tools} else "unknown tool"
        self.enter(f"executing {tool_name} on the {self.agent.platform.value} worker")
        try:
            if name == "click":
                x, y = self.px(args)
                await self.guarded(w.click(x, y, str(args.get("button", "left"))))
                args = {**args, "px": [x, y]}
            elif name == "double_click":
                x, y = self.px(args)
                await self.guarded(w.double_click(x, y))
                args = {**args, "px": [x, y]}
            elif name == "type_text":
                await self.guarded(w.type_text(str(args.get("text", ""))))
                if args.get("press_enter"):
                    await self.guarded(w.key("enter"))
            elif name == "key":
                combo = str(args.get("keys") or args.get("key") or args.get("combo") or "")
                await self.guarded(w.key(combo))
            elif name == "scroll":
                x, y = self.px(args)
                amount = max(1, min(10, int(args.get("amount") or 3)))
                d = str(args.get("direction", "down"))
                dx = amount if d == "right" else -amount if d == "left" else 0
                dy = amount if d == "down" else -amount if d == "up" else 0
                await self.guarded(w.scroll(x, y, dx, dy))
                args = {**args, "px": [x, y]}
            elif name == "wait":
                secs = max(0.5, min(30.0, float(args.get("seconds") or 2)))
                self.enter(f"waiting {secs:.0f}s for the screen to change (wait tool)")
                await self.guarded(asyncio.sleep(secs))
            elif name == "shell" and self.allow_shell:
                rc, out, err = await self.guarded(w.shell(str(args.get("command", "")), 30))
                result = f"exit={rc}\nstdout:\n{out[-2000:]}\nstderr:\n{err[-1000:]}"
            else:
                result = f"error: unknown tool {name!r}"
        except (_Verdict, _Stopped, _Deadline):
            raise
        except Exception as e:  # noqa: BLE001  - surface tool errors to the model, keep going
            log.warning("tool %s failed (%s)", tool_name, type(e).__name__)
            result = f"error: {type(e).__name__}: {e}"
        self.actions_done += 1
        await self.send("agent.action", {"action": name, "args": args, "thought": thought})
        return result
