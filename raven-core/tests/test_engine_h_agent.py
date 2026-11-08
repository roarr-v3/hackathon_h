import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

from raven_core.engine import run_agent
from raven_core.engine.h_agent import _shutdown, verdict_from
from raven_core.events import Agent, Platform
from raven_core.hosts import Host
from raven_core.store import StopSignal

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class FakeHost(Host):
    def __init__(self, tmp: Path, answer: str, wait_for_steer: bool = False):
        super().__init__(Platform.linux, "fake")
        self.tmp, self.answer, self.wait = tmp, answer, wait_for_steer

    def _spawn_argv(self, host_args, env):
        shot = self.tmp / "s.png"
        shot.write_bytes(PNG)
        lines = [
            {"kind": "started", "session_id": "ses_1", "mode": "local-runtime", "agent": "h/holo-desktop"},
            {"kind": "action", "tool": "click", "args": {"x": 10, "y": 20}, "thought": "open it"},
            {"kind": "screenshot", "path": str(shot)},
        ]
        script = "import json,sys\n" + "".join(f"print(json.dumps({json.dumps(l)}), flush=True)\n" for l in lines)
        if self.wait:
            script += "line=sys.stdin.readline().strip()\nprint(json.dumps({'kind':'steered','text':line}),flush=True)\n"
            script += "print(json.dumps({'kind':'message','text':'got '+line}),flush=True)\n"
        script += f"print(json.dumps({{'kind':'result','status':'completed','answer':{self.answer!r},'outcome':'success'}}), flush=True)\n"
        return [sys.executable, "-c", script]

    async def read_file(self, path):
        return Path(path).read_bytes()


def _run(tmp_path, monkeypatch, host, steer=None, run_id=None):
    monkeypatch.setenv("HAI_API_KEY", "test")
    events = []

    async def emit(e):
        events.append(e)

    async def main():
        agent = Agent(platform=Platform.linux, worker="fake", title="t", task="do it", run_id=run_id)
        inbox, stop = asyncio.Queue(), asyncio.Event()
        if steer:
            inbox.put_nowait(steer)
        await run_agent(agent, host, emit, inbox, stop, tmp_path / "artifacts")
        return agent

    return asyncio.run(main()), events


def test_h_agent_pass(tmp_path, monkeypatch):
    agent, events = _run(tmp_path, monkeypatch, FakeHost(tmp_path, "VERDICT: PASSED all three messages visible"))
    types = [e.type for e in events]
    assert "agent.action" in types and "agent.screenshot" in types
    assert agent.status == "passed"
    shot = next(e for e in events if e.type == "agent.screenshot")
    assert (tmp_path / "artifacts" / agent.id / "000.png").exists() and shot.data["width"] == 1


def test_h_agent_fail_and_steer(tmp_path, monkeypatch):
    agent, events = _run(tmp_path, monkeypatch, FakeHost(tmp_path, "VERDICT: FAILED no message", True), steer="hurry")
    assert agent.status == "failed"
    assert any(e.type == "agent.message" and "got hurry" in e.data.get("text", "") for e in events)


def test_verdict_parsing():
    assert verdict_from("VERDICT: PASSED ok", "success", "completed")[0] is True
    assert verdict_from("verdict: failed nope", "success", "completed")[0] is False
    assert verdict_from(None, None, "timed_out")[0] is False
    assert verdict_from("done", "success", "completed")[0] is False
    assert verdict_from("The required format was VERDICT: PASSED", "success", "completed")[0] is False


@pytest.mark.parametrize("run_id", [None, "run_test"])
@pytest.mark.parametrize("answer", ["done", "The expected response is VERDICT: PASSED"])
def test_h_agent_suite_requires_a_verdict(tmp_path, monkeypatch, run_id, answer):
    agent, _ = _run(tmp_path, monkeypatch, FakeHost(tmp_path, answer), run_id=run_id)
    assert agent.status == "error"
    assert "without an explicit test verdict" in agent.verdict_reason


class MemoryProcess:
    def __init__(self, lines=(), *, close_exits=True, terminate_exits=True):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.returncode = None
        self.closed = False
        self.terminated = False
        self.killed = False
        self.close_exits, self.terminate_exits = close_exits, terminate_exits
        self.exited = asyncio.Event()
        self.stdin = self
        for line in lines:
            self.stdout.feed_data((json.dumps(line) + "\n").encode())

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True
        if self.close_exits:
            self.exit(0)

    def write(self, data):
        pass

    async def drain(self):
        await asyncio.sleep(0)

    def exit(self, code):
        self.returncode = code
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.exited.set()

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.terminate_exits:
            self.exit(-15)

    def kill(self):
        self.killed = True
        self.exit(-9)


class MemoryHost(Host):
    def __init__(self, proc):
        super().__init__(Platform.linux, "fake")
        self.proc = proc
        self.spawned = asyncio.Event()
        self.args = None

    async def spawn(self, args):
        self.args = args
        self.spawned.set()
        return self.proc


def test_local_spawn_cancellation_reaps_a_child_created_after_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("HAI_API_KEY", "test")

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        process = MemoryProcess()

        async def spawn(*args, **kw):
            started.set()
            await release.wait()
            return process

        monkeypatch.setattr("raven_core.hosts.asyncio.create_subprocess_exec", spawn)
        host = FakeHost(tmp_path, "VERDICT: PASSED")
        task = asyncio.create_task(host.spawn([]))
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.closed and process.returncode is not None

    asyncio.run(scenario())


def session_events(events):
    return [e.data for e in events if e.type == "agent.message" and e.data.get("phase") == "h_session"]


def test_shutdown_escalates_and_reaps_the_process():
    async def main():
        proc = MemoryProcess(close_exits=False, terminate_exits=False)
        await _shutdown(proc, grace_s=0.03)
        assert proc.closed and proc.terminated and proc.killed
        assert proc.returncode == -9
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]

    asyncio.run(main())


def test_h_host_start_is_bounded_by_agent_deadline(tmp_path):
    events = []

    async def main():
        cleaned = asyncio.Event()

        class StartingHost(Host):
            async def spawn(self, args):
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0.01)
                    cleaned.set()

        async def emit(event):
            events.append(event)

        agent = Agent(platform=Platform.linux, worker="fake", title="t", task="do it")
        host = StartingHost(Platform.linux, "fake")
        await asyncio.wait_for(
            run_agent(agent, host, emit, asyncio.Queue(), asyncio.Event(), tmp_path, timeout_s=0.1), 0.5,
        )
        assert agent.status == "error"
        assert "timed out" in agent.verdict_reason.lower()
        assert "starting" in agent.verdict_reason.lower()
        assert cleaned.is_set()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]

    asyncio.run(main())
    assert session_events(events)[-1]["state"] == "failed"


def test_h_stdout_eof_does_not_leave_an_unbounded_process_wait(tmp_path):
    async def main():
        proc = MemoryProcess()
        proc.stdout.feed_eof()
        host = MemoryHost(proc)

        async def emit(event):
            pass

        agent = Agent(platform=Platform.linux, worker="fake", title="t", task="do it")
        await asyncio.wait_for(
            run_agent(agent, host, emit, asyncio.Queue(), asyncio.Event(), tmp_path, timeout_s=0.1), 0.5,
        )
        assert agent.status == "error"
        assert proc.returncode is not None
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]

    asyncio.run(main())


def test_h_preexisting_stop_does_not_spawn_a_host(tmp_path):
    async def main():
        proc = MemoryProcess()
        host = MemoryHost(proc)
        stop = StopSignal()
        stop.set("QA attempt timed out before host launch")

        async def emit(event):
            pass

        agent = Agent(platform=Platform.linux, worker="fake", title="t", task="do it")
        await run_agent(agent, host, emit, asyncio.Queue(), stop, tmp_path)
        assert not host.spawned.is_set()
        assert agent.status == "stopped"
        assert "QA attempt timed out" in agent.verdict_reason
        assert "Stopped by user" not in agent.verdict_reason

    asyncio.run(main())


@pytest.mark.parametrize("cancel_task", [False, True])
def test_h_stop_cause_survives_shutdown_without_late_events(tmp_path, caplog, cancel_task):
    events = []
    reason = "QA attempt exceeded its computer-use budget"

    async def main():
        proc = MemoryProcess([{"kind": "started", "session_id": "ses_test", "mode": "agents-api"}])
        host, stop = MemoryHost(proc), StopSignal()

        async def emit(event):
            events.append(event)

        agent = Agent(platform=Platform.linux, worker="fake", title="t", task="do it")
        task = asyncio.create_task(run_agent(agent, host, emit, asyncio.Queue(), stop, tmp_path))
        await host.spawned.wait()
        await asyncio.sleep(0)
        stop.set(reason)
        if cancel_task:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(task, 1)
        assert reason in agent.verdict_reason
        assert "Stopped by user" not in agent.verdict_reason
        assert proc.returncode is not None
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        count = len(events)
        await asyncio.sleep(0.02)
        assert len(events) == count

    with caplog.at_level("INFO", logger="raven_core.engine.h_agent"):
        asyncio.run(main())
    assert session_events(events)[-1]["state"] == "cancelled"
    assert any("cancelled" in r.getMessage() for r in caplog.records)


def test_h_session_telemetry_is_honest_and_does_not_echo_errors(tmp_path, caplog):
    events = []
    secret = "secret-key-and-prompt-data:image/png;base64,private"

    async def main():
        proc = MemoryProcess([
            {"kind": "started", "session_id": "ses_test", "mode": "agents-api"},
            {"kind": "error", "error": secret},
            {"kind": "result", "status": "timed_out", "error": secret, "answer": secret},
        ])
        proc.stderr.feed_data(secret.encode())
        proc.exit(1)
        host = MemoryHost(proc)

        async def emit(event):
            events.append(event)

        agent = Agent(platform=Platform.linux, worker="fake", title="t", task=secret)
        await run_agent(
            agent, host, emit, asyncio.Queue(), asyncio.Event(), tmp_path,
            timeout_s=3, request_timeout_s=0.5, request_attempts=2, mode="agents-api",
        )
        assert agent.status == "error"
        assert secret not in agent.verdict_reason

    with caplog.at_level("INFO", logger="raven_core.engine.h_agent"):
        asyncio.run(main())
    telemetry = session_events(events)
    assert telemetry[0]["state"] == "started" and telemetry[-1]["state"] == "failed"
    assert telemetry[-1]["elapsed_s"] >= 0
    texts = "\n".join(e.data.get("text", "") for e in events if e.type == "agent.message")
    assert "not observable" in texts and "per-request" in texts
    assert secret not in texts and secret not in caplog.text


def test_h_stop_reason_callback_is_forwarded_by_engine_dispatch(tmp_path):
    async def main():
        proc = MemoryProcess()
        host, stop = MemoryHost(proc), asyncio.Event()
        stop.set()

        async def emit(event):
            pass

        agent = Agent(platform=Platform.linux, worker="fake", title="t", task="do it")
        await run_agent(
            agent, host, emit, asyncio.Queue(), stop, tmp_path,
            get_stop_reason=lambda: "Runner backstop timed out",
        )
        assert "Runner backstop timed out" in agent.verdict_reason

    asyncio.run(main())


def test_h_idle_session_obeys_limit_without_sixty_second_extension(tmp_path):
    events = []

    async def main():
        proc = MemoryProcess([{"kind": "started", "session_id": "ses_test"}])
        host = MemoryHost(proc)

        async def emit(event):
            events.append(event)

        agent = Agent(platform=Platform.linux, worker="fake", title="t", task="do it")
        await asyncio.wait_for(run_agent(
            agent, host, emit, asyncio.Queue(), asyncio.Event(), tmp_path, timeout_s=0.1,
        ), 0.5)
        assert agent.status == "error"
        assert "timed out" in agent.verdict_reason
        assert "waiting for H session events" in agent.verdict_reason
        assert proc.returncode is not None
        assert 0 < float(host.args[host.args.index("--max-time") + 1]) < 0.1
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]

    asyncio.run(main())
    assert session_events(events)[-1]["state"] == "failed"


def test_h_first_result_is_final_and_late_actions_are_ignored(tmp_path, caplog):
    events = []

    async def main():
        proc = MemoryProcess([
            {"kind": "result", "status": "completed", "answer": "VERDICT: PASSED verified", "outcome": "success"},
            {"kind": "action", "tool": "click", "args": {"x": 100, "y": 100}},
            {"kind": "result", "status": "interrupted"},
        ])
        host = MemoryHost(proc)

        async def emit(event):
            events.append(event)

        agent = Agent(platform=Platform.linux, worker="fake", title="t", task="do it", run_id="run_test")
        await run_agent(agent, host, emit, asyncio.Queue(), asyncio.Event(), tmp_path, timeout_s=0.3)
        assert agent.status == "passed"
        assert agent.verdict_reason == "VERDICT: PASSED verified"
        assert proc.returncode is not None
        assert not [e for e in events if e.type == "agent.action"]

    with caplog.at_level("INFO", logger="raven_core.engine.h_agent"):
        asyncio.run(main())
    assert session_events(events)[-1]["state"] == "succeeded"
    assert any("succeeded" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("cancel_task", [False, True])
def test_h_user_stop_is_not_reported_as_timeout(tmp_path, cancel_task):
    events = []

    async def main():
        proc, stop = MemoryProcess(), asyncio.Event()
        host = MemoryHost(proc)

        async def emit(event):
            events.append(event)

        agent = Agent(platform=Platform.linux, worker="fake", title="t", task="do it")
        task = asyncio.create_task(run_agent(agent, host, emit, asyncio.Queue(), stop, tmp_path))
        await host.spawned.wait()
        stop.set()
        if cancel_task:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert agent.status == "stopped"
        assert agent.verdict_reason.startswith("Stopped by user while ")
        assert "timed out" not in agent.verdict_reason

    asyncio.run(main())
    assert session_events(events)[-1]["state"] == "cancelled"


def test_h_cancellation_waits_for_screenshot_cleanup(tmp_path):
    events = []

    async def main():
        started, cleaned = asyncio.Event(), asyncio.Event()
        proc, stop = MemoryProcess([{"kind": "screenshot", "path": "/tmp/fake.png"}]), asyncio.Event()

        class ScreenshotHost(MemoryHost):
            async def read_file(self, path):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await asyncio.sleep(0.02)
                    cleaned.set()
                    return PNG

        host = ScreenshotHost(proc)

        async def emit(event):
            events.append(event)

        agent = Agent(platform=Platform.linux, worker="fake", title="t", task="do it")
        task = asyncio.create_task(run_agent(agent, host, emit, asyncio.Queue(), stop, tmp_path))
        await started.wait()
        stop.set()
        await task
        assert cleaned.is_set()
        assert not [e for e in events if e.type == "agent.screenshot"]
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]

    asyncio.run(main())


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        ("VERDICT: FAILED assertion disproved", "failed"),
        ("VERDICT: BLOCKED no app available", "error"),
    ],
)
def test_h_explicit_assertion_and_blocker_semantics_are_preserved(tmp_path, monkeypatch, answer, status):
    agent, _ = _run(tmp_path, monkeypatch, FakeHost(tmp_path, answer), run_id="run_test")
    assert agent.status == status
    assert agent.verdict_reason == answer


def test_h_cancellation_during_initial_status_still_emits_verdict(tmp_path):
    events = []

    async def main():
        started = asyncio.Event()
        proc = MemoryProcess()
        host = MemoryHost(proc)
        agent = Agent(platform=Platform.linux, worker="fake", title="t", task="do it")

        async def block_initial_status(event):
            events.append(event)
            if event.type == "agent.status" and event.data["status"] == "running":
                started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(run_agent(
            agent, host, block_initial_status, asyncio.Queue(), asyncio.Event(), tmp_path,
        ))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert agent.status == "stopped"
        assert not host.spawned.is_set()

    asyncio.run(main())
    assert events[-2].type == "agent.verdict"
