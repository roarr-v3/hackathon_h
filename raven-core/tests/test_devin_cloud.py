import asyncio
import json

import httpx
import pytest

from raven_core import integrations, workers
from raven_core.engine import run_agent
from raven_core.events import Agent, AgentStatus, CreateAgentRequest, Platform, Run
from raven_core.hosts import get_host
from raven_core.hosts.devin_cloud import DevinCloudHost, RESULT_SCHEMA
from raven_core.runner import run_spec
from raven_core.server import Raven
from raven_core.server.config import Settings
from raven_core.specs import Spec, SpecAgent
from raven_core.store import Store
from raven_core.workers.base import WorkerUnavailable


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("DEVIN_API_KEY", "private-devin-key")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-test")
    monkeypatch.setenv("HAI_API_KEY", 'private-h-key-"quoted')
    monkeypatch.setenv("HAI_MODEL", "private-h-model")
    monkeypatch.setenv("HAI_BASE_URL", "https://private-h-base.invalid")
    monkeypatch.setenv("RAVEN_HOST_LINUX", "devin-cloud")
    monkeypatch.setenv("RAVEN_HOST_WINDOWS", "devin-cloud")
    monkeypatch.delenv("RAVEN_FAKE_WORKERS", raising=False)
    monkeypatch.setattr("raven_core.hosts.devin_cloud.INITIAL_POLL_S", 0.001)


def output(driver="h", verdict="PASSED"):
    return {"verdict": verdict, "driver": driver,
            "fallback_reason": "No Windows runtime" if driver == "devin" else None,
            "report_markdown": f"VERDICT: {verdict}\n## Setup\nReady\n## Task\nRun\n"
                               "## Expected Result\nVisible\n## Verification\nVisible",
            "steps": ["Opened app", "Verified screen"]}


def execute(tmp_path, monkeypatch, handler, task="## Setup\nReady\n## Task\nVerify"):
    monkeypatch.setattr(integrations, "TRANSPORT", httpx.MockTransport(handler))
    events = []

    async def emit(event):
        events.append(event)

    async def main():
        agent = Agent(platform=Platform.linux, worker="cloud", title="QA", task=task)
        await run_agent(agent, get_host(Platform.linux), emit, asyncio.Queue(), asyncio.Event(), tmp_path)
        return agent

    return asyncio.run(main()), events


@pytest.mark.parametrize("driver", ["h", "devin"])
def test_payload_poll_and_driver(tmp_path, monkeypatch, cloud, driver):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer private-devin-key"
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload["platform"] == "linux"
            assert payload["structured_output_required"] is True
            assert payload["structured_output_schema"] == RESULT_SCHEMA
            assert RESULT_SCHEMA["$schema"].endswith("draft-07/schema#")
            assert {s["key"] for s in payload["session_secrets"]} == {"HAI_API_KEY", "HAI_MODEL", "HAI_BASE_URL"}
            assert all(s["sensitive"] is True for s in payload["session_secrets"])
            assert all(s["value"] not in payload["prompt"] for s in payload["session_secrets"])
            assert "uv tool install 'hai-agents[cli,desktop]>=1.4.3'" in payload["prompt"]
            assert "holo run --local --agent h/holo-desktop" in payload["prompt"]
            return httpx.Response(200, json={"session_id": "devin-test", "url": "https://app.devin.ai/s/test"})
        assert request.url.path == "/v3/organizations/org-test/sessions/devin-test"
        if len(calls) == 2:
            return httpx.Response(200, json={"status": "running", "status_detail": "working"})
        return httpx.Response(200, json={"status": "running", "status_detail": "finished",
                                       "structured_output": output(driver)})

    agent, events = execute(tmp_path, monkeypatch, handler)
    assert agent.status == AgentStatus.passed
    assert len(calls) == 3
    assert any(f"Driver: {driver}" in e.data.get("text", "") for e in events)
    assert any(e.type == "report.ready" and e.data["devin_session_url"] == "https://app.devin.ai/s/test" for e in events)
    assert len([e for e in events if e.type == "report.ready"]) == 1
    assert any(e.type == "agent.message" and "https://app.devin.ai/s/test" in e.data.get("text", "") for e in events)


def test_cli_waits_for_verdict_after_cloud_report(tmp_path, monkeypatch, cloud, capsys):
    from raven_core import _run_cli

    def handler(request):
        return httpx.Response(200, json={"session_id": "devin-test", "url": "https://app.devin.ai/s/test"}
                              if request.method == "POST" else {"structured_output": output()})

    monkeypatch.setattr(integrations, "TRANSPORT", httpx.MockTransport(handler))
    raven = Raven(settings=Settings(artifacts_dir=tmp_path, reports=False), store=Store())
    spec = Spec(name="cloud", agents=[SpecAgent(platform=Platform.linux, task="Verify")])
    monkeypatch.setattr(raven, "load_spec", lambda name: spec)
    monkeypatch.setattr("raven_core.server.Raven", lambda: raven)
    assert asyncio.run(_run_cli("cloud", None, False)) == 0
    printed = capsys.readouterr().out
    assert "https://app.devin.ai/s/test" in printed
    assert "agent.verdict" in printed and "run.status" in printed


@pytest.mark.parametrize("missing", ["DEVIN_API_KEY", "DEVIN_ORG_ID"])
def test_missing_credentials(monkeypatch, cloud, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(WorkerUnavailable, match="DEVIN_API_KEY and DEVIN_ORG_ID"):
        get_host(Platform.linux)
    with pytest.raises(WorkerUnavailable, match="DEVIN_API_KEY and DEVIN_ORG_ID"):
        workers.get_worker(Platform.windows)


@pytest.mark.parametrize("mode", ["output", "http_error", "transport_error"])
def test_secrets_never_leak(tmp_path, monkeypatch, cloud, caplog, mode):
    secret = 'private-h-key-"quoted'
    values = [secret, "private-h-model", "https://private-h-base.invalid", "private-devin-key"]

    def handler(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            assert all(value not in payload["prompt"] for value in values)
            if mode == "http_error":
                return httpx.Response(401, text=" ".join(values))
            if mode == "transport_error":
                raise RuntimeError(" ".join(values))
            return httpx.Response(200, json={"session_id": "devin-test"})
        result = output("devin")
        result["report_markdown"] += " ".join(values)
        result["fallback_reason"] = " ".join(values)
        result["steps"] = values
        return httpx.Response(200, json={"structured_output": result})

    agent, events = execute(tmp_path, monkeypatch, handler, task="Task: " + " ".join(values))
    observed = " ".join(e.model_dump_json() for e in events) + str(agent.verdict_reason) + caplog.text
    assert all(value not in observed for value in values)
    assert agent.status == (AgentStatus.passed if mode == "output" else AgentStatus.error)


@pytest.mark.parametrize("response", [
    {"status": "expired"}, {"status": "blocked"}, {"status": "exit"},
    {"status": "running", "status_detail": "finished"},
    {"status": "running", "status_detail": "waiting_for_user"},
    {"structured_output": {}}, {"structured_output": {**output(), "report_markdown": "bad"}},
])
def test_terminal_or_invalid_output(tmp_path, monkeypatch, cloud, response):
    def handler(request):
        return httpx.Response(200, json={"session_id": "devin-test"} if request.method == "POST" else response)

    agent, _ = execute(tmp_path, monkeypatch, handler)
    assert agent.status == AgentStatus.error


@pytest.mark.parametrize("verdict, expected", [("FAILED", "failed"), ("ERROR", "error")])
def test_failed_and_error_results(tmp_path, monkeypatch, cloud, verdict, expected):
    def handler(request):
        return httpx.Response(200, json={"session_id": "devin-test"} if request.method == "POST"
                              else {"structured_output": output(verdict=verdict)})

    agent, _ = execute(tmp_path, monkeypatch, handler)
    assert agent.status == expected


def test_timeout(tmp_path, monkeypatch, cloud):
    monkeypatch.setenv("RAVEN_DEVIN_TIMEOUT_S", "0.01")

    def handler(request):
        return httpx.Response(200, json={"session_id": "devin-test"} if request.method == "POST"
                              else {"status": "running"})

    agent, _ = execute(tmp_path, monkeypatch, handler)
    assert agent.status == AgentStatus.error
    assert "timed out" in agent.verdict_reason


def test_selection_overrides_local_and_models_loop(monkeypatch, cloud):
    monkeypatch.setenv("RAVEN_H_MODE", "models-loop")
    assert isinstance(workers.get_worker(Platform.linux), DevinCloudHost)
    assert isinstance(workers.get_worker(Platform.windows), DevinCloudHost)


def test_server_skips_local_machine_management(tmp_path, monkeypatch, cloud):
    async def forbidden(*args):
        pytest.fail("cloud host must not prepare or show a local VM")

    monkeypatch.setattr("raven_core.machines.ensure", forbidden)
    monkeypatch.setattr("raven_core.machines.show", forbidden)

    def handler(request):
        return httpx.Response(200, json={"session_id": "devin-test"} if request.method == "POST"
                              else {"structured_output": output()})

    monkeypatch.setattr(integrations, "TRANSPORT", httpx.MockTransport(handler))

    async def main():
        raven = Raven(settings=Settings(artifacts_dir=tmp_path), store=Store())
        agent = raven.start_agent(CreateAgentRequest(platform=Platform.windows, task="Verify"))
        await raven.store.handles[agent.id].task
        assert agent.status == AgentStatus.passed

    asyncio.run(main())


@pytest.mark.parametrize("use_cloud", [True, False])
def test_server_cloud_deadline_exceeds_default_cap(tmp_path, monkeypatch, cloud, use_cloud):
    monkeypatch.setenv("RAVEN_DEVIN_TIMEOUT_S", "1800")
    if not use_cloud:
        monkeypatch.delenv("RAVEN_HOST_LINUX")
    completed = []

    async def runner(spec, platforms, start, run):
        await asyncio.sleep(0.01)
        completed.append(True)

    async def main():
        raven = Raven(settings=Settings(artifacts_dir=tmp_path, run_timeout_s=0.001, reports=False), store=Store())
        raven.run_spec = runner
        spec = Spec(name="cloud", agents=[SpecAgent(platform=Platform.linux, task="Verify")])
        run = Run(spec=spec.name, platforms=spec.platforms)
        raven.store.add_run(run)
        await raven._run_task(run, spec, spec.platforms)

    asyncio.run(main())
    assert bool(completed) is use_cloud


def test_stop_before_create(tmp_path, monkeypatch, cloud):
    def forbidden(request):
        pytest.fail("stopped cloud agent must not create a session")

    monkeypatch.setattr(integrations, "TRANSPORT", httpx.MockTransport(forbidden))

    async def main():
        agent = Agent(platform=Platform.linux, worker="cloud", title="QA", task="Verify")
        stop = asyncio.Event()
        stop.set()

        async def emit(event):
            pass

        await run_agent(agent, get_host(Platform.linux), emit, asyncio.Queue(), stop, tmp_path)
        assert agent.status == AgentStatus.stopped

    asyncio.run(main())


def test_runner_one_session_per_platform_in_parallel(tmp_path, monkeypatch, cloud):
    posts = []
    reached = set()

    async def handler(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            posts.append(payload)
            platform = payload["platform"]
            reached.add(platform)
            while len(reached) < 2:
                await asyncio.sleep(0.001)
            return httpx.Response(200, json={"session_id": f"devin-{platform}"})
        return httpx.Response(200, json={"structured_output": output()})

    monkeypatch.setattr(integrations, "TRANSPORT", httpx.MockTransport(handler))

    async def no_local_setup(*args):
        pytest.fail("cloud setup must not run on local workers")

    monkeypatch.setattr("raven_core.runner.setup_platform", no_local_setup)
    spec = Spec(name="cloud", setup={Platform.linux: ["linux-app"], Platform.windows: ["windows-app"]},
                agents=[SpecAgent(platform=Platform.linux, task="first"),
                        SpecAgent(platform=Platform.linux, task="second"),
                        SpecAgent(platform=Platform.windows, task="third")])
    run = Run(spec="cloud", platforms=spec.platforms)

    async def main():
        tasks = []

        async def emit(event):
            pass

        async def start(req, run_id):
            agent = Agent(platform=req.platform, worker="cloud", title="QA", task=req.task, run_id=run_id)
            tasks.append(asyncio.create_task(run_agent(agent, get_host(req.platform), emit,
                                                     asyncio.Queue(), asyncio.Event(), tmp_path)))
            return agent

        await asyncio.wait_for(run_spec(spec, None, start, run, poll_s=0.001), 2)
        await asyncio.gather(*tasks)

    asyncio.run(main())
    assert run.status == "passed"
    assert len(posts) == len(run.agent_ids) == 2
    linux = next(p for p in posts if p["platform"] == "linux")
    assert all(text in linux["prompt"] for text in ["linux-app", "first", "second", "## Expected Result", "## Verification"])


@pytest.mark.parametrize("delay, expected", [(None, 20), (0, 0), (7, 7)])
def test_cloud_sender_preserves_start_gate(monkeypatch, cloud, delay, expected):
    from raven_core import runner

    sleeps = []
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        if seconds in (20, 7):
            sleeps.append(seconds)
            return
        await real_sleep(seconds)

    monkeypatch.setattr(runner.asyncio, "sleep", sleep)
    spec = Spec(name="cloud", agents=[SpecAgent(platform=Platform.linux, task="Receive"),
                SpecAgent(platform=Platform.windows, role="sender", task="Send", start_delay_s=delay),
                SpecAgent(platform=Platform.windows, task="Verify", start_delay_s=0)])
    run = Run(spec=spec.name, platforms=spec.platforms)

    async def start(req, run_id):
        return Agent(platform=req.platform, worker="cloud", title="QA", task=req.task, status=AgentStatus.passed)

    asyncio.run(run_spec(spec, None, start, run, poll_s=0.001))
    assert run.status == "passed"
    assert sleeps == ([expected] if expected else [])
