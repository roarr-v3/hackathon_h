"""Raven as an ACP agent (Agent Client Protocol, v1, JSON-RPC over stdio).

Each ACP session is a conversation with Raven. A prompt is parsed into an intent
(run a spec / start one agent / stop / list...) and executed against raven-core;
Raven events are streamed back as `session/update` notifications:

    agent.message    -> agent_message_chunk
    agent.action     -> tool_call (in_progress)  "[linux] click (412, 88)"
    agent.screenshot -> tool_call_update (completed) with an image content block
    agent.verdict / run.status / report.ready -> agent_message_chunk
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import socket
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from raven_core.acp.backend import HttpBackend, LocalBackend, agent_terminal
from raven_core.acp.jsonrpc import Connection, MethodNotFound, RpcError
from raven_core.server.intents import HELP, parse_command, parse_platforms

log = logging.getLogger("raven.acp")

PROTOCOL_VERSION = 1
MODELS = [
    {"modelId": "holo4-27b", "name": "Holo4 27B", "description": "H Company Holo4 27B computer-use model"},
    {"modelId": "holo4-35b-a3b", "name": "Holo4 35B-A3B", "description": "H Company Holo4 35B MoE (3B active)"},
]
COMMANDS = [
    {"name": "run", "description": "Run a test suite on several OSes", "input": {"hint": "<spec> on linux,macos,windows"}},
    {"name": "stop", "description": "Stop all running Raven agents"},
    {"name": "specs", "description": "List test suites"},
    {"name": "status", "description": "List agents"},
    {"name": "help", "description": "How to talk to Raven"},
]
RUN_TERMINAL = {"passed", "failed", "stopped"}
MAX_IMG_W = int(os.environ.get("RAVEN_ACP_IMG_WIDTH") or 640)
REPORT_WAIT_S = float(os.environ.get("RAVEN_ACP_REPORT_WAIT_S") or 60)


def _default_model() -> str:
    m = os.environ.get("HAI_MODEL") or MODELS[0]["modelId"]
    return m if any(x["modelId"] == m for x in MODELS) else MODELS[0]["modelId"]


def shrink_image(data: bytes, max_w: int = MAX_IMG_W) -> tuple[str, str] | None:
    """Return (base64, mime) of a downscaled PNG; None if it can't be made small."""
    try:
        from PIL import Image  # optional dependency
    except ImportError:
        if len(data) <= 700_000:
            return base64.b64encode(data).decode(), "image/png"
        return None
    try:
        im = Image.open(io.BytesIO(data))
        if im.width > max_w:
            im = im.resize((max_w, max(1, round(im.height * max_w / im.width))))
        if im.mode not in ("RGB", "L", "P"):
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, format="PNG", optimize=True)
        return base64.b64encode(out.getvalue()).decode(), "image/png"
    except Exception:
        return None


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def action_title(tag: str, action: str, args: dict[str, Any]) -> tuple[str, str]:
    """-> (title, ACP tool kind)"""
    a = (action or "action").lower()
    x, y = args.get("x"), args.get("y")
    if x is not None and y is not None and a in ("click", "double_click", "right_click", "move", "left_click", "tap", "drag"):
        btn = args.get("button")
        extra = f" {btn}" if btn and btn != "left" else ""
        return f"{tag} {a}{extra} ({x}, {y})", "other"
    if a in ("type", "type_text", "write"):
        t = str(args.get("text", ""))
        return f'{tag} type "{t[:48]}{"…" if len(t) > 48 else ""}"', "edit"
    if a in ("key", "hotkey", "press", "keypress"):
        return f"{tag} key {args.get('combo') or args.get('key') or args.get('keys') or ''}".rstrip(), "other"
    if a == "scroll":
        return f"{tag} scroll ({args.get('dx', 0)}, {args.get('dy', 0)}) at ({x}, {y})", "other"
    if a in ("shell", "exec", "run", "command"):
        cmd = str(args.get("cmd") or args.get("command") or "")
        return f"{tag} $ {cmd[:70]}", "execute"
    if a in ("wait", "sleep"):
        return f"{tag} wait {args.get('seconds', args.get('s', ''))}".rstrip(), "think"
    if a in ("screenshot",):
        return f"{tag} screenshot", "read"
    compact = json.dumps(args, ensure_ascii=False)[:60] if args else ""
    return f"{tag} {a} {compact}".rstrip(), "other"


@dataclass
class Session:
    id: str
    cwd: str = ""
    model: str = field(default_factory=_default_model)
    cancel: asyncio.Event | None = None
    agent_ids: list[str] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)


class Renderer:
    """Turns Raven events into ACP session/update notifications for one prompt turn."""

    def __init__(self, acp: "RavenAcpAgent", session_id: str, follow_agent: str | None, follow_run: str | None):
        self.acp = acp
        self.sid = session_id
        self.follow_agent = follow_agent
        self.follow_run = follow_run
        self.agents: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, tuple[str, list[dict[str, Any]]]] = {}  # agent -> (toolCallId, content)
        self.seen: set[str] = set()
        self.done = False
        self.run_status: str | None = None
        self.report_seen = False
        self.verdicts: dict[str, dict[str, Any]] = {}

    def update(self, upd: dict[str, Any]) -> None:
        self.acp.update(self.sid, upd)

    def say(self, text: str) -> None:
        self.update({"sessionUpdate": "agent_message_chunk", "content": text_block(text)})

    def tag(self, agent_id: str | None) -> str:
        a = self.agents.get(agent_id or "")
        return f"[{a['platform']}]" if a else "[raven]"

    def plan(self) -> None:
        if not self.follow_run or not self.agents:
            return
        entries = []
        for a in self.agents.values():
            st = a.get("status", "queued")
            status = "completed" if agent_terminal(st) else ("in_progress" if st == "running" else "pending")
            mark = {"passed": " ✅", "failed": " ❌", "error": " ⚠️", "stopped": " ⏹"}.get(st, "")
            entries.append({"content": f"[{a['platform']}] {a['title']}{mark}", "priority": "medium", "status": status})
        self.update({"sessionUpdate": "plan", "entries": entries})

    def close_pending(self, agent_id: str, status: str = "completed") -> None:
        p = self.pending.pop(agent_id, None)
        if p:
            self.update({"sessionUpdate": "tool_call_update", "toolCallId": p[0], "status": status})

    def close_all(self, status: str = "completed") -> None:
        for aid in list(self.pending):
            self.close_pending(aid, status)

    async def handle(self, ev: dict[str, Any]) -> None:
        eid = ev.get("id")
        if eid in self.seen:
            return
        self.seen.add(eid)
        t = ev.get("type")
        d = ev.get("data") or {}
        aid = ev.get("agent_id")
        if t == "agent.created":
            a = d.get("agent") or {}
            self.agents[a.get("id") or aid] = {
                "platform": a.get("platform", "?"),
                "title": a.get("title", ""),
                "status": a.get("status", "queued"),
            }
            self.say(f"▶️ **{self.tag(aid)}** agent started on `{a.get('worker', '?')}` — {a.get('task', '').strip()[:300]}\n\n")
            self.plan()
        elif t == "agent.message":
            role = d.get("role")
            text = str(d.get("text") or "").strip()
            if not text or role == "user":
                return
            if role == "system":
                self.say(f"_{self.tag(aid)} {text}_\n\n")
            else:
                self.say(f"**{self.tag(aid)}** {text}\n\n")
        elif t == "agent.action":
            self.close_pending(aid or "")
            args = d.get("args") or {}
            title, kind = action_title(self.tag(aid), str(d.get("action") or ""), args if isinstance(args, dict) else {})
            content: list[dict[str, Any]] = []
            if d.get("thought"):
                content.append({"type": "content", "content": text_block(str(d["thought"]))})
            tcid = f"tc_{eid}"
            self.pending[aid or ""] = (tcid, content)
            upd: dict[str, Any] = {
                "sessionUpdate": "tool_call",
                "toolCallId": tcid,
                "title": title,
                "kind": kind,
                "status": "in_progress",
                "rawInput": {"action": d.get("action"), **(args if isinstance(args, dict) else {"args": args})},
            }
            if content:
                upd["content"] = content
            self.update(upd)
        elif t == "agent.screenshot":
            url = str(d.get("url") or "")
            img = None
            raw = await self.acp.backend_fetch(url) if url else None
            if raw:
                img = shrink_image(raw)
            block: dict[str, Any]
            if img:
                block = {"type": "content", "content": {"type": "image", "data": img[0], "mimeType": img[1]}}
            else:
                block = {"type": "content", "content": text_block(f"screenshot: {self.acp.public_url(url)}")}
            p = self.pending.pop(aid or "", None)
            if p:
                self.update(
                    {"sessionUpdate": "tool_call_update", "toolCallId": p[0], "status": "completed", "content": [*p[1], block]}
                )
            else:
                self.update(
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": f"tc_{eid}",
                        "title": f"{self.tag(aid)} screenshot",
                        "kind": "read",
                        "status": "completed",
                        "content": [block],
                    }
                )
        elif t == "agent.status":
            st = str(d.get("status") or "")
            if aid in self.agents:
                self.agents[aid]["status"] = st
            if agent_terminal(st):
                self.close_pending(aid or "", "failed" if st in ("error",) else "completed")
                if st in ("error", "stopped") and aid not in self.verdicts:
                    self.say(f"⚠️ **{self.tag(aid)}** agent {st}\n\n")
                if self.follow_agent and aid == self.follow_agent:
                    self.done = True
            self.plan()
        elif t == "agent.verdict":
            passed = bool(d.get("passed"))
            self.verdicts[aid or ""] = d
            self.say(f"{'✅' if passed else '❌'} **{self.tag(aid)} {'PASSED' if passed else 'FAILED'}** — {d.get('reason', '')}\n\n")
        elif t == "run.status":
            st = str(d.get("status") or "")
            if self.run_status:  # runner and server may both announce the final status
                return
            if self.follow_run and ev.get("run_id") == self.follow_run and st in RUN_TERMINAL:
                self.run_status = st
                self.close_all()
                n_pass = sum(1 for a in self.agents.values() if a.get("status") == "passed")
                icon = {"passed": "✅", "failed": "❌", "stopped": "⏹"}[st]
                self.say(f"{icon} **Run {st}** — {n_pass}/{len(self.agents)} platforms passed.\n\n")
                if st != "failed":
                    self.done = True
                else:
                    self.say("_Researching the failure (Linkup)…_\n\n")
        elif t == "report.ready":
            if self.follow_run and ev.get("run_id") != self.follow_run:
                return
            self.report_seen = True
            md = str(d.get("markdown") or "")
            self.say(md + "\n\n")
            if d.get("devin_session_url"):
                self.say(f"🛠️ Devin is on it: {d['devin_session_url']}\n\n")
            if self.run_status:
                self.done = True


class RavenAcpAgent:
    def __init__(self, write: Any, backend: Any | None = None) -> None:
        self.conn = Connection(write, self.handle)
        self.sessions: dict[str, Session] = {}
        self._backend = backend
        self._backend_lock = asyncio.Lock()
        self._server: Any = None
        self._server_task: asyncio.Task[Any] | None = None
        self._raven: Any = None
        self.client_info: dict[str, Any] = {}

    # ------------------------------------------------------------------ plumbing
    def update(self, session_id: str, upd: dict[str, Any]) -> None:
        self.conn.notify("session/update", {"sessionId": session_id, "update": upd})

    async def backend(self) -> Any:
        async with self._backend_lock:
            if self._backend is None:
                self._backend = await self._connect()
            return self._backend

    async def backend_fetch(self, url: str) -> bytes | None:
        b = await self.backend()
        return await b.fetch(url)

    def public_url(self, url: str) -> str:
        b = self._backend
        return b.public_url(url) if b is not None else url

    async def _connect(self) -> Any:
        url = os.environ.get("RAVEN_URL") or f"http://localhost:{os.environ.get('RAVEN_PORT') or 8787}"
        token = os.environ.get("RAVEN_TOKEN") or None
        try:
            async with httpx.AsyncClient(timeout=1.5) as c:
                r = await c.get(url.rstrip("/") + "/health")
                if r.status_code == 200 and r.json().get("ok"):
                    log.info("using raven-core at %s", url)
                    return HttpBackend(url, token)
        except Exception:
            pass
        log.info("raven-core not reachable at %s; starting in-process server", url)
        return await self._start_in_process(url)

    async def _start_in_process(self, url: str) -> Any:
        from raven_core.server import Raven, create_app

        raven = Raven()
        raven.store.bind_loop(asyncio.get_running_loop())
        self._raven = raven
        app = create_app(raven)
        parsed = urlparse(url)
        port = parsed.port or raven.settings.port
        host = os.environ.get("RAVEN_HOST") or "127.0.0.1"
        if os.environ.get("RAVEN_ACP_SERVE", "1") not in ("0", "false"):
            try:
                import uvicorn

                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host, port))
                sock.listen(128)
                sock.setblocking(False)
                config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on", log_config=None)
                self._server = uvicorn.Server(config)
                self._server_task = asyncio.get_running_loop().create_task(self._server.serve(sockets=[sock]))
                raven.settings.port = port
            except Exception as e:
                log.warning("could not start in-process HTTP server on :%s (%s); continuing headless", port, e)
        return LocalBackend(raven)

    async def close(self) -> None:
        self.conn.cancel_all()
        if self._server is not None:
            self._server.should_exit = True
            if self._server_task is not None:
                try:
                    await asyncio.wait_for(self._server_task, 5)
                except Exception:
                    pass
        if self._raven is not None:
            await self._raven.shutdown()
        if self._backend is not None:
            try:
                await self._backend.aclose()
            except Exception:
                pass

    # ------------------------------------------------------------------ dispatch
    async def handle(self, method: str, params: dict[str, Any], is_request: bool) -> Any:
        if method == "initialize":
            return self.initialize(params)
        if method == "authenticate":
            return {}
        if method == "session/new":
            return self.new_session(params)
        if method == "session/prompt":
            return await self.prompt(params)
        if method == "session/cancel":
            self.cancel(params)
            return None
        if method in ("session/set_model", "session/set_session_model"):
            s = self._session(params)
            s.model = str(params.get("modelId") or s.model)
            return {}
        if method == "session/set_mode":
            return {}
        if method == "session/set_config_option":
            s = self._session(params)
            if params.get("configId") == "model" and params.get("value"):
                s.model = str(params["value"])
            return {"configOptions": []}
        if method == "session/close":
            self.sessions.pop(str(params.get("sessionId")), None)
            return {}
        if is_request:
            raise MethodNotFound(method)
        return None

    def _session(self, params: dict[str, Any]) -> Session:
        sid = str(params.get("sessionId") or "")
        s = self.sessions.get(sid)
        if s is None:
            raise RpcError(-32602, f"unknown session {sid}")
        return s

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        self.client_info = params.get("clientInfo") or {}
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": False,
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
                "mcpCapabilities": {"http": False, "sse": False},
            },
            "agentInfo": {"name": "raven", "title": "Raven", "version": "0.1.0"},
            "authMethods": [],
            "_meta": {"modelState": self._models(_default_model())},
        }

    def _models(self, current: str) -> dict[str, Any]:
        return {"currentModelId": current, "availableModels": MODELS}

    def new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = f"raven-{uuid.uuid4().hex[:12]}"
        s = Session(id=sid, cwd=str(params.get("cwd") or ""))
        self.sessions[sid] = s
        loop = asyncio.get_running_loop()
        loop.call_later(
            0.05,
            lambda: self.update(sid, {"sessionUpdate": "available_commands_update", "availableCommands": COMMANDS}),
        )
        return {"sessionId": sid, "models": self._models(s.model)}

    def cancel(self, params: dict[str, Any]) -> None:
        s = self.sessions.get(str(params.get("sessionId") or ""))
        if s is not None and s.cancel is not None:
            s.cancel.set()

    # ------------------------------------------------------------------ prompt turn
    @staticmethod
    def prompt_text(blocks: list[Any]) -> str:
        parts = []
        for b in blocks or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
            elif b.get("type") == "resource":
                res = b.get("resource") or {}
                if res.get("text"):
                    parts.append(str(res["text"]))
        return "\n".join(p for p in parts if p).strip()

    async def prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        s = self._session(params)
        text = self.prompt_text(params.get("prompt") or [])
        s.cancel = asyncio.Event()
        try:
            reason = await self._turn(s, text)
        except Exception as e:
            log.exception("prompt failed")
            self.update(s.id, {"sessionUpdate": "agent_message_chunk", "content": text_block(f"⚠️ Raven error: {e}")})
            reason = "end_turn"
        finally:
            s.cancel = None
        return {"stopReason": reason}

    def _say(self, s: Session, text: str) -> None:
        self.update(s.id, {"sessionUpdate": "agent_message_chunk", "content": text_block(text)})

    async def _turn(self, s: Session, text: str) -> str:
        if not text:
            self._say(s, HELP)
            return "end_turn"
        low = text.strip().lstrip("/").lower()
        if low in ("help", "?", "commands"):
            self._say(s, HELP)
            return "end_turn"
        b = await self.backend()
        specs = await b.list_specs()
        names = [x["name"] for x in specs if x.get("name")]
        intent = parse_command(text, names)

        if intent.kind == "specs":
            lines = [f"- **{x['name']}** ({', '.join(x.get('platforms') or [])}) — {x.get('description', '')}" for x in specs]
            self._say(s, "**Test suites**\n" + ("\n".join(lines) or "_none in specs/_"))
            return "end_turn"
        if intent.kind == "list":
            agents = await b.list_agents()
            lines = [f"- `{a['id']}` [{a['platform']}] **{a['status']}** — {a['title']}" for a in agents[-20:]]
            self._say(s, "**Agents**\n" + ("\n".join(lines) or "_no agents yet_"))
            return "end_turn"
        if intent.kind == "stop_all":
            ids = await b.stop_all()
            self._say(s, f"⏹ Stopping {len(ids)} agent(s).")
            return "end_turn"
        if intent.kind == "run" and intent.spec:
            plats = [p.value for p in intent.platforms] if intent.platforms else None
            run = await b.create_run(intent.spec, plats, model=s.model)
            s.run_ids.append(run["id"])
            self._say(s, f"🧪 Running **{run['spec']}** on {', '.join(run['platforms'])} (`{run['id']}`)\n\n")
            return await self._follow(s, run_id=run["id"])

        if intent.kind == "agent" and intent.platform and intent.task:
            platform, task = intent.platform.value, intent.task
        else:
            plats = parse_platforms(text)
            platform = plats[0].value if len(plats) == 1 else (os.environ.get("RAVEN_DEFAULT_PLATFORM") or "linux")
            task = text
        agent = await b.create_agent(platform, task, model=s.model)
        s.agent_ids.append(agent["id"])
        return await self._follow(s, agent_id=agent["id"])

    async def _follow(self, s: Session, run_id: str | None = None, agent_id: str | None = None) -> str:
        b = await self.backend()
        r = Renderer(self, s.id, follow_agent=agent_id, follow_run=run_id)
        assert s.cancel is not None
        consume = asyncio.create_task(self._consume(b, r, run_id, agent_id))
        cancelled = asyncio.create_task(s.cancel.wait())
        try:
            done, _ = await asyncio.wait({consume, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if consume in done:
                consume.result()
                return "end_turn"
            # session/cancel: stop the agents, let final events flush briefly
            try:
                if run_id:
                    await b.stop_run(run_id)
                elif agent_id:
                    await b.stop_agent(agent_id)
            except Exception as e:
                log.warning("stop on cancel failed: %s", e)
            await asyncio.wait({consume}, timeout=3)
            r.close_all("failed")
            return "cancelled"
        finally:
            for t in (consume, cancelled):
                if not t.done():
                    t.cancel()

    async def _consume(self, b: Any, r: Renderer, run_id: str | None, agent_id: str | None) -> None:
        it = b.events(run_id=run_id, agent_id=agent_id).__aiter__()
        try:
            while not r.done:
                timeout = REPORT_WAIT_S if r.run_status == "failed" and not r.report_seen else None
                try:
                    ev = await asyncio.wait_for(it.__anext__(), timeout=timeout)
                except StopAsyncIteration:
                    return
                except (TimeoutError, asyncio.TimeoutError):
                    r.say("_No failure report arrived (Linkup/Devin keys missing or slow)._\n\n")
                    return
                await r.handle(ev)
        finally:
            aclose = getattr(it, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass


async def serve_stdio() -> None:
    """Entry point for `raven-core acp`: ACP over this process's stdin/stdout."""
    import sys

    from raven_core.acp.jsonrpc import stdin_line_reader

    # Keep stdout pristine for JSON-RPC: protocol gets a private dup of fd 1,
    # everything else (prints, child processes, uvicorn) goes to stderr.
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    out = os.fdopen(proto_fd, "wb")

    def write(data: bytes) -> None:
        out.write(data)
        out.flush()

    loop = asyncio.get_running_loop()
    agent = RavenAcpAgent(write)
    try:
        await agent.conn.serve(stdin_line_reader(loop))
    finally:
        await agent.close()
