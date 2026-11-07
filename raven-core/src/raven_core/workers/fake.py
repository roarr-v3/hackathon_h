"""Offline worker: generates a PNG, records every action. Enabled with RAVEN_FAKE_WORKERS=1."""

from __future__ import annotations

import asyncio
import logging
import struct
import zlib

from raven_core.events import Platform
from raven_core.workers.base import ShellResult

log = logging.getLogger(__name__)

_COLORS = {
    Platform.macos: (40, 40, 48),
    Platform.linux: (36, 52, 40),
    Platform.windows: (24, 44, 72),
    Platform.ios: (10, 10, 10),
}


def make_png(width: int, height: int, rgb: tuple[int, int, int], marker: tuple[int, int] | None = None) -> bytes:
    """Tiny dependency-free PNG encoder (solid color + optional crosshair)."""
    r, g, b = rgb
    row = bytes([r, g, b]) * width
    rows = []
    mx, my = marker if marker else (-1, -1)
    for y in range(height):
        if y == my:
            line = b"\xff\x40\x40" * width
        elif mx >= 0:
            line = row[: mx * 3] + b"\xff\x40\x40" + row[(mx + 1) * 3 :]
        else:
            line = row
        rows.append(b"\x00" + line)
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


class FakeWorker:
    def __init__(self, platform: Platform, name: str | None = None, width: int = 1280, height: int = 800):
        self.platform = Platform(platform)
        self.name = name or f"fake-{self.platform.value}"
        self.width = width
        self.height = height
        self.actions: list[tuple] = []
        self.shell_results: dict[str, ShellResult] = {}
        self._last_click: tuple[int, int] | None = None

    def _record(self, *action) -> None:
        self.actions.append(action)
        log.info("[%s] %s", self.name, action)

    async def screenshot(self) -> bytes:
        await asyncio.sleep(0)
        return make_png(self.width, self.height, _COLORS.get(self.platform, (30, 30, 30)), self._last_click)

    async def size(self) -> tuple[int, int]:
        return self.width, self.height

    async def click(self, x: int, y: int, button: str = "left") -> None:
        self._last_click = (max(0, min(self.width - 1, x)), max(0, min(self.height - 1, y)))
        self._record("click", x, y, button)

    async def double_click(self, x: int, y: int) -> None:
        self._last_click = (max(0, min(self.width - 1, x)), max(0, min(self.height - 1, y)))
        self._record("double_click", x, y)

    async def type_text(self, text: str) -> None:
        self._record("type_text", text)

    async def key(self, combo: str) -> None:
        self._record("key", combo)

    async def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        self._record("scroll", x, y, dx, dy)

    async def shell(self, cmd: str, timeout: int = 60) -> ShellResult:
        self._record("shell", cmd)
        return self.shell_results.get(cmd, (0, "", ""))

    async def close(self) -> None:
        self._record("close")
