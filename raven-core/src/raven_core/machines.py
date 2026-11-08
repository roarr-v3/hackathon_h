"""Test machines on demand: start what a test needs, show it, suspend what nobody uses.

Cua sandboxes (Lume macOS VM, Docker Linux container) are created on first use, resumed when
suspended, and suspended again after RAVEN_IDLE_SUSPEND_MIN minutes without agents (0 = never).
Docker itself (colima) is started when the Linux machine needs it. While an agent uses one,
`show()` opens Cua's live, watch-only viewer of its desktop (RAVEN_SHOW_VM=0 turns that off).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
import webbrowser
from pathlib import Path
from typing import Awaitable, Callable

from raven_core.events import Platform
from raven_core.workers.base import WorkerUnavailable

log = logging.getLogger(__name__)
Say = Callable[[str], Awaitable[None]]

SANDBOXES = {
    Platform.linux: (lambda: os.environ.get("RAVEN_LINUX_SANDBOX", "raven-linux"), ["linux", "--wait", "desktop"]),
    Platform.macos: (lambda: os.environ.get("RAVEN_MACOS_SANDBOX", "raven-mac"), ["macos"]),
}
EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin")]

VIEW_TTL_S = 8 * 3600

_locks: dict[Platform, asyncio.Lock] = {}
_last_used: dict[Platform, float] = {}
_shown: dict[str, float] = {}


def tool_env() -> dict[str, str]:
    """GUI-launched processes (Raven Agent) don't get the shell PATH; add brew and ~/.local/bin."""
    path = os.environ.get("PATH", "")
    extra = [p for p in EXTRA_PATH if p not in path.split(":")]
    return {**os.environ, "PATH": ":".join([*extra, path] if path else extra)}


def _which(name: str) -> str:
    """Tools from the system, never raven-core's own venv (hai-drivers ships a stub `cua` there)."""
    venv_bin = Path(sys.prefix) / "bin"
    dirs = [d for d in [*EXTRA_PATH, *tool_env()["PATH"].split(":")] if d and Path(d) != venv_bin]
    found = shutil.which(name, path=":".join(dirs))
    if not found:
        raise WorkerUnavailable(f"`{name}` not found (install it, see TESTING.md)")
    return found


async def _run(*argv: str, timeout: float = 900) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=tool_env()
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        return 124, f"timed out after {timeout:.0f}s"
    return proc.returncode or 0, out.decode(errors="replace")


async def _sandbox_state(name: str) -> str | None:
    rc, out = await _run(_which("cua"), "--json", "sb", "ls", timeout=60)
    if rc:
        raise WorkerUnavailable(f"cua sb ls failed: {out[-300:]}")
    for sb in json.loads(out[out.find("[") :] or "[]"):
        if sb.get("name") == name:
            return str(sb.get("state") or sb.get("status") or "unknown")
    return None


async def _ensure_docker(say: Say) -> None:
    rc, _ = await _run(_which("docker"), "info", timeout=20)
    if rc == 0:
        return
    await say("Starting Docker (colima)…")
    rc, out = await _run(_which("colima"), "start", "--cpu", "4", "--memory", "6", "--disk", "60", "--vm-type", "vz", timeout=300)
    if rc:
        raise WorkerUnavailable(f"colima start failed: {out[-400:]}")


async def ensure(platform: Platform, say: Say | None = None) -> None:
    """Make the machine for `platform` ready. No-op for platforms Raven doesn't host locally."""
    platform = Platform(platform)
    if platform not in SANDBOXES or os.environ.get("RAVEN_MANAGE_MACHINES", "1") in ("0", "false"):
        return
    _last_used[platform] = time.time()

    async def _say(text: str) -> None:
        log.info("[%s] %s", platform.value, text)
        if say:
            await say(text)

    name_of, create_args = SANDBOXES[platform]
    name = name_of()
    async with _locks.setdefault(platform, asyncio.Lock()):
        if platform == Platform.linux:
            await _ensure_docker(_say)
        state = await _sandbox_state(name)
        if state is None:
            hint = " (first macOS download is ~23 GB)" if platform == Platform.macos else ""
            await _say(f"Creating {platform.value} machine `{name}`{hint}…")
            rc, out = await _run(_which("cua"), "sb", "create", *create_args[:1], "--name", name, *create_args[1:], timeout=3600)
            if rc:
                raise WorkerUnavailable(f"creating {name} failed: {out[-400:]}")
        elif state not in ("running", "ready"):
            await _say(f"Waking {platform.value} machine `{name}` ({state})…")
            rc, out = await _run(_which("cua"), "sb", "resume", name, timeout=300)
            if rc:
                raise WorkerUnavailable(f"resuming {name} failed: {out[-400:]}")


def _show_enabled() -> bool:
    return os.environ.get("RAVEN_SHOW_VM", "1").strip().lower() not in ("0", "false", "no", "off")


async def show(platform: Platform, say: Say | None = None) -> str | None:
    """Open a live, watch-only view of the machine for `platform`; once per link lifetime."""
    platform = Platform(platform)
    if platform not in SANDBOXES or not _show_enabled():
        return None
    name = SANDBOXES[platform][0]()
    if time.time() - _shown.get(name, 0.0) < VIEW_TTL_S - 300:
        return None
    rc, out = await _run(
        _which("cua"), "--json", "sb", "view", name, "--view-only", "--no-open", "--ttl", f"{VIEW_TTL_S // 3600}h",
        timeout=60,
    )
    if rc:
        raise WorkerUnavailable(f"cua sb view {name} failed: {out[-300:]}")
    try:
        url = str(json.loads(out[out.find("{") :])["url"])
    except (ValueError, KeyError, TypeError) as e:
        raise WorkerUnavailable(f"cua sb view {name} printed no viewer link") from e
    _shown[name] = time.time()
    await asyncio.to_thread(webbrowser.open, url)
    log.info("opened the live view of %s", name)
    if say:
        await say(f"Watching `{name}` live (view only): {url}")
    return url


async def local_shell(cmd: str, timeout: float = 60) -> tuple[int, str, str]:
    """Run `cmd` on this machine (bare-metal setup commands), with the GUI-safe PATH."""
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", "-lc", cmd,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=tool_env(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        return 124, "", f"timed out after {timeout:.0f}s"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def touch(platform: Platform) -> None:
    _last_used[Platform(platform)] = time.time()


async def idle_reaper(busy: Callable[[Platform], bool], interval_s: float = 60) -> None:
    """Suspend machines Raven started once no agent has used them for RAVEN_IDLE_SUSPEND_MIN."""
    minutes = float(os.environ.get("RAVEN_IDLE_SUSPEND_MIN", "20"))
    if minutes <= 0:
        return
    while True:
        await asyncio.sleep(interval_s)
        for platform, last in list(_last_used.items()):
            if busy(platform):
                _last_used[platform] = time.time()
                continue
            if time.time() - last < minutes * 60:
                continue
            name = SANDBOXES[platform][0]()
            rc, out = await _run(_which("cua"), "sb", "suspend", name, timeout=120)
            log.info("suspended idle %s machine %s (rc=%s) %s", platform.value, name, rc, out[-200:] if rc else "")
            _last_used.pop(platform, None)
