"""Local Space lifecycle. Native Mac/iOS need no VM; Linux uses Apple Containerization.

The existing ``raven-linux-apple`` Space is the default. Missing/broken/auth-failed
Spaces are never implicitly recreated. Only explicit setup passes ``create=True``.
Legacy, explicitly requested macOS VMs still use Lume. No machine is deleted, and
only Spaces this process created/resumed are eligible for idle suspension.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
import webbrowser
from pathlib import Path
from typing import Awaitable, Callable

from raven_core.events import ExecutionProfile, Platform
from raven_core.hosts.capabilities import CapabilityCategory as Category, CapabilityError, command_failure

log = logging.getLogger(__name__)
Say = Callable[[str], Awaitable[None]]
APPLE_SPACE = "raven-linux-apple"
EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin")]
VIEW_TTL_S = 8 * 3600

# Compatibility for the old placement API; profile-aware callers pass the resolved name.
SANDBOXES = {
    Platform.linux: (lambda: os.environ.get("RAVEN_LINUX_SANDBOX", APPLE_SPACE), ["linux"]),
    Platform.macos: (lambda: os.environ.get("RAVEN_MACOS_SANDBOX", "raven-mac"), ["macos"]),
}
_locks: dict[str, asyncio.Lock] = {}
_last_used: dict[tuple[Platform, str], float] = {}
_managed: set[tuple[Platform, str]] = set()
_shown: dict[str, float] = {}


def tool_env() -> dict[str, str]:
    """GUI-launched processes do not get the shell's PATH."""
    path = os.environ.get("PATH", "")
    extra = [p for p in EXTRA_PATH if p not in path.split(os.pathsep)]
    return {**os.environ, "PATH": os.pathsep.join([*extra, path] if path else extra)}


def _which(name: str) -> str:
    """System tools, not the stub `cua` shipped by hai-drivers in our venv."""
    venv_bin = Path(sys.prefix) / "bin"
    dirs = [d for d in [*EXTRA_PATH, *tool_env()["PATH"].split(os.pathsep)] if d and Path(d) != venv_bin]
    found = shutil.which(name, path=os.pathsep.join(dirs))
    if not found:
        raise CapabilityError(Category.dependency, f"`{name}` is not installed or not on Raven's PATH")
    return found


async def _run(*argv: str, timeout: float = 900) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=tool_env(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return 124, f"timed out after {timeout:.0f}s"
    return proc.returncode or 0, (out if proc.returncode == 0 else out + b"\n" + err).decode(errors="replace")


def space_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        raise CapabilityError(Category.configuration, "Invalid local Cua Space name")
    return name


async def local_spaces() -> list[dict]:
    """Read-only metadata; never request `info --connection` (contains a token)."""
    rc, output = await _run(_which("cua"), "--json", "sb", "ls", "--local", "--no-size", timeout=30)
    if rc:
        raise command_failure("Listing local Cua Spaces", output)
    try:
        rows = json.loads(output)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError
        return [{k: row.get(k) for k in ("name", "state", "status", "runtime", "kind", "location")} for row in rows]
    except (ValueError, TypeError) as exc:
        raise CapabilityError(Category.protocol, "Cua returned invalid Space metadata; no machine was created") from exc


async def space_info(name: str) -> dict | None:
    name = space_name(name)
    return next((row for row in await local_spaces() if row.get("name") == name), None)


async def _sandbox_state(name: str) -> str | None:
    """Compatibility only; selection code needs the complete runtime metadata."""
    row = await space_info(name)
    return str(row.get("state") or row.get("status") or "unknown") if row else None


def validate_space(row: dict, platform: Platform) -> None:
    expected = "containerization" if platform == Platform.linux else "lume"
    expected_kind = "microvm" if platform == Platform.linux else "vm"
    if row.get("runtime") != expected or row.get("kind") != expected_kind or row.get("location") != "local":
        raise CapabilityError(
            Category.configuration,
            f"Space `{row.get('name')}` must be local {expected_kind} runtime `{expected}`; "
            "refusing to use or replace a differently configured machine.",
        )


async def ensure_space(
    platform: Platform, name: str, say: Say | None = None, *, create: bool = False,
) -> dict:
    """Validate an existing Space and resume only that Space. Creation is opt-in."""
    platform, name = Platform(platform), space_name(name)
    if platform not in SANDBOXES:
        raise CapabilityError(Category.configuration, f"No local Space route for {platform.value}")
    if sys.platform != "darwin":
        raise CapabilityError(Category.desktop_unavailable, "Apple Containerization/Lume requires a Mac host")
    try:
        cua = _which("cua")
    except CapabilityError as exc:
        raise CapabilityError(Category.desktop_unavailable, "The local Cua desktop is unavailable: install the Cua CLI") from exc

    async def announce(text: str) -> None:
        log.info("%s", text)
        if say:
            await say(text)

    async with _locks.setdefault(name, asyncio.Lock()):
        row = await space_info(name)  # failed auth/listing MUST NOT become "missing"
        if row is None:
            if not create:
                raise CapabilityError(
                    Category.desktop_unavailable,
                    f"Existing local Space `{name}` was not found; use the explicit sandbox setup command to create it.",
                )
            if os.environ.get("RAVEN_MANAGE_MACHINES", "1").lower() in ("0", "false"):
                raise CapabilityError(Category.configuration, "Machine creation is disabled by RAVEN_MANAGE_MACHINES")
            runtime, kind = ("containerization", "microvm") if platform == Platform.linux else ("lume", "vm")
            await announce(f"Creating local {platform.value} Space `{name}` using {runtime}…")
            args = [
                cua, "sb", "create", "linux" if platform == Platform.linux else "macos",
                "--on", "local", "--kind", kind, "--runtime", runtime, "--name", name, "--keep-on-failure",
            ]
            if platform == Platform.linux:
                args += ["--wait", "desktop"]
            rc, output = await _run(*args, timeout=3600)
            if rc:
                raise command_failure(f"Creating Space `{name}` (kept for diagnosis)", output, local_desktop=True)
            _managed.add((platform, name))
            row = await space_info(name)
            if row is None:
                raise CapabilityError(Category.protocol, "Created Space is absent from Cua metadata; not retrying creation")
        validate_space(row, platform)  # check running Spaces too; name alone is insufficient
        state = str(row.get("state") or row.get("status") or "unknown").lower()
        if state in ("stopped", "suspended", "paused"):
            if os.environ.get("RAVEN_MANAGE_MACHINES", "1").lower() in ("0", "false"):
                raise CapabilityError(Category.desktop_unavailable, f"Space `{name}` is {state}; automatic resume is disabled")
            await announce(f"Resuming local {platform.value} Space `{name}`…")
            rc, output = await _run(cua, "sb", "resume", f"local:{name}", timeout=300)
            if rc:
                raise command_failure(f"Resuming Space `{name}`", output, local_desktop=True)
            _managed.add((platform, name))
            row = await space_info(name)
            if row is None:
                raise CapabilityError(Category.protocol, "Resumed Space is absent from Cua metadata")
            validate_space(row, platform)
            state = str(row.get("state") or row.get("status") or "unknown").lower()
        if state not in ("running", "ready"):
            raise CapabilityError(Category.desktop_unavailable, f"Space `{name}` is not running; it will not be replaced")
        _last_used[(platform, name)] = time.time()
        return row


async def ensure(
    platform: Platform, say: Say | None = None, *, execution: ExecutionProfile | None = None, name: str | None = None,
) -> None:
    """Compatibility entry point. New code prepares its resolved worker, not a platform."""
    platform = Platform(platform)
    if execution is not None and (platform != Platform.linux or execution.linux_target != "apple-container"):
        return
    if platform not in SANDBOXES or os.environ.get("RAVEN_MANAGE_MACHINES", "1").lower() in ("0", "false"):
        return
    name = name or (execution.linux_space if execution else SANDBOXES[platform][0]())
    await ensure_space(platform, name, say)


def _show_enabled() -> bool:
    return os.environ.get("RAVEN_SHOW_VM", "1").strip().lower() not in ("0", "false", "no", "off")


async def show(
    platform: Platform, say: Say | None = None, *, execution: ExecutionProfile | None = None, name: str | None = None,
) -> str | None:
    """A viewer for the selected local Space, never an unrelated platform default."""
    platform = Platform(platform)
    if platform not in SANDBOXES or not _show_enabled():
        return None
    if execution is not None and (platform != Platform.linux or execution.linux_target != "apple-container"):
        return None
    name = space_name(name or (execution.linux_space if execution else SANDBOXES[platform][0]()))
    if time.time() - _shown.get(name, 0.0) < VIEW_TTL_S - 300:
        return None
    rc, output = await _run(
        _which("cua"), "--json", "sb", "view", f"local:{name}", "--view-only", "--no-open", "--ttl", f"{VIEW_TTL_S // 3600}h",
        timeout=60,
    )
    if rc:
        raise command_failure("Opening the selected Space viewer", output)
    try:
        url = str(json.loads(output[output.find("{"):])["url"])
        if not url.startswith(("http://", "https://")):
            raise ValueError
    except (ValueError, KeyError, TypeError) as exc:
        raise CapabilityError(Category.protocol, "Cua printed no valid viewer link") from exc
    _shown[name] = time.time()
    await asyncio.to_thread(webbrowser.open, url)
    if say:
        await say(f"Opened a read-only live viewer for `{name}`.")
    return url


async def local_shell(cmd: str, timeout: float = 60) -> tuple[int, str, str]:
    """Setup on this machine. Reap children on timeout and cancellation."""
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", "-lc", cmd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=tool_env(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return 124, "", f"timed out after {timeout:.0f}s"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def touch(platform: Platform, name: str | None = None) -> None:
    platform = Platform(platform)
    for key in list(_last_used):
        if key[0] == platform and (name is None or key[1] == name):
            _last_used[key] = time.time()


async def idle_reaper(busy: Callable[[Platform], bool], interval_s: float = 60) -> None:
    minutes = float(os.environ.get("RAVEN_IDLE_SUSPEND_MIN", "20"))
    if minutes <= 0:
        return
    while True:
        await asyncio.sleep(interval_s)
        for key, last in list(_last_used.items()):
            platform, name = key
            if key not in _managed:
                continue  # an existing, already-running Space is not ours to suspend
            if busy(platform):
                _last_used[key] = time.time()
            elif time.time() - last >= minutes * 60:
                rc, _ = await _run(_which("cua"), "sb", "suspend", f"local:{name}", timeout=120)
                log.info("idle suspend of %s: rc=%s", name, rc)
                if rc == 0:
                    _last_used.pop(key, None)
                    _managed.discard(key)
