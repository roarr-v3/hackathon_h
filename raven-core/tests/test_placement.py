"""Bare metal vs VM: prompt keywords, the routing rule, get_worker, the service and the live view."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from raven_core import machines, placement, workers
from raven_core.events import CreateAgentRequest, Placement, Platform
from raven_core.server.config import Settings
from raven_core.server.intents import parse_command
from raven_core.server.service import Raven
from raven_core.store import Store


@pytest.fixture(autouse=True)
def mac_host(monkeypatch, tmp_path):
    monkeypatch.setattr(placement, "host_platform", lambda: Platform.macos)
    monkeypatch.setenv("RAVEN_ENV_FILE", str(tmp_path / "missing.env"))
    for k in ("RAVEN_FAKE_WORKERS", "RAVEN_PLACEMENT", "RAVEN_SHOW_VM"):
        monkeypatch.delenv(k, raising=False)
    workers.reset_workers()
    yield
    workers.reset_workers()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("on mac in a vm: open TextEdit", Placement.vm),
        ("run it in a sandbox please", Placement.vm),
        ("use a virtual machine", Placement.vm),
        ("use this mac open the youtube app", Placement.bare_metal),
        ("run it bare metal", Placement.bare_metal),
        ("on mac: open TextEdit", Placement.auto),
        ("", Placement.auto),
    ],
)
def test_requested_placement_from_prompt(text, expected):
    assert placement.requested(text) == expected


def test_resolve_prefers_bare_metal_for_this_os(monkeypatch):
    assert placement.resolve(Platform.macos) == Placement.bare_metal
    assert placement.resolve(Platform.ios) == Placement.bare_metal
    assert placement.resolve(Platform.linux) == Placement.vm
    assert placement.resolve(Platform.windows) == Placement.vm
    assert placement.resolve(Platform.macos, Placement.vm) == Placement.vm
    with pytest.raises(workers.WorkerUnavailable, match="can't run bare metal"):
        placement.resolve(Platform.linux, Placement.bare_metal)
    monkeypatch.setenv("RAVEN_PLACEMENT", "vm")
    assert placement.resolve(Platform.macos) == Placement.vm
    assert placement.resolve(Platform.macos, Placement.bare_metal) == Placement.bare_metal
    monkeypatch.setenv("RAVEN_PLACEMENT", "nonsense")
    assert placement.resolve(Platform.macos) == Placement.bare_metal


@pytest.mark.skipif(sys.platform != "darwin", reason="LocalMacWorker drives macOS")
def test_get_worker_routes_by_placement(monkeypatch):
    from raven_core.hosts import LocalHost
    from raven_core.workers.cua import CuaWorker
    from raven_core.workers.local_mac import LocalMacWorker

    monkeypatch.setenv("RAVEN_H_MODE", "models-loop")
    assert isinstance(workers.get_worker(Platform.macos), LocalMacWorker)
    assert isinstance(workers.get_worker(Platform.ios), LocalMacWorker)
    vm = workers.get_worker(Platform.macos, placement=Placement.vm)
    assert isinstance(vm, CuaWorker) and vm.name == "raven-mac"
    with pytest.raises(workers.WorkerUnavailable, match="no iOS VM"):
        workers.get_worker(Platform.ios, placement=Placement.vm)
    monkeypatch.setenv("RAVEN_H_MODE", "local-runtime")
    workers.reset_workers()
    assert isinstance(workers.get_worker(Platform.macos), LocalHost)


def test_intents_carry_placement_and_drop_the_phrase():
    i = parse_command("on mac in a vm: open TextEdit and type hello", [])
    assert (i.kind, i.platform, i.placement, i.task) == ("agent", Platform.macos, Placement.vm, "open TextEdit and type hello")
    assert parse_command("on mac: open TextEdit", []).placement == Placement.auto
    run = parse_command("run demo on macos in a vm", ["demo"])
    assert (run.kind, run.placement, run.platforms) == ("run", Placement.vm, [Platform.macos])


class ScreenWorker:
    bare_metal = True

    def __init__(self) -> None:
        self.name = "this-mac"
        self.preflights = 0
        self.begins = 0
        self.finishes = 0

    async def preflight(self) -> None:
        self.preflights += 1

    async def begin(self) -> None:
        self.begins += 1

    async def finish(self) -> None:
        self.finishes += 1


class VmWorker:
    def __init__(self, name: str) -> None:
        self.name = name
        self.target = "apple-container" if name == "raven-linux" else "lume"


def test_service_bare_metal_skips_vm_and_takes_turns(monkeypatch, tmp_path: Path):
    screen = ScreenWorker()
    vms = {Platform.linux: VmWorker("raven-linux"), Platform.macos: VmWorker("raven-mac")}
    ensured: list[Platform] = []
    shown: list[Platform] = []
    running: list[str] = []
    overlap: list[tuple[str, str]] = []

    async def fake_ensure(platform, say=None):
        ensured.append(Platform(platform))

    async def fake_show(platform, say=None):
        shown.append(Platform(platform))

    async def fake_run_agent(agent, worker, emit, inbox, stop, artifacts_dir, **kw):
        if worker is screen and any(r.startswith("screen") for r in running):
            overlap.append((agent.id, running[0]))
        tag = ("screen:" if worker is screen else "vm:") + agent.id
        running.append(tag)
        await asyncio.sleep(0.05)
        running.remove(tag)

    def get_worker(platform, placement=Placement.auto):
        return vms[platform] if Placement(placement) == Placement.vm or platform == Platform.linux else screen

    monkeypatch.setattr(machines, "ensure", fake_ensure)
    monkeypatch.setattr(machines, "show", fake_show)

    async def scenario() -> list:
        from test_server_api import fake_plan_task

        raven = Raven(settings=Settings(token=None, artifacts_dir=tmp_path / "a", specs_dir=tmp_path / "s"), store=Store())
        raven.get_worker = get_worker
        raven.run_agent = fake_run_agent
        raven.plan_task = fake_plan_task
        agents = [
            raven.start_agent(CreateAgentRequest(platform=Platform.macos, task="one")),
            raven.start_agent(CreateAgentRequest(platform=Platform.macos, task="two")),
            raven.start_agent(CreateAgentRequest(platform=Platform.linux, task="three")),
            raven.start_agent(CreateAgentRequest(platform=Platform.macos, task="four", placement=Placement.vm)),
        ]
        await asyncio.gather(*(raven.store.handles[a.id].task for a in agents))
        return agents

    agents = asyncio.run(scenario())
    assert [a.placement for a in agents] == [Placement.bare_metal, Placement.bare_metal, Placement.vm, Placement.vm]
    assert (screen.preflights, screen.begins, screen.finishes) == (2, 2, 2)
    assert overlap == []
    assert sorted(ensured) == sorted([Platform.linux, Platform.macos])
    assert sorted(shown) == sorted([Platform.linux, Platform.macos])


def test_show_opens_the_viewer_once(monkeypatch):
    calls: list[tuple[str, ...]] = []
    opened: list[str] = []
    said: list[str] = []

    async def fake_run(*argv, timeout=900):
        calls.append(argv)
        return 0, '{"expires_at_unix": 1, "name": "raven-mac", "url": "http://192.168.64.2:3211/viewer/#ticket=t"}'

    async def say(text: str) -> None:
        said.append(text)

    monkeypatch.setattr(machines, "_run", fake_run)
    monkeypatch.setattr(machines, "_which", lambda name: name)
    monkeypatch.setattr(machines, "_shown", {})
    monkeypatch.setattr(machines.webbrowser, "open", lambda url: opened.append(url) or True)

    async def scenario() -> list:
        first = await machines.show(Platform.macos, say)
        again = await machines.show(Platform.macos, say)
        windows = await machines.show(Platform.windows, say)
        return [first, again, windows]

    first, again, windows = asyncio.run(scenario())
    assert first == "http://192.168.64.2:3211/viewer/#ticket=t" and again is None and windows is None
    assert opened == [first]
    assert calls[0][1:] == ("--json", "sb", "view", "local:raven-mac", "--view-only", "--no-open", "--ttl", "8h")
    assert all("ticket=" not in message for message in said)
    assert said and "raven-mac" in said[0]

    monkeypatch.setattr(machines, "_shown", {})
    monkeypatch.setenv("RAVEN_SHOW_VM", "0")
    assert asyncio.run(machines.show(Platform.linux, say)) is None
    assert opened == [first]
