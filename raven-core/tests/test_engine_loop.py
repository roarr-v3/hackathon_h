"""Engine tests: FakeWorker + scripted (mocked) Holo client. No network."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

from raven_core.engine import run_agent
from raven_core.engine.holo import build_tools, trim_to_last_n_images
from raven_core.events import Agent, AgentStatus, Event, Platform
from raven_core.workers import FakeWorker, png_size


def call(name: str, **args):
    return SimpleNamespace(id=f"call_{name}", type="function", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


class ScriptedClient:
    """Mimics AsyncOpenAI: client.chat.completions.create(**kw) -> response."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        item = self.script.pop(0) if self.script else [call("wait", seconds=0.5)]
        if isinstance(item, Exception):
            raise item
        content, calls = (item if isinstance(item, tuple) else (None, item))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))])


def make_case(tmp_path: Path, script, **kw):
    agent = Agent(platform=Platform.linux, worker="fake-linux", title="t", task="open a terminal and type echo hello", run_id="run_x")
    worker = FakeWorker(Platform.linux)
    events: list[Event] = []

    async def emit(e: Event) -> None:
        events.append(e)

    client = ScriptedClient(script)
    return agent, worker, events, emit, client


def test_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr("raven_core.engine.holo.SETTLE_S", 0)
    agent, worker, events, emit, client = make_case(
        tmp_path,
        [
            ("I see the desktop", [call("click", element="terminal icon", x=500, y=500)]),
            [call("type_text", text="echo hello", press_enter=True)],
            [call("key", keys="ctrl+l")],
            [call("scroll", x=100, y=900, direction="down", amount=2)],
            [call("done", passed=True, reason="hello printed")],
        ],
    )
    inbox: asyncio.Queue[str] = asyncio.Queue()
    asyncio.run(run_agent(agent, worker, emit, inbox, asyncio.Event(), tmp_path, client=client))

    assert agent.status == AgentStatus.passed
    assert agent.verdict_reason == "hello printed"
    assert worker.actions[0] == ("click", 640, 400, "left")
    assert ("type_text", "echo hello") in worker.actions
    assert ("key", "enter") in worker.actions
    assert ("key", "ctrl+l") in worker.actions
    assert ("scroll", 128, 720, 0, 2) in worker.actions

    types = [e.type for e in events]
    assert types[0] == "agent.status" and events[0].data == {"status": "running"}
    assert types[-2:] == ["agent.verdict", "agent.status"]
    assert events[-2].data == {"passed": True, "reason": "hello printed"}
    assert events[-1].data == {"status": "passed"}
    assert all(e.agent_id == agent.id and e.run_id == "run_x" for e in events)

    shots = [e for e in events if e.type == "agent.screenshot"]
    assert len(shots) == 5
    assert shots[0].data == {"url": f"/artifacts/{agent.id}/000.png", "width": 1280, "height": 800}
    assert (tmp_path / agent.id / "000.png").read_bytes()[:4] == b"\x89PNG"
    assert agent.last_screenshot == shots[-1].data["url"]
    actions = [e for e in events if e.type == "agent.action"]
    assert actions[0].data["action"] == "click" and actions[0].data["thought"] == "I see the desktop"

    # request shape per Holo docs
    req = client.requests[0]
    assert req["tool_choice"] == "required" and req["temperature"] == 0.8
    assert {t["function"]["name"] for t in req["tools"]} >= {"click", "double_click", "type_text", "key", "scroll", "wait", "done"}
    # image budget respected and tool results answered
    for r in client.requests:
        n_images = sum(
            1 for m in r["messages"] if isinstance(m.get("content"), list) for p in m["content"] if p.get("type") == "image_url"
        )
        assert 1 <= n_images <= 2
    last = client.requests[-1]["messages"]
    tool_ids = {m["tool_call_id"] for m in last if m["role"] == "tool"}
    call_ids = {c["id"] for m in last if m["role"] == "assistant" for c in m.get("tool_calls", [])}
    assert tool_ids == call_ids


def test_inbox_steering_and_failed_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr("raven_core.engine.holo.SETTLE_S", 0)
    agent, worker, events, emit, client = make_case(
        tmp_path, [[call("wait", seconds=0.5)], [call("done", passed=False, reason="no message arrived")]]
    )
    inbox: asyncio.Queue[str] = asyncio.Queue()
    inbox.put_nowait("join channel u09t instead")
    asyncio.run(run_agent(agent, worker, emit, inbox, asyncio.Event(), tmp_path, client=client))
    texts = json.dumps(client.requests[0]["messages"])
    assert "<operator>" in texts and "join channel u09t instead" in texts
    assert agent.status == AgentStatus.failed
    assert events[-2].data == {"passed": False, "reason": "no message arrived"}


def test_stop(tmp_path, monkeypatch):
    monkeypatch.setattr("raven_core.engine.holo.SETTLE_S", 0)
    agent, worker, events, emit, client = make_case(tmp_path, [[call("wait", seconds=30)]])

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(run_agent(agent, worker, emit, asyncio.Queue(), stop, tmp_path, client=client))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(main())
    assert agent.status == AgentStatus.stopped
    assert events[-1].data == {"status": "stopped"}
    assert events[-2].type == "agent.verdict" and events[-2].data["passed"] is False


def test_timeout_and_max_steps(tmp_path, monkeypatch):
    monkeypatch.setattr("raven_core.engine.holo.SETTLE_S", 0)
    agent, worker, events, emit, client = make_case(tmp_path, [[call("wait", seconds=30)]])
    asyncio.run(run_agent(agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path, client=client, timeout_s=0.5))
    assert agent.status == AgentStatus.failed and "Timed out" in agent.verdict_reason

    agent, worker, events, emit, client = make_case(tmp_path, [])  # default script: wait 0.5s forever
    asyncio.run(run_agent(agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path, client=client, max_steps=2))
    assert agent.status == AgentStatus.failed and "Step budget" in agent.verdict_reason


def test_model_error_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr("raven_core.engine.holo.SETTLE_S", 0)
    err = RuntimeError("boom")
    err.status_code = 400  # type: ignore[attr-defined]
    agent, worker, events, emit, client = make_case(tmp_path, [err])
    asyncio.run(run_agent(agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path, client=client))
    assert agent.status == AgentStatus.error
    assert events[-1].data == {"status": "error"}


def test_model_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("raven_core.engine.holo.SETTLE_S", 0)
    err = RuntimeError("model not found")
    err.status_code = 404  # type: ignore[attr-defined]
    agent, worker, events, emit, client = make_case(tmp_path, [err, [call("done", passed=True, reason="ok")]])
    asyncio.run(run_agent(agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path, client=client, model="holo4-27b"))
    assert agent.status == AgentStatus.passed
    assert client.requests[-1]["model"] == "holo3-1-35b-a3b"


def test_helpers():
    msgs = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "a"}}]},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "b"}}]},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "c"}}]},
    ]
    trim_to_last_n_images(msgs, 2)
    assert msgs[0]["content"][0]["type"] == "text"
    assert msgs[2]["content"][0]["type"] == "image_url"
    assert "shell" not in {t["function"]["name"] for t in build_tools(False)}
    assert "shell" in {t["function"]["name"] for t in build_tools(True)}
    png = asyncio.run(FakeWorker(Platform.macos).screenshot())
    assert png_size(png) == (1280, 800)
