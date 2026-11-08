"""Machines on which H's official harness runs, with a native driver on that machine.

``spawn`` returns a stdio process: NDJSON events out, steering in, EOF cancels H.
Remote hosts transmit credentials on stdin, never in argv or an uploaded script.
Selection is explicit and lazy; importing this package cannot provision anything.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from raven_core.events import ExecutionProfile, HMode, Platform
from raven_core.hosts.capabilities import CapabilityCategory as Category, CapabilityError
from raven_core.workers.base import WorkerUnavailable

HOST_SCRIPT = Path(__file__).with_name("raven_host.py")
PASS_ENV = ("HAI_API_KEY", "HAI_MODEL", "HAI_BASE_URL", "HAI_API_BASE_URL")


def h_environment() -> dict[str, str]:
    env = {k: os.environ[k] for k in PASS_ENV if os.environ.get(k)}
    if "HAI_API_KEY" not in env:
        raise CapabilityError(Category.authentication, "HAI_API_KEY is not configured in Raven's environment")
    return env


def check_preflight_result(rc: int, output: str) -> dict:
    """Host preflight emits only safe capability metadata, never an H API response."""
    try:
        result = json.loads(output)
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise CapabilityError(Category.protocol, "The guest did not return a valid Raven capability report") from exc
    if not result["ok"] or rc:
        try:
            category = Category(result.get("category", "bootstrap"))
        except ValueError:
            category = Category.bootstrap
        # Use a fixed vocabulary, not guest exception strings (which can contain secrets).
        messages = {
            Category.unsupported_runtime: "H local runtime is not published for this guest OS/architecture; select hosted H or a supported VM.",
            Category.desktop_unavailable: "The native desktop driver cannot capture this guest's desktop; check its GUI session and permissions.",
            Category.dependency: "The guest needs Python, hai-agents[desktop], and its native desktop dependencies.",
            Category.configuration: "The guest OS does not match the requested target.",
        }
        raise CapabilityError(category, messages.get(category, "Guest capability preflight failed"))
    return result


class Host:
    """Base class; subclasses implement `_spawn_argv`, `prepare` and `read_file`."""

    kind = "host"

    def __init__(self, platform: Platform, name: str, *, mode: HMode | str | None = None):
        self.platform = Platform(platform)
        self.name = name
        self._ready = False
        self._prepare_lock = asyncio.Lock()
        self._capabilities: dict = {}
        if mode is not None:
            self.default_mode = HMode(mode).value

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.platform.value}, {self.name})"

    async def prepare(self) -> None:
        self._ready = True

    def _spawn_argv(self, host_args: list[str], env: dict[str, str]) -> list[str]:
        raise NotImplementedError

    async def spawn(self, host_args: list[str]) -> asyncio.subprocess.Process:
        if not self._ready:
            await self.prepare()
        env = h_environment()
        argv = self._spawn_argv(host_args, env)
        from raven_core.machines import tool_env

        start = asyncio.create_task(asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**tool_env(), **env},
        ))
        try:
            return await asyncio.shield(start)
        except asyncio.CancelledError:
            from raven_core.hosts.remote import finish_cleanup, stop_process

            async def cleanup() -> None:
                proc = await start
                await stop_process(proc, grace_s=1)

            await finish_cleanup(asyncio.create_task(cleanup()))
            raise

    async def read_file(self, path: str) -> bytes:
        raise NotImplementedError

    async def close(self) -> None:
        """Disconnect run-owned transports; never delete a machine."""

    async def cancel(self) -> None:
        await self.close()


async def _run(*argv: str, timeout: float = 600) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    return proc.returncode or 0, out.decode(errors="replace")


class LocalHost(Host):
    """This machine, bare metal (macOS, or the iOS Simulator on a Mac). H's agent takes over this screen."""

    kind = "bare-metal"
    bare_metal = True

    target = "native"

    def __init__(self, platform: Platform, name: str | None = None, *, mode: HMode | str | None = None):
        super().__init__(platform, name or ("this-mac" if sys.platform == "darwin" else "this-machine"), mode=mode)
        self._hidden: list = []

    async def prepare(self) -> None:
        async with self._prepare_lock:
            if self._ready:
                return
            await self.preflight()
            rc, output = await _run(
                sys.executable, str(HOST_SCRIPT), "--preflight", "--mode", getattr(self, "default_mode", "local-runtime"),
                "--expected-platform", self.platform.value, timeout=45,
            )
            self._capabilities = check_preflight_result(rc, output)
            self._ready = True

    async def begin(self) -> None:
        if sys.platform == "darwin":
            from raven_core.workers.local_mac import hide_raven_window

            self._hidden = await asyncio.to_thread(hide_raven_window)

    async def finish(self) -> None:
        apps, self._hidden = self._hidden, []
        for app in apps:
            await asyncio.to_thread(app.unhide)

    async def preflight(self) -> None:
        if self._ready:
            return
        if sys.platform == "darwin":
            from raven_core.workers.local_mac import check_mac_permissions

            try:
                await asyncio.to_thread(check_mac_permissions)
            except WorkerUnavailable as exc:
                raise CapabilityError(Category.desktop_unavailable, str(exc)) from exc

    def _spawn_argv(self, host_args: list[str], env: dict[str, str]) -> list[str]:
        return [sys.executable, str(HOST_SCRIPT), *host_args]

    async def read_file(self, path: str) -> bytes:
        return Path(path).read_bytes()

    async def screenshot(self) -> bytes:
        if sys.platform != "darwin":
            raise CapabilityError(Category.dependency, "Native screenshot helper is only available on macOS")
        from raven_core.workers.local_mac import _screencapture

        return await asyncio.to_thread(_screencapture)

    async def shell(self, cmd: str, timeout: int = 60) -> tuple[int, str, str]:
        from raven_core.machines import local_shell

        return await local_shell(cmd, timeout)


def get_host(platform: Platform, *, execution: ExecutionProfile | None = None, name: str | None = None) -> Host:
    platform = Platform(platform)
    choice = os.environ.get(f"RAVEN_HOST_{platform.value.upper()}", "").strip()
    if execution is not None:
        choice = execution.linux_target if platform == Platform.linux else execution.windows_target
    if platform == Platform.linux and choice in ("", "cua-local", "apple-container"):
        from raven_core.hosts.cua import CuaHost

        return CuaHost(
            platform, name or (execution.linux_space if execution else os.environ.get("RAVEN_LINUX_SANDBOX", "raven-linux-apple")),
            mode=execution.mode_for(platform) if execution else HMode.agents_api,
        )
    if platform in (Platform.linux, Platform.windows) and choice in ("devin-vm", "devin-cloud", ""):
        from raven_core.hosts.devin import DevinHost

        return DevinHost(platform, name=name, mode=execution.mode_for(platform) if execution else HMode.agents_api)
    raise WorkerUnavailable(
        f"Host '{choice or 'default'}' for {platform.value} has no supported H stdio transport"
    )


def host_args(task: str, mode: str, model: str | None, shots_dir: str, max_steps: int = 60, max_time: float = 600) -> list[str]:
    args = ["--task", task, "--mode", mode, "--shots-dir", shots_dir,
            "--max-steps", str(max_steps), "--max-time", str(max_time)]
    if model:
        args += ["--model", model]
    return args


__all__ = ["Host", "LocalHost", "get_host", "host_args", "HOST_SCRIPT", "CapabilityError"]
