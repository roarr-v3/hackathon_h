"""Devin Linux/Windows VMs: documented v3 lifecycle + authenticated CLI SSH.

References (verified without using credentials):
* https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions
* https://docs.devin.ai/api-reference/v3/sessions/get-organizations-session
* https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions-archive
* https://docs.devin.ai/cli/reference/commands
* https://docs.devin.ai/cli/ssh
* https://docs.devin.ai/onboard-devin/environment/windows-support

The REST API is NOT a shell/event-stream API. `devin ssh <id> [ssh options...]`
uses the logged-in CLI to approve SSH, and streams our own host script's NDJSON.
Artifacts come back over that authenticated transport. No imaginary exec/SSE API,
no browser-login automation, no keys in argv, no Windows -> remote-PC fallback.

Gates: DEVIN_API_KEY (v3 service-user/PAT), DEVIN_ORG_ID, logged-in Devin CLI,
organization access to the requested platform (Windows is limited access), and
a guest with Python/uv plus an interactive desktop accessible from SSH. Bootstrap
installs the existing H harness dependency; native GUI prerequisites must exist.

Cancellation closes H's stdin and archives/puts to sleep ONLY sessions created by
this adapter. It never deletes a VM or archives an explicitly attached session.
Archive requires ManageOrgSessions; cleanup refusal is surfaced, not hidden.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from raven_core.events import HMode, Platform
from raven_core.hosts.capabilities import CapabilityCategory as Category, CapabilityError, command_failure
from raven_core.hosts.remote import RemoteHost, finish_cleanup

log = logging.getLogger(__name__)
PLATFORMS = (Platform.linux, Platform.windows)
API_BASE = "https://api.devin.ai/v3"
_CLI_AUTH_SECTIONS = {"Credentials:", "User / team info:", "Account:", "Team settings:"}
_CLI_AUTH_FIELDS = {
    "File", "API server", "Devin webapp", "Devin API", "Credentials path",
    "Name", "Email", "User ID", "Tier", "Plan", "Team ID", "Team membership",
    "Enterprise", "Primary org", "Telemetry", "Allowed models", "Default model",
    "MCP servers", "Sandbox", "Disabled tools", "Fetched at",
}


def _authenticated_cli_status(output: str) -> bool:
    """Recognize the CLI's positive Devin-login banner without exposing account data."""
    # CLI 3000.12.28 has no documented structured auth status and returns rc=0
    # even when logged out. Only its known Devin-backed login banner is trusted;
    # other providers/formats do not establish authenticated Devin Cloud SSH.
    if not isinstance(output, str):
        return False
    text = re.sub(r"\x1b\[[0-9;]*m", "", output)
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        return False
    # A stored-credentials banner can precede a failed server/account lookup.
    # Accept only known metadata alongside exactly one positive banner: negative,
    # conflicting, or unknown status lines must fail closed. Metadata VALUES are
    # not status (an email/name can contain "invalid", "error", etc.) and stay private.
    authenticated = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line in _CLI_AUTH_SECTIONS:
            continue
        if line.casefold() == "logged in (via devin)." and not authenticated:
            authenticated = True
            continue
        label, separator, _ = line.partition(":")
        if not separator or label not in _CLI_AUTH_FIELDS:
            return False
    return authenticated


def _identifier(value: str, prefix: str) -> str:
    if not isinstance(value, str):
        raise CapabilityError(Category.protocol, "Devin returned an invalid identifier")
    if not value.startswith(prefix):
        value = prefix + value
    if not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise CapabilityError(Category.configuration, f"Invalid Devin {prefix.rstrip('-')} identifier")
    return value


def _api_base(value: str | None = None) -> str:
    value = value or os.environ.get("DEVIN_API_BASE") or API_BASE
    parts = urlsplit(value)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise CapabilityError(Category.configuration, "DEVIN_API_BASE must be an HTTP(S) API URL without credentials")
    path = re.sub(r"/v[123]$", "", parts.path.rstrip("/")) + "/v3"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _max_acu() -> int:
    try:
        value = int(os.environ.get("RAVEN_DEVIN_MAX_ACU", "2"))
        if not 1 <= value <= 10:
            raise ValueError
        return value
    except ValueError as exc:
        raise CapabilityError(Category.configuration, "RAVEN_DEVIN_MAX_ACU must be an integer from 1 to 10") from exc


def inspect_devin(platform: Platform) -> dict:
    """No login probing, API requests or session creation. Never return env values."""
    from raven_core.machines import _which

    missing = [name for name in ("DEVIN_API_KEY", "DEVIN_ORG_ID") if not os.environ.get(name)]
    if missing:
        return {"status": "blocked", "detail": "Devin v3 VM needs " + " and ".join(missing) + "; no VM was created"}
    try:
        _which("devin")
        _identifier(os.environ["DEVIN_ORG_ID"], "org-")
        _api_base()
        _max_acu()
    except CapabilityError as exc:
        return {"status": "blocked", "detail": exc.detail}
    detail = "Devin v3 configuration and CLI present; organization access, CLI login, VM desktop and H runtime are unverified"
    if platform == Platform.windows:
        detail += ". Windows requires limited-access enablement by Cognition"
    return {"status": "unchecked", "detail": detail}


class DevinSessionAPI:
    """Small v3 lifecycle client. Never logs requests, credentials or response bodies."""

    def __init__(
        self, *, org_id: str | None = None, base_url: str | None = None, client: httpx.AsyncClient | None = None,
    ):
        self.org_id = org_id
        self.base_url = base_url
        self._client = client  # caller owns an injected client, useful for mocked tests

    def validate(self) -> None:
        if not os.environ.get("DEVIN_API_KEY"):
            raise CapabilityError(Category.authentication, "Devin VM provisioning needs DEVIN_API_KEY with v3 session permissions")
        org = self.org_id or os.environ.get("DEVIN_ORG_ID")
        if not org:
            raise CapabilityError(Category.configuration, "Devin v3 VM provisioning needs DEVIN_ORG_ID")
        _identifier(org, "org-")
        _api_base(self.base_url)
        _max_acu()

    async def _request(self, method: str, suffix: str, *, body: dict | None = None) -> dict:
        self.validate()
        org = _identifier(self.org_id or os.environ["DEVIN_ORG_ID"], "org-")
        url = f"{_api_base(self.base_url)}/organizations/{org}/sessions{suffix}"
        headers = {"Authorization": f"Bearer {os.environ['DEVIN_API_KEY']}", "Accept": "application/json"}

        async def send(client: httpx.AsyncClient) -> httpx.Response:
            return await client.request(method, url, json=body, headers=headers, timeout=30)

        try:
            if self._client is not None:
                response = await send(self._client)
            else:
                async with httpx.AsyncClient(follow_redirects=False) as client:
                    response = await send(client)
        except httpx.TimeoutException as exc:
            raise CapabilityError(
                Category.timeout,
                "Devin v3 request timed out. A create may have succeeded; inspect Devin sessions before retrying. No automatic retry.",
            ) from exc
        except httpx.HTTPError as exc:
            raise CapabilityError(Category.transport, "Devin v3 is unreachable; no alternate target was selected") from exc
        if response.status_code == 401:
            raise CapabilityError(Category.authentication, "Devin v3 rejected the configured credential")
        if response.status_code == 403:
            raise CapabilityError(
                Category.access_denied,
                "Devin organization/session permission denied. Verify UseDevinSessions and requested platform access; "
                "Windows is limited access. Pausing owned sessions also needs ManageOrgSessions.",
            )
        if response.status_code in (400, 404, 409, 410, 422):
            raise CapabilityError(
                Category.configuration,
                f"Devin v3 rejected the requested organization, session or platform (HTTP {response.status_code}); "
                "verify organization platform enablement. No target fallback.",
            )
        if response.status_code == 429:
            raise CapabilityError(Category.access_denied, "Devin rate/usage limit reached; no automatic provisioning retry")
        if response.status_code >= 300:
            raise CapabilityError(Category.transport, f"Devin v3 returned HTTP {response.status_code}; no automatic retry")
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, TypeError) as exc:
            raise CapabilityError(Category.protocol, "Devin v3 returned an invalid lifecycle response") from exc

    async def create(self, platform: Platform) -> dict:
        platform = Platform(platform)
        if platform not in PLATFORMS:
            raise CapabilityError(Category.configuration, "Devin execution VM must be explicitly linux or windows")
        return await self._request("POST", "", body={
            "platform": platform.value,
            "title": f"Raven {platform.value} H execution host",
            "prompt": (
                f"Allocate this {platform.value} VM as a Raven QA execution host. "
                "Do not perform computer use, run tests, change code, or create child sessions. "
                "Raven will bootstrap and run H Company's official desktop harness over authenticated SSH. "
                "Confirm the environment is available and wait for further instructions."
            ),
            "max_acu_limit": _max_acu(),
            "resumable": True,
            "tags": ["raven-execution", f"raven-{platform.value}"],
        })

    async def get(self, session_id: str) -> dict:
        return await self._request("GET", "/" + _identifier(session_id, "devin-"))

    async def archive(self, session_id: str) -> dict:
        # Documented non-destructive pause, NOT DELETE /sessions/{id}.
        return await self._request("POST", "/" + _identifier(session_id, "devin-") + "/archive")


class DevinHost(RemoteHost):
    kind = "devin-vm"
    target = "devin-vm"

    def __init__(
        self, platform: Platform, name: str | None = None, *, mode: HMode | str = HMode.agents_api,
        session_id: str | None = None, api: DevinSessionAPI | None = None,
        ready_timeout: float = 180, poll_interval: float = 2,
    ):
        platform = Platform(platform)
        if platform not in PLATFORMS:
            raise CapabilityError(Category.configuration, "Only Linux and Windows use the Devin VM execution adapter")
        super().__init__(platform, name or f"devin-{platform.value}", mode=mode)
        existing = session_id or os.environ.get(f"RAVEN_DEVIN_{platform.value.upper()}_SESSION_ID")
        self.session_id = _identifier(existing, "devin-") if existing else None
        self.session_url: str | None = None
        self._api = api or DevinSessionAPI()
        self._cli: str | None = None
        self._owned = False
        self._archived = False
        self._creation_attempted = False
        self._closed = False
        self._cancel_lock = asyncio.Lock()
        self._pause_lock = asyncio.Lock()
        self._creating: asyncio.Task | None = None
        self.ready_timeout = min(max(1, ready_timeout), 600)
        self.poll_interval = max(0.01, poll_interval)
        self.cleanup_error: str | None = None

    async def _check_cli(self) -> None:
        from raven_core import machines

        self._cli = None
        cli = machines._which("devin")
        try:
            rc, output = await machines._run(cli, "auth", "status", timeout=20)
            authenticated = rc == 0 and _authenticated_cli_status(output)
        except Exception:
            # Do not surface raw CLI/OS errors or their credential-bearing context.
            # CancelledError is a BaseException and must still propagate.
            authenticated = False
        if not authenticated:
            raise CapabilityError(
                Category.authentication,
                "Devin CLI SSH authentication could not be verified. Check `devin auth status` and sign in with `devin auth login` yourself; API credentials alone are not an SSH login.",
            ) from None
        self._cli = cli

    def _ssh_argv(self, command: list[str]) -> list[str]:
        if not self._cli or not self.session_id:
            raise CapabilityError(Category.configuration, "Devin SSH needs a selected session and an authenticated CLI")
        remote = shlex.join(command)
        if self.platform == Platform.windows:
            # Documented Windows default shell is Git Bash. Prevent MSYS from
            # rewriting `cmd /c` and native Windows paths in suite setup commands.
            remote = "env MSYS_NO_PATHCONV=1 " + remote
        # CLI help/docs pass SSH_ARGS directly after SESSION, without a `--`.
        # TOFU stores new gateway keys in normal known_hosts; changed keys fail.
        return [
            self._cli, "ssh", self.session_id,
            "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15", remote,
        ]

    async def _start(self, argv: list[str]) -> asyncio.subprocess.Process:
        from raven_core.machines import tool_env

        if self._closed:
            raise CapabilityError(Category.configuration, "The selected Devin execution host is closed")
        return await asyncio.create_subprocess_exec(
            *self._ssh_argv(argv), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=tool_env(),
        )

    def _failure(self, operation: str, output: str) -> CapabilityError:
        if any(marker in output.lower() for marker in (
            "host key verification failed", "remote host identification has changed",
        )):
            return CapabilityError(
                Category.authentication,
                "Devin SSH gateway host key was rejected. Verify the gateway key before updating known_hosts; Raven never replaces changed keys or disables host-key checking.",
            )
        return command_failure(operation, output)

    def _record_session(self, response: dict) -> None:
        value = response.get("session_id")
        if not isinstance(value, str):
            raise CapabilityError(Category.protocol, "Devin create returned no session ID; inspect Devin before retrying")
        self.session_id = _identifier(value, "devin-")
        raw_url = response.get("url")
        if isinstance(raw_url, str):
            parsed = urlsplit(raw_url)
            if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                # Use the API's documented URL, not a guessed ID-to-URL mapping.
                self.session_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        self._owned = True
        self.name = f"{self.platform.value}:{self.session_id}"

    async def _wait_for_vm(self) -> None:
        assert self.session_id
        deadline = time.monotonic() + self.ready_timeout
        while True:
            if self._closed:
                raise asyncio.CancelledError
            response = await self._api.get(self.session_id)
            if response.get("session_id") and _identifier(response["session_id"], "devin-") != self.session_id:
                raise CapabilityError(Category.protocol, "Devin returned a different session; refusing to change targets")
            status = response.get("status")
            if status == "running":
                return  # SSH + native desktop preflight still must succeed
            if status not in ("new", "claimed", "resuming"):
                raise CapabilityError(
                    Category.access_denied,
                    "Devin VM did not enter a running session; check organization platform access, usage quota and session status.",
                )
            if time.monotonic() >= deadline:
                raise CapabilityError(Category.timeout, "Devin VM readiness deadline exceeded; no alternate target selected")
            await asyncio.sleep(min(self.poll_interval, max(0.01, deadline - time.monotonic())))

    async def _wait_for_ssh(self) -> None:
        """Bounded transport readiness only. Never retry auth or H/model failures."""
        deadline = time.monotonic() + self.ready_timeout
        while True:
            if self._closed:
                raise asyncio.CancelledError
            try:
                rc, _, err = await self._capture(["true"], timeout=20)
            except TimeoutError:
                rc, err = 124, b"connection timed out"
            if rc == 0:
                return
            text = err.decode(errors="replace")
            failure = self._failure("Connecting to the selected Devin VM over SSH", text)
            transient = any(word in text.lower() for word in (
                "connection refused", "connection reset", "timed out", "not ready", "still starting",
            ))
            if not transient or failure.category != Category.transport:
                raise failure
            if time.monotonic() >= deadline:
                raise CapabilityError(Category.timeout, "Devin SSH readiness deadline exceeded; verify CLI/session access")
            await asyncio.sleep(min(self.poll_interval, max(0.01, deadline - time.monotonic())))

    async def _pause_owned(self) -> None:
        async with self._pause_lock:
            if not self._owned or not self.session_id or self._archived:
                return
            try:
                await self._api.archive(self.session_id)
                self._archived = True
            except Exception as exc:
                self.cleanup_error = (
                    f"Could not put Raven-owned session {self.session_id} to sleep; pause it in Devin. "
                    "Archive requires ManageOrgSessions; no VM was deleted."
                )
                log.warning("%s", self.cleanup_error)
                raise CapabilityError(Category.access_denied, self.cleanup_error) from exc

    async def prepare(self) -> None:
        async with self._prepare_lock:
            if self._ready:
                return
            if self._closed:
                raise CapabilityError(Category.configuration, "This Devin execution host is closed; resolve a new worker")
            self._api.validate()
            await self._check_cli()  # no paid allocation until both auth prerequisites exist
            if self._closed:
                raise asyncio.CancelledError
            try:
                if not self.session_id:
                    if self._creation_attempted:
                        raise CapabilityError(Category.configuration, "A VM create was already attempted; inspect Devin before resolving a new worker")
                    self._creation_attempted = True
                    create = self._creating = asyncio.create_task(self._api.create(self.platform))
                    try:
                        response = await asyncio.shield(create)
                    except asyncio.CancelledError:
                        # Never orphan a paid session whose POST finishes after Stop.
                        try:
                            self._record_session(await finish_cleanup(create))
                        except Exception:
                            self.cleanup_error = "VM creation outcome could not be confirmed after cancellation; inspect Devin sessions before retrying"
                            log.warning("%s", self.cleanup_error)
                        raise
                    self._record_session(response)
                if self._closed:
                    raise asyncio.CancelledError
                await self._wait_for_vm()
                await self._wait_for_ssh()
                await self._bootstrap()
                await self._probe()  # verify actual OS, H mode, and desktop, not just API status
                if self._closed:
                    raise asyncio.CancelledError
                self._ready = True
            except BaseException as exc:
                async def cleanup() -> None:
                    try:
                        await RemoteHost.close(self)
                    finally:
                        await self._pause_owned()

                try:
                    await finish_cleanup(asyncio.create_task(cleanup()))
                except Exception as cleanup:
                    if isinstance(exc, CapabilityError):
                        raise CapabilityError(exc.category, f"{exc.detail} {self.cleanup_error or 'VM cleanup needs manual attention.'}") from cleanup
                finally:
                    if self._archived or isinstance(exc, asyncio.CancelledError):
                        self._closed = True
                raise

    async def cancel(self) -> None:
        async def cleanup() -> None:
            async with self._cancel_lock:
                self._ready = False
                self._closed = True
                if self._creating is not None and not self.session_id:
                    try:
                        self._record_session(await finish_cleanup(self._creating))
                    except Exception:
                        self.cleanup_error = "VM creation outcome is unknown; inspect Devin sessions before retrying"
                        log.warning("%s", self.cleanup_error)
                try:
                    await RemoteHost.close(self)  # EOF cancels H before the guest is paused
                finally:
                    await self._pause_owned()

        task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await finish_cleanup(task)
            finally:
                raise

    async def close(self) -> None:
        await self.cancel()


__all__ = ["DevinHost", "DevinSessionAPI", "inspect_devin"]
