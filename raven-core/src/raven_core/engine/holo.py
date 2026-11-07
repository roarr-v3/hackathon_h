"""Holo computer-use agent loop (OpenAI-compatible function calling).

Follows https://hub.hcompany.ai/models-api/build-an-agent/{core-concepts,function-calling}:
- tools declared in `tools`, tool_choice="required", temperature 0.8
- each step the screenshot is a `user` message wrapped in <observation> tags
- only the last N screenshots stay in context (max 5 images/request; we keep 2)
- coordinates are integers in [0, 1000] normalized to the image, origin top-left
- each assistant tool_call gets a `role: tool` result with its tool_call_id
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from raven_core.events import Agent, AgentStatus, Event
from raven_core.workers.base import Worker, png_size

log = logging.getLogger(__name__)

DEFAULT_MODEL = "holo4-27b"
FALLBACK_MODEL = "holo3-1-35b-a3b"
DEFAULT_BASE_URL = "https://api.hcompany.ai/v1"
MAX_STEPS = 40
KEEP_IMAGES = 2
SETTLE_S = float(os.environ.get("RAVEN_SETTLE_S", "0.8"))

Emit = Callable[[Event], Awaitable[None]]

# ---------------------------------------------------------------- env


_env_loaded = False


def load_env() -> None:
    """Load repo-root .env into os.environ (no override, never logged)."""
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    candidates = [Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents]
    seen: set[Path] = set()
    for d in candidates:
        f = d / ".env"
        if f in seen:
            continue
        seen.add(f)
        if f.is_file():
            try:
                for line in f.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip().removeprefix("export ").strip()
                    v = v.strip().strip('"').strip("'")
                    if k and v and k not in os.environ:
                        os.environ[k] = v
            except OSError:
                pass
            return


def make_client() -> Any:
    """AsyncOpenAI client for the H Models API."""
    load_env()
    from openai import AsyncOpenAI

    key = os.environ.get("HAI_API_KEY")
    if not key:
        raise RuntimeError("HAI_API_KEY is not set (put it in the repo-root .env)")
    return AsyncOpenAI(
        base_url=os.environ.get("HAI_BASE_URL", DEFAULT_BASE_URL),
        api_key=key,
        timeout=float(os.environ.get("HAI_TIMEOUT_S", "120")),
        max_retries=2,
    )


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
        "If the app misbehaves, shows an error, or the goal is impossible after reasonable attempts, call `done` "
        "with passed=false and describe exactly what you saw. Do not give up early: messages over the network can "
        "take up to a minute to arrive."
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
) -> None:
    """Run one agent to a verdict. Never raises (except CancelledError, after emitting)."""
    artifacts_dir = Path(artifacts_dir)
    shot_dir = artifacts_dir / agent.id
    shot_dir.mkdir(parents=True, exist_ok=True)

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

    if allow_shell is None:
        allow_shell = os.environ.get("RAVEN_AGENT_SHELL", "").lower() in ("1", "true", "yes")
    deadline = time.monotonic() + timeout_s if timeout_s else None

    await set_status(AgentStatus.running)
    try:
        if client is None:
            client = make_client()
        model = model or os.environ.get("HAI_MODEL") or DEFAULT_MODEL
        loop = _Loop(agent, worker, send, inbox, stop, shot_dir, client, model, allow_shell, deadline, max_steps)
        await loop.run()
        await finish(AgentStatus.failed, False, f"Step budget exhausted ({max_steps} steps) without a verdict")
    except _Verdict as v:
        await finish(AgentStatus.passed if v.passed else AgentStatus.failed, v.passed, v.reason)
    except _Stopped as s:
        await finish(AgentStatus.stopped, False, s.reason)
    except asyncio.CancelledError:
        await finish(AgentStatus.stopped, False, "Agent task cancelled")
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("agent %s crashed", agent.id)
        await send("agent.message", {"role": "system", "text": f"Agent error: {type(e).__name__}: {e}"})
        await finish(AgentStatus.error, False, f"Agent error: {type(e).__name__}: {e}")


class _Stopped(Exception):
    def __init__(self, reason: str):
        self.reason = reason


class _Loop:
    def __init__(self, agent, worker, send, inbox, stop, shot_dir, client, model, allow_shell, deadline, max_steps):
        self.agent: Agent = agent
        self.worker: Worker = worker
        self.send = send
        self.inbox: asyncio.Queue[str] = inbox
        self.stop: asyncio.Event = stop
        self.shot_dir: Path = shot_dir
        self.client = client
        self.model: str = model
        self.allow_shell: bool = allow_shell
        self.deadline: float | None = deadline
        self.max_steps = max_steps
        self.tools = build_tools(allow_shell)
        self.messages: list[dict] = []
        self.screen = (0, 0)

    def check(self) -> None:
        if self.stop.is_set():
            raise _Stopped("Stopped by user")
        if self.deadline and time.monotonic() > self.deadline:
            raise _Verdict(False, "Timed out before reaching a verdict")

    async def guarded(self, aw: Awaitable[Any]) -> Any:
        """Await `aw` but abort promptly on stop / deadline."""
        task = asyncio.ensure_future(aw)
        stopper = asyncio.ensure_future(self.stop.wait())
        timeout = max(0.1, self.deadline - time.monotonic()) if self.deadline else None
        try:
            done, _ = await asyncio.wait({task, stopper}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()
            if not task.done():
                task.cancel()
        if task in done:
            return task.result()
        self.check()
        raise _Verdict(False, "Timed out before reaching a verdict")

    def drain_inbox(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except asyncio.QueueEmpty:
                return out

    async def run(self) -> None:
        self.screen = await self.guarded(self.worker.size())
        self.messages = [
            {"role": "system", "content": system_prompt(self.agent, self.screen, self.allow_shell)},
            {"role": "user", "content": f"Test task:\n{self.agent.task}"},
        ]
        for step in range(self.max_steps):
            self.check()
            for text in self.drain_inbox():
                self.messages.append({"role": "user", "content": f"<operator>\n{text}\n</operator>"})

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
            await asyncio.sleep(SETTLE_S)

    async def complete(self) -> Any:
        """One chat completion with model fallback + light retry. Returns the message object."""
        last: Exception | None = None
        for attempt in range(4):
            kwargs: dict[str, Any] = dict(
                model=self.model,
                messages=self.messages,
                tools=self.tools,
                tool_choice="required",
                temperature=0.8,
                max_tokens=int(os.environ.get("HAI_MAX_TOKENS", "4096")),
            )
            extra: dict[str, Any] = {}
            effort = os.environ.get("HAI_REASONING_EFFORT", "low" if self.model == "holo4-27b" else "")
            if effort:
                extra["reasoning_effort"] = effort
            if extra:
                kwargs["extra_body"] = extra
            try:
                resp = await self.client.chat.completions.create(**kwargs)
                return resp.choices[0].message
            except Exception as e:  # noqa: BLE001
                last = e
                status = getattr(e, "status_code", None)
                if status in (401,):
                    raise
                if status in (403, 404) and self.model != FALLBACK_MODEL:
                    log.warning("model %s unavailable (%s), falling back to %s", self.model, status, FALLBACK_MODEL)
                    await self.send(
                        "agent.message",
                        {"role": "system", "text": f"Model {self.model} unavailable, falling back to {FALLBACK_MODEL}"},
                    )
                    self.model = FALLBACK_MODEL
                    continue
                if status == 400 and "reasoning_effort" in str(e):
                    os.environ["HAI_REASONING_EFFORT"] = ""
                    continue
                if status is not None and 400 <= status < 500 and status != 429:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
        assert last is not None
        raise last

    def px(self, args: dict) -> tuple[int, int]:
        w, h = self.screen
        x = max(0, min(1000, int(round(float(args.get("x", 500))))))
        y = max(0, min(1000, int(round(float(args.get("y", 500))))))
        return min(w - 1, int(x / 1000 * w)), min(h - 1, int(y / 1000 * h))

    async def execute(self, name: str, args: dict, thought: str | None) -> str:
        w = self.worker
        result = "ok"
        if name == "done":
            passed = args.get("passed")
            if isinstance(passed, str):
                passed = passed.strip().lower() in ("true", "yes", "1", "pass", "passed")
            reason = str(args.get("reason") or args.get("_raw") or "")
            await self.send("agent.action", {"action": "done", "args": args, "thought": thought})
            raise _Verdict(bool(passed), reason or ("passed" if passed else "failed"))
        if name == "answer":  # Holo's native terminator, in case it slips through
            text = str(args.get("content", ""))
            await self.send("agent.action", {"action": "answer", "args": args, "thought": thought})
            low = text.lower()
            raise _Verdict(not any(k in low for k in ("fail", "not ", "unable", "error", "could not")), text)

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
                await self.guarded(asyncio.sleep(secs))
            elif name == "shell" and self.allow_shell:
                rc, out, err = await self.guarded(w.shell(str(args.get("command", "")), 30))
                result = f"exit={rc}\nstdout:\n{out[-2000:]}\nstderr:\n{err[-1000:]}"
            else:
                result = f"error: unknown tool {name!r}"
        except (_Verdict, _Stopped):
            raise
        except Exception as e:  # noqa: BLE001  - surface tool errors to the model, keep going
            log.warning("tool %s failed: %s", name, e)
            result = f"error: {type(e).__name__}: {e}"
        await self.send("agent.action", {"action": name, "args": args, "thought": thought})
        return result
