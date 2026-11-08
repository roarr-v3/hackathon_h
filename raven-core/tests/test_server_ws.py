"""WebSocket /ws/events: replay of recent events + live streaming + filters."""

from __future__ import annotations

import sys
import time
from types import SimpleNamespace

import anyio
from fastapi.testclient import TestClient

from raven_core.server import Raven, create_app
from raven_core.server.config import Settings
from raven_core.store import EventBus, Store
from raven_core.events import Event


class FakeWorker:
    name = "fake"


async def quick_agent(agent, worker, emit, inbox, stop, artifacts_dir):
    emit("agent.message", {"role": "agent", "text": "hi"})
    emit("agent.verdict", {"passed": True, "reason": "ok"})


def make_raven(tmp_path, monkeypatch) -> Raven:
    monkeypatch.delenv("RAVEN_TOKEN", raising=False)
    monkeypatch.setitem(sys.modules, "raven_core.specs", None)
    monkeypatch.setitem(sys.modules, "raven_core.runner", None)
    raven = Raven(
        settings=Settings(token=None, artifacts_dir=tmp_path / "a", specs_dir=tmp_path / "s"),
        store=Store(),
    )
    raven.get_worker = lambda platform, name=None: FakeWorker()
    raven.run_agent = quick_agent

    async def planner(task, platform, **kwargs):
        return SimpleNamespace(task=task, plan=None, session_id=None)

    raven.plan_task = planner
    return raven


def wait_until(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timeout")


def receive_json(ws):
    async def receive():
        with anyio.fail_after(5):
            return await anyio.to_thread.run_sync(ws.receive_json, abandon_on_cancel=True)

    return anyio.run(receive)


def test_ring_buffer_keeps_last_200():
    bus = EventBus()
    for i in range(250):
        bus.publish(Event(type="agent.message", data={"i": i}))
    recent = bus.recent()
    assert len(recent) == 200 and recent[0].data["i"] == 50 and recent[-1].data["i"] == 249


def test_ws_replay_and_live(tmp_path, monkeypatch):
    raven = make_raven(tmp_path, monkeypatch)
    with TestClient(create_app(raven)) as client:
        a1 = client.post("/agents", json={"platform": "linux", "task": "one"}).json()
        wait_until(lambda: client.get(f"/agents/{a1['id']}").json()["agent"]["status"] == "passed")
        n = len(raven.store.bus.recent())
        assert n >= 5

        with client.websocket_connect("/ws/events") as ws:
            replay = [receive_json(ws) for _ in range(n)]
            assert [e["id"] for e in replay] == [e.id for e in raven.store.bus.recent()[:n]]
            assert replay[0]["type"] == "agent.created"
            assert replay[0]["data"]["agent"]["id"] == a1["id"]

            a2 = client.post("/agents", json={"platform": "macos", "task": "two"}).json()
            live = receive_json(ws)
            assert live["type"] == "agent.created" and live["agent_id"] == a2["id"]

        wait_until(lambda: client.get(f"/agents/{a2['id']}").json()["agent"]["status"] == "passed")
        wait_until(lambda: any(
            event.agent_id == a2["id"] and event.type == "agent.status" and event.data.get("status") == "passed"
            for event in raven.store.bus.recent()
        ))
        expected = [event for event in raven.store.bus.recent() if event.agent_id == a2["id"]]
        with client.websocket_connect(f"/ws/events?agent_id={a2['id']}") as ws:
            replay = [receive_json(ws) for _ in expected]
            first = replay[0]
            assert first["agent_id"] == a2["id"] and first["type"] == "agent.created"
            assert [event["id"] for event in replay] == [event.id for event in expected]
            assert all(event["agent_id"] == a2["id"] for event in replay)
            assert any(event["type"] == "agent.verdict" for event in replay)
            assert replay[-1]["type"] == "agent.status" and replay[-1]["data"]["status"] == "passed"
