"""Shared, authenticated stdio host protocol over Cua exec or Devin CLI SSH.

Only the transport chooses a machine. Setup, preflight, H's script, native driver
commands and artifact reads all use that same transport. No secret-bearing argv.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from raven_core.events import HMode, Platform
from raven_core.hosts import HOST_SCRIPT, Host, check_preflight_result, h_environment
from raven_core.hosts.capabilities import CapabilityCategory as Category, CapabilityError, command_failure
from raven_core.workers.base import ShellResult, parse_combo

# Both Devin Windows (Git Bash) and Linux have a bash command shell. Use a private
# Python environment if necessary; never install a system driver or require sudo.
# Missing native GUI prerequisites are reported by --preflight, not "fixed" blindly.
BOOTSTRAP = r"""
set -eu
PY=""
for candidate in "$HOME/.local/share/uv/tools/hai-agents/bin/python" \
                 "$HOME/.local/share/uv/tools/hai-agents/Scripts/python.exe" python3 python; do
  if "$candidate" -c 'import hai_agents, hai_drivers' >/dev/null 2>&1; then
    PY="$candidate"; break
  fi
done
if [ -z "$PY" ]; then
  command -v uv >/dev/null 2>&1 || { echo RAVEN_NEEDS_UV_AND_HAI >&2; exit 78; }
  uv tool install --python 3.12 --with 'hai-drivers==0.1.16' --with 'soupsieve<4' \
    'hai-agents[cli,desktop]==1.4.3' >&2
  for candidate in "$HOME/.local/share/uv/tools/hai-agents/bin/python" \
                   "$HOME/.local/share/uv/tools/hai-agents/Scripts/python.exe"; do
    if "$candidate" -c 'import hai_agents, hai_drivers' >/dev/null 2>&1; then
      PY="$candidate"; break
    fi
  done
  # On Windows uv's tool directory is normally under LOCALAPPDATA, not ~/.local.
  if [ -z "$PY" ]; then
    ROOT="$(uv tool dir)"
    for candidate in "$ROOT/hai-agents/bin/python" "$ROOT/hai-agents/Scripts/python.exe"; do
      if "$candidate" -c 'import hai_agents, hai_drivers' >/dev/null 2>&1; then
        PY="$candidate"; break
      fi
    done
  fi
fi
[ -n "$PY" ] || { echo RAVEN_NEEDS_PYTHON >&2; exit 78; }
"$PY" -c 'import json,sys,pathlib; print(json.dumps({"python":sys.executable,"home":str(pathlib.Path.home())}))'
"""

UPLOAD = """
import os, pathlib, sys, tempfile
root = pathlib.Path.home() / ".cache" / "raven"
root.mkdir(mode=0o700, parents=True, exist_ok=True)
fd, name = tempfile.mkstemp(dir=root, prefix="host-", suffix=".py")
try:
    with os.fdopen(fd, "wb") as f:
        f.write(sys.stdin.buffer.read())
    os.replace(name, root / "raven_host.py")
finally:
    pathlib.Path(name).unlink(missing_ok=True)
"""

READ_ARTIFACT = """
import json, pathlib, sys, tempfile
p = pathlib.Path(json.load(sys.stdin)["path"]).resolve()
root = (pathlib.Path(tempfile.gettempdir()) / "raven-shots").resolve()
if not p.is_relative_to(root) or p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
    raise ValueError("not a Raven screenshot artifact")
sys.stdout.buffer.write(p.read_bytes())
"""


async def stop_process(proc: asyncio.subprocess.Process, grace_s: float = 5) -> None:
    """EOF lets raven_host cancel H first; the transport then terminates its remote child."""
    if proc.returncode is not None:
        return
    if proc.stdin and not proc.stdin.is_closing():
        proc.stdin.close()
    async def discard(stream: Any) -> None:
        if stream is not None:
            try:
                while await stream.read(65536):
                    pass
            except (RuntimeError, OSError):
                pass  # an engine reader may already be draining this pipe

    drains = [asyncio.create_task(discard(getattr(proc, name, None))) for name in ("stdout", "stderr")]
    try:
        for action in (proc.terminate, proc.kill, None):
            try:
                await asyncio.wait_for(proc.wait(), grace_s)
                return
            except TimeoutError:
                if proc.returncode is not None:
                    return
                if action is not None:
                    try:
                        action()
                    except ProcessLookupError:
                        return
        raise CapabilityError(Category.transport, "Could not confirm the host process stopped; inspect the selected machine")
    finally:
        for task in drains:
            task.cancel()
        await asyncio.gather(*drains, return_exceptions=True)


async def finish_cleanup(task: asyncio.Task) -> Any:
    """Finish known in-flight allocation/cleanup even after repeated Stop signals.

    Call only while propagating cancellation or cleaning up an error. The task's
    own timeout still bounds it; cancellation must not orphan a VM/process.
    """
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class RemoteHost(Host):
    """Transport subclasses implement `_start(argv)` and machine preparation."""

    def __init__(self, platform: Platform, name: str, *, mode: HMode | str):
        super().__init__(platform, name, mode=mode)
        self._python: str | None = None
        self._script: str | None = None
        self._processes: set[asyncio.subprocess.Process] = set()

    async def _start(self, argv: list[str]) -> asyncio.subprocess.Process:
        raise NotImplementedError

    async def _launch(self, argv: list[str]) -> asyncio.subprocess.Process:
        start = asyncio.create_task(self._start(argv))
        try:
            proc = await asyncio.shield(start)
        except asyncio.CancelledError:
            async def cleanup() -> None:
                proc = await start
                await stop_process(proc, grace_s=1)

            await finish_cleanup(asyncio.create_task(cleanup()))
            raise
        self._processes.add(proc)
        return proc

    def _failure(self, operation: str, output: str) -> CapabilityError:
        return command_failure(operation, output)

    async def _capture(
        self, argv: list[str], *, data: bytes | None = None, timeout: float = 60,
    ) -> tuple[int, bytes, bytes]:
        proc = await self._launch(argv)
        try:
            out, err = await asyncio.wait_for(proc.communicate(data), timeout)
            return proc.returncode or 0, out, err
        except (TimeoutError, asyncio.CancelledError):
            await finish_cleanup(asyncio.create_task(stop_process(proc, grace_s=1)))
            raise
        finally:
            self._processes.discard(proc)

    async def _bootstrap(self) -> None:
        rc, out, err = await self._capture(["bash", "-lc", BOOTSTRAP], timeout=300)
        if rc:
            if rc == 78:
                raise CapabilityError(Category.dependency, "Guest bootstrap needs uv/Python and hai-agents[desktop]; configure its environment first.")
            raise self._failure("Bootstrapping H on the selected guest", err.decode(errors="replace"))
        try:
            info = json.loads(out)
            if not all(isinstance(info[k], str) and info[k] for k in ("python", "home")):
                raise ValueError
            self._python = str(info["python"]).replace("\\", "/")
            self._script = str(info["home"]).replace("\\", "/").rstrip("/") + "/.cache/raven/raven_host.py"
        except (ValueError, KeyError, TypeError) as exc:
            raise CapabilityError(Category.protocol, "Guest bootstrap returned invalid Python metadata") from exc
        rc, _, err = await self._capture([self._python, "-c", UPLOAD], data=HOST_SCRIPT.read_bytes())
        if rc:
            raise self._failure("Uploading Raven's host script", err.decode(errors="replace"))

    def _script_argv(self, args: list[str]) -> list[str]:
        if not self._python or not self._script:
            raise CapabilityError(Category.configuration, "Guest has not been prepared")
        return [self._python, "-u", self._script, *args]

    async def _probe(self) -> None:
        rc, out, err = await self._capture(self._script_argv([
            "--preflight", "--mode", self.default_mode, "--expected-platform", self.platform.value,
        ]), timeout=45)
        if not out.strip():
            raise self._failure("Checking the selected guest's desktop", err.decode(errors="replace"))
        self._capabilities = check_preflight_result(rc, out.decode(errors="replace"))

    async def spawn(self, host_args: list[str]) -> asyncio.subprocess.Process:
        env = h_environment()
        if not self._ready:
            await self.prepare()
        proc = await self._launch(self._script_argv(["--env-stdin", *host_args]))
        try:
            assert proc.stdin
            # raven_host consumes exactly one credential envelope before the steering loop.
            proc.stdin.write((json.dumps(env) + "\n").encode())
            await proc.stdin.drain()
        except BaseException:
            await finish_cleanup(asyncio.create_task(stop_process(proc, grace_s=1)))
            self._processes.discard(proc)
            raise
        return proc

    async def shell(self, cmd: str, timeout: int = 60) -> ShellResult:
        # Setup must follow prepare_worker. No resolving/creating/fallback here.
        if not self._ready:
            raise CapabilityError(Category.configuration, "Prepare and retain this worker before running suite setup")
        try:
            rc, out, err = await self._capture(["bash", "-lc", cmd], timeout=timeout)
        except TimeoutError:
            return 124, "", f"setup command timed out after {timeout}s"
        return rc, out.decode(errors="replace"), err.decode(errors="replace")

    async def read_file(self, path: str) -> bytes:
        if not self._python:
            raise CapabilityError(Category.configuration, "Guest has not been prepared")
        rc, out, err = await self._capture(
            [self._python, "-c", READ_ARTIFACT], data=json.dumps({"path": path}).encode(),
        )
        if rc:
            raise self._failure("Reading a Raven screenshot artifact", err.decode(errors="replace"))
        return out

    async def driver(self, operation: str, **args: Any) -> Any:
        if not self._ready:
            raise CapabilityError(Category.configuration, "Prepare this worker before using its desktop")
        rc, out, err = await self._capture(
            self._script_argv(["--driver-json"]), data=json.dumps({"operation": operation, **args}).encode(),
        )
        if rc:
            raise self._failure("Executing a native desktop action", err.decode(errors="replace"))
        try:
            return json.loads(out)["result"]
        except (ValueError, TypeError, KeyError) as exc:
            raise CapabilityError(Category.protocol, "Native desktop driver returned an invalid response") from exc

    async def screenshot(self) -> bytes:
        return base64.b64decode(await self.driver("screenshot"))

    async def close(self) -> None:
        processes, self._processes = self._processes, set()
        await asyncio.gather(*(stop_process(p) for p in processes))


class NativeRemoteWorker:
    """Explicit Models-API fallback. Native driver executes; it makes no decisions.

    Not a Host subclass, so the existing engine selects its Models loop, not H's
    local/hosted harness. It still shares the exact host used by setup.
    """

    def __init__(self, host: RemoteHost):
        self.host = host
        self.platform = host.platform
        self.target = getattr(host, "target", "remote")

    @property
    def name(self) -> str:
        return self.host.name

    async def prepare(self) -> None:
        await self.host.prepare()

    async def screenshot(self) -> bytes:
        return await self.host.screenshot()

    async def size(self) -> tuple[int, int]:
        width, height = await self.host.driver("size")
        return int(width), int(height)

    async def click(self, x: int, y: int, button: str = "left") -> None:
        await self.host.driver("click", x=x, y=y, button=button)

    async def double_click(self, x: int, y: int) -> None:
        await self.host.driver("double_click", x=x, y=y)

    async def type_text(self, text: str) -> None:
        await self.host.driver("type_text", text=text)

    async def key(self, combo: str) -> None:
        await self.host.driver("key", keys=parse_combo(combo))

    async def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        await self.host.driver("scroll", x=x, y=y, dx=dx, dy=dy)

    async def shell(self, cmd: str, timeout: int = 60) -> ShellResult:
        return await self.host.shell(cmd, timeout)

    async def close(self) -> None:
        await self.host.close()

    async def cancel(self) -> None:
        await self.host.cancel()
