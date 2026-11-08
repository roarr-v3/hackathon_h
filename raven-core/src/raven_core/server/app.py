"""FastAPI app implementing the raven-core API (see AGENTS.md)."""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from raven_core.events import (
    Agent,
    CreateAgentRequest,
    CreateRunRequest,
    Event,
    Run,
    SendMessageRequest,
    VoiceSession,
)
from raven_core.server.pairing import is_local, pairing_urls
from raven_core.server.service import NotFound, Raven
from raven_core.server.voice import VoiceError, create_voice_session

log = logging.getLogger("raven.app")


class VoiceCommandRequest(BaseModel):
    text: str
    agent_id: str | None = None


def _token_ok(expected: str | None, header: str | None, query: str | None) -> bool:
    if not expected:
        return True
    if header and header.lower().startswith("bearer "):
        if secrets.compare_digest(header[7:].strip(), expected):
            return True
    return bool(query) and secrets.compare_digest(query, expected)


def create_app(raven: Raven | None = None) -> FastAPI:
    raven = raven or Raven()
    settings = raven.settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        raven.store.bind_loop(asyncio.get_running_loop())
        settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        if app.state.serve_background:
            raven.start_background()
        yield
        await raven.shutdown()

    app = FastAPI(title="raven-core", lifespan=lifespan)
    app.state.raven = raven
    app.state.serve_background = False  # `raven-core serve` turns this on (idle machine reaper)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    async def auth(request: Request) -> None:
        if is_local(request.client.host if request.client else None):
            return
        if not _token_ok(settings.token, request.headers.get("authorization"), request.query_params.get("token")):
            raise HTTPException(status_code=401, detail="unauthorized")

    def nf(fn, *a: Any) -> Any:
        try:
            return fn(*a)
        except NotFound as e:
            raise HTTPException(status_code=404, detail=f"not found: {e.args[0] if e.args else ''}")

    A = [Depends(auth)]

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/pairing")
    async def pairing(request: Request) -> dict[str, Any]:
        if not is_local(request.client.host if request.client else None):
            raise HTTPException(status_code=403, detail="pairing details are only served to this machine")
        return {"token": settings.token, "urls": await asyncio.to_thread(pairing_urls, settings.port)}

    # ---------------------------------------------------------------- agents
    @app.get("/agents", dependencies=A, response_model=list[Agent])
    async def list_agents() -> list[Agent]:
        return sorted(raven.store.agents.values(), key=lambda a: a.created_at)

    @app.post("/agents", dependencies=A, response_model=Agent)
    async def create_agent(req: CreateAgentRequest) -> Agent:
        return raven.start_agent(req)

    @app.get("/agents/{agent_id}", dependencies=A)
    async def get_agent(agent_id: str) -> dict[str, Any]:
        agent = nf(raven.get_agent, agent_id)
        events = raven.store.agent_events.get(agent_id, [])
        return {
            "agent": agent.model_dump(mode="json"),
            "events": [e.model_dump(mode="json") for e in events],
        }

    @app.post("/agents/{agent_id}/messages", dependencies=A)
    async def send_message(agent_id: str, req: SendMessageRequest) -> dict[str, Any]:
        delivered = nf(raven.send_message, agent_id, req.text)
        return {"ok": True, "delivered": delivered}

    @app.post("/agents/{agent_id}/stop", dependencies=A, response_model=Agent)
    async def stop_agent(agent_id: str) -> Agent:
        return nf(raven.stop_agent, agent_id)

    @app.post("/stop", dependencies=A)
    async def stop_everything() -> dict[str, Any]:
        return {"ok": True, "stopped": raven.stop_all()}

    # ---------------------------------------------------------------- runs
    @app.get("/runs", dependencies=A, response_model=list[Run])
    async def list_runs() -> list[Run]:
        return sorted(raven.store.runs.values(), key=lambda r: r.created_at)

    @app.post("/runs", dependencies=A, response_model=Run)
    async def create_run(req: CreateRunRequest) -> Run:
        return nf(raven.create_run, req)

    @app.get("/runs/{run_id}", dependencies=A)
    async def get_run(run_id: str) -> dict[str, Any]:
        """A `Run` plus `agents` and `report` (extra keys; decodes as Run)."""
        run = nf(raven.get_run, run_id)
        return {
            **run.model_dump(mode="json"),
            "agents": [a.model_dump(mode="json") for a in raven.store.agents_for_run(run_id)],
            "report": raven.store.reports.get(run_id),
        }

    @app.post("/runs/{run_id}/stop", dependencies=A, response_model=Run)
    async def stop_run(run_id: str) -> Run:
        return nf(raven.stop_run, run_id)

    @app.post("/runs/{run_id}/report", dependencies=A)
    async def make_report(run_id: str, devin: bool = False) -> dict[str, Any]:
        nf(raven.get_run, run_id)
        return await raven.report(run_id, devin=devin)

    # ---------------------------------------------------------------- specs
    @app.get("/specs", dependencies=A)
    async def list_specs() -> list[dict[str, Any]]:
        return raven.list_specs()

    # ---------------------------------------------------------------- voice
    @app.post("/voice/session", dependencies=A, response_model=VoiceSession)
    async def voice_session() -> VoiceSession:
        try:
            return await create_voice_session()
        except VoiceError as e:
            raise HTTPException(status_code=e.status, detail=str(e))

    @app.post("/voice/command", dependencies=A)
    async def voice_command(req: VoiceCommandRequest) -> dict[str, Any]:
        return nf(raven.command, req.text, req.agent_id)

    # ---------------------------------------------------------------- artifacts
    @app.get("/artifacts/{path:path}", dependencies=A)
    async def artifact(path: str) -> FileResponse:
        root = settings.artifacts_dir.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        return FileResponse(target)

    # ---------------------------------------------------------------- websocket
    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket) -> None:
        local = is_local(ws.client.host if ws.client else None)
        if not local and not _token_ok(settings.token, ws.headers.get("authorization"), ws.query_params.get("token")):
            await ws.close(code=1008)
            return
        run_f = ws.query_params.get("run_id")
        agent_f = ws.query_params.get("agent_id")

        def want(e: Event) -> bool:
            if run_f and e.run_id != run_f:
                return False
            if agent_f and e.agent_id != agent_f:
                return False
            return True

        await ws.accept()
        bus = raven.store.bus
        q = bus.subscribe()
        recv: asyncio.Task[Any] | None = None
        try:
            replay = bus.recent()
            seen = {e.id for e in replay}
            for e in replay:
                if want(e):
                    await ws.send_text(e.model_dump_json())

            async def drain() -> None:
                while True:
                    await ws.receive_text()

            recv = asyncio.create_task(drain())
            while True:
                getter = asyncio.create_task(q.get())
                done, _ = await asyncio.wait({getter, recv}, return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:
                    getter.cancel()
                    break
                e = getter.result()
                if e.id in seen:
                    seen.discard(e.id)
                    continue
                if want(e):
                    await ws.send_text(e.model_dump_json())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            bus.unsubscribe(q)
            if recv is not None:
                if recv.done() and not recv.cancelled():
                    recv.exception()  # mark retrieved
                recv.cancel()

    return app
