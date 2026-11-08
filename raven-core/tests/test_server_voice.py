"""POST /voice/session against a mocked Gladia + intent parser unit tests."""

from __future__ import annotations

import json
import sys

import httpx
from fastapi.testclient import TestClient

import raven_core.integrations as integrations
from raven_core.events import Platform
from raven_core.server import Raven, create_app
from raven_core.server.config import Settings
from raven_core.server.intents import match_spec, parse_command, parse_platforms, task_platforms
from raven_core.store import Store


def make_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.delenv("RAVEN_TOKEN", raising=False)
    monkeypatch.setitem(sys.modules, "raven_core.specs", None)
    raven = Raven(settings=Settings(token=None, artifacts_dir=tmp_path / "a", specs_dir=tmp_path / "s"), store=Store())
    return TestClient(create_app(raven))


def test_voice_session_calls_gladia(tmp_path, monkeypatch):
    monkeypatch.setenv("GLADIA_API_KEY", "gl-test")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-gladia-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "sess-1", "url": "wss://api.gladia.io/v2/live?token=abc"})

    monkeypatch.setattr(integrations, "TRANSPORT", httpx.MockTransport(handler))
    with make_client(tmp_path, monkeypatch) as c:
        r = c.post("/voice/session")
    assert r.status_code == 200
    assert r.json() == {
        "id": "sess-1",
        "url": "wss://api.gladia.io/v2/live?token=abc",
        "sample_rate": 16000,
        "encoding": "wav/pcm",
        "bit_depth": 16,
        "channels": 1,
    }
    assert seen["url"] == "https://api.gladia.io/v2/live"
    assert seen["key"] == "gl-test"
    b = seen["body"]
    assert b["model"] == "solaria-1"
    assert (b["encoding"], b["sample_rate"], b["bit_depth"], b["channels"]) == ("wav/pcm", 16000, 16, 1)
    assert b["messages_config"]["receive_partial_transcripts"] is True
    assert b["language_config"] == {"languages": [], "code_switching": True}


def test_voice_session_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("GLADIA_API_KEY", raising=False)
    with make_client(tmp_path, monkeypatch) as c:
        assert c.post("/voice/session").status_code == 503


def test_voice_session_gladia_error(tmp_path, monkeypatch):
    monkeypatch.setenv("GLADIA_API_KEY", "gl-test")
    monkeypatch.setattr(
        integrations, "TRANSPORT", httpx.MockTransport(lambda req: httpx.Response(401, json={"message": "bad key"}))
    )
    with make_client(tmp_path, monkeypatch) as c:
        assert c.post("/voice/session").status_code == 502


SPECS = ["bitchat-geohash-interop", "login-smoke"]


def test_parse_run():
    i = parse_command("run bitchat-geohash-interop on linux,macos", SPECS)
    assert (i.kind, i.spec, i.platforms) == ("run", "bitchat-geohash-interop", [Platform.linux, Platform.macos])
    i = parse_command("Run the bitchat geohash interop test on Linux and Mac OS.", SPECS)
    assert (i.kind, i.spec, i.platforms) == ("run", "bitchat-geohash-interop", [Platform.linux, Platform.macos])
    i = parse_command("/run bitchat", SPECS)
    assert (i.kind, i.spec, i.platforms) == ("run", "bitchat-geohash-interop", None)
    i = parse_command("run login smoke on windows and iphone", SPECS)
    assert (i.spec, i.platforms) == ("login-smoke", [Platform.windows, Platform.ios])


def test_parse_agent_and_misc():
    i = parse_command("on linux: open a terminal and run uname -a", SPECS)
    assert (i.kind, i.platform, i.task) == ("agent", Platform.linux, "open a terminal and run uname -a")
    i = parse_command("start a windows agent to open notepad", SPECS)
    assert (i.kind, i.platform, i.task) == ("agent", Platform.windows, "open notepad")
    i = parse_command("macos: open bitchat", SPECS)
    assert (i.kind, i.platform) == ("agent", Platform.macos)
    assert parse_command("stop all", SPECS).kind == "stop_all"
    assert parse_command("Stop everything!", SPECS).kind == "stop_all"
    assert parse_command("stop", SPECS).kind == "stop_all"
    assert parse_command("specs", SPECS).kind == "specs"
    assert parse_command("status", SPECS).kind == "list"
    assert parse_command("hello there", SPECS).kind == "unknown"
    assert parse_platforms("linux, macOS and Windows 11") == [Platform.linux, Platform.macos, Platform.windows]
    assert task_platforms("open xcode and build the app for sinan's iphone") == [Platform.macos]
    assert task_platforms("open Device Hub and control my iPhone") == [Platform.macos]
    assert task_platforms("open settings on my iphone") == []
    assert task_platforms("on linux, check the iphone sync") == [Platform.linux]
    assert match_spec("geohash", SPECS) == "bitchat-geohash-interop"
    assert match_spec("zzz", SPECS) is None
