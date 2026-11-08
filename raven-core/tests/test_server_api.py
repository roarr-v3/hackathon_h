"""FastAPI endpoint tests with a stubbed engine (no sandboxes, no network)."""

from __future__ import annotations

import asyncio
import base64
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import raven_core.integrations as integrations
from raven_core.events import CreateAgentRequest, Event
from raven_core.server import Raven, create_app
from raven_core.server.config import Settings
from raven_core.store import Store

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

SPEC = """\
name: demo-interop
description: Demo spec for tests
vars:
  code: "{{random_word}}"
agents:
  - platform: linux
    role: receiver
    task: say hello {{code}}
  - platform: macos
    role: sender
    task: fail please {{code}}
timeout_s: 30
"""


class FakeWorker:
    def __init__(self, platform):
        self.name = f"fake-{platform.value}"


async def fake_plan_task(task, platform, **kw):
    return SimpleNamespace(task=task, plan=None, session_id=None)


async def fake_run_agent(agent, worker, emit, inbox, stop, artifacts_dir: Path):
    emit("agent.message", {"role": "agent", "text": f"hello from {worker.name}"})
    emit(Event(type="agent.action", data={"action": "click", "args": {"x": 1, "y": 2}, "thought": "click it"}))
    p = artifacts_dir / agent.id / "0.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(PNG)
    await emit("agent.screenshot", {"url": f"/artifacts/{agent.id}/0.png", "width": 1, "height": 1})
    if "wait" in agent.task:
        while not stop.is_set():
            try:
                msg = await asyncio.wait_for(inbox.get(), 0.05)
            except (TimeoutError, asyncio.TimeoutError):
                continue
            emit("agent.message", {"role": "agent", "text": f"got {msg}"})
            break
        if stop.is_set():
            return
    passed = "fail" not in agent.task
    emit("agent.verdict", {"passed": passed, "reason": "all good" if passed else "message never arrived"})


def make_raven(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str | None = None) -> Raven:
    monkeypatch.setenv("RAVEN_FAKE_WORKERS", "1")
    for k in ("RAVEN_TOKEN", "LINKUP_API_KEY", "DEVIN_API_KEY", "GLADIA_API_KEY", "RAVEN_AUTO_DEVIN"):
        monkeypatch.delenv(k, raising=False)
    # deterministic: use the server's built-in spec loader + fallback runner
    monkeypatch.setitem(sys.modules, "raven_core.specs", None)
    monkeypatch.setitem(sys.modules, "raven_core.runner", None)
    monkeypatch.setattr(integrations, "TRANSPORT", None)
    specs = tmp_path / "specs"
    specs.mkdir(exist_ok=True)
    (specs / "demo.yaml").write_text(SPEC)
    settings = Settings(token=token, artifacts_dir=tmp_path / "artifacts", specs_dir=specs)
    raven = Raven(settings=settings, store=Store())
    raven.get_worker = lambda platform, name=None: FakeWorker(platform)
    raven.run_agent = fake_run_agent
    raven.plan_task = fake_plan_task
    return raven


def wait_until(cond, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = cond()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def raven(tmp_path, monkeypatch):
    return make_raven(tmp_path, monkeypatch)


@pytest.fixture
def client(raven):
    with TestClient(create_app(raven)) as c:
        yield c


def agent_status(client, agent_id):
    return client.get(f"/agents/{agent_id}").json()["agent"]["status"]


def test_health(client):
    assert client.get("/health").json() == {"ok": True}


def test_specs(client):
    specs = client.get("/specs").json()
    assert specs == [
        {"name": "demo-interop", "description": "Demo spec for tests", "platforms": ["linux", "macos"]}
    ]


def test_create_agent_lifecycle_and_artifacts(client):
    r = client.post("/agents", json={"platform": "linux", "task": "say hi"})
    assert r.status_code == 200
    agent = r.json()
    assert agent["platform"] == "linux" and agent["worker"] == "fake-linux"
    assert agent["title"] == "say hi"
    wait_until(lambda: agent_status(client, agent["id"]) == "passed")
    detail = client.get(f"/agents/{agent['id']}").json()
    types = [e["type"] for e in detail["events"]]
    for t in ("agent.created", "agent.status", "agent.message", "agent.action", "agent.screenshot", "agent.verdict"):
        assert t in types
    assert detail["agent"]["verdict_reason"] == "all good"
    shot = detail["agent"]["last_screenshot"]
    assert shot == f"/artifacts/{agent['id']}/0.png"
    img = client.get(shot)
    assert img.status_code == 200 and img.content == PNG
    assert client.get("/artifacts/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.get("/artifacts/nope.png").status_code == 404
    assert [a["id"] for a in client.get("/agents").json()] == [agent["id"]]


def test_messages_steer_agent(client):
    agent = client.post("/agents", json={"platform": "linux", "task": "wait for a message"}).json()
    wait_until(lambda: agent_status(client, agent["id"]) == "running")
    r = client.post(f"/agents/{agent['id']}/messages", json={"text": "hi there"})
    assert r.status_code == 200 and r.json()["delivered"] is True
    wait_until(lambda: agent_status(client, agent["id"]) == "passed")
    texts = [e["data"].get("text") for e in client.get(f"/agents/{agent['id']}").json()["events"]]
    assert "hi there" in texts and "got hi there" in texts


def test_reply_to_finished_agent_reruns_it_with_the_earlier_task(client):
    first = client.post("/agents", json={"platform": "linux", "task": "fail please"}).json()
    wait_until(lambda: agent_status(client, first["id"]) == "failed")
    r = client.post(f"/agents/{first['id']}/messages", json={"text": "try the menu instead"}).json()
    assert r["delivered"] is True and r["agent_id"] not in (None, first["id"])
    follow = client.get(f"/agents/{r['agent_id']}").json()["agent"]
    assert follow["platform"] == "linux"
    assert follow["original_task"] == "try the menu instead\n\n(Earlier task in this conversation: fail please)"
    texts = [e["data"].get("text") or "" for e in client.get(f"/agents/{first['id']}").json()["events"]]
    assert "try the menu instead" in texts and any(r["agent_id"] in text for text in texts)
    r = client.post(f"/agents/{first['id']}/messages", json={"text": "open xcode and build it"}).json()
    assert client.get(f"/agents/{r['agent_id']}").json()["agent"]["platform"] == "macos"


def test_stop_agent(client):
    agent = client.post("/agents", json={"platform": "linux", "task": "wait forever"}).json()
    wait_until(lambda: agent_status(client, agent["id"]) == "running")
    assert client.post(f"/agents/{agent['id']}/stop").status_code == 200
    wait_until(lambda: agent_status(client, agent["id"]) == "stopped")


def test_agent_budget_and_stop_reason_reach_the_engine(client, raven):
    seen = {}

    async def run_agent(agent, worker, emit, inbox, stop, artifacts_dir, timeout_s=None):
        seen["timeout_s"] = timeout_s
        await stop.wait()
        emit("agent.verdict", {"passed": False, "reason": f"{stop.reason} while waiting for Holo request #1"})
        emit("agent.status", {"status": "stopped"})

    raven.run_agent = run_agent
    agent = client.post("/agents", json={"platform": "linux", "task": "wait", "timeout_s": 75}).json()
    wait_until(lambda: seen.get("timeout_s") == 75)
    # what the runner does on an attempt timeout (on the server's loop)
    raven.store.loop.call_soon_threadsafe(
        raven.stop_agent, agent["id"], "QA attempt exceeded 155s while waiting for computer use"
    )
    wait_until(lambda: agent_status(client, agent["id"]) == "stopped")
    detail = client.get(f"/agents/{agent['id']}").json()
    verdict = next(e for e in detail["events"] if e["type"] == "agent.verdict")
    assert verdict["data"]["reason"] == (
        "QA attempt exceeded 155s while waiting for computer use while waiting for Holo request #1"
    )

    # the API's own stop is the user's; the engine without a timeout_s kwarg still works (fake_run_agent)
    agent = client.post("/agents", json={"platform": "linux", "task": "wait", "timeout_s": 10}).json()
    wait_until(lambda: agent_status(client, agent["id"]) == "running")
    assert client.post(f"/agents/{agent['id']}/stop").status_code == 200
    wait_until(lambda: agent_status(client, agent["id"]) == "stopped")
    assert raven.store.handles[agent["id"]].stop.reason == "Stopped by user"
    assert client.post("/agents", json={"platform": "linux", "task": "x", "timeout_s": 0}).status_code == 422


def test_not_found(client):
    assert client.get("/agents/nope").status_code == 404
    assert client.post("/agents/nope/stop").status_code == 404
    assert client.post("/agents/nope/messages", json={"text": "x"}).status_code == 404
    assert client.get("/runs/nope").status_code == 404
    assert client.post("/runs", json={"spec": "does-not-exist"}).status_code == 404
    assert client.post("/agents", json={"platform": "amiga", "task": "x"}).status_code == 422


def test_run_passes(client):
    r = client.post("/runs", json={"spec": "demo-interop", "platforms": ["linux"]})
    assert r.status_code == 200
    run = r.json()
    assert run["spec"] == "demo-interop" and run["platforms"] == ["linux"]
    wait_until(lambda: client.get(f"/runs/{run['id']}").json()["status"] == "passed")
    detail = client.get(f"/runs/{run['id']}").json()
    assert len(detail["agents"]) == 1 and detail["agent_ids"] == [detail["agents"][0]["id"]]
    assert "{{" not in detail["agents"][0]["task"]  # vars rendered
    assert detail["report"] is None
    assert [x["id"] for x in client.get("/runs").json()] == [run["id"]]


def test_run_fails_and_reports_without_keys(client, raven):
    run = client.post("/runs", json={"spec": "demo-interop"}).json()
    assert run["platforms"] == ["linux", "macos"]
    wait_until(lambda: client.get(f"/runs/{run['id']}").json()["report"] is not None)
    detail = client.get(f"/runs/{run['id']}").json()
    assert detail["status"] == "failed"
    rep = detail["report"]
    assert "macos" in rep["markdown"] and "message never arrived" in rep["markdown"]
    assert rep["linkup_sources"] == [] and rep["devin_session_url"] is None
    assert any(e.type == "report.ready" for e in raven.store.bus.recent())
    notes = client.get(f"/artifacts/{run['id']}/failure.md")
    assert notes.status_code == 200
    assert "message never arrived" in notes.text
    assert "click" in notes.text


def test_run_with_custom_runner_awaiting_start_agent(client, raven):
    seen = {}

    async def stub_run_spec(spec, platforms, start_agent, run):
        a = await start_agent(CreateAgentRequest(platform=platforms[0], task="custom"), run.id)
        b = start_agent(CreateAgentRequest(platform=platforms[0], task="custom 2"), run.id)
        seen["ids"] = [a.id, b.id]

    raven.run_spec = stub_run_spec
    run = client.post("/runs", json={"spec": "demo-interop", "platforms": ["linux"]}).json()
    wait_until(lambda: client.get(f"/runs/{run['id']}").json()["status"] == "passed")
    assert client.get(f"/runs/{run['id']}").json()["agent_ids"] == seen["ids"]


def test_failure_report_with_linkup_and_devin(client, raven, monkeypatch):
    monkeypatch.setenv("LINKUP_API_KEY", "lk-test")
    monkeypatch.setenv("DEVIN_API_KEY", "dv-test")
    raven.settings.auto_devin = True
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "api.linkup.so":
            assert request.headers["authorization"] == "Bearer lk-test"
            return httpx.Response(
                200,
                json={
                    "answer": "Geohash channels need a Nostr relay.",
                    "sources": [{"name": "bitchat docs", "url": "https://example.com/doc", "snippet": "relay"}],
                },
            )
        if request.url.host == "api.devin.ai":
            assert request.url.path == "/v1/sessions"
            assert request.headers["authorization"] == "Bearer dv-test"
            return httpx.Response(200, json={"session_id": "devin-123", "url": "https://app.devin.ai/sessions/123"})
        return httpx.Response(404)

    monkeypatch.setattr(integrations, "TRANSPORT", httpx.MockTransport(handler))
    run = client.post("/runs", json={"spec": "demo-interop"}).json()
    rep = wait_until(lambda: client.get(f"/runs/{run['id']}").json()["report"])
    assert rep["devin_session_url"] == "https://app.devin.ai/sessions/123"
    assert rep["linkup_sources"][0]["url"] == "https://example.com/doc"
    assert "Geohash channels need a Nostr relay." in rep["markdown"]
    assert {c.url.host for c in calls} == {"api.linkup.so", "api.devin.ai"}


def test_manual_report_endpoint(client):
    run = client.post("/runs", json={"spec": "demo-interop", "platforms": ["linux"]}).json()
    wait_until(lambda: client.get(f"/runs/{run['id']}").json()["status"] == "passed")
    r = client.post(f"/runs/{run['id']}/report")
    assert r.status_code == 200 and "demo-interop" in r.json()["markdown"]


def test_stop_run_and_stop_all(client, raven):
    async def waiting_run_spec(spec, platforms, start_agent, run):
        await start_agent(CreateAgentRequest(platform=platforms[0], task="wait forever"), run.id)

    raven.run_spec = waiting_run_spec
    run = client.post("/runs", json={"spec": "demo-interop", "platforms": ["linux"]}).json()
    assert client.post(f"/runs/{run['id']}/stop").status_code == 200
    wait_until(lambda: client.get(f"/runs/{run['id']}").json()["status"] == "stopped")
    agent = client.post("/agents", json={"platform": "linux", "task": "wait forever"}).json()
    wait_until(lambda: agent_status(client, agent["id"]) == "running")
    assert agent["id"] in client.post("/stop").json()["stopped"]
    wait_until(lambda: agent_status(client, agent["id"]) == "stopped")


def test_voice_command_intents(client):
    r = client.post("/voice/command", json={"text": "run demo interop on linux"}).json()
    assert r["action"] == "run" and r["run"]["platforms"] == ["linux"]
    r = client.post("/voice/command", json={"text": "on linux: wait for my message"}).json()
    assert r["action"] == "agent" and r["agent"]["platform"] == "linux"
    agent_id = r["agent"]["id"]
    wait_until(lambda: agent_status(client, agent_id) == "running")
    r = client.post("/voice/command", json={"text": "continue", "agent_id": agent_id}).json()
    assert r["action"] == "message" and r["delivered"] is True
    wait_until(lambda: agent_status(client, agent_id) == "passed")
    r = client.post("/voice/command", json={"text": "Stop all."}).json()
    assert r["action"] == "stop_all"
    r = client.post("/voice/command", json={"text": "blorp"}).json()
    assert r["action"] == "none"
    assert client.post("/voice/command", json={"text": "x", "agent_id": "nope"}).status_code == 404


def test_worker_failure_marks_agent_error(client, raven):
    def broken(platform, name=None):
        raise RuntimeError("no sandbox")

    raven.get_worker = broken
    agent = client.post("/agents", json={"platform": "windows", "task": "x"}).json()
    wait_until(lambda: agent_status(client, agent["id"]) == "error")


def test_auth(tmp_path, monkeypatch):
    raven = make_raven(tmp_path, monkeypatch, token="s3cret")
    with TestClient(create_app(raven)) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/agents").status_code == 401
        assert c.get("/agents", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/agents", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/agents?token=s3cret").status_code == 200
        with pytest.raises(Exception):
            with c.websocket_connect("/ws/events") as ws:
                ws.receive_text()
        with c.websocket_connect("/ws/events?token=s3cret"):
            pass
