"""Hermetic execution routing tests. No machine, key store, model or API is contacted."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import types

import pytest

from raven_core import machines, placement, workers
from raven_core.events import ExecutionProfile, HMode, Placement, Platform
from raven_core.hosts import LocalHost
from raven_core.hosts import raven_host
from raven_core.hosts.capabilities import CapabilityCategory as Category, CapabilityError
from raven_core.hosts.cua import CuaHost
from raven_core.hosts.devin import DevinHost
from raven_core.hosts.remote import NativeRemoteWorker
from raven_core.workers.cua import CuaWorker


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("RAVEN_ENV_FILE", str(tmp_path / "does-not-exist.env"))
    monkeypatch.setenv("HAI_API_KEY", "fake-h-key-never-sent")
    for key in (
        "RAVEN_FAKE_WORKERS", "RAVEN_PLACEMENT", "RAVEN_H_MODE", "RAVEN_HOST_LINUX", "RAVEN_HOST_WINDOWS",
        "RAVEN_LINUX_SANDBOX", "RAVEN_MACOS_SANDBOX", "WINDOWS_WORKER_HOST", "DEVIN_API_KEY", "DEVIN_ORG_ID",
        "RAVEN_DEVIN_LINUX_SESSION_ID", "RAVEN_DEVIN_WINDOWS_SESSION_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(placement, "host_platform", lambda: Platform.macos)
    monkeypatch.setattr(machines, "_locks", {})
    monkeypatch.setattr(machines, "_last_used", {})
    monkeypatch.setattr(machines, "_managed", set())
    workers.reset_workers()

    async def no_process(*args, **kwargs):
        raise AssertionError(f"unmocked subprocess: {args[0] if args else '?'}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_process)
    yield
    workers.reset_workers()


def test_profile_defaults_ignore_legacy_globals_and_force_native_mac(monkeypatch):
    monkeypatch.setenv("RAVEN_PLACEMENT", "vm")
    monkeypatch.setenv("RAVEN_H_MODE", "models-loop")
    monkeypatch.setenv("RAVEN_HOST_WINDOWS", "home-pc")
    monkeypatch.setenv("RAVEN_LINUX_SANDBOX", "old-non-apple-space")
    profile = ExecutionProfile()
    mac = workers.get_worker(Platform.macos, placement=Placement.vm, execution=profile)
    ios = workers.get_worker(Platform.ios, execution=profile)
    linux = workers.get_worker(Platform.linux, execution=profile)
    windows = workers.get_worker(Platform.windows, execution=profile)
    assert isinstance(mac, LocalHost) and mac.bare_metal
    assert isinstance(ios, LocalHost) and ios.bare_metal
    assert mac.default_mode == "local-runtime"
    assert isinstance(linux, CuaHost) and linux.name == "raven-linux-apple"
    assert linux.default_mode == "agents-api"
    assert isinstance(windows, DevinHost) and windows.platform == Platform.windows
    assert not windows.session_id


def test_complete_profile_cache_and_legacy_mode_changes(monkeypatch):
    base = ExecutionProfile()
    one = workers.get_worker(Platform.linux, execution=base)
    equivalent = ExecutionProfile(platform_modes={})
    assert workers.get_worker(Platform.linux, execution=equivalent) is one
    # Each field can affect a run; unrelated platform overrides must not leak either.
    profiles = [
        ExecutionProfile(planner="cloud"),
        ExecutionProfile(linux_fallback=False),
        ExecutionProfile(linux_space="other-apple"),
        ExecutionProfile(platform_modes={Platform.windows: HMode.local_runtime}),
        ExecutionProfile(ios_device_id="some-device"),
    ]
    assert all(workers.get_worker(Platform.linux, execution=p) is not one for p in profiles)
    monkeypatch.setenv("RAVEN_H_MODE", "agents-api")
    old = workers.get_worker(Platform.linux)
    monkeypatch.setenv("RAVEN_H_MODE", "models-loop")
    changed = workers.get_worker(Platform.linux)
    assert isinstance(old, CuaHost) and isinstance(changed, CuaWorker) and old is not changed


def test_nested_profile_mutation_cannot_reuse_wrong_worker():
    profile = ExecutionProfile()
    before = workers.get_worker(Platform.linux, execution=profile)
    profile.platform_modes[Platform.linux] = HMode.models_loop
    after = workers.get_worker(Platform.linux, execution=profile)
    assert before is not after and isinstance(after, CuaWorker)
    with pytest.raises(CapabilityError, match="another execution profile"):
        asyncio.run(workers.prepare_worker(before, execution=profile))


def test_windows_only_uses_devin_and_rejects_removed_targets(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv("WINDOWS_WORKER_HOST", "192.0.2.10")
    assert isinstance(workers.get_worker(Platform.windows, execution=ExecutionProfile()), DevinHost)
    with pytest.raises(ValidationError):
        ExecutionProfile(windows_target="remote-pc")
    profile = ExecutionProfile(platform_modes={Platform.windows: HMode.models_loop})
    worker = workers.get_worker(Platform.windows, execution=profile)
    assert isinstance(worker, NativeRemoteWorker) and isinstance(worker.host, DevinHost)


def test_explicit_devin_models_mode_returns_native_worker_not_host():
    profile = ExecutionProfile(linux_target="devin-vm", h_mode=HMode.models_loop)
    worker = workers.get_worker(Platform.linux, execution=profile)
    assert isinstance(worker, NativeRemoteWorker)
    assert worker.host.default_mode == "models-loop"


def test_no_profile_windows_preserves_eager_unavailable_without_remote_pc_fallback(monkeypatch):
    with pytest.raises(workers.WorkerUnavailable, match="Devin v3 VM needs"):
        workers.get_worker(Platform.windows)
    monkeypatch.setenv("WINDOWS_WORKER_HOST", "192.0.2.10")
    with pytest.raises(workers.WorkerUnavailable, match="Devin v3 VM needs"):
        workers.get_worker(Platform.windows)
    # Explicit execution profiles are lazy, and surface prerequisites via inspection/preflight.
    assert isinstance(workers.get_worker(Platform.windows, execution=ExecutionProfile()), DevinHost)


class PreparedWorker:
    platform = Platform.linux
    name = "devin-linux-test"
    target = "devin-vm"

    def __init__(self):
        self.calls = []

    async def prepare(self):
        self.calls.append("prepare")
        await asyncio.sleep(0)

    async def shell(self, command, timeout=60):
        self.calls.append(("setup", command))
        return 0, "", ""

    async def execute(self):
        self.calls.append("H executes on the same worker")


@pytest.mark.parametrize("category", [Category.unsupported_runtime, Category.desktop_unavailable])
def test_one_announced_fallback_and_same_worker_for_setup_and_execution(monkeypatch, category):
    profile = ExecutionProfile()
    original = workers.get_worker(Platform.linux, execution=profile)
    fallback = PreparedWorker()
    attempts = []
    messages = []

    async def blocked():
        attempts.append("Apple preflight")
        raise CapabilityError(category, "a local capability is unavailable")

    async def say(text):
        messages.append(text)

    def make_fallback(*args):
        attempts.append("allocate one fallback")
        return fallback

    monkeypatch.setattr(original, "prepare", blocked)
    monkeypatch.setattr(workers, "_devin_worker", make_fallback)

    async def scenario():
        a, b = await asyncio.gather(
            workers.prepare_worker(original, execution=profile, say=say),
            workers.prepare_worker(original, execution=profile, say=say),
        )
        assert a is b is fallback
        assert workers.get_worker(Platform.linux, execution=profile) is fallback
        assert await workers.prepare_worker(fallback, execution=profile, say=say) is fallback
        await a.shell("install the app under test")
        await b.execute()

    asyncio.run(scenario())
    assert attempts == ["Apple preflight", "allocate one fallback"]
    assert fallback.calls == ["prepare", ("setup", "install the app under test"), "H executes on the same worker"]
    assert sum("Falling back once" in text for text in messages) == 1


@pytest.mark.parametrize("failure", [
    CapabilityError(Category.authentication, "H auth refused"),
    CapabilityError(Category.access_denied, "H quota"),
    CapabilityError(Category.configuration, "wrong local runtime"),
    CapabilityError(Category.dependency, "package missing"),
    CapabilityError(Category.protocol, "invalid probe"),
    CapabilityError(Category.transport, "H endpoint connection"),
    CapabilityError(Category.timeout, "H model took too long"),
    TimeoutError("H model latency"),
    RuntimeError("VERDICT: FAILED application assertion"),
])
def test_non_capability_errors_never_switch_targets(monkeypatch, failure):
    profile = ExecutionProfile()
    worker = workers.get_worker(Platform.linux, execution=profile)

    async def fail():
        raise failure

    monkeypatch.setattr(worker, "prepare", fail)
    monkeypatch.setattr(workers, "_devin_worker", lambda *a: pytest.fail("must not provision fallback"))
    with pytest.raises(type(failure)):
        asyncio.run(workers.prepare_worker(worker, execution=profile))


def test_disabled_fallback_and_missing_h_key_never_allocate(monkeypatch):
    profile = ExecutionProfile(linux_fallback=False)
    worker = workers.get_worker(Platform.linux, execution=profile)

    async def fail():
        raise CapabilityError(Category.desktop_unavailable, "not running")

    monkeypatch.setattr(worker, "prepare", fail)
    monkeypatch.setattr(workers, "_devin_worker", lambda *a: pytest.fail("fallback disabled"))
    with pytest.raises(CapabilityError, match="not running"):
        asyncio.run(workers.prepare_worker(worker, execution=profile))
    monkeypatch.delenv("HAI_API_KEY")
    with pytest.raises(CapabilityError) as error:
        asyncio.run(workers.prepare_worker(worker, execution=ExecutionProfile()))
    assert error.value.category == Category.configuration  # worker profile mismatch is rejected first
    other = workers.get_worker(Platform.linux, execution=ExecutionProfile())
    monkeypatch.setattr(other, "prepare", lambda: pytest.fail("missing H key must be caught before preflight"))
    with pytest.raises(CapabilityError) as error:
        asyncio.run(workers.prepare_worker(other, execution=ExecutionProfile()))
    assert error.value.category == Category.authentication


def test_failed_fallback_is_not_retried_or_recursively_resolved(monkeypatch):
    profile = ExecutionProfile()
    worker = workers.get_worker(Platform.linux, execution=profile)
    attempts = []

    async def fail_apple():
        raise CapabilityError(Category.desktop_unavailable, "no display")

    class FailedFallback(PreparedWorker):
        async def prepare(self):
            attempts.append("once")
            raise CapabilityError(Category.access_denied, "Devin organization refused")

    monkeypatch.setattr(worker, "prepare", fail_apple)
    monkeypatch.setattr(workers, "_devin_worker", lambda *args: FailedFallback())
    for _ in range(2):
        with pytest.raises(CapabilityError, match="organization refused"):
            asyncio.run(workers.prepare_worker(worker, execution=profile))
    assert attempts == ["once"]


@pytest.mark.parametrize("mode,unsupported", [(HMode.local_runtime, True), (HMode.agents_api, False), (HMode.models_loop, False)])
def test_linux_arm64_unsupported_only_for_selected_local_h_runtime(monkeypatch, mode, unsupported):
    calls = []

    async def ensure(*args, **kwargs):
        calls.append("ensure existing Apple")

    async def cli(*args, **kwargs):
        calls.append(args)
        return 0, "--stdin --user --cwd"

    host = CuaHost(Platform.linux, "raven-linux-apple", mode=mode)

    async def capture(argv, **kwargs):
        assert argv == ["uname", "-m"]
        return 0, b"aarch64\n", b""

    async def bootstrap():
        calls.append("bootstrap")

    async def probe():
        calls.append("probe without a model request")

    monkeypatch.setattr(machines, "ensure_space", ensure)
    monkeypatch.setattr(machines, "_which", lambda name: name)
    monkeypatch.setattr(machines, "_run", cli)
    monkeypatch.setattr(host, "_capture", capture)
    monkeypatch.setattr(host, "_bootstrap", bootstrap)
    monkeypatch.setattr(host, "_probe", probe)
    if unsupported:
        with pytest.raises(CapabilityError) as error:
            asyncio.run(host.prepare())
        assert error.value.category == Category.unsupported_runtime
        assert "bootstrap" not in calls
    else:
        asyncio.run(host.prepare())
        assert host._ready and "bootstrap" in calls


def apple_row(**changes):
    return {"name": "raven-linux-apple", "state": "running", "status": "ready",
            "runtime": "containerization", "kind": "microvm", "location": "local", **changes}


def test_explicit_creation_uses_apple_runtime_no_docker_or_colima(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("RAVEN_MANAGE_MACHINES", "1")
    monkeypatch.setattr(machines, "_which", lambda name: name)
    commands = []
    existing = False

    async def cli(*args, **kwargs):
        nonlocal existing
        commands.append(args)
        if "create" in args:
            existing = True
            return 0, "{}"
        assert args[1:] == ("--json", "sb", "ls", "--local", "--no-size")
        return 0, json.dumps([apple_row()] if existing else [])

    monkeypatch.setattr(machines, "_run", cli)
    asyncio.run(machines.ensure_space(Platform.linux, "raven-linux-apple", create=True))
    create = next(cmd for cmd in commands if "create" in cmd)
    for flag, expected in (("--runtime", "containerization"), ("--kind", "microvm"), ("--on", "local")):
        assert create[create.index(flag) + 1] == expected
    assert "--keep-on-failure" in create and "--wait" in create
    assert all(cmd[0] == "cua" and not any(s in cmd for s in ("docker", "colima", "rm", "delete")) for cmd in commands)


@pytest.mark.parametrize("case", ["authentication", "runtime-mismatch", "missing"])
def test_failed_connection_or_wrong_runtime_never_recreates(monkeypatch, case):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("RAVEN_MANAGE_MACHINES", "1")
    monkeypatch.setattr(machines, "_which", lambda name: name)
    calls = []

    async def cli(*args, **kwargs):
        calls.append(args)
        if case == "authentication":
            return 1, "401 unauthorized fake-secret-must-not-leak"
        if case == "runtime-mismatch":
            return 0, json.dumps([apple_row(runtime="runc", kind="container")])
        return 0, "[]"

    monkeypatch.setattr(machines, "_run", cli)
    with pytest.raises(CapabilityError) as error:
        asyncio.run(machines.ensure_space(Platform.linux, "raven-linux-apple", create=case != "missing"))
    assert "fake-secret" not in str(error.value)
    assert not any("create" in cmd or "resume" in cmd for cmd in calls)
    expected = {"authentication": Category.authentication, "runtime-mismatch": Category.configuration, "missing": Category.desktop_unavailable}
    assert error.value.category == expected[case]


def test_preexisting_running_space_is_not_owned_for_idle_suspension(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(machines, "_which", lambda name: name)

    async def metadata(name):
        return apple_row()

    monkeypatch.setattr(machines, "space_info", metadata)
    asyncio.run(machines.ensure_space(Platform.linux, "raven-linux-apple"))
    assert not machines._managed


def test_cua_models_connects_existing_local_space_never_sdk_create(monkeypatch):
    from raven_core.workers import cua as cua_module
    from raven_core.workers.fake import make_png

    calls = []

    class Sandbox:
        @staticmethod
        async def connect(name, **kwargs):
            calls.append((name, kwargs))

            async def screenshot():
                return make_png(1, 1, (0, 0, 0))

            return types.SimpleNamespace(screenshot=screenshot)

        @staticmethod
        def create(*args, **kwargs):
            pytest.fail("connect must never create a sandbox")

    async def ensure(platform, name):
        assert (platform, name) == (Platform.linux, "raven-linux-apple")

    monkeypatch.setitem(sys.modules, "cua_sandbox", types.SimpleNamespace(Sandbox=Sandbox))
    monkeypatch.setattr(cua_module, "_patch_cua_embedded", lambda: None)
    monkeypatch.setattr(machines, "ensure_space", ensure)
    worker = CuaWorker(Platform.linux, "raven-linux-apple")
    asyncio.run(worker.prepare())
    assert calls == [("local:raven-linux-apple", {"local": True})]


class Input:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed


def test_cua_streams_credentials_on_stdin_not_argv(monkeypatch):
    host = CuaHost(Platform.linux, "raven-linux-apple")
    host._cli, host._python, host._script, host._ready = "cua", "/python", "/home/cua/.cache/raven/raven_host.py", True
    proc = types.SimpleNamespace(stdin=Input(), returncode=None)
    # SimpleNamespace is unhashable; the process set needs a process-like object.
    proc = type("Process", (), {"stdin": proc.stdin, "returncode": None})()
    calls = []

    async def start(argv):
        calls.append(host._argv(argv))
        return proc

    monkeypatch.setattr(host, "_start", start)
    assert asyncio.run(host.spawn(["--task", "check screen", "--mode", "agents-api"])) is proc
    argv = calls[0]
    assert argv[:4] == ["cua", "sb", "exec", "--stdin"]
    assert "local:raven-linux-apple" in argv and "--env-stdin" in argv
    assert "fake-h-key-never-sent" not in " ".join(argv)
    assert json.loads(proc.stdin.data)["HAI_API_KEY"] == "fake-h-key-never-sent"


def test_inspection_is_read_only_truthful_and_secret_free(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(machines, "_which", lambda name: name)

    async def metadata(name):
        return apple_row()

    async def devices():
        return [{"id": "sim-one", "kind": "simulator", "available": True}]

    from raven_core.workers import local_mac
    monkeypatch.setattr(local_mac, "missing_mac_permissions", lambda prompt=False: [])
    monkeypatch.setattr(machines, "space_info", metadata)
    monkeypatch.setattr(machines, "ensure_space", lambda *a, **k: pytest.fail("inspection may not prepare"))
    monkeypatch.setattr(workers, "get_worker", lambda *a, **k: pytest.fail("inspection may not resolve/provision"))
    monkeypatch.setitem(sys.modules, "raven_core.workers.device_hub", types.SimpleNamespace(list_devices=devices))
    result = asyncio.run(workers.inspect_targets(ExecutionProfile()))
    by_platform = {row["platform"]: row for row in result}
    assert set(by_platform) == {p.value for p in Platform}
    assert by_platform["linux"]["status"] == "unchecked"  # running is not a successful desktop/H preflight
    assert by_platform["windows"]["status"] == "blocked"
    assert by_platform["macos"]["status"] == "unchecked"
    assert "fake-h-key" not in json.dumps(result)


def test_devicehub_is_lazy_and_prepared_once_on_the_selected_worker(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    profile = ExecutionProfile()
    calls = []

    async def device(execution):
        calls.append(("device", execution))
        return {"id": "fixed-sim", "kind": "simulator", "available": True}

    monkeypatch.setitem(sys.modules, "raven_core.workers.device_hub", types.SimpleNamespace(prepare_device=device))
    worker = workers.get_worker(Platform.ios, execution=profile)

    async def prepare():
        calls.append("native H preflight")

    monkeypatch.setattr(worker, "prepare", prepare)

    async def scenario():
        assert await workers.prepare_worker(worker, execution=profile) is worker
        assert await workers.prepare_worker(worker, execution=profile) is worker

    asyncio.run(scenario())
    assert calls == [("device", profile), "native H preflight"]
    assert worker.device["id"] == "fixed-sim"


@pytest.mark.parametrize("mode", ["local-runtime", "agents-api"])
def test_official_h_modes_bind_inline_spec_to_this_desktop(monkeypatch, tmp_path, mode):
    starts = []
    closes = []
    spec = {"name": "h/holo-desktop", "environments": [{"kind": "desktop", "id": "desktop", "host": "cloud", "session_id": "wrong-machine"}]}

    class Handle:
        id = "session"

        def stream(self, **kwargs):
            return iter([])

        def wait_for_completion(self):
            return {"status": "completed", "answer": "VERDICT: PASSED", "outcome": "success"}

        def cancel(self):
            closes.append("cancel")

    class Client:
        def __init__(self, **kwargs):
            self.agents = types.SimpleNamespace(get_agent=lambda name, resolve: types.SimpleNamespace(model_dump=lambda **kw: spec))
            self.sessions = types.SimpleNamespace(bridged_kinds=lambda sid: ["desktop"])

        @classmethod
        def local(cls):
            return cls()

        def start_session(self, **kwargs):
            starts.append(kwargs)
            return Handle()

        def close(self):
            closes.append("client")

    monkeypatch.setitem(sys.modules, "hai_agents", types.SimpleNamespace(Client=Client))
    monkeypatch.setattr(raven_host.threading, "Thread", lambda **kw: types.SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(raven_host.signal, "signal", lambda *args: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    rc = raven_host.main(["--task", "test", "--mode", mode, "--shots-dir", str(tmp_path), "--no-cancel-on-eof"])
    assert rc == 0 and len(starts) == 1
    env = starts[0]["agent"]["environments"][0]
    assert env["host"] == "user_device" and "session_id" not in env
    assert spec["environments"][0]["host"] == "cloud"  # no mutation of a shared catalog spec
    assert "cancel" in closes


def test_unknown_h_spec_cannot_select_an_unrelated_cloud_environment():
    with pytest.raises(ValueError):
        raven_host.bound_desktop_spec({"environments": ["unresolved-name"]})
    with pytest.raises(ValueError):
        raven_host.bound_desktop_spec({"environments": [{"kind": "web", "host": "cloud"}]})


@pytest.mark.parametrize("cancelled", [False, True])
def test_preserves_dirty_host_subprocess_reaping_on_timeout_and_cancel(monkeypatch, cancelled):
    from raven_core import hosts

    class Process:
        returncode = None
        killed = False
        waited = False

        async def communicate(self):
            raise asyncio.CancelledError if cancelled else TimeoutError

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return -9

    proc = Process()

    async def spawn(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(asyncio.CancelledError if cancelled else TimeoutError):
        asyncio.run(hosts._run("mock"))
    assert proc.killed and proc.waited
