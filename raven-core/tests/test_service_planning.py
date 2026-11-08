import asyncio
from types import SimpleNamespace

import pytest
from raven_core.events import AgentStatus, CreateAgentRequest, CreateRunRequest, Event, ExecutionProfile, Placement, Platform, Run, RunStatus, StopCause
from raven_core.server.config import Settings
from raven_core.server.service import Raven
from raven_core.store import Store
from raven_core.specs import Spec, SpecAgent
from raven_core.workers.fake import FakeWorker


def service(tmp_path):
    raven = Raven(Settings(token=None, artifacts_dir=tmp_path, specs_dir=tmp_path), Store())
    raven.get_worker = lambda platform, **kw: FakeWorker(platform)
    return raven


def test_free_form_is_planned_before_execution_and_profile_stays_frozen(tmp_path):
    async def scenario():
        raven = service(tmp_path)
        calls = []

        async def planner(task, platform, **kw):
            calls.append(("plan", task, kw["mode"], kw["repo_path"]))
            return SimpleNamespace(task="Setup: ready\nTask: planned\nVerification: visible", plan=None, session_id="planner-1")

        async def engine(agent, worker, emit, inbox, stop, artifacts_dir, **kw):
            calls.append(("execute", agent.task, kw["model"]))
            await emit(Event(type="agent.verdict", data={"passed": True, "reason": "verified"}))

        raven.plan_task = planner
        raven.run_agent = engine
        agent = raven.start_agent(CreateAgentRequest(
            platform=Platform.linux, task="original", repo_path=str(tmp_path),
            execution=ExecutionProfile(planner="cloud", model="holo4-35b-a3b"),
        ))
        raven.execution_profiles.save(ExecutionProfile(planner="local"))
        await raven.store.handles[agent.id].task
        assert calls == [
            ("plan", "original", "cloud", str(tmp_path)),
            ("execute", "Setup: ready\nTask: planned\nVerification: visible", "holo4-35b-a3b"),
        ]
        assert agent.original_task == "original"
        assert agent.planning_session_id == "planner-1"
        assert agent.status == AgentStatus.passed
        assert agent.execution.planner == "cloud"
        await raven.shutdown()

    asyncio.run(scenario())


def test_planning_failure_never_falls_back_to_unplanned_execution(tmp_path):
    async def scenario():
        raven = service(tmp_path)
        ran = []

        async def planner(*args, **kw):
            raise RuntimeError("planner unavailable")

        async def engine(*args, **kw):
            ran.append(True)

        raven.plan_task = planner
        raven.run_agent = engine
        agent = raven.start_agent(CreateAgentRequest(platform=Platform.linux, task="do something"))
        await raven.store.handles[agent.id].task
        assert not ran
        assert agent.status == AgentStatus.error
        assert "planning" in agent.verdict_reason.lower()
        await raven.shutdown()

    asyncio.run(scenario())


def test_named_suite_does_not_rewrite_tasks_with_devin(tmp_path):
    async def scenario():
        raven = service(tmp_path)
        seen = []

        async def planner(*args, **kw):
            raise AssertionError("named YAML must not be replanned")

        async def engine(agent, *args, **kw):
            seen.append(agent.task)
            agent.status = AgentStatus.passed

        raven.plan_task = planner
        raven.run_agent = engine
        run = Run(spec="named", platforms=[Platform.linux], execution=ExecutionProfile())
        raven.store.add_run(run)
        agent = raven.start_agent(CreateAgentRequest(platform=Platform.linux, task="original YAML assertion"), run.id)
        await raven.store.handles[agent.id].task
        assert seen == ["original YAML assertion"]
        await raven.shutdown()

    asyncio.run(scenario())


def test_stop_during_planning_cancels_planner_without_starting_h(tmp_path):
    async def scenario():
        raven = service(tmp_path)
        started, cancelled = asyncio.Event(), asyncio.Event()
        executed = []

        async def planner(*args, **kw):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async def engine(*args, **kw):
            executed.append(True)

        raven.plan_task = planner
        raven.run_agent = engine
        agent = raven.start_agent(CreateAgentRequest(platform=Platform.linux, task="plan this"))
        await asyncio.wait_for(started.wait(), 1)
        raven.stop_agent(agent.id)
        await asyncio.wait_for(raven.store.handles[agent.id].task, 2)
        assert cancelled.is_set()
        assert not executed
        assert agent.status == AgentStatus.stopped
        await raven.shutdown()

    asyncio.run(scenario())


def test_timeout_origin_stop_is_error_in_every_status_event(tmp_path):
    async def scenario():
        raven = service(tmp_path)
        started = asyncio.Event()

        async def planner(task, platform, **kw):
            return SimpleNamespace(task=task, plan=None, session_id=None)

        async def engine(agent, worker, emit, inbox, stop, artifacts_dir, **kw):
            started.set()
            await stop.wait()
            await emit(Event(type="agent.verdict", data={"passed": False, "reason": stop.reason}))
            await emit(Event(type="agent.status", data={"status": "stopped"}))

        raven.plan_task = planner
        raven.run_agent = engine
        agent = raven.start_agent(CreateAgentRequest(platform=Platform.linux, task="test timeout"))
        await started.wait()
        raven.stop_agent(agent.id, "QA attempt exceeded its deadline", cause=StopCause.deadline)
        await raven.store.handles[agent.id].task
        assert agent.status == AgentStatus.error
        assert all(
            event.data["status"] != "stopped"
            for event in raven.store.agent_events[agent.id] if event.type == "agent.status"
        )
        await raven.shutdown()

    asyncio.run(scenario())


def test_suite_setup_and_h_share_resolved_worker_until_run_cleanup(tmp_path):
    from functools import partial

    from raven_core.runner import run_spec
    from raven_core.specs import Spec, SpecAgent

    async def scenario():
        calls = []

        class Target:
            platform = Platform.linux
            bare_metal = False

            def __init__(self, name):
                self.name = name

            async def shell(self, command, timeout=60):
                calls.append(("setup", self.name, command))
                return 0, "", ""

            async def close(self):
                calls.append(("close", self.name))

        initial, resolved = Target("apple-space"), Target("devin-linux-session")
        spec = Spec(name="unchanged", agents=[SpecAgent(platform=Platform.linux, task="verify original assertion")],
                    setup={Platform.linux: ["sh:prepare app"]}, timeout_s=2)
        raven = service(tmp_path)
        raven.load_spec = lambda name: spec
        raven.get_worker = lambda *args, **kw: initial
        raven.run_spec = partial(run_spec, poll_s=0.01)

        async def prepare(worker, **kw):
            return resolved

        async def planner(*args, **kw):
            raise AssertionError("YAML must retain its predefined task")

        async def engine(agent, worker, emit, *args, **kw):
            assert worker is resolved
            assert agent.execution.linux_space == "selected-space"
            calls.append(("execute", worker.name))
            await emit(Event(type="agent.verdict", data={"passed": True, "reason": "visible assertion confirmed"}))

        raven.prepare_worker = prepare
        raven.plan_task = planner
        raven.run_agent = engine
        run = raven.create_run(CreateRunRequest(spec="unchanged", execution=ExecutionProfile(linux_space="selected-space")))
        await raven._run_tasks[run.id]
        assert run.status == "passed"
        assert calls.index(("setup", resolved.name, "prepare app")) < calls.index(("execute", resolved.name))
        assert calls.index(("execute", resolved.name)) < calls.index(("close", resolved.name))
        assert all(kind != "setup" or name == resolved.name for kind, name, *_ in calls)
        assert raven.store.agents_for_run(run.id)[0].worker == resolved.name
        await raven.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("preparations", [0, 1, 2])
def test_ios_binds_once_per_preparation_and_execution_under_screen_lock(tmp_path, monkeypatch, preparations):
    from raven_core import hosts, workers
    from raven_core.workers import device_hub

    async def scenario():
        raven = Raven(Settings(token=None, artifacts_dir=tmp_path, specs_dir=tmp_path), Store())
        profile = ExecutionProfile(ios_target="device", ios_device_id="selected-device")
        run = Run(spec="ios", platforms=[Platform.ios], execution=profile)
        raven.store.add_run(run)
        target = SimpleNamespace(platform=Platform.ios, name="device-hub", bare_metal=True, target="native")
        bindings, preflights = [], []

        async def bind(execution):
            assert raven._screen_lock.locked()
            assert execution == profile
            binding = {"id": execution.ios_device_id, "kind": execution.ios_target, "available": True}
            bindings.append(binding)
            return binding

        async def prepare():
            assert raven._screen_lock.locked()
            preflights.append(True)

        async def engine(agent, worker, emit, *args, **kw):
            assert raven._screen_lock.locked()
            assert worker is target and worker.device is bindings[-1]
            assert worker.device["id"] == "selected-device"
            await emit(Event(type="agent.verdict", data={"passed": True, "reason": "Exact target verified"}))

        target.prepare = prepare
        monkeypatch.setattr(workers, "sys", SimpleNamespace(platform="darwin"))
        monkeypatch.setattr(workers, "get_worker", lambda *args, **kw: target)
        monkeypatch.setattr(hosts, "h_environment", lambda: {})
        monkeypatch.setattr(device_hub, "prepare_device", bind)
        raven.run_agent = engine
        try:
            for index in range(preparations):
                target.device = {"id": "unrelated-foreground"}
                await raven._prepare_run_platform(run, Platform.ios, [], Placement.auto)
                assert len(bindings) == index + 1
                assert target.device is bindings[-1]
            async with raven._screen_lock:
                target.device = {"id": "unrelated-foreground"}
                agent = raven.start_agent(CreateAgentRequest(platform=Platform.ios, task="Check selected device"), run.id)
                await asyncio.sleep(0)
                assert len(bindings) == preparations
            await asyncio.wait_for(raven.store.handles[agent.id].task, 1)
            assert agent.status == AgentStatus.passed
            assert len(bindings) == preparations + 1
            assert preflights == [True]
            assert not raven._screen_lock.locked()
        finally:
            await raven.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("run_budget", [None, 90.0])
def test_setup_gets_remaining_preparation_and_run_budget(tmp_path, run_budget):
    async def scenario():
        raven = service(tmp_path)
        raven.settings.prepare_timeout_s = 180
        run = Run(spec="budget", platforms=[Platform.linux], execution=ExecutionProfile())
        target = SimpleNamespace(platform=Platform.linux, name="selected-target")
        budgets = []

        async def prepare(worker, **kw):
            await asyncio.sleep(0.02)
            return worker

        async def shell(command, timeout):
            assert raven._lock_for_worker(target).locked()
            budgets.append(timeout)
            await asyncio.sleep(0.01)
            return 0, "", ""

        target.shell = shell
        raven.get_worker = lambda *args, **kw: target
        raven.prepare_worker = prepare
        if run_budget is not None:
            raven._run_deadlines[run.id] = asyncio.get_running_loop().time() + run_budget
        try:
            result = await raven._prepare_run_platform(run, Platform.linux, ["sh:first", "sh:second"], Placement.auto)
            assert result is target
            limit = min(raven.settings.prepare_timeout_s, run_budget or float("inf"))
            assert 60 < budgets[1] < budgets[0] <= limit - 0.02
            assert not raven._lock_for_worker(target).locked()
        finally:
            await raven.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", ["preparation", "run"])
def test_setup_timeout_reports_setup_not_ready_desktop_or_h(tmp_path, limit):
    async def scenario():
        raven = service(tmp_path)
        raven.settings.prepare_timeout_s = 0.1 if limit == "preparation" else 180
        raven.settings.run_timeout_s = 5 if limit == "preparation" else 0.1
        raven.settings.reports = False
        target = SimpleNamespace(platform=Platform.linux, name="ready-target", preparation_phase="desktop ready")
        cancelled = asyncio.Event()
        spec = Spec(name="setup", agents=[SpecAgent(platform=Platform.linux, task="Must not execute")],
                    setup={Platform.linux: ["sh:blocked setup"]})

        async def shell(command, timeout):
            try:
                await asyncio.Future()  # A driver ignoring its own timeout must still be bounded.
            finally:
                cancelled.set()

        async def engine(*args, **kw):
            pytest.fail("H must not start when application setup times out")

        target.shell = shell
        raven.get_worker = lambda *args, **kw: target
        raven.run_agent = engine
        raven.load_spec = lambda _: spec
        try:
            run = raven.create_run(CreateRunRequest(spec=spec.name))
            await asyncio.wait_for(raven._run_tasks[run.id], 2)
            assert run.status == RunStatus.failed and cancelled.is_set()
            assert not raven.store.agents_for_run(run.id)
            messages = [event.data for event in raven.store.run_events[run.id] if event.type == "agent.message"]
            assert any(message.get("phase") == "setup" for message in messages)
            reason = next(message["text"] for message in messages if "timed out" in message.get("text", ""))
            assert "before H computer use" in reason
            if limit == "preparation":
                assert "setup command 1/1" in reason
            else:
                assert "setup command 1/1" in reason or "running application setup" in reason
                assert "run deadline" in reason
            assert "desktop ready" not in reason
            assert all("waiting for computer use" not in message.get("text", "") for message in messages)
            assert not raven._lock_for_worker(target).locked()
        finally:
            await raven.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("allow", [True, False])
def test_same_session_repair_waits_for_approval_and_reuses_exact_qa(tmp_path, allow):
    from raven_core.planning import FixReport, FixResult, PlanResult, PlanningError, QAPlan

    async def scenario():
        raven = service(tmp_path)
        ready = asyncio.Event()
        executed = []
        plan = QAPlan(platform=Platform.linux, setup="Start the app", task="Click send",
                      expected_result="Message arrives", verification="Read the new message", blocker="No app")
        approved = PlanResult(task=plan.render(), plan=plan, session_id="retained-session")

        async def planner(*args, **kw):
            return approved

        async def engine(agent, worker, emit, *args, **kw):
            executed.append(agent.task)
            await emit(Event(type="agent.verdict", data={
                "passed": len(executed) > 1, "reason": "Visible assertion result",
            }))

        async def fix(task, platform, **kw):
            assert kw["plan"].session_id == "retained-session"
            assert kw["failure_context"]["failures"][0]["verdict"]["category"] == "assertion"
            ready.set()
            choice = await kw["approve"]({
                "sessionId": "retained-session",
                "toolCall": {"toolCallId": "edit-1", "kind": "edit", "locations": [{"path": str(tmp_path / "app.py")}]},
                "options": [
                    {"optionId": "once", "kind": "allow_once", "name": "Allow once"},
                    {"optionId": "always", "kind": "allow_always", "name": "Always allow"},
                    {"optionId": "no", "kind": "reject_once", "name": "Decline"},
                ],
            })
            if choice != "once":
                raise PlanningError("Repair permission declined")
            return FixResult(session_id="retained-session", task=approved.task, report=FixReport(
                outcome="changes_applied", summary="Corrected receive path", changed_files=["app.py"],
                assertions_unchanged=True,
            ))

        raven.plan_task, raven.fix_task, raven.run_agent = planner, fix, engine
        first = raven.start_agent(CreateAgentRequest(platform=Platform.linux, task="Check messages", repo_path=str(tmp_path)))
        await raven.store.handles[first.id].task
        second = raven.start_agent(CreateAgentRequest(
            platform=Platform.linux, task="Check messages", repo_path=str(tmp_path),
            previous_agent_id=first.id, repair=True,
        ))
        await asyncio.wait_for(ready.wait(), 1)
        approval_id = next(iter(raven._approvals))
        operation = raven.get_approval(second.id, approval_id)
        assert {option["kind"] for option in operation["options"]} == {"allow_once", "reject_once"}
        assert executed == [approved.task]
        with pytest.raises(ValueError):
            raven.decide_approval(second.id, approval_id, "always")
        raven.decide_approval(second.id, approval_id, "once" if allow else "no")
        await raven.store.handles[second.id].task
        assert second.status == (AgentStatus.passed if allow else AgentStatus.error)
        assert executed == ([approved.task, approved.task] if allow else [approved.task])
        assert not raven._approvals
        await raven.shutdown()

    asyncio.run(scenario())


def test_run_remaining_deadline_caps_the_engine_budget(tmp_path):
    async def scenario():
        raven = service(tmp_path)
        raven.settings.run_timeout_s = 40
        spec = Spec(name="budget", timeout_s=120, agents=[SpecAgent(platform=Platform.linux, task="Original QA")])
        raven.load_spec = lambda _: spec
        budgets = []

        async def engine(agent, worker, emit, *args, **kw):
            budgets.append(kw["timeout_s"])
            await emit(Event(type="agent.verdict", data={"passed": True, "reason": "Verified"}))

        raven.run_agent = engine
        run = raven.create_run(CreateRunRequest(spec=spec.name))
        await raven._run_tasks[run.id]
        assert run.status == RunStatus.passed
        assert len(budgets) == 1 and 0 < budgets[0] < raven.settings.run_timeout_s
        assert not raven._run_deadlines
        await raven.shutdown()

    asyncio.run(scenario())
