"""Cua-backed worker (python SDK `cua_sandbox`, pip/uv package `cua-sandbox`).

- Existing local Spaces:    Sandbox.connect("local:raven-linux-apple") (Apple Containerization)
                             Sandbox.connect("local:raven-mac")         (explicit Lume VM)

The SDK is imported lazily so tests / fake mode don't need it installed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from raven_core.events import Platform
from raven_core.workers.base import ShellResult, parse_combo, png_size

log = logging.getLogger(__name__)


def _patch_cua_embedded() -> None:
    """cua-sandbox 0.9.1 passes kwargs (fleet_from_env) that cua 0.12.1's embedded() rejects."""
    import inspect

    import cua  # type: ignore

    orig = cua.embedded
    if getattr(orig, "_raven_patched", False):
        return
    allowed = inspect.signature(orig).parameters

    def embedded(*args: Any, **kwargs: Any) -> Any:
        return orig(*args, **{k: v for k, v in kwargs.items() if k in allowed})

    embedded._raven_patched = True  # type: ignore[attr-defined]
    cua.embedded = embedded


class CuaWorker:
    def __init__(
        self,
        platform: Platform,
        name: str,
        *,
        connect_timeout: float = 60.0,
    ):
        from raven_core.machines import space_name

        self.platform = Platform(platform)
        if self.platform not in (Platform.linux, Platform.macos):
            from raven_core.hosts.capabilities import CapabilityCategory, CapabilityError

            raise CapabilityError(CapabilityCategory.configuration, "Cua supports named local Linux/macOS Spaces; Windows uses Devin.")
        self.name = space_name(name)
        self._connect_timeout = connect_timeout
        self._sb: Any = None
        self._lock = asyncio.Lock()
        self._size: tuple[int, int] | None = None
        self._prepared = False
        self.preparation_phase = "checking Space metadata"
        self._runtime_checked = False
        self.target = "apple-container" if self.platform == Platform.linux else "lume"

    def __repr__(self) -> str:
        return f"CuaWorker({self.platform.value}, local:{self.name})"

    async def prepare(self) -> None:
        if self._prepared:
            return
        from raven_core.machines import ensure_space

        await ensure_space(self.platform, self.name)
        self._runtime_checked = True
        self.preparation_phase = "connecting to the selected Space"
        await self._sandbox()
        from raven_core.hosts.capabilities import CapabilityCategory, CapabilityError

        try:
            self.preparation_phase = "waiting for the selected desktop to return a screenshot"
            png = await asyncio.wait_for(self.screenshot(), timeout=min(15.0, self._connect_timeout))
        except TimeoutError:
            raise CapabilityError(
                CapabilityCategory.desktop_unavailable,
                f"The selected desktop `{self.name}` did not answer its screenshot probe within "
                f"{min(15.0, self._connect_timeout):g}s. A running Space listing does not establish "
                "desktop reachability; check the guest service, network route, and local-network permissions.",
            ) from None
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            from raven_core.hosts.capabilities import command_failure

            raise command_failure("Checking the selected Cua desktop", str(exc), local_desktop=True) from None
        if png_size(png) == (0, 0):
            raise CapabilityError(CapabilityCategory.desktop_unavailable, "The selected Cua desktop returned no PNG screenshot")
        self._prepared = True
        self.preparation_phase = "desktop ready"

    async def _sandbox(self) -> Any:
        if self._sb is not None:
            return self._sb
        async with self._lock:
            if self._sb is not None:
                return self._sb
            if not self._runtime_checked:
                from raven_core.hosts.capabilities import CapabilityCategory, CapabilityError
                from raven_core.machines import space_info, validate_space

                row = await space_info(self.name)
                if row is None:
                    raise CapabilityError(CapabilityCategory.desktop_unavailable, "The selected local Space does not exist")
                validate_space(row, self.platform)
                self._runtime_checked = True
            try:
                _patch_cua_embedded()
                from cua_sandbox import Sandbox  # type: ignore
            except ImportError:  # pragma: no cover - depends on env
                from raven_core.hosts.capabilities import CapabilityCategory, CapabilityError

                raise CapabilityError(
                    CapabilityCategory.dependency, "Install Raven's cua-sandbox dependency to use the Cua Models driver",
                ) from None
            try:
                conn = Sandbox.connect(f"local:{self.name}", local=True)
                self._sb = await asyncio.wait_for(_awaitable(conn), self._connect_timeout)
            except Exception as e:
                from raven_core.hosts.capabilities import command_failure

                raise command_failure(f"Connecting to {self!r}", str(e), local_desktop=True) from None
            log.info("connected %r", self)
            return self._sb

    async def _reset(self) -> None:
        sb, self._sb = self._sb, None
        self._runtime_checked = False
        if sb is not None:
            try:
                await sb.disconnect()
            except Exception:
                pass

    async def _call(self, fn_name: str, *path_and_args: Any, **kwargs: Any) -> Any:
        """Read-only calls may reconnect once. Never replay input/setup after an uncertain send."""
        path = fn_name.split(".")
        attempts = (0, 1) if fn_name in ("screenshot", "screen.size", "get_dimensions") else (1,)
        for attempt in attempts:
            sb = await self._sandbox()
            target: Any = sb
            for p in path:
                target = getattr(target, p)
            try:
                return await target(*path_and_args, **kwargs)
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                if attempt:
                    raise
                log.warning("%r %s failed (%s), reconnecting", self, fn_name, type(e).__name__)
                await self._reset()

    # --- Worker protocol -------------------------------------------------
    async def screenshot(self) -> bytes:
        png = await self._call("screenshot")
        if isinstance(png, str):  # some SDK versions return base64
            import base64

            png = base64.b64decode(png)
        return bytes(png)

    async def size(self) -> tuple[int, int]:
        if self._size is None:
            try:
                w, h = await self._call("screen.size")
            except Exception:
                try:
                    w, h = await self._call("get_dimensions")
                except Exception:
                    w, h = png_size(await self.screenshot())
            self._size = (int(w), int(h))
        return self._size

    async def click(self, x: int, y: int, button: str = "left") -> None:
        if button == "right":
            await self._call("mouse.right_click", int(x), int(y))
        else:
            await self._call("mouse.click", int(x), int(y), button=button)

    async def double_click(self, x: int, y: int) -> None:
        await self._call("mouse.double_click", int(x), int(y))

    async def type_text(self, text: str) -> None:
        await self._call("keyboard.type", text)

    async def key(self, combo: str) -> None:
        keys = parse_combo(combo)
        meta = {Platform.windows: "win", Platform.linux: "super"}.get(self.platform)
        if meta:
            keys = [meta if k == "cmd" else k for k in keys]
        try:
            await self._call("keyboard.keypress", keys)
        except TypeError:
            await self._call("keyboard.keypress", "+".join(keys))

    async def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        await self._call("mouse.scroll", int(x), int(y), scroll_x=int(dx), scroll_y=int(dy))

    async def shell(self, cmd: str, timeout: int = 60) -> ShellResult:
        r = await self._call("shell.run", cmd, timeout=timeout)
        return int(getattr(r, "returncode", 0) or 0), str(getattr(r, "stdout", "") or ""), str(getattr(r, "stderr", "") or "")

    async def close(self) -> None:
        await self._reset()
        self._prepared = False


async def _awaitable(x: Any) -> Any:
    return await x
