"""specs.py, runner.py and get_worker tests (FakeWorker, no network)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from raven_core import workers
from raven_core.events import Agent, AgentStatus, CreateAgentRequest, Platform, Run, RunStatus
from raven_core.runner import launch_command, run_spec
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


def test_get_worker_fake_and_partial(monkeypatch):
    w = workers.get_worker(Platform.linux)
    assert isinstance(w, workers.FakeWorker)
    assert workers.get_worker(Platform.linux) is w
    monkeypatch.setenv("RAVEN_FAKE_WORKERS", "windows")
    workers.reset_workers()
    assert isinstance(workers.get_worker(Platform.windows), workers.FakeWorker)
    assert not isinstance(workers.get_worker(Platform.linux), workers.FakeWorker)
    monkeypatch.setenv("WINDOWS_WORKER_HOST", "")
    monkeypatch.setenv("RAVEN_FAKE_WORKERS", "linux")
    workers.reset_workers()
    with pytest.raises(workers.WorkerUnavailable):
        workers.get_worker(Platform.windows)
    monkeypatch.setenv("WINDOWS_WORKER_HOST", "100.64.0.7")
    w = workers.get_worker(Platform.windows)
    assert w._url == "http://100.64.0.7:3211"  # type: ignore[attr-defined]
    assert "token" not in repr(w).lower()


def test_launch_command():
    cmd, bg = launch_command(Platform.linux, "bitchat-tui")
    assert "xfce4-terminal" in cmd and "bitchat-tui" in cmd and bg
    assert launch_command(Platform.macos, "open -a bitchat") == ("open -a bitchat", True)
    assert launch_command(Platform.linux, "sh:echo hi") == ("echo hi", False)
    assert "start" in launch_command(Platform.windows, "bitchat-tui.exe")[0]


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
    assert [e.data["status"] for e in events] == ["running", "passed"]
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
