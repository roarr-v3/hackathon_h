"""Documented Devin v3 + CLI SSH adapter tests, entirely mocked and unpaid."""

from __future__ import annotations

import asyncio
import json
import shlex
import traceback

import httpx
import pytest

from raven_core import machines
from raven_core.events import Platform
from raven_core.hosts.capabilities import CapabilityCategory as Category, CapabilityError
from raven_core.hosts.devin import DevinHost, DevinSessionAPI, inspect_devin
from raven_core.hosts.remote import NativeRemoteWorker


@pytest.fixture(autouse=True)
def no_live_resources(monkeypatch, tmp_path):
    monkeypatch.setenv("RAVEN_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("DEVIN_API_KEY", "fake-devin-secret")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-test")
    monkeypatch.setenv("HAI_API_KEY", "fake-hai-secret")
    for key in ("DEVIN_API_BASE", "RAVEN_DEVIN_MAX_ACU", "RAVEN_DEVIN_LINUX_SESSION_ID", "RAVEN_DEVIN_WINDOWS_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(machines, "_which", lambda name: f"/mock/{name}")

    async def no_process(*args, **kwargs):
        raise AssertionError("unexpected live subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_process)


@pytest.mark.parametrize("platform", [Platform.linux, Platform.windows])
def test_api_uses_documented_v3_platform_lifecycle_and_header_auth(monkeypatch, platform):
    requests = []
    monkeypatch.setenv("DEVIN_API_BASE", "https://api.devin.ai/v1")

    def http(request):
        requests.append(request)
        return httpx.Response(200, json={"session_id": "devin-test", "status": "running"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
            api = DevinSessionAPI(client=client)
            await api.create(platform)
            await api.get("devin-test")
            await api.archive("devin-test")

    asyncio.run(scenario())
    assert [(r.method, r.url.path) for r in requests] == [
        ("POST", "/v3/organizations/org-test/sessions"),
        ("GET", "/v3/organizations/org-test/sessions/devin-test"),
        ("POST", "/v3/organizations/org-test/sessions/devin-test/archive"),
    ]
    payload = json.loads(requests[0].content)
    assert payload["platform"] == platform.value and payload["max_acu_limit"] == 2
    assert payload["resumable"] is True
    assert "Do not perform computer use" in payload["prompt"]
    assert all(r.headers["Authorization"] == "Bearer fake-devin-secret" for r in requests)
    assert all("fake-devin-secret" not in str(r.url) and "fake-devin-secret" not in r.content.decode() for r in requests)
    assert not any(r.method == "DELETE" for r in requests)


@pytest.mark.parametrize("status,category", [
    (401, Category.authentication), (403, Category.access_denied),
    (400, Category.configuration), (422, Category.configuration),
    (429, Category.access_denied), (503, Category.transport),
])
def test_api_access_failures_are_typed_and_do_not_echo_response_secrets(status, category):
    calls = []

    def http(request):
        calls.append(request)
        return httpx.Response(status, json={"detail": "raw provider error fake-devin-secret"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
            await DevinSessionAPI(client=client).create(Platform.windows)

    with pytest.raises(CapabilityError) as error:
        asyncio.run(scenario())
    assert error.value.category == category
    assert not error.value.allows_linux_fallback
    assert "fake-devin-secret" not in str(error.value) and len(calls) == 1


def test_inspection_never_confuses_configuration_with_a_working_vm(monkeypatch):
    monkeypatch.delenv("DEVIN_ORG_ID")
    assert inspect_devin(Platform.windows)["status"] == "blocked"
    monkeypatch.setenv("DEVIN_ORG_ID", "org-test")
    row = inspect_devin(Platform.windows)
    assert row["status"] == "unchecked" and "limited-access" in row["detail"]
    assert "fake-devin-secret" not in json.dumps(row)


class API:
    def __init__(self, *, statuses=None, archive_error=None):
        self.calls = []
        self.statuses = list(statuses or ["running"])
        self.archive_error = archive_error

    def validate(self):
        self.calls.append("validate")

    async def create(self, platform):
        self.calls.append(("create", platform))
        return {"session_id": "devin-owned", "status": "new"}

    async def get(self, session_id):
        self.calls.append(("get", session_id))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"session_id": session_id, "status": status}

    async def archive(self, session_id):
        self.calls.append(("archive", session_id))
        if self.archive_error:
            raise self.archive_error
        return {"session_id": session_id, "status": "suspended"}


def mock_guest(monkeypatch, host, *, mock_auth=True):
    calls = []

    async def auth():
        host._cli = "/mock/devin"
        calls.append("authenticated CLI")

    async def bootstrap():
        host._python, host._script = "/guest/python", "/guest/raven_host.py"
        calls.append(("bootstrap", host.session_id))

    async def probe():
        calls.append(("desktop+H preflight", host.session_id))

    async def ssh_ready():
        calls.append(("SSH ready", host.session_id))

    if mock_auth:
        monkeypatch.setattr(host, "_check_cli", auth)
    monkeypatch.setattr(host, "_wait_for_ssh", ssh_ready)
    monkeypatch.setattr(host, "_bootstrap", bootstrap)
    monkeypatch.setattr(host, "_probe", probe)
    return calls


@pytest.mark.parametrize("platform", [Platform.linux, Platform.windows])
def test_vm_lifecycle_is_idempotent_and_cancel_is_non_destructive(monkeypatch, platform):
    api = API(statuses=["new", "claimed", "running"])
    host = DevinHost(platform, api=api, poll_interval=.01)
    calls = mock_guest(monkeypatch, host)

    async def scenario():
        await host.prepare()
        await host.prepare()
        assert host._ready and host.session_id == "devin-owned"
        await host.cancel()
        await host.close()

    asyncio.run(scenario())
    assert api.calls.count(("create", platform)) == 1
    assert api.calls.count(("archive", "devin-owned")) == 1
    assert calls == ["authenticated CLI", ("SSH ready", "devin-owned"), ("bootstrap", "devin-owned"), ("desktop+H preflight", "devin-owned")]
    assert host._closed and host._archived and not host._ready


def test_attached_session_is_never_created_archived_or_deleted(monkeypatch):
    api = API()
    host = DevinHost(Platform.windows, session_id="devin-existing", api=api)
    mock_guest(monkeypatch, host)

    async def scenario():
        await host.prepare()
        await host.cancel()

    asyncio.run(scenario())
    assert not any(isinstance(call, tuple) and call[0] in ("create", "archive") for call in api.calls)
    assert host.session_id == "devin-existing" and not host._owned


@pytest.mark.parametrize("platform", [Platform.linux, Platform.windows])
@pytest.mark.parametrize("rc,output", [
    pytest.param(1, "raw-auth-error fake-devin-secret private@example.invalid", id="nonzero-error"),
    pytest.param(1, "Logged in (via Devin).", id="nonzero-positive"),
    pytest.param(124, "timed out", id="timeout"),
    pytest.param(0, "Not logged in.\nRun `devin auth login` to authenticate.\n", id="zero-logged-out"),
    pytest.param(0, "Unauthenticated", id="zero-unauthenticated"),
    pytest.param(0, "Not authenticated", id="zero-not-authenticated"),
    pytest.param(0, "", id="empty"),
    pytest.param(0, "Authentication status: unknown", id="unknown"),
    pytest.param(0, '{"authenticated": true}', id="undocumented-json"),
    pytest.param(0, None, id="malformed-type"),
    pytest.param(0, "Logged in (via ).", id="malformed-banner"),
    pytest.param(0, "Logged in (via Devin). trailing garbage", id="malformed-suffix"),
    pytest.param(0, "Logged in (via another-provider).", id="unknown-provider"),
    pytest.param(0, "Logged in (via Windsurf).", id="not-devin-cloud-login"),
    pytest.param(0, "Logged in (via Devin).\nNot logged in.", id="contradictory-status"),
    pytest.param(
        0, "Logged in (via Devin).\n  Failed to fetch from server: raw-auth-error fake-devin-secret private@example.invalid",
        id="stored-credentials-but-server-rejected",
    ),
    pytest.param(0, "Logged in (via Devin).\n  Unavailable: fake-devin-secret", id="lookup-unavailable"),
    pytest.param(0, "Logged in (via Devin).\n  Status: unknown", id="unknown-after-banner"),
    pytest.param(0, "Logged in (via Devin).\nsomething malformed", id="unknown-trailing-text"),
    pytest.param(0, "Logged in (via Devin).\nLogged in (via Devin).", id="duplicate-banner"),
    pytest.param(0, "Logged in (via Devin).\n  New status field: yes", id="unknown-metadata"),
    pytest.param(0, "Logged in (via Devin).\n  Email: value\x00", id="invalid-control-character"),
    pytest.param(0, "  Email: Logged in (via Devin).", id="banner-only-in-metadata"),
])
def test_unverified_cli_blocks_before_paid_creation(monkeypatch, caplog, capsys, platform, rc, output):
    api = API()
    host = DevinHost(platform, api=api)
    host._cli = "/mock/stale-cli"

    async def status(*args, **kwargs):
        assert args == ("/mock/devin", "auth", "status")
        assert kwargs == {"timeout": 20}
        assert api.calls == ["validate"]
        return rc, output

    monkeypatch.setattr(machines, "_run", status)
    with pytest.raises(CapabilityError) as error:
        asyncio.run(host.prepare())
    assert error.value.category == Category.authentication
    assert not error.value.allows_linux_fallback
    assert api.calls == ["validate"]
    assert host._cli is None and host.session_id is None
    assert not host._creation_attempted and not host._owned and not host._ready
    detail = "".join(traceback.format_exception(error.value))
    assert all(value not in detail for value in ("raw-auth-error", "fake-devin-secret", "private@example.invalid"))
    assert caplog.text == ""
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    with pytest.raises(CapabilityError) as ssh_error:
        host._ssh_argv(["true"])
    assert ssh_error.value.category == Category.configuration


@pytest.mark.parametrize("platform", [Platform.linux, Platform.windows])
@pytest.mark.parametrize("output", [
    pytest.param("Logged in (via Devin).\n", id="known-banner"),
    pytest.param(
        "Logged in (via devin).\r\n  User ID: test-user\r\n  Email: private@example.invalid\r\n",
        id="account-metadata-crlf",
    ),
    pytest.param(
        "\x1b[32mLogged in (via Devin).\x1b[0m\n  Email: private@example.invalid\n",
        id="styled-banner",
    ),
    pytest.param(
        "Credentials:\nLogged in (via Devin).\n"
        "  File: /mock/credentials.toml\n  Devin webapp: https://app.devin.ai\n"
        "  Devin API: https://api.devin.ai\n\nUser / team info:\n"
        "  User ID: test-user\n  Name: Unknown Error\n  Email: private@example.invalid\n"
        "Account:\n  Primary org: org-test\nTeam settings:\n  Sandbox: disabled\n  Disabled tools: \n",
        id="known-sections-and-metadata-not-status",
    ),
])
def test_known_authenticated_cli_allows_mocked_creation(monkeypatch, caplog, capsys, platform, output):
    api = API()
    host = DevinHost(platform, api=api)
    guest_calls = mock_guest(monkeypatch, host, mock_auth=False)

    async def status(*args, **kwargs):
        assert args == ("/mock/devin", "auth", "status")
        assert kwargs == {"timeout": 20}
        assert api.calls == ["validate"]
        return 0, output

    monkeypatch.setattr(machines, "_run", status)

    async def scenario():
        await host.prepare()
        assert host._ready and host._cli == "/mock/devin"
        await host.close()

    asyncio.run(scenario())
    assert api.calls == [
        "validate", ("create", platform), ("get", "devin-owned"), ("archive", "devin-owned"),
    ]
    assert guest_calls == [
        ("SSH ready", "devin-owned"), ("bootstrap", "devin-owned"), ("desktop+H preflight", "devin-owned"),
    ]
    assert caplog.text == ""
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("failure", [OSError, TimeoutError])
def test_auth_status_exception_is_redacted_and_blocks_creation(monkeypatch, caplog, capsys, failure):
    api = API()
    host = DevinHost(Platform.windows, api=api)

    async def status(*args, **kwargs):
        raise failure("raw-auth-error fake-devin-secret private@example.invalid")

    monkeypatch.setattr(machines, "_run", status)
    with pytest.raises(CapabilityError) as error:
        asyncio.run(host.prepare())
    assert error.value.category == Category.authentication
    assert api.calls == ["validate"] and host._cli is None
    detail = "".join(traceback.format_exception(error.value))
    assert all(value not in detail for value in ("raw-auth-error", "fake-devin-secret", "private@example.invalid"))
    assert caplog.text == ""
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_cancellation_during_auth_status_does_not_allocate(monkeypatch):
    api = API()
    host = DevinHost(Platform.linux, api=api)

    async def status(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(machines, "_run", status)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(host.prepare())
    assert api.calls == ["validate"] and host._cli is None
    assert not host._creation_attempted


def test_cancellation_during_create_waits_for_id_then_pauses_owned_vm(monkeypatch):
    class PendingAPI(API):
        async def create(self, platform):
            self.calls.append(("create", platform))
            started.set()
            await release.wait()
            return {"session_id": "devin-owned"}

    async def scenario():
        nonlocal started, release
        started, release = asyncio.Event(), asyncio.Event()
        api = PendingAPI()
        host = DevinHost(Platform.linux, api=api)
        mock_guest(monkeypatch, host)
        task = asyncio.create_task(host.prepare())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # repeated Stop must not orphan the eventual session ID
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return api, host

    started = release = None
    api, host = asyncio.run(scenario())
    assert api.calls.count(("archive", "devin-owned")) == 1
    assert host._owned and host._archived and not host._ready


@pytest.mark.parametrize("failure", [
    CapabilityError(Category.configuration, "guest reports wrong OS"),
    CapabilityError(Category.desktop_unavailable, "Windows SSH has no interactive desktop"),
    CapabilityError(Category.transport, "SSH unavailable"),
])
def test_bootstrap_or_desktop_failure_pauses_without_other_target(monkeypatch, failure):
    api = API()
    host = DevinHost(Platform.windows, api=api)
    mock_guest(monkeypatch, host)

    async def fail():
        raise failure

    monkeypatch.setattr(host, "_probe", fail)
    with pytest.raises(CapabilityError, match=failure.detail):
        asyncio.run(host.prepare())
    assert api.calls.count(("create", Platform.windows)) == 1
    assert api.calls.count(("archive", "devin-owned")) == 1


def test_archive_refusal_surfaces_manual_cleanup_not_fake_success(monkeypatch):
    api = API(archive_error=CapabilityError(Category.access_denied, "no ManageOrgSessions"))
    host = DevinHost(Platform.linux, api=api)
    mock_guest(monkeypatch, host)

    async def scenario():
        await host.prepare()
        await host.cancel()

    with pytest.raises(CapabilityError, match="pause it in Devin"):
        asyncio.run(scenario())
    assert not host._archived and host.cleanup_error and "devin-owned" in host.cleanup_error


def test_failed_create_is_not_retried_without_re_resolution(monkeypatch):
    class TimedOutAPI(API):
        async def create(self, platform):
            self.calls.append(("create", platform))
            raise CapabilityError(Category.timeout, "create outcome unknown")

    api = TimedOutAPI()
    host = DevinHost(Platform.linux, api=api)
    mock_guest(monkeypatch, host)

    async def scenario():
        with pytest.raises(CapabilityError, match="outcome unknown"):
            await host.prepare()
        with pytest.raises(CapabilityError, match="already attempted"):
            await host.prepare()

    asyncio.run(scenario())
    assert api.calls.count(("create", Platform.linux)) == 1
    assert not any(isinstance(call, tuple) and call[0] == "archive" for call in api.calls)


@pytest.mark.parametrize("platform", [Platform.linux, Platform.windows])
def test_ssh_uses_documented_authenticated_cli_stdio_without_key_argv(platform):
    host = DevinHost(platform, session_id="devin-existing")
    host._cli = "/mock/devin"
    script = r"C:\Users\QA\Raven Host\raven_host.py" if platform == Platform.windows else "/guest/raven_host.py"
    command = ["python", "-u", script, "--env-stdin", "--task", "a quoted ' task"]
    argv = host._ssh_argv(command)
    assert argv[:-1] == [
        "/mock/devin", "ssh", "devin-existing",
        "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=15",
    ]
    # Keep normal known_hosts storage: accept only new keys, never changed ones.
    assert not any("KnownHostsFile" in arg for arg in argv)
    assert "--" not in argv and "StrictHostKeyChecking=no" not in argv
    assert "fake-devin-secret" not in " ".join(argv) and "fake-hai-secret" not in " ".join(argv)
    remote = shlex.split(argv[-1])
    if platform == Platform.windows:
        assert remote[:2] == ["env", "MSYS_NO_PATHCONV=1"]
        remote = remote[2:]
    assert remote == command


@pytest.mark.parametrize("platform", [Platform.linux, Platform.windows])
def test_devin_ssh_keeps_secrets_on_stdin_and_disables_pty(monkeypatch, platform):
    host = DevinHost(platform, session_id="devin-existing")
    host._cli, host._python, host._script, host._ready = "/mock/devin", "/python", "/host.py", True
    launches = []

    class Input:
        data = b""

        def write(self, data):
            self.data += data

        async def drain(self):
            pass

    class Process:
        stdin = Input()
        returncode = None

    proc = Process()

    async def start(*args, **kwargs):
        launches.append((args, kwargs))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    assert asyncio.run(host.spawn(["--task", "test", "--mode", "agents-api"])) is proc
    argv, kwargs = launches[0]
    assert argv[:4] == ("/mock/devin", "ssh", "devin-existing", "-T")
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == asyncio.subprocess.PIPE
    assert "--env-stdin" in argv[-1]
    assert "fake-devin-secret" not in " ".join(argv) and "fake-hai-secret" not in " ".join(argv)
    envelope = json.loads(proc.stdin.data)
    assert envelope["HAI_API_KEY"] == "fake-hai-secret" and "DEVIN_API_KEY" not in envelope


@pytest.mark.parametrize("output", [
    b"Host key verification failed. raw-auth-error fake-devin-secret",
    b"WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! raw-auth-error fake-devin-secret",
])
def test_changed_gateway_key_fails_without_retry_or_raw_error(monkeypatch, output):
    host = DevinHost(Platform.linux, session_id="devin-existing")
    attempts = []

    async def capture(argv, **kwargs):
        attempts.append(argv)
        return 255, b"", output

    monkeypatch.setattr(host, "_capture", capture)
    with pytest.raises(CapabilityError) as error:
        asyncio.run(host._wait_for_ssh())
    assert error.value.category == Category.authentication and not error.value.allows_linux_fallback
    assert "known_hosts" in str(error.value) and "never replaces changed keys" in str(error.value)
    assert "raw-auth-error" not in str(error.value) and "fake-devin-secret" not in str(error.value)
    assert attempts == [["true"]]


def test_artifacts_and_models_driver_share_the_selected_authenticated_transport(monkeypatch):
    host = DevinHost(Platform.linux, session_id="devin-existing")
    host._python, host._script, host._ready = "/guest/python", "/guest/host.py", True
    captures = []

    async def capture(argv, *, data=None, timeout=60):
        captures.append((argv, json.loads(data)))
        if "--driver-json" in argv:
            return 0, b'{"result":[1200,800]}', b""
        return 0, b"PNG bytes from selected guest", b""

    monkeypatch.setattr(host, "_capture", capture)
    worker = NativeRemoteWorker(host)

    async def scenario():
        assert await host.read_file("/tmp/raven-shots/agent/000.png") == b"PNG bytes from selected guest"
        assert await worker.size() == (1200, 800)
        await worker.key("ctrl+enter")

    asyncio.run(scenario())
    assert captures[0][1] == {"path": "/tmp/raven-shots/agent/000.png"}
    assert captures[1][1] == {"operation": "size"}
    assert captures[2][1] == {"operation": "key", "keys": ["ctrl", "enter"]}
    assert all(argv[0] == "/guest/python" for argv, _ in captures)


def test_ssh_readiness_retries_only_transport_not_auth(monkeypatch):
    host = DevinHost(Platform.linux, session_id="devin-existing", poll_interval=.01)
    attempts = []

    async def capture(argv, **kwargs):
        attempts.append(argv)
        return (255, b"", b"connection refused") if len(attempts) == 1 else (0, b"", b"")

    monkeypatch.setattr(host, "_capture", capture)
    asyncio.run(host._wait_for_ssh())
    assert attempts == [["true"], ["true"]]
    attempts.clear()

    async def auth_error(argv, **kwargs):
        attempts.append(argv)
        return 255, b"", b"Permission denied"

    monkeypatch.setattr(host, "_capture", auth_error)
    with pytest.raises(CapabilityError) as error:
        asyncio.run(host._wait_for_ssh())
    assert error.value.category == Category.access_denied
    assert attempts == [["true"]]


def test_cancellation_during_stream_spawn_reaps_the_transport(monkeypatch):
    host = DevinHost(Platform.linux, session_id="devin-existing")
    host._ready, host._python, host._script = True, "/python", "/host.py"

    class Input:
        closed = False

        def close(self):
            self.closed = True

        def is_closing(self):
            return self.closed

    class Process:
        returncode = None
        stdin = Input()

        async def wait(self):
            self.returncode = 0
            return 0

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    proc = Process()

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def start(argv):
            started.set()
            await release.wait()
            return proc

        monkeypatch.setattr(host, "_start", start)
        task = asyncio.create_task(host.spawn(["--task", "test", "--mode", "agents-api"]))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert proc.stdin.closed and proc.returncode == 0
    assert not host._processes


def test_stop_during_archive_still_pauses_owned_vm(monkeypatch):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        class SlowArchive(API):
            async def archive(self, session_id):
                started.set()
                await release.wait()
                return await super().archive(session_id)

        api = SlowArchive()
        host = DevinHost(Platform.linux, api=api)
        mock_guest(monkeypatch, host)
        await host.prepare()
        task = asyncio.create_task(host.cancel())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert host._archived and host._closed
        assert api.calls.count(("archive", "devin-owned")) == 1

    asyncio.run(scenario())


def test_explicit_cancel_while_create_is_pending_prevents_late_ready(monkeypatch):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        class SlowCreate(API):
            async def create(self, platform):
                started.set()
                await release.wait()
                return await super().create(platform)

        api = SlowCreate()
        host = DevinHost(Platform.windows, api=api)
        guest_calls = mock_guest(monkeypatch, host)
        preparing = asyncio.create_task(host.prepare())
        await started.wait()
        stopping = asyncio.create_task(host.cancel())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        await stopping
        with pytest.raises(asyncio.CancelledError):
            await preparing
        assert host._closed and host._archived and not host._ready
        assert api.calls.count(("archive", "devin-owned")) == 1
        assert guest_calls == ["authenticated CLI"]

    asyncio.run(scenario())
