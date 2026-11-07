"""Cua-backed worker (python SDK `cua_sandbox`, pip/uv package `cua-sandbox`).

- Local sandboxes by name:   Sandbox.connect("raven-linux")   (Docker/colima container)
                             Sandbox.connect("raven-mac")     (Lume VM)
- Remote cua-spacesd by URL: Sandbox.connect(url="http://<tailscale-ip>:3211", token=...)
  (the Windows PC; see workers/windows/README.md)

The SDK is imported lazily so tests / fake mode don't need it installed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from raven_core.events import Platform
from raven_core.workers.base import ShellResult, WorkerUnavailable, parse_combo, png_size

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
        url: str | None = None,
        token: str | None = None,
        connect_timeout: float = 60.0,
    ):
        self.platform = Platform(platform)
        self.name = name
        self._url = url
        self._token = token
        self._connect_timeout = connect_timeout
        self._sb: Any = None
        self._lock = asyncio.Lock()
        self._size: tuple[int, int] | None = None

    def __repr__(self) -> str:  # never include the token
        where = self._url or f"local:{self.name}"
        return f"CuaWorker({self.platform.value}, {where})"

    async def _sandbox(self) -> Any:
        if self._sb is not None:
            return self._sb
        async with self._lock:
            if self._sb is not None:
                return self._sb
            try:
                _patch_cua_embedded()
                from cua_sandbox import Sandbox  # type: ignore
            except ImportError as e:  # pragma: no cover - depends on env
                raise WorkerUnavailable(
                    "cua-sandbox is not installed (cd raven-core && uv add cua-sandbox), "
                    "or set RAVEN_FAKE_WORKERS=1"
                ) from e
            try:
                if self._url:
                    conn = Sandbox.connect(url=self._url, token=self._token)
                else:
                    conn = Sandbox.connect(self.name)
                self._sb = await asyncio.wait_for(_awaitable(conn), self._connect_timeout)
            except Exception as e:
                raise WorkerUnavailable(f"cannot connect to {self!r}: {type(e).__name__}: {e}") from e
            log.info("connected %r", self)
            return self._sb

    async def _reset(self) -> None:
        sb, self._sb = self._sb, None
        if sb is not None:
            try:
                await sb.disconnect()
            except Exception:
                pass

    async def _call(self, fn_name: str, *path_and_args: Any, **kwargs: Any) -> Any:
        """Call sb.<path>(*args) with one reconnect-retry on transport errors."""
        path = fn_name.split(".")
        for attempt in (0, 1):
            sb = await self._sandbox()
            target: Any = sb
            for p in path:
                target = getattr(target, p)
            try:
                return await target(*path_and_args, **kwargs)
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                if attempt:
                    raise
                log.warning("%r %s failed (%s), reconnecting", self, fn_name, e)
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


async def _awaitable(x: Any) -> Any:
    return await x
