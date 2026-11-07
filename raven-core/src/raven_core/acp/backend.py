"""How the ACP bridge talks to Raven: over HTTP/WS (`HttpBackend`) or directly
to an in-process `Raven` service (`LocalBackend`)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlencode

import httpx

from raven_core.events import CreateAgentRequest, CreateRunRequest, Platform
from raven_core.store import TERMINAL_AGENT


class HttpBackend:
    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=30)

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        r = await self.client.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except Exception:
                detail = r.text[:200]
            raise RuntimeError(f"raven-core {method} {path} -> {r.status_code}: {detail}")
        return r.json()

    async def list_specs(self) -> list[dict[str, Any]]:
        return await self._json("GET", "/specs")

    async def list_agents(self) -> list[dict[str, Any]]:
        return await self._json("GET", "/agents")

    async def create_run(self, spec: str, platforms: list[str] | None, model: str | None = None) -> dict[str, Any]:
        return await self._json("POST", "/runs", json={"spec": spec, "platforms": platforms})

    async def create_agent(self, platform: str, task: str, title: str | None = None, model: str | None = None) -> dict[str, Any]:
        return await self._json("POST", "/agents", json={"platform": platform, "task": task, "title": title})

    async def send_message(self, agent_id: str, text: str) -> None:
        await self._json("POST", f"/agents/{agent_id}/messages", json={"text": text})

    async def stop_agent(self, agent_id: str) -> None:
        await self._json("POST", f"/agents/{agent_id}/stop")

    async def stop_run(self, run_id: str) -> None:
        await self._json("POST", f"/runs/{run_id}/stop")

    async def stop_all(self) -> list[str]:
        return (await self._json("POST", "/stop")).get("stopped", [])

    async def fetch(self, url: str) -> bytes | None:
        try:
            r = await self.client.get(url)
            return r.content if r.status_code == 200 else None
        except Exception:
            return None

    def public_url(self, url: str) -> str:
        return self.base_url + url if url.startswith("/") else url

    async def events(self, run_id: str | None = None, agent_id: str | None = None) -> AsyncIterator[dict[str, Any]]:
        try:
            from websockets.asyncio.client import connect
        except ImportError:  # pragma: no cover - older websockets
            from websockets import connect  # type: ignore[no-redef]
        params = {k: v for k, v in (("run_id", run_id), ("agent_id", agent_id), ("token", self.token)) if v}
        ws_url = self.base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        url = f"{ws_url}/ws/events" + (f"?{urlencode(params)}" if params else "")
        async with connect(url, max_size=None) as ws:
            async for msg in ws:
                yield json.loads(msg)

    async def aclose(self) -> None:
        await self.client.aclose()


class LocalBackend:
    """Talks to a Raven service living in this process (same event loop)."""

    def __init__(self, raven: Any) -> None:
        self.raven = raven

    def _model(self, model: str | None) -> None:
        # CreateAgentRequest has no model field; the engine reads HAI_MODEL.
        if model:
            os.environ["HAI_MODEL"] = model

    async def list_specs(self) -> list[dict[str, Any]]:
        return self.raven.list_specs()

    async def list_agents(self) -> list[dict[str, Any]]:
        return [a.model_dump(mode="json") for a in self.raven.store.agents.values()]

    async def create_run(self, spec: str, platforms: list[str] | None, model: str | None = None) -> dict[str, Any]:
        self._model(model)
        plats = [Platform(p) for p in platforms] if platforms else None
        return self.raven.create_run(CreateRunRequest(spec=spec, platforms=plats)).model_dump(mode="json")

    async def create_agent(self, platform: str, task: str, title: str | None = None, model: str | None = None) -> dict[str, Any]:
        self._model(model)
        req = CreateAgentRequest(platform=Platform(platform), task=task, title=title)
        return self.raven.start_agent(req).model_dump(mode="json")

    async def send_message(self, agent_id: str, text: str) -> None:
        self.raven.send_message(agent_id, text)

    async def stop_agent(self, agent_id: str) -> None:
        self.raven.stop_agent(agent_id)

    async def stop_run(self, run_id: str) -> None:
        self.raven.stop_run(run_id)

    async def stop_all(self) -> list[str]:
        return self.raven.stop_all()

    async def fetch(self, url: str) -> bytes | None:
        if not url.startswith("/artifacts/"):
            return None
        root: Path = self.raven.settings.artifacts_dir.resolve()
        p = (root / url.removeprefix("/artifacts/")).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            return None
        return p.read_bytes()

    def public_url(self, url: str) -> str:
        port = self.raven.settings.port
        return f"http://localhost:{port}{url}" if url.startswith("/") else url

    async def events(self, run_id: str | None = None, agent_id: str | None = None) -> AsyncIterator[dict[str, Any]]:
        bus = self.raven.store.bus
        q = bus.subscribe()
        try:
            replay = bus.recent()
            seen = {e.id for e in replay}

            def want(e: Any) -> bool:
                return (not run_id or e.run_id == run_id) and (not agent_id or e.agent_id == agent_id)

            for e in replay:
                if want(e):
                    yield e.model_dump(mode="json")
            while True:
                e = await q.get()
                if e.id in seen:
                    continue
                if want(e):
                    yield e.model_dump(mode="json")
        finally:
            bus.unsubscribe(q)

    async def aclose(self) -> None:
        pass


def agent_terminal(status: str | None) -> bool:
    return status in {s.value for s in TERMINAL_AGENT}
