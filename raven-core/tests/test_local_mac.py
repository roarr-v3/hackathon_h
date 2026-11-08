"""Bare-metal Mac input (layout-aware shortcuts, Unicode typing), tool lookup and ACP follow-ups."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven_core import machines
from raven_core.acp.agent import EARLIER_TASK, RavenAcpAgent, Session
from raven_core.events import Platform
from raven_core.server.intents import parse_command
from raven_core.workers.local_mac import NAMED_KEYCODES, US_KEYCODES, resolve_combo, typing_plan

AZERTY = {"q": 0, "s": 1, "d": 2, "f": 3, "z": 6, "x": 7, "c": 8, "v": 9, "a": 12, "w": 13, ",": 46, "m": 41}


def test_shortcuts_follow_the_keyboard_layout():
    assert resolve_combo("cmd+a", AZERTY) == (["cmd"], 12)
    assert resolve_combo("cmd+q", AZERTY) == (["cmd"], 0)
    assert resolve_combo("Command + W", AZERTY) == (["cmd"], 13)
    assert resolve_combo("cmd+a", {}) == (["cmd"], US_KEYCODES["a"])
    assert resolve_combo("ctrl+shift+t", {}) == (["ctrl", "shift"], US_KEYCODES["t"])
    assert resolve_combo("Return", AZERTY) == ([], NAMED_KEYCODES["enter"])
    assert resolve_combo("cmd", AZERTY) == (["cmd"], None)
    with pytest.raises(ValueError):
        resolve_combo("cmd+launchpad", AZERTY)


def test_typing_plan_sends_line_breaks_and_tabs_as_keys():
    assert typing_plan("héllo\nwörld\t!") == [
        ("text", "héllo"), ("key", "enter"), ("text", "wörld"), ("key", "tab"), ("text", "!"),
    ]
    assert typing_plan("") == []


def test_tools_never_come_from_raven_cores_venv(monkeypatch, tmp_path: Path):
    venv, system = tmp_path / "venv", tmp_path / "system"
    for d in (venv / "bin", system):
        d.mkdir(parents=True)
        (d / "cua").write_text("#!/bin/sh\n")
        (d / "cua").chmod(0o755)
    monkeypatch.setattr(machines.sys, "prefix", str(venv))
    monkeypatch.setattr(machines, "EXTRA_PATH", [])
    monkeypatch.setenv("PATH", f"{venv / 'bin'}:{system}")
    assert machines._which("cua") == str(system / "cua")


def test_follow_ups_stay_on_the_same_machine(monkeypatch):
    monkeypatch.setattr("raven_core.acp.agent.host_platform", lambda: Platform.macos)
    monkeypatch.delenv("RAVEN_DEFAULT_PLATFORM", raising=False)
    s = Session(id="s")

    def ask(text: str) -> tuple[str, str, str]:
        req = RavenAcpAgent.agent_request(s, text, parse_command(text, []))
        s.last = req
        return req

    assert ask("open TextEdit and type bonjour") == ("macos", "open TextEdit and type bonjour", "auto")
    assert ask("retry") == ("macos", "open TextEdit and type bonjour", "auto")
    platform, task, placement = ask("careful, the keyboard is AZERTY")
    assert (platform, placement) == ("macos", "auto")
    assert task == f"careful, the keyboard is AZERTY{EARLIER_TASK}open TextEdit and type bonjour)"
    assert ask("now type merci")[1] == f"now type merci{EARLIER_TASK}open TextEdit and type bonjour)"
    assert ask("same thing but in a vm")[2] == "vm"
    assert ask("on linux: open a terminal") == ("linux", "open a terminal", "auto")
