"""specs.py, runner.py and get_worker tests (FakeWorker, no network)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven_core import workers
from raven_core.events import Agent, AgentStatus, CreateAgentRequest, Platform, Run, RunStatus
from raven_core.runner import SetupTimeoutError, agent_request, launch_command, run_spec, setup_platform
from raven_core.specs import list_specs, load_spec

SPECS = Path(__file__).resolve().parents[2] / "specs"


@pytest.fixture(autouse=True)
def fake_workers(monkeypatch):
    monkeypatch.setenv("RAVEN_FAKE_WORKERS", "1")
    monkeypatch.setattr("raven_core.runner.SENDER_DELAY_S", 0.0)
    workers.reset_workers()
    yield
    workers.reset_workers()


def test_list_and_load_real_spec():
    specs = list_specs(SPECS)
    entry = next(s for s in specs if s["name"] == "bitchat-geohash-interop")
    assert entry["platforms"] == ["macos", "linux", "windows"]
    assert entry["description"]

    spec = load_spec("bitchat-geohash-interop", SPECS)
    code = spec.vars["code"]
    assert code and "{{" not in code
    for a in spec.agents:
        assert "{{" not in a.task
        assert f"raven {code}" in a.task
        assert "#u09t" in a.task
    assert spec.setup[Platform.linux] == ["bitchat-tui"]
    assert spec.timeout_s == 180
    # random_word re-rolls per load
    codes = {load_spec("bitchat-geohash-interop", SPECS).vars["code"] for _ in range(5)}
    assert len(codes) > 1


def test_load_by_stem(tmp_path):
    (tmp_path / "hello.yaml").write_text(
        "description: d\nvars: {who: '{{random_number}}'}\nagents:\n  - platform: linux\n    task: say {{who}} {{unknown}}\n"
    )
    spec = load_spec("hello", tmp_path)
    assert spec.name == "hello"
    assert spec.agents[0].task.startswith("say ") and "{{unknown}}" in spec.agents[0].task
    with pytest.raises(FileNotFoundError):
        load_spec("nope", tmp_path)
    (tmp_path / "hello.yaml").write_text("name: changed\nagents: []\n")
    retry = spec.rerender()
    assert retry.name == "hello" and retry.agents[0].task.startswith("say ")
    assert "{{unknown}}" in retry.agents[0].task


def test_get_worker_fake_and_partial(monkeypatch):
    w = workers.get_worker(Platform.linux)
    assert isinstance(w, workers.FakeWorker)
    assert workers.get_worker(Platform.linux) is w
    monkeypatch.setenv("RAVEN_FAKE_WORKERS", "windows")
    workers.reset_workers()
    assert isinstance(workers.get_worker(Platform.windows), workers.FakeWorker)
    assert not isinstance(workers.get_worker(Platform.linux), workers.FakeWorker)
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.delenv("DEVIN_ORG_ID", raising=False)
    monkeypatch.setenv("RAVEN_FAKE_WORKERS", "linux")
    workers.reset_workers()
    with pytest.raises(workers.WorkerUnavailable):
        workers.get_worker(Platform.windows)
    monkeypatch.setenv("DEVIN_API_KEY", "test-key")
    monkeypatch.setenv("DEVIN_ORG_ID", "test-org")
    w = workers.get_worker(Platform.windows)
    from raven_core.hosts.devin import DevinHost

    assert isinstance(w, DevinHost)
    assert w.platform == Platform.windows and not w.session_id


def test_launch_command():
    cmd, bg = launch_command(Platform.linux, "bitchat-tui")
    assert "xfce4-terminal" in cmd and "bitchat-tui" in cmd and bg
    assert launch_command(Platform.macos, "open -a bitchat") == ("open -a bitchat", True)
    assert launch_command(Platform.linux, "sh:echo hi") == ("echo hi", False)
    assert "start" in launch_command(Platform.windows, "bitchat-tui.exe")[0]


@pytest.mark.parametrize("window_wait", [False, True])
def test_setup_shares_deadline_across_commands_and_window_waits(window_wait):
    async def scenario():
        budgets = []
        cancelled = asyncio.Event()

        async def shell(command, timeout):
            budgets.append(timeout)
            if len(budgets) == 1:
                await asyncio.sleep(0.02)
            elif not window_wait:
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            return 0, "", ""

        target = SimpleNamespace(shell=shell)
        commands = ["sh:first", "launch app" if window_wait else "sh:blocked"]
        started = asyncio.get_running_loop().time()
        with pytest.raises(SetupTimeoutError) as error:
            await setup_platform(Platform.linux, commands, worker=target, timeout_s=0.15)
        assert asyncio.get_running_loop().time() - started < 1
        assert 0 < budgets[1] < budgets[0] <= 0.15
        assert "setup command 2/2" in str(error.value)
        assert ("waiting for the window" in str(error.value)) == window_wait
        assert cancelled.is_set() != window_wait

    asyncio.run(scenario())


@pytest.mark.parametrize("raises", [False, True])
def test_setup_labels_driver_command_timeouts(raises):
    async def shell(command, timeout):
        if raises:
            raise TimeoutError("driver deadline")
        return 124, "", "driver deadline"

    with pytest.raises(SetupTimeoutError, match="setup timed out while running setup command 1/1"):
        asyncio.run(setup_platform(Platform.linux, ["sh:blocked"], worker=SimpleNamespace(shell=shell)))


def test_legacy_setup_defaults_to_one_finite_budget():
    async def scenario():
        budgets = []

        async def shell(command, timeout):
            budgets.append(timeout)
            await asyncio.sleep(0)
            return 0, "", ""

        target = SimpleNamespace(shell=shell)
        assert await setup_platform(Platform.linux, ["sh:first", "sh:second"], worker=target) is target
        assert 0 < budgets[1] < budgets[0] <= 60

    asyncio.run(scenario())


@pytest.mark.parametrize("timeout_s", [0, -1, float("inf"), float("nan")])
def test_setup_rejects_unbounded_or_invalid_budget(timeout_s):
    with pytest.raises(ValueError, match="finite and positive"):
        asyncio.run(setup_platform(Platform.linux, [], timeout_s=timeout_s))


def _fake_start(outcomes: dict[Platform, AgentStatus], created: list[tuple[CreateAgentRequest, Agent]]):
    async def start_agent(req: CreateAgentRequest, run_id: str) -> Agent:
        agent = Agent(platform=req.platform, worker="fake", title=req.title or "", task=req.task, run_id=run_id)
        created.append((req, agent))

        async def finish():
            agent.status = AgentStatus.running
            await asyncio.sleep(0.05)
            agent.status = outcomes[req.platform]

        asyncio.get_running_loop().create_task(finish())
        return agent

    return start_agent


def test_run_spec_pass_and_fail():
    spec = load_spec("bitchat-geohash-interop", SPECS)
    created: list = []
    run = Run(spec=spec.name, platforms=spec.platforms)
    events = []

    async def emit(e):
        events.append(e)

    ok = {p: AgentStatus.passed for p in Platform}
    asyncio.run(run_spec(spec, None, _fake_start(ok, created), run, emit=emit, poll_s=0.01))
    assert run.status == RunStatus.passed
    assert len(run.agent_ids) == 3
    assert [e.data["status"] for e in events if e.type == "run.status"] == ["running", "passed"]
    # setup commands ran on the fake workers
    linux = workers.get_worker(Platform.linux)
    assert any(a[0] == "shell" and "bitchat-tui" in a[1] for a in linux.actions)  # type: ignore[attr-defined]
    req = created[0][0]
    assert "Context:" in req.task and req.title

    created2: list = []
    run2 = Run(spec=spec.name, platforms=[Platform.linux, Platform.windows])
    bad = {**ok, Platform.windows: AgentStatus.failed}
    asyncio.run(run_spec(spec, [Platform.linux, Platform.windows], _fake_start(bad, created2), run2, poll_s=0.01))
    assert run2.status == RunStatus.failed
    assert {r.platform for r, _ in created2} == {Platform.linux, Platform.windows}


def test_run_spec_start_failure():
    spec = load_spec("bitchat-geohash-interop", SPECS)

    async def broken(req, run_id):
        raise RuntimeError("no worker")

    run = Run(spec=spec.name, platforms=[Platform.linux])
    asyncio.run(run_spec(spec, [Platform.linux], broken, run, poll_s=0.01))
    assert run.status == RunStatus.failed


@pytest.mark.parametrize("outcome,expected_attempts", [(AgentStatus.error, 2), (AgentStatus.failed, 1)])
def test_retry_execution_error_but_keep_app_failure(outcome, expected_attempts):
    spec = load_spec("holo-linux-smoke", SPECS)
    run = Run(spec=spec.name, platforms=spec.platforms)
    requests = []
    events = []
    retry_spec = spec.model_copy(deep=True)
    retry_spec.agents[0].task = "fresh challenge"

    async def start(req, run_id):
        requests.append(req)
        status = outcome if len(requests) == 1 else AgentStatus.passed
        return Agent(platform=req.platform, worker="fake", title="", task=req.task, run_id=run_id, status=status)

    async def emit(event):
        events.append(event)

    asyncio.run(run_spec(spec, None, start, run, reload_spec=lambda: retry_spec, emit=emit, poll_s=0.01))
    assert len(requests) == expected_attempts
    if outcome == AgentStatus.error:
        assert run.status == RunStatus.passed
        assert "fresh challenge" in requests[-1].task
        assert any("Retrying" in e.data.get("text", "") for e in events)
    else:
        assert run.status == RunStatus.failed


def test_agents_inherit_the_suite_deadline():
    spec = load_spec("holo-linux-smoke", SPECS)
    req = agent_request(spec, spec.agents[0])
    assert req.timeout_s == spec.timeout_s == 75


def test_attempt_timeout_stops_agents_with_a_reason():
    spec = load_spec("holo-linux-smoke", SPECS).model_copy(update={"timeout_s": 1, "attempts": 1})
    run = Run(spec=spec.name, platforms=spec.platforms)
    agents: list[Agent] = []
    stops: list[tuple[str, str | None]] = []
    events = []

    async def start(req, run_id):
        agent = Agent(platform=req.platform, worker="fake", title="", task=req.task, run_id=run_id, status=AgentStatus.running)
        agents.append(agent)
        return agent

    async def stop_agent(agent_id, reason=None):
        stops.append((agent_id, reason))
        for a in agents:
            if a.id == agent_id:
                a.status = AgentStatus.stopped
                a.verdict_reason = f"{reason} while waiting for Holo request #2 (step 2, attempt 1/3, 30s of its 45s limit)"

    async def emit(event):
        events.append(event)

    asyncio.run(run_spec(spec, None, start, run, emit=emit, stop_agent=stop_agent, poll_s=0.01, grace_s=0.0))
    assert run.status == RunStatus.failed
    assert stops == [(agents[0].id, "QA attempt exceeded 1s while waiting for computer use")]
    summary = next(e.data["text"] for e in events if e.data.get("text", "").startswith("Attempt 1/1:"))
    assert "QA attempt exceeded 1s while waiting for computer use." in summary
    assert "linux: QA attempt exceeded 1s while waiting for computer use while waiting for Holo request #2" in summary
    assert "Stopped by user" not in summary


def test_failed_setup_skips_computer_use(monkeypatch):
    spec = load_spec("holo-linux-smoke", SPECS)
    run = Run(spec=spec.name, platforms=spec.platforms)
    events = []

    async def setup(*args):
        raise RuntimeError("terminal unavailable")

    async def start(*args):
        pytest.fail("computer use must not start when setup fails")

    async def emit(event):
        events.append(event)

    monkeypatch.setattr("raven_core.runner.setup_platform", setup)
    asyncio.run(run_spec(spec, None, start, run, emit=emit, poll_s=0.01))
    assert run.status == RunStatus.failed
    assert sum("terminal unavailable" in e.data.get("text", "") for e in events) == 2
