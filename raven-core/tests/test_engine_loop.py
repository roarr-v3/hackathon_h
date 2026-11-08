"""Engine tests: FakeWorker + scripted (mocked) Holo client. No network."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven_core.engine import run_agent
from raven_core.engine import holo
from raven_core.engine.holo import build_tools, trim_to_last_n_images
from raven_core.events import Agent, AgentStatus, Event, Platform
from raven_core.workers import FakeWorker, png_size


def call(name: str, **args):
    return SimpleNamespace(id=f"call_{name}", type="function", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


class Hang:
    """Script item: the Holo request never answers (sleeps `seconds`)."""

    def __init__(self, seconds: float = 60.0):
        self.seconds = seconds


class ScriptedClient:
    """Mimics AsyncOpenAI: client.chat.completions.create(**kw) -> response."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        item = self.script.pop(0) if self.script else [call("wait", seconds=0.5)]
        if isinstance(item, Hang):
            await asyncio.sleep(item.seconds)
            item = [call("wait", seconds=0.5)]
        if isinstance(item, Exception):
            raise item
        content, calls = (item if isinstance(item, tuple) else (None, item))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))])


def system_texts(events: list[Event]) -> list[str]:
    return [e.data["text"] for e in events if e.type == "agent.message" and e.data.get("role") == "system"]


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
    assert agent.status == AgentStatus.error and "Timed out" in agent.verdict_reason

    agent, worker, events, emit, client = make_case(tmp_path, [])  # default script: wait 0.5s forever
    asyncio.run(run_agent(agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path, client=client, max_steps=2))
    assert agent.status == AgentStatus.error and "Step budget" in agent.verdict_reason


def test_stop_reason_names_the_inflight_holo_request(tmp_path, monkeypatch):
    """The runner's stop (not a user's) is reported as such, with the request it interrupted."""
    monkeypatch.setattr("raven_core.engine.holo.SETTLE_S", 0)
    from raven_core.store import StopSignal

    agent, worker, events, emit, client = make_case(tmp_path, [Hang()])

    async def main():
        stop = StopSignal()
        task = asyncio.create_task(run_agent(agent, worker, emit, asyncio.Queue(), stop, tmp_path, client=client))
        await asyncio.sleep(0.3)
        stop.set("QA attempt exceeded 155s while waiting for computer use")
        await asyncio.wait_for(task, 5)

    asyncio.run(main())
    assert agent.status == AgentStatus.stopped
    assert agent.verdict_reason.startswith("QA attempt exceeded 155s while waiting for computer use while waiting for Holo request #1 ")
    assert "step 1, attempt 1/3" in agent.verdict_reason and "45s limit" in agent.verdict_reason
    assert "Stopped by user" not in agent.verdict_reason


def test_holo_request_timeout_is_logged_and_retried(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("raven_core.engine.holo.SETTLE_S", 0)
    monkeypatch.setattr("raven_core.engine.holo.REQUEST_GRACE_S", 0.0)
    monkeypatch.setattr("raven_core.engine.holo.RETRY_BACKOFF_S", 0.01)
    monkeypatch.setenv("HAI_SLOW_REQUEST_S", "0.1")
    agent, worker, events, emit, client = make_case(tmp_path, [Hang(), [call("done", passed=True, reason="ok")]])
    with caplog.at_level("INFO", logger="raven_core.engine.holo"):
        asyncio.run(
            run_agent(agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path, client=client, request_timeout_s=0.4)
        )
    assert agent.status == AgentStatus.passed
    assert len(client.requests) == 2  # the stuck request was abandoned and sent again
    # each request carries its own deadline, far below the agent budget
    t = client.requests[0]["timeout"]
    assert t.read == 0.4 and t.connect == 0.4
    texts = system_texts(events)
    assert texts[0].startswith("Holo loop on holo4-27b: 600s budget, 0.4s per request, up to 3 attempts per step")
    assert any(x.startswith("Holo request 1 still pending after ") for x in texts)
    assert any(x.startswith("Holo request 1 timed out after ") for x in texts)
    assert any(x.startswith("Holo request 1 retrying in 0.01 s (attempt 2/3)") for x in texts)
    assert any("after 2 attempts: done" in x for x in texts)
    assert [e["state"] for e in request_events(events)] == ["started", "pending", "failed", "retrying", "started", "succeeded"]
    log_lines = [r.getMessage() for r in caplog.records]
    assert any("Holo request 1 started: step 1, attempt 1/3, model=holo4-27b, images=1" in m for m in log_lines)
    assert any("model_request failed" in m and "timed out after" in m for m in log_lines)
    assert any("model_request retrying" in m for m in log_lines)
    assert any("Holo request 2 started: step 1, attempt 2/3" in m for m in log_lines)
    assert any("model_request succeeded" in m and "Holo answered request 2 in" in m for m in log_lines)


def test_holo_gives_up_after_the_configured_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr("raven_core.engine.holo.SETTLE_S", 0)
    monkeypatch.setattr("raven_core.engine.holo.REQUEST_GRACE_S", 0.0)
    monkeypatch.setattr("raven_core.engine.holo.RETRY_BACKOFF_S", 0.01)
    agent, worker, events, emit, client = make_case(tmp_path, [Hang(), Hang(), Hang()])
    asyncio.run(
        run_agent(
            agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path,
            client=client, request_timeout_s=0.2, request_attempts=2,
        )
    )
    assert agent.status == AgentStatus.error
    assert len(client.requests) == 2
    assert agent.verdict_reason.startswith("Holo did not answer: request #2 timed out after ")
    assert "(TimeoutError); gave up after 2 attempts in step 1." in agent.verdict_reason
    assert "2 Holo request(s) sent, 1 retried" in agent.verdict_reason
    assert any(e.type == "agent.screenshot" for e in events)  # the screenshot came before the stall


def test_request_deadline_never_outlives_the_agent_budget(tmp_path, monkeypatch):
    monkeypatch.setattr("raven_core.engine.holo.SETTLE_S", 0)
    monkeypatch.setattr("raven_core.engine.holo.MIN_REQUEST_S", 0.05)
    agent, worker, events, emit, client = make_case(tmp_path, [Hang()])
    asyncio.run(run_agent(agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path, client=client, timeout_s=0.5))
    assert agent.status == AgentStatus.error
    # clamped: 0.5s left of the budget beats the 45s per-request default
    assert client.requests[0]["timeout"].read <= 0.5
    reason = agent.verdict_reason
    assert reason.startswith("Timed out before reaching a verdict:")
    assert "too little for Holo request #2" in reason
    assert "verification and cleanup" in reason
    assert "1 Holo request(s) sent, 0 retried" in reason and "0 action(s) executed" in reason

    # when too little budget is left for another request, say so instead of sending one
    agent, worker, events, emit, client = make_case(tmp_path, [[call("wait", seconds=30)]])
    worker.width = worker.height = 1
    screenshot = worker.screenshot

    async def slow_screenshot():
        await asyncio.sleep(0.26)
        return await screenshot()

    worker.screenshot = slow_screenshot
    asyncio.run(run_agent(agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path, client=client, timeout_s=0.3))
    assert agent.status == AgentStatus.error
    assert client.requests == []
    assert "too little for Holo request #1 (step 1)" in agent.verdict_reason


def test_timeout_names_the_phase_outside_holo(tmp_path, monkeypatch):
    monkeypatch.setattr("raven_core.engine.holo.SETTLE_S", 0)
    monkeypatch.setattr("raven_core.engine.holo.MIN_REQUEST_S", 0.0)
    agent, worker, events, emit, client = make_case(tmp_path, [[call("wait", seconds=30)]])
    asyncio.run(run_agent(agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path, client=client, timeout_s=0.5))
    assert agent.status == AgentStatus.error
    assert "while waiting 30s for the screen to change (wait tool) (step 1," in agent.verdict_reason
    assert "1 Holo request(s) sent" in agent.verdict_reason


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


def request_events(events):
    return [e.data for e in events if e.type == "agent.message" and e.data.get("phase") == "model_request"]


def test_request_hard_deadline_reserves_headroom(tmp_path, monkeypatch):
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    monkeypatch.setattr(holo, "MIN_REQUEST_S", 0)
    agent, worker, events, emit, client = make_case(tmp_path, [Hang()])
    asyncio.run(
        run_agent(
            agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path,
            client=client, timeout_s=0.4, request_attempts=1,
        )
    )
    started = next(e for e in request_events(events) if e["state"] == "started")
    assert 0 < started["timeout_s"] < started["hard_timeout_s"] < started["remaining_s"]
    assert started["remaining_s"] - started["hard_timeout_s"] >= 0.039
    assert [e["state"] for e in request_events(events)] == ["started", "failed"]
    assert agent.status == AgentStatus.error


def test_tiny_budget_can_reach_a_real_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    agent, worker, events, emit, client = make_case(tmp_path, [[call("done", passed=True, reason="verified")]])
    asyncio.run(
        run_agent(
            agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path,
            client=client, timeout_s=0.3,
        )
    )
    assert agent.status == AgentStatus.passed
    assert agent.verdict_reason == "verified"


@pytest.mark.parametrize("status", [400, 404])
def test_configuration_retries_consume_attempts(tmp_path, monkeypatch, status):
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    monkeypatch.setenv("HAI_REASONING_EFFORT", "low")
    error = RuntimeError("unsupported reasoning_effort")
    error.status_code = status
    agent, worker, events, emit, client = make_case(tmp_path, [error, [call("done", passed=True, reason="unexpected")]])
    asyncio.run(
        run_agent(
            agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path,
            client=client, request_attempts=1,
        )
    )
    assert len(client.requests) == 1
    assert agent.status == AgentStatus.error
    assert os.environ["HAI_REASONING_EFFORT"] == "low"
    assert [e["state"] for e in request_events(events)] == ["started", "failed"]


def test_reasoning_configuration_fallback_is_local_and_single_use(tmp_path, monkeypatch):
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    monkeypatch.setattr(holo, "RETRY_BACKOFF_S", 0)
    monkeypatch.setenv("HAI_REASONING_EFFORT", "low")
    error = RuntimeError("unsupported reasoning_effort")
    error.status_code = 400
    agent, worker, events, emit, client = make_case(tmp_path, [error, error, [call("done", passed=True, reason="unexpected")]])
    asyncio.run(run_agent(agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path, client=client))
    assert agent.status == AgentStatus.error
    assert len(client.requests) == 2
    assert "extra_body" in client.requests[0] and "extra_body" not in client.requests[1]
    assert os.environ["HAI_REASONING_EFFORT"] == "low"
    assert [e["state"] for e in request_events(events)] == ["started", "failed", "retrying", "started", "failed"]


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "invalid"])
def test_invalid_deadline_configuration_stays_finite(tmp_path, monkeypatch, value):
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    for name in ("RAVEN_AGENT_TIMEOUT_S", "HAI_REQUEST_TIMEOUT_S", "HAI_REQUEST_ATTEMPTS", "HAI_SLOW_REQUEST_S"):
        monkeypatch.setenv(name, value)
    agent, worker, events, emit, client = make_case(tmp_path, [[call("done", passed=True, reason="ok")]])
    asyncio.run(run_agent(agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path, client=client))
    assert agent.status == AgentStatus.passed
    assert 0 < client.requests[0]["timeout"].read <= holo.REQUEST_TIMEOUT_S
    assert math.isfinite(client.requests[0]["timeout"].read)


def test_request_attempt_configuration_is_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("HAI_REQUEST_ATTEMPTS", "1000000")
    agent, worker, events, emit, client = make_case(tmp_path, [[call("done", passed=True, reason="ok")]])
    asyncio.run(run_agent(agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path, client=client))
    started = next(e for e in request_events(events) if e["state"] == "started")
    assert 1 <= started["max_attempts"] <= 10


@pytest.mark.parametrize("status", [401, 500])
def test_failed_request_telemetry_is_complete_and_redacted(tmp_path, monkeypatch, caplog, status):
    secret = "private-key-and-prompt-data:image/png;base64,not-for-logs"
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    error = RuntimeError(secret)
    error.status_code = status
    agent, worker, events, emit, client = make_case(tmp_path, [error])
    agent.task = secret
    with caplog.at_level("INFO", logger="raven_core.engine.holo"):
        asyncio.run(
            run_agent(
                agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path,
                client=client, request_attempts=1,
            )
        )
    telemetry = request_events(events)
    assert [e["state"] for e in telemetry] == ["started", "failed"]
    assert telemetry[-1]["elapsed_s"] >= 0
    assert telemetry[-1]["http_status"] == status
    assert secret not in caplog.text
    assert secret not in json.dumps(system_texts(events))
    assert secret not in agent.verdict_reason
    assert any("started" in r.getMessage() for r in caplog.records)
    assert any("failed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("cancel_task", [False, True])
def test_cancellation_drains_requests_and_notices_before_return(tmp_path, monkeypatch, caplog, cancel_task):
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    agent, worker, events, emit, client = make_case(tmp_path, [])

    async def main():
        started, notice_started = asyncio.Event(), asyncio.Event()
        cleaned, notice_cleaned = asyncio.Event(), asyncio.Event()

        async def request(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.02)
                cleaned.set()
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content=None, tool_calls=[call("click", x=10, y=10)],
                ))])

        async def notice(self, n, budget):
            notice_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.02)
                notice_cleaned.set()

        client.chat.completions.create = request
        monkeypatch.setattr(holo._Loop, "_slow_notice", notice)
        stop = asyncio.Event()
        task = asyncio.create_task(run_agent(agent, worker, emit, asyncio.Queue(), stop, tmp_path, client=client))
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(notice_started.wait(), 1)
        if cancel_task:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            stop.set()
            await asyncio.wait_for(task, 1)
        assert cleaned.is_set() and notice_cleaned.is_set()
        assert worker.actions == []
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        count = len(events)
        await asyncio.sleep(0.03)
        assert len(events) == count

    with caplog.at_level("INFO", logger="raven_core.engine.holo"):
        asyncio.run(main())
    assert [e["state"] for e in request_events(events)] == ["started", "cancelled"]
    assert any("cancelled" in r.getMessage() for r in caplog.records)
    assert events[-1].data == {"status": "stopped"}


def test_simultaneous_stop_and_response_never_executes_a_late_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    agent, worker, events, emit, client = make_case(tmp_path, [])

    async def main():
        stop = asyncio.Event()

        async def request(**kwargs):
            stop.set()
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=None, tool_calls=[call("done", passed=True, reason="too late")],
            ))])

        client.chat.completions.create = request
        await run_agent(agent, worker, emit, asyncio.Queue(), stop, tmp_path, client=client)

    asyncio.run(main())
    assert agent.status == AgentStatus.stopped
    assert not [e for e in events if e.type == "agent.action"]


def test_stop_between_batched_actions_prevents_late_done(tmp_path, monkeypatch):
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    agent, worker, events, emit, client = make_case(
        tmp_path, [[call("click", x=10, y=10), call("done", passed=True, reason="too late")]],
    )

    async def main():
        stop = asyncio.Event()

        async def stop_after_action(event):
            await emit(event)
            if event.type == "agent.action":
                stop.set()

        await run_agent(agent, worker, stop_after_action, asyncio.Queue(), stop, tmp_path, client=client)

    asyncio.run(main())
    assert agent.status == AgentStatus.stopped
    assert [e.data["action"] for e in events if e.type == "agent.action"] == ["click"]


def test_retry_backoff_cannot_consume_verification_headroom(tmp_path, monkeypatch):
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    monkeypatch.setattr(holo, "MIN_REQUEST_S", 0)
    monkeypatch.setattr(holo, "RETRY_BACKOFF_S", 30)
    agent, worker, events, emit, client = make_case(tmp_path, [TimeoutError(), [call("done", passed=True, reason="ok")]])
    asyncio.run(
        run_agent(
            agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path,
            client=client, timeout_s=0.3, request_attempts=2,
        )
    )
    assert len(client.requests) == 1
    assert agent.status == AgentStatus.error
    assert "retry" in agent.verdict_reason.lower()
    assert not [e for e in request_events(events) if e["state"] == "retrying"]


def test_sdk_retries_are_disabled_without_loading_secrets(monkeypatch):
    import openai

    captured = {}
    monkeypatch.setattr(holo, "load_env", lambda: None)
    monkeypatch.setenv("HAI_API_KEY", "fake-test-key")
    monkeypatch.setenv("HAI_TIMEOUT_S", "inf")
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kwargs: captured.update(kwargs))
    holo.make_client()
    assert captured["max_retries"] == 0
    assert captured["timeout"] == holo.REQUEST_TIMEOUT_S


def test_response_after_hard_timeout_is_discarded(tmp_path, monkeypatch):
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    monkeypatch.setattr(holo, "REQUEST_GRACE_S", 0)
    agent, worker, events, emit, client = make_case(tmp_path, [])

    async def request(**kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=None, tool_calls=[call("done", passed=True, reason="late response")],
            ))])

    client.chat.completions.create = request
    asyncio.run(run_agent(
        agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path,
        client=client, timeout_s=0.3, request_timeout_s=0.03, request_attempts=1,
    ))
    assert agent.status == AgentStatus.error
    assert "timed out" in agent.verdict_reason
    assert not [e for e in events if e.type == "agent.action"]
    assert [e["state"] for e in request_events(events)] == ["started", "failed"]


def test_transport_error_during_cancellation_does_not_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    monkeypatch.setattr(holo, "RETRY_BACKOFF_S", 0)
    agent, worker, events, emit, client = make_case(tmp_path, [])

    async def main():
        started = asyncio.Event()
        requests = 0

        async def request(**kwargs):
            nonlocal requests
            requests += 1
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("transport closed during cancellation") from None

        client.chat.completions.create = request
        task = asyncio.create_task(run_agent(
            agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path, client=client,
        ))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert requests == 1
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]

    asyncio.run(main())
    assert [e["state"] for e in request_events(events)] == ["started", "cancelled"]


def test_repeated_cancellation_still_drains_request_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    agent, worker, events, emit, client = make_case(tmp_path, [])

    async def main():
        started, cleaning, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def request(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await asyncio.sleep(0.03)
                cleaned.set()

        client.chat.completions.create = request
        task = asyncio.create_task(run_agent(
            agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path, client=client,
        ))
        await started.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]

    asyncio.run(main())
    assert events[-1].data == {"status": "stopped"}


def test_stop_during_retry_backoff_retains_cause_and_phase(tmp_path, monkeypatch):
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    agent, worker, events, emit, client = make_case(tmp_path, [TimeoutError()])

    async def main():
        stop = asyncio.Event()

        async def stop_on_retry(event):
            await emit(event)
            if event.data.get("state") == "retrying":
                stop.set()

        await run_agent(
            agent, worker, stop_on_retry, asyncio.Queue(), stop, tmp_path,
            client=client, get_stop_reason=lambda: "Runner backstop timed out",
        )
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]

    asyncio.run(main())
    assert len(client.requests) == 1
    assert "Runner backstop timed out" in agent.verdict_reason
    assert "retry backoff" in agent.verdict_reason
    assert "Stopped by user" not in agent.verdict_reason


def test_request_logs_do_not_echo_response_tool_names_or_bodies(tmp_path, monkeypatch, caplog):
    secret = "private-response-data:image/png;base64,hidden"
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    agent, worker, events, emit, client = make_case(tmp_path, [(secret, [call(secret)])])
    with caplog.at_level("INFO", logger="raven_core.engine.holo"):
        asyncio.run(run_agent(
            agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path,
            client=client, max_steps=1,
        ))
    assert secret not in caplog.text
    assert secret not in json.dumps(system_texts(events))
    assert any("unknown tool" in e["text"] for e in request_events(events))


@pytest.mark.parametrize(
    ("args", "status"),
    [
        ({"passed": False, "reason": "assertion failed"}, AgentStatus.failed),
        ({"passed": "true", "reason": "assertion verified"}, AgentStatus.passed),
        ({"passed": False, "blocked": True, "reason": "setup blocked"}, AgentStatus.error),
    ],
)
def test_done_assertion_and_blocker_semantics_are_preserved(tmp_path, monkeypatch, args, status):
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    agent, worker, events, emit, client = make_case(tmp_path, [[call("done", **args)]])
    asyncio.run(run_agent(agent, worker, emit, asyncio.Queue(), asyncio.Event(), tmp_path, client=client))
    assert agent.status == status
    assert agent.verdict_reason == args["reason"]
    assert events[-2].type == "agent.verdict"


def test_preexisting_stop_does_not_construct_a_client(tmp_path, monkeypatch):
    agent, worker, events, emit, client = make_case(tmp_path, [])
    constructed = []

    def unexpected_client():
        constructed.append(True)
        raise RuntimeError("client should not be needed")

    monkeypatch.setattr(holo, "make_client", unexpected_client)

    async def main():
        stop = asyncio.Event()
        stop.set()
        await run_agent(agent, worker, emit, asyncio.Queue(), stop, tmp_path)

    asyncio.run(main())
    assert constructed == []
    assert agent.status == AgentStatus.stopped
    assert agent.verdict_reason.startswith("Stopped by user")


def test_user_stop_cause_survives_task_cancellation(tmp_path, monkeypatch):
    monkeypatch.setattr(holo, "SETTLE_S", 0)
    agent, worker, events, emit, client = make_case(tmp_path, [Hang()])

    async def main():
        started, stop = asyncio.Event(), asyncio.Event()

        async def watch_request(event):
            await emit(event)
            if event.data.get("state") == "started":
                started.set()

        task = asyncio.create_task(run_agent(
            agent, worker, watch_request, asyncio.Queue(), stop, tmp_path, client=client,
        ))
        await started.wait()
        stop.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert agent.verdict_reason.startswith("Stopped by user")
    assert agent.status == AgentStatus.stopped


def test_cancellation_during_initial_status_still_emits_verdict(tmp_path):
    agent, worker, events, emit, client = make_case(tmp_path, [])

    async def main():
        started = asyncio.Event()

        async def block_initial_status(event):
            await emit(event)
            if event.type == "agent.status" and event.data["status"] == "running":
                started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(run_agent(
            agent, worker, block_initial_status, asyncio.Queue(), asyncio.Event(), tmp_path, client=client,
        ))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert agent.status == AgentStatus.stopped
    assert events[-2].type == "agent.verdict"
