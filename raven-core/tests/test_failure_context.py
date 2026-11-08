from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from raven_core import integrations, planning
from raven_core.events import Agent, AgentStatus, Event, Platform, Run, RunStatus
from raven_core.integrations import devin, linkup, report


def agent(id="agent-1", **kwargs):
    return Agent(**{
        "id": id, "run_id": "run-1", "platform": Platform.linux, "worker": "test-worker",
        "task": "Observe the current message", "title": "Delivery", "status": AgentStatus.failed,
        "created_at": 1, "verdict_reason": "model fallback reason", **kwargs,
    })


def event(type, ts, data, *, agent_id="agent-1", run_id="run-1"):
    return Event(type=type, id=f"event-{ts}-{agent_id}", ts=ts, agent_id=agent_id, run_id=run_id, data=data)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def unexpected(request):
        raise AssertionError("Unexpected HTTP request in failure-context test")

    async def unexpected_spawn(*args, **kwargs):
        raise AssertionError("Unexpected subprocess in failure-context test")

    monkeypatch.setattr(integrations, "TRANSPORT", httpx.MockTransport(unexpected))
    monkeypatch.setattr(devin, "spawn", unexpected_spawn)
    for name in ("LINKUP_API_KEY", "DEVIN_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_context_scopes_events_to_agent_run_and_platform():
    current = agent()
    passed = agent("passed", status=AgentStatus.passed)
    other_platform = agent("mac", platform=Platform.macos)
    events = [
        event("agent.verdict", 4, {"passed": False, "reason": "Relevant failure", "category": "blocked"}),
        event("agent.verdict", 7, {"passed": False, "reason": "Unrelated agent"}, agent_id="stranger"),
        event("agent.verdict", 8, {"passed": False, "reason": "Wrong run"}, run_id="other-run"),
        event("agent.verdict", 9, {"passed": False, "reason": "Wrong platform", "platform": "windows"}),
        event("agent.message", 10, {"role": "agent", "text": "Unrelated conversation"}, agent_id="stranger"),
    ]
    context = planning.build_failure_context([current, passed, other_platform], events, platform=Platform.linux)
    assert context["version"] == 1 and len(context["failures"]) == 1
    failure = context["failures"][0]
    assert failure["agent_id"] == current.id and failure["run_id"] == "run-1"
    assert failure["platform"] == "linux" and failure["task"] == current.task
    assert failure["verdict"]["reason"] == "Relevant failure"
    assert failure["verdict"]["category"] == "blocked"
    serialized = json.dumps(context)
    for unrelated in ("Unrelated", "Wrong run", "Wrong platform", "model fallback reason"):
        assert unrelated not in serialized


def test_last_verified_checkpoint_is_not_the_latest_action_or_screenshot():
    events = [
        event("agent.screenshot", 1, {"url": "/artifacts/agent-1/starting.png"}),
        event("agent.message", 2, {
            "role": "agent", "checkpoint": {
                "verified": True, "state": "Message pane and channel header visible",
                "evidence": "Channel header is on screen",
            },
        }),
        event("agent.action", 3, {"action": "click", "args": {"x": 10, "y": 20}, "result": "ok"}),
        event("agent.screenshot", 4, {"url": "/artifacts/agent-1/unchanged.png"}),
        event("agent.action", 5, {
            "action": "key", "args": {"keys": "enter"}, "success": True,
            "thought": "I think the message is verified",
        }),
        event("agent.action", 6, {"action": "click", "args": {}, "error": "No active window"}),
        event("agent.verdict", 7, {"passed": False, "reason": "VERDICT: BLOCKED\nNo focused window"}),
    ]
    failure = report.build_failure_context([agent(status=AgentStatus.error)], list(reversed(events)))["failures"][0]
    checkpoint = failure["last_verified_checkpoint"]
    assert checkpoint["state"] == "Message pane and channel header visible"
    assert checkpoint["event_id"] == "event-2-agent-1"
    assert checkpoint["screenshot"] == "/artifacts/agent-1/starting.png"
    assert failure["recent_actions"][0]["outcome"] == "ok"
    assert failure["recent_actions"][0]["screenshot_after"] == "/artifacts/agent-1/unchanged.png"
    assert failure["recent_actions"][-1]["outcome"] == {"status": "failed", "error": "No active window"}
    assert failure["verdict"]["category"] == "blocked"


def test_successful_tool_calls_and_user_claims_do_not_verify_state():
    events = [
        event("agent.action", 1, {
            "action": "click", "args": {}, "result": "ok", "success": True,
            "thought": "Verified state: I clicked the button",
        }),
        event("agent.message", 2, {"role": "user", "text": "Last verified state: logged in"}),
        event("agent.message", 3, {"role": "system", "checkpoint": {"verified": True, "state": "system claim"}}),
        event("agent.message", 4, {"role": "agent", "text": "I will verify the current screen"}),
        event("agent.screenshot", 5, {"url": "/artifacts/agent-1/uninspected.png"}),
    ]
    failure = report.build_failure_context([agent()], events)["failures"][0]
    assert failure["last_verified_checkpoint"] is None
    assert failure["recent_actions"][0]["outcome"] == "ok"


@pytest.mark.parametrize("data", [
    {"role": "agent", "text": "Last verified state: channel header visible"},
    {"role": "agent", "verified": True, "checkpoint": "channel header visible"},
    {"role": "agent", "last_verified_checkpoint": {"state": "channel header visible"}},
    {"role": "agent", "last_verified_state": "channel header visible"},
    {"role": "agent", "verification": {"passed": True, "state": "channel header visible"}},
])
def test_explicit_verified_checkpoint_shapes(data):
    checkpoint = report.build_failure_context(
        [agent()], [event("agent.message", 1, data)],
    )["failures"][0]["last_verified_checkpoint"]
    assert checkpoint["state"] == "channel header visible" and checkpoint["verified"] is True


def test_failed_verification_cannot_overwrite_a_verified_checkpoint():
    events = [
        event("agent.message", 1, {"role": "agent", "verified": True, "checkpoint": "Starting screen"}),
        event("agent.message", 2, {"role": "agent", "checkpoint": {"verified": False, "state": "Not reached"}}),
        event("agent.verdict", 3, {"passed": False, "last_verified_checkpoint": {"verified": False, "state": "Not reached"}}),
    ]
    failure = report.build_failure_context([agent()], events)["failures"][0]
    assert failure["last_verified_checkpoint"]["state"] == "Starting screen"


@pytest.mark.parametrize("state", ["none", "unknown", "unverified", "not verified", "no verified state"])
def test_unknown_checkpoint_claims_are_not_verified(state):
    failure = report.build_failure_context([agent()], [
        event("agent.verdict", 1, {"passed": False, "reason": f"Last verified state: {state}"}),
    ])["failures"][0]
    assert failure["last_verified_checkpoint"] is None


def test_retrospective_checkpoint_does_not_claim_latest_screenshot_as_proof():
    events = [
        event("agent.screenshot", 1, {"url": "/artifacts/agent-1/starting.png"}),
        event("agent.message", 2, {
            "role": "agent", "checkpoint": {"state": "Channel header visible", "verified": True},
        }),
        event("agent.action", 3, {"action": "click", "args": {}}),
        event("agent.screenshot", 4, {"url": "/artifacts/agent-1/failed.png"}),
        event("agent.verdict", 5, {
            "passed": False, "reason": "VERDICT: FAILED\nLast verified state: Channel header visible",
        }),
    ]
    checkpoint = report.build_failure_context([agent()], events)["failures"][0]["last_verified_checkpoint"]
    assert checkpoint["event_id"] == "event-2-agent-1"
    assert checkpoint["screenshot"] == "/artifacts/agent-1/starting.png"
    unknown_source = report.build_failure_context([agent()], [events[3], events[4]])["failures"][0]
    assert unknown_source["last_verified_checkpoint"]["screenshot"] is None


def test_late_actions_cannot_change_a_finished_attempt():
    events = [
        event("agent.verdict", 1, {"passed": False, "reason": "Final failure"}),
        event("agent.action", 2, {"action": "unrelated-late-click", "args": {}}),
        event("agent.message", 3, {"role": "agent", "verified": True, "checkpoint": "Too late"}),
    ]
    failure = report.build_failure_context([agent()], events)["failures"][0]
    assert failure["recent_actions"] == [] and failure["last_verified_checkpoint"] is None


def test_recent_actions_unknown_outcomes_and_screenshots_are_bounded():
    events = [event("agent.message", 0, {"role": "agent", "verified": True, "checkpoint": "Early checkpoint"})]
    for number in range(10):
        events.append(event("agent.action", number * 2 + 1, {"action": "click", "args": {"x": number}}))
        events.append(event("agent.screenshot", number * 2 + 2, {"url": f"/artifacts/agent-1/{number}.png"}))
    failure = report.build_failure_context([agent()], events)["failures"][0]
    assert len(failure["recent_actions"]) == 6 and len(failure["screenshots"]) == 4
    assert failure["recent_actions"][0]["args"]["x"] == 4
    assert failure["recent_actions"][-1]["outcome"] == "unknown"
    assert failure["last_verified_checkpoint"]["state"] == "Early checkpoint"


def test_old_inputs_are_removed_even_when_the_typing_event_is_outside_recent_actions():
    events = [
        event("agent.action", 1, {"action": "type_text", "args": {"text": "old-random-1839"}, "result": "ok"}),
        event("agent.message", 2, {
            "role": "agent", "verified": True, "checkpoint": "Channel old-random-1839 visible",
        }),
    ]
    events.extend(event("agent.action", number, {"action": "click", "args": {}}) for number in range(3, 12))
    events.append(event("agent.verdict", 12, {"passed": False, "reason": "old-random-1839 not delivered"}))
    context = report.build_failure_context([agent()], events)
    assert "old-random-1839" not in json.dumps(context)
    assert "[prior-attempt input]" in json.dumps(context)
    run = Run(id="run-1", spec="example", platforms=[Platform.linux], status=RunStatus.failed)
    assert "old-random-1839" not in report.failure_notes(run, [agent()], events)

@pytest.mark.parametrize("status,reason,category", [
    (AgentStatus.error, "tool execution error", "execution"),
    (AgentStatus.error, "H timed out before answering", "timeout"),
    (AgentStatus.stopped, "Stopped by user", "cancelled"),
    (AgentStatus.failed, "Message did not arrive", "assertion"),
    (AgentStatus.failed, "VERDICT: FAILED\nCategory: BLOCKED\nApp was not available", "blocked"),
])
def test_category_without_rich_events_is_conservative(status, reason, category):
    failure = report.build_failure_context(
        [agent(status=status, verdict_reason=reason, last_screenshot="/artifacts/agent-1/last.png")], [],
    )["failures"][0]
    assert failure["verdict"]["category"] == category
    assert failure["screenshots"][0]["url"] == "/artifacts/agent-1/last.png"
    assert failure["last_verified_checkpoint"] is None


def test_passed_verdict_and_nonterminal_agents_are_not_stale_failures():
    events = [event("agent.verdict", 1, {"passed": True, "reason": "Fresh pass"})]
    agents = [
        agent(), agent("queued", status=AgentStatus.queued), agent("running", status=AgentStatus.running),
    ]
    assert report.build_failure_context(agents, events) == {"version": 1, "failures": []}


def test_attempts_across_runs_remain_separate_and_planning_uses_only_the_latest_matching_task(monkeypatch):
    old = agent("old", run_id="old-run", created_at=1)
    current = agent("current", run_id="current-run", created_at=2)
    unrelated = agent("unrelated", task="Entirely different task", run_id="unrelated-run", created_at=99)
    mac = agent("mac", platform=Platform.macos, created_at=100)
    events = [
        event("agent.verdict", 1, {"passed": False, "reason": "Older failure"}, agent_id=old.id, run_id=old.run_id),
        event("agent.verdict", 2, {"passed": False, "reason": "Latest relevant failure"}, agent_id=current.id, run_id=current.run_id),
        event("agent.verdict", 3, {"passed": False, "reason": "Unrelated failure"}, agent_id=unrelated.id, run_id=unrelated.run_id),
    ]
    context = report.build_failure_context([old, current, unrelated, mac], events)
    assert {failure["run_id"] for failure in context["failures"]} == {"old-run", "current-run", "unrelated-run", "run-1"}
    selected = planning._failure_for_task(current.task, current.platform, context)
    assert selected["source"]["agent_id"] == current.id
    assert selected["verdict"]["reason"] == "Latest relevant failure"
    assert "Unrelated failure" not in json.dumps(selected) and "Older failure" not in json.dumps(selected)
    assert planning._failure_for_task("Unseen task", Platform.linux, context) is None


def test_original_task_binding_survives_rendered_specs_without_reusing_old_inputs():
    original = "Verify message receipt"
    failed = agent(task="Rendered QA spec using old-random-9432")
    events = [
        event("agent.action", 1, {"action": "type_text", "args": {"text": "old-random-9432"}, "outcome": "ok"}),
        event("agent.message", 2, {
            "role": "agent", "checkpoint": {"verified": True, "state": "Channel old-random-9432 visible"},
        }),
        event("agent.verdict", 3, {"passed": False, "reason": "Message old-random-9432 did not arrive"}),
    ]
    context = report.build_failure_context([failed], events)
    context.update({
        "task": original, "platform": "linux",
        "task_id": planning.task_identity(original, Platform.linux),
        "vars": {"channel": "stale-other-random-9999"},
    })
    reusable = planning._failure_for_task(original, Platform.linux, context)
    serialized = json.dumps(reusable)
    assert "old-random-9432" not in serialized and "stale-other-random-9999" not in serialized
    assert "Rendered QA spec" not in serialized
    assert "[prior-attempt input]" in serialized and reusable["historical_evidence_only"] is True
    assert reusable["last_verified_checkpoint"]["verified"] is True
    assert planning._failure_for_task("Different task", Platform.linux, context) is None
    assert planning._failure_for_task(original, Platform.macos, context) is None


def test_agent_original_task_is_used_as_identity_not_the_generated_spec():
    failed = agent(task="Setup: generated plan using old data", original_task="Check delivery")
    context = report.build_failure_context([failed], [])
    assert context["failures"][0]["task"] == "Check delivery"
    assert planning._failure_for_task("Check delivery", Platform.linux, context) is not None
    assert planning._failure_for_task(failed.task, Platform.linux, context) is None


def test_malformed_or_wrong_identity_is_not_reused():
    context = report.build_failure_context([agent()], [])
    assert planning._failure_for_task(agent().task, Platform.linux, {**context, "task": 12}) is None
    assert planning._failure_for_task(agent().task, Platform.linux, {**context, "task_id": "wrong-identity"}) is None

def test_task_identity_is_stable_and_platform_specific():
    assert planning.task_identity(" Task ", Platform.linux) == planning.task_identity("Task", Platform.linux)
    assert planning.task_identity("Task", Platform.linux) != planning.task_identity("Task", Platform.macos)
    assert planning.task_identity("Other", Platform.linux) != planning.task_identity("Task", Platform.linux)


def test_failure_notes_and_report_derive_from_the_same_attempt_data(monkeypatch):
    run = Run(id="run-1", spec="example", platforms=[Platform.linux], status=RunStatus.failed)
    failed = agent()
    foreign = agent("foreign", run_id="run-foreign", verdict_reason="Private unrelated failure")
    events = [
        event("agent.message", 1, {"role": "system", "text": "Starting QA attempt 1/2"}, agent_id=None),
        event("agent.message", 2, {"role": "agent", "verified": True, "checkpoint": "Channel is open"}),
        event("agent.action", 3, {"action": "click", "args": {"x": 4}, "outcome": "unchanged screen"}),
        event("agent.screenshot", 4, {"url": "/artifacts/agent-1/last.png"}),
        event("agent.verdict", 5, {"passed": False, "reason": "Delivery not observed", "category": "assertion"}),
        event("agent.message", 6, {"role": "system", "text": "Unrelated run timeline"}, agent_id=None, run_id="run-foreign"),
    ]

    async def search(query):
        return None

    monkeypatch.setattr(linkup, "search", search)
    payload = asyncio.run(report.build_failure_report(run, [failed, foreign], {failed.id: events}))
    context = report.build_failure_context([failed], events)
    assert payload["failure_context"] == context
    notes = report.failure_notes(run, [failed, foreign], events)
    for text in (
        "Channel is open", "Delivery not observed", "assertion", "unchanged screen",
        "/artifacts/agent-1/last.png", "agent-1",
    ):
        assert text in notes and text in payload["markdown"]
    assert "Starting QA attempt 1/2" in notes
    assert "Before rerunning" in notes and "Check the last screenshot" in notes
    for text in ("Unrelated run timeline", "Private unrelated failure"):
        assert text not in notes and text not in payload["markdown"]


def test_no_evidence_is_not_presented_as_a_verified_state():
    run = Run(id="run-1", spec="example", platforms=[Platform.linux], status=RunStatus.failed)
    notes = report.failure_notes(run, [agent(status=AgentStatus.error)], [])
    assert "Last verified checkpoint:** none recorded" in notes
    assert "model fallback reason" in notes


def test_linkup_receives_only_public_category_context_not_code_or_task(monkeypatch):
    monkeypatch.setenv("DEVIN_API_KEY", "secret-api-key")
    private_reason = "PrivateSource.swift: let client = secret-api-key; token=hidden-value"
    failed = agent(task="Private repository task", verdict_reason=private_reason)
    events = [
        event("agent.verdict", 1, {"passed": False, "reason": private_reason, "category": "assertion"}),
        event("agent.action", 2, {"action": "type_text", "args": {"password": "hidden-password"}, "result": "ok"}),
    ]
    queries = []

    async def search(query):
        queries.append(query)
        return {"query": query, "answer": "Public troubleshooting guidance", "sources": []}

    monkeypatch.setattr(linkup, "search", search)
    run = Run(id="run-1", spec="example", platforms=[Platform.linux], status=RunStatus.failed)
    payload = asyncio.run(report.build_failure_report(run, [failed], {failed.id: events}))
    assert queries == ["linux GUI testing visible assertion failure reproduction"]
    for private in ("PrivateSource.swift", "secret-api-key", "hidden-value", "Private repository task"):
        assert private not in queries[0]
    assert "secret-api-key" not in payload["markdown"] and "hidden-password" not in payload["markdown"]
    assert "hidden-value" not in payload["markdown"]
    assert "Public troubleshooting guidance" in payload["markdown"]


def test_setup_failure_timeline_survives_without_agents():
    run = Run(id="run-1", spec="example", platforms=[Platform.linux], status=RunStatus.failed)
    notes = report.failure_notes(run, [], [
        event("agent.message", 1, {"role": "system", "text": "Linux setup: worker unavailable"}, agent_id=None),
    ])
    assert "Linux setup: worker unavailable" in notes
