"""Cua CLI transport verified against installed ``cua sb exec --help`` (0.10.1).

`exec --stdin` is argv-preserving, streams all three pipes, forwards signals and
kills the guest process when the client exits. It does NOT share `cua do switch`'s
global target. The older Python SDK lacks Apple creation enums; creation goes
through machines.ensure_space's explicit --runtime containerization instead.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from raven_core.events import HMode, Platform
from raven_core.hosts.capabilities import CapabilityCategory as Category, CapabilityError, command_failure
from raven_core.hosts.remote import RemoteHost


class CuaHost(RemoteHost):
    kind = "cua-local"

    def __init__(
        self, platform: Platform, name: str, *, mode: HMode | str = HMode.agents_api,
        user: str = "cua", display: str = ":1",
    ):
        from raven_core.machines import space_name

        super().__init__(platform, space_name(name), mode=mode)
        self.target = "apple-container" if self.platform == Platform.linux else "lume"
        self.user, self.display = user, display
        self._cli: str | None = None

    def _argv(self, command: list[str]) -> list[str]:
        from raven_core.machines import _which

        cua = self._cli or _which("cua")
        argv = [cua, "sb", "exec", "--stdin"]
        if self.platform == Platform.linux:
            argv += ["--user", self.user, "--cwd", f"/home/{self.user}"]
        argv += [f"local:{self.name}", "--"]
        if self.platform == Platform.linux:
            argv += ["env", f"HOME=/home/{self.user}", f"DISPLAY={self.display}", "XDG_SESSION_TYPE=x11"]
        return [*argv, *command]

    async def _start(self, argv: list[str]) -> asyncio.subprocess.Process:
        from raven_core.machines import tool_env

        return await asyncio.create_subprocess_exec(
            *self._argv(argv), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=tool_env(),
        )

    def _failure(self, operation: str, output: str) -> CapabilityError:
        return command_failure(operation, output, local_desktop=True)

    async def prepare(self) -> None:
        from raven_core import machines

        async with self._prepare_lock:
            if self._ready:
                return
            await machines.ensure_space(self.platform, self.name)
            self._cli = machines._which("cua")
            rc, help_text = await machines._run(self._cli, "sb", "exec", "--help", timeout=15)
            if rc or "--stdin" not in help_text:
                raise CapabilityError(Category.dependency, "This Cua CLI lacks `sb exec --stdin`; update Cua for safe streaming execution.")
            if self.platform == Platform.linux and self.default_mode == HMode.local_runtime:
                rc, out, err = await self._capture(["uname", "-m"])
                if rc:
                    raise self._failure("Checking Linux guest architecture", err.decode(errors="replace"))
                if out.decode().strip().lower() in ("arm64", "aarch64"):
                    raise CapabilityError(
                        Category.unsupported_runtime,
                        "H local runtime is not published for Linux ARM64 inside this Space. Hosted H works on ARM64; "
                        "the selected local-runtime mode requires a supported Linux VM.",
                    )
            await self._bootstrap()
            await self._probe()
            self._ready = True

    async def screenshot(self) -> bytes:
        from raven_core import machines

        with tempfile.TemporaryDirectory(prefix="raven-cua-shot-") as folder:
            path = Path(folder) / "screen.png"
            rc, output = await machines._run(
                self._cli or machines._which("cua"), "sb", "screenshot", f"local:{self.name}", "--output", str(path),
                timeout=30,
            )
            if rc:
                raise self._failure("Capturing the selected Space", output)
            return path.read_bytes()


AppleContainerHost = CuaHost
