"""ACP bridge tests: a fake ACP client drives the Raven agent.

1. subprocess over real stdio (initialize / session/new / session/prompt "help")
2. in-process with a stubbed engine: agent turn, run turn with failure report, cancel
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from raven_core.acp.agent import RavenAcpAgent, action_title
from raven_core.acp.backend import LocalBackend

sys.path.insert(0, str(Path(__file__).parent))
from test_server_api import make_raven  # noqa: E402


# --------------------------------------------------------------------------- stdio subprocess
def test_acp_stdio_handshake_and_help(tmp_path):
    env = dict(os.environ)
    env.update(
        {
            "RAVEN_URL": "http://127.0.0.1:9",  # unreachable; help must not need a server
            "RAVEN_ACP_SERVE": "0",
            "RAVEN_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
            "RAVEN_PERSIST": "0",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", "from raven_core import main; main(['acp'])"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    lines: queue.Queue[bytes] = queue.Queue()

    def pump():
        for line in proc.stdout:  # type: ignore[union-attr]
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()
    received: list[dict] = []

    def send(obj):
        proc.stdin.write((json.dumps(obj) + "\n").encode())  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    def response(id_, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            msg = json.loads(raw)  # every stdout line must be JSON-RPC
            received.append(msg)
            if msg.get("id") == id_:
                return msg
        raise AssertionError(f"no response for id {id_}")

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
        init = response(1)["result"]
        assert init["protocolVersion"] == 1
        assert init["agentInfo"]["name"] == "raven"

        send({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path), "mcpServers": []}})
        new = response(2)["result"]
        sid = new["sessionId"]
        assert {m["modelId"] for m in new["models"]["availableModels"]} >= {"holo4-27b", "holo4-35b-a3b"}

        send({"jsonrpc": "2.0", "id": 3, "method": "session/set_model", "params": {"sessionId": sid, "modelId": "holo4-35b-a3b"}})
        assert response(3)["result"] == {}

        send({"jsonrpc": "2.0", "id": 4, "method": "session/prompt",
              "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "help"}]}})
        res = response(4)
        assert res["result"] == {"stopReason": "end_turn"}
        chunks = [
            m["params"]["update"]["content"]["text"]
            for m in received
            if m.get("method") == "session/update"
            and m["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
        ]
        assert any("Raven commands" in c for c in chunks)

        send({"jsonrpc": "2.0", "id": 5, "method": "nope/unknown", "params": {}})
        assert response(5)["error"]["code"] == -32601
    finally:
        proc.stdin.close()  # type: ignore[union-attr]
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# --------------------------------------------------------------------------- in-process
class FakeClient:
    def __init__(self) -> None:
        self.msgs: list[dict] = []
        self.next_id = 100

    def write(self, data: bytes) -> None:
        for line in data.splitlines():
            if line.strip():
                self.msgs.append(json.loads(line))

    def updates(self, kind: str | None = None) -> list[dict]:
        ups = [m["params"]["update"] for m in self.msgs if m.get("method") == "session/update"]
        return [u for u in ups if kind is None or u["sessionUpdate"] == kind]

    def text(self) -> str:
        return "".join(u["content"]["text"] for u in self.updates("agent_message_chunk"))

    async def response(self, id_, timeout=10.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m in self.msgs:
                if m.get("id") == id_ and ("result" in m or "error" in m):
                    return m
            await asyncio.sleep(0.01)
        raise AssertionError(f"no response {id_}")


def req(agent: RavenAcpAgent, id_, method, params):
    agent.conn.feed(json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params}))


def test_acp_agent_turn_run_turn_and_cancel(tmp_path, monkeypatch):
    raven = make_raven(tmp_path, monkeypatch)
    monkeypatch.setenv("HAI_MODEL", "holo4-27b")  # LocalBackend writes HAI_MODEL; restore after

    async def scenario():
        raven.store.bind_loop(asyncio.get_running_loop())
        client = FakeClient()
        acp = RavenAcpAgent(client.write, backend=LocalBackend(raven))
        req(acp, 1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        await client.response(1)
        req(acp, 2, "session/new", {"cwd": "/", "mcpServers": []})
        sid = (await client.response(2))["result"]["sessionId"]

        # --- single agent turn
        req(acp, 3, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "on linux: open a terminal"}]})
        assert (await client.response(3))["result"]["stopReason"] == "end_turn"
        calls = client.updates("tool_call")
        assert any(c["title"] == "[linux] click (1, 2)" and c["status"] == "in_progress" for c in calls)
        imgs = [
            blk["content"]
            for u in client.updates("tool_call_update")
            for blk in (u.get("content") or [])
            if blk["content"]["type"] == "image"
        ]
        assert imgs and imgs[0]["mimeType"] == "image/png" and imgs[0]["data"]
        assert "PASSED" in client.text()

        # --- run turn that fails -> report
        client.msgs.clear()
        req(acp, 4, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "run demo-interop on linux, macos"}]})
        assert (await client.response(4))["result"]["stopReason"] == "end_turn"
        text = client.text()
        assert "Run failed" in text and "FAILED" in text and "Raven report" in text
        assert client.updates("plan")

        # --- cancel
        client.msgs.clear()
        req(acp, 5, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "on linux: wait forever"}]})
        deadline = time.time() + 5
        while not client.updates("tool_call") and time.time() < deadline:
            await asyncio.sleep(0.01)
        acp.conn.feed(json.dumps({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}}))
        assert (await client.response(5))["result"]["stopReason"] == "cancelled"
        agent_id = next(a for a in raven.store.agents.values() if a.task == "wait forever").id
        deadline = time.time() + 5
        while raven.store.agents[agent_id].status != "stopped" and time.time() < deadline:
            await asyncio.sleep(0.01)
        assert raven.store.agents[agent_id].status == "stopped"
        await acp.close()
        await raven.shutdown()

    asyncio.run(scenario())


def test_prompt_text_strips_client_instruction_blocks():
    blocks = [{"type": "text", "text": "<t3_code_instructions>\n## Mode: Default\nBe nice.\n</t3_code_instructions>\n\nhelp"}]
    assert RavenAcpAgent.prompt_text(blocks) == "help"
    t3 = (
        "<t3_code_instructions>\nmode\n</t3_code_instructions>\n\n<user_request>\non linux: open a terminal\n</user_request>"
        "\n<runtime_info>you are running in T3 Code</runtime_info>"
    )
    assert RavenAcpAgent.prompt_text([{"type": "text", "text": t3}]) == "on linux: open a terminal"


def test_action_titles():
    assert action_title("[linux]", "click", {"x": 412, "y": 88}) == ("[linux] click (412, 88)", "other")
    assert action_title("[macos]", "shell", {"cmd": "uname -a"}) == ("[macos] $ uname -a", "execute")
    assert action_title("[win]", "type_text", {"text": "hi"})[0] == '[win] type "hi"'
    assert action_title("[win]", "key", {"combo": "cmd+space"})[0] == "[win] key cmd+space"
