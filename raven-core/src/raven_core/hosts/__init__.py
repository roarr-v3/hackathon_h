"""Hosts = test machines that run H's desktop agent via `raven_host.py` (copied in).

A host knows how to (1) prepare the machine (hai-agents + raven_host.py), (2) spawn the
host script with stdin/stdout pipes, (3) read back screenshot files it wrote.
Selection per platform comes from env (later raven.yaml):
  RAVEN_HOST_LINUX   cua-local (default) | devin-cloud
  RAVEN_HOST_MACOS   cua-local (default) | devin-cloud
  RAVEN_HOST_IOS     mac-simulator (default) | devin-cloud
  RAVEN_HOST_WINDOWS home-pc
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from raven_core.events import Platform
from raven_core.workers.base import WorkerUnavailable

HOST_SCRIPT = Path(__file__).with_name("raven_host.py")
PASS_ENV = ("HAI_API_KEY", "HAI_MODEL", "HAI_BASE_URL")


class Host:
    """Base class; subclasses implement `_spawn_argv`, `prepare` and `read_file`."""

    kind = "host"

    def __init__(self, platform: Platform, name: str):
        self.platform = Platform(platform)
        self.name = name
        self._ready = False

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.platform.value}, {self.name})"

    async def prepare(self) -> None:
        self._ready = True

    def _spawn_argv(self, host_args: list[str], env: dict[str, str]) -> list[str]:
        raise NotImplementedError

    async def spawn(self, host_args: list[str]) -> asyncio.subprocess.Process:
        if not self._ready:
            await self.prepare()
        env = {k: os.environ[k] for k in PASS_ENV if os.environ.get(k)}
        if "HAI_API_KEY" not in env:
            raise WorkerUnavailable("HAI_API_KEY is not set (.env at repo root)")
        argv = self._spawn_argv(host_args, env)
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **env},
        )

    async def read_file(self, path: str) -> bytes:
        raise NotImplementedError


async def _run(*argv: str, timeout: float = 600) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    return proc.returncode or 0, out.decode(errors="replace")


class DockerHost(Host):
    """Cua Linux sandbox (a Docker container under colima). Uses `docker exec` directly:
    `cua sb exec` mangles sudo/env args and doesn't stream stdin."""

    kind = "cua-local"
    # H ships no hai-agent-runtime for linux-arm64 (Apple-silicon containers), so the loop runs at H.
    default_mode = "agents-api"

    def __init__(self, platform: Platform, container: str, user: str = "cua", display: str = ":1"):
        super().__init__(platform, container)
        self.user, self.home, self.display = user, f"/home/{user}", display

    def _exec(self, *cmd: str, interactive: bool = False, env: dict[str, str] | None = None) -> list[str]:
        argv = ["docker", "exec"] + (["-i"] if interactive else [])
        argv += ["-u", self.user, "-w", self.home, "-e", f"HOME={self.home}", "-e", f"DISPLAY={self.display}"]
        argv += ["-e", "XDG_SESSION_TYPE=x11"]  # pyscreeze only uses scrot when it knows it's on X11
        for k in env or {}:
            argv += ["-e", k]  # value inherited from our process env; never on the command line
        return argv + [self.name, *cmd]

    async def prepare(self) -> None:
        # H's desktop driver (pyautogui): mouseinfo sys.exit()s without tkinter, pyscreeze needs scrot,
        # Xlib wants an ~/.Xauthority file even on an auth-less Xvfb.
        rc, out = await _run(
            "docker", "exec", "-u", "root", self.name, "bash", "-c",
            "dpkg -s python3-tk scrot >/dev/null 2>&1 || (export DEBIAN_FRONTEND=noninteractive; "
            "apt-get update -qq && apt-get install -y -qq python3-tk scrot python3-dev gcc)",
        )
        if rc:
            raise WorkerUnavailable(f"{self!r}: installing desktop driver deps failed: {out[-400:]}")
        rc, out = await _run(*self._exec("bash", "-lc", "touch $HOME/.Xauthority; test -x $HOME/.local/bin/holo || uv tool install 'hai-agents[cli,desktop]>=1.4.3'"))
        if rc:
            raise WorkerUnavailable(f"{self!r}: installing hai-agents failed: {out[-400:]}")
        rc, out = await _run("docker", "cp", str(HOST_SCRIPT), f"{self.name}:{self.home}/raven_host.py")
        if rc:
            raise WorkerUnavailable(f"{self!r}: copying raven_host.py failed: {out[-400:]}")
        self._ready = True

    def _spawn_argv(self, host_args: list[str], env: dict[str, str]) -> list[str]:
        py = f"{self.home}/.local/share/uv/tools/hai-agents/bin/python"
        return self._exec(py, f"{self.home}/raven_host.py", *host_args, interactive=True, env=env)

    async def read_file(self, path: str) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            *self._exec("cat", path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        data, _ = await proc.communicate()
        return data


class LocalHost(Host):
    """This Mac (e.g. driving the iOS Simulator). H's agent takes over this screen."""

    kind = "mac-simulator"

    def _spawn_argv(self, host_args: list[str], env: dict[str, str]) -> list[str]:
        return [sys.executable, str(HOST_SCRIPT), *host_args]

    async def read_file(self, path: str) -> bytes:
        return Path(path).read_bytes()


def get_host(platform: Platform) -> Host:
    platform = Platform(platform)
    choice = os.environ.get(f"RAVEN_HOST_{platform.value.upper()}", "").strip()
    if platform == Platform.linux and choice in ("", "cua-local"):
        return DockerHost(platform, os.environ.get("RAVEN_LINUX_SANDBOX", "raven-linux"))
    if platform == Platform.ios and choice in ("", "mac-simulator"):
        return LocalHost(platform, "this-mac")
    raise WorkerUnavailable(
        f"host '{choice or 'default'}' for {platform.value} is not implemented yet "
        f"(set RAVEN_H_MODE=models-loop to use the Holo4 loop through Cua)"
    )


def host_args(task: str, mode: str, model: str | None, shots_dir: str, max_steps: int = 60, max_time: float = 600) -> list[str]:
    args = ["--task", task, "--mode", mode, "--shots-dir", shots_dir,
            "--max-steps", str(max_steps), "--max-time", str(max_time)]
    if model:
        args += ["--model", model]
    return args


__all__ = ["Host", "DockerHost", "LocalHost", "get_host", "host_args", "HOST_SCRIPT"]
