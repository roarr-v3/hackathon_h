import asyncio
import base64
import json
import sys
from pathlib import Path

from raven_core.engine import run_agent
from raven_core.engine.h_agent import verdict_from
from raven_core.events import Agent, Platform
from raven_core.hosts import Host

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


def _run(tmp_path, monkeypatch, host, steer=None):
    monkeypatch.setenv("HAI_API_KEY", "test")
    events = []

    async def emit(e):
        events.append(e)

    async def main():
        agent = Agent(platform=Platform.linux, worker="fake", title="t", task="do it")
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
    assert verdict_from("done", "success", "completed")[0] is True
