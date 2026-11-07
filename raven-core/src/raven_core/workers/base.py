"""Worker protocol: one remote computer (Cua sandbox, remote cua-spacesd, or fake).

All methods are async. Coordinates are *screen* pixels as reported by `size()`
(the engine maps Holo's [0, 1000] grid onto `size()`, not onto the PNG, so a
HiDPI screenshot that is 2x the logical screen still clicks correctly).
"""

from __future__ import annotations

import struct
from typing import Protocol, runtime_checkable

from raven_core.events import Platform

ShellResult = tuple[int, str, str]  # (returncode, stdout, stderr)


@runtime_checkable
class Worker(Protocol):
    name: str
    platform: Platform

    async def screenshot(self) -> bytes:
        """PNG bytes of the full screen."""
        ...

    async def size(self) -> tuple[int, int]:
        """Logical screen size (w, h) in the coordinate space of click()."""
        ...

    async def click(self, x: int, y: int, button: str = "left") -> None: ...

    async def double_click(self, x: int, y: int) -> None: ...

    async def type_text(self, text: str) -> None: ...

    async def key(self, combo: str) -> None:
        """Press a key or chord, e.g. "enter", "ctrl+c", "cmd+shift+t"."""
        ...

    async def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        """Wheel scroll at (x, y). dy > 0 scrolls down, dx > 0 scrolls right (in notches)."""
        ...

    async def shell(self, cmd: str, timeout: int = 60) -> ShellResult: ...

    async def close(self) -> None: ...


class WorkerUnavailable(RuntimeError):
    """Raised when a worker for the requested platform can't be built/reached."""


def png_size(png: bytes) -> tuple[int, int]:
    """(width, height) from a PNG's IHDR chunk; (0, 0) if not a PNG."""
    if len(png) >= 24 and png[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", png[16:24])
        return int(w), int(h)
    return 0, 0


_KEY_ALIASES = {
    "return": "enter",
    "ret": "enter",
    "esc": "escape",
    "control": "ctrl",
    "ctl": "ctrl",
    "command": "cmd",
    "meta": "cmd",
    "super": "cmd",
    "win": "cmd",
    "windows": "cmd",
    "option": "alt",
    "opt": "alt",
    "del": "delete",
    "bksp": "backspace",
    "back_space": "backspace",
    "pgup": "pageup",
    "page_up": "pageup",
    "pgdn": "pagedown",
    "page_down": "pagedown",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "spacebar": "space",
    " ": "space",
}


def parse_combo(combo: str) -> list[str]:
    """'Ctrl + Shift+T' -> ["ctrl", "shift", "t"]; normalizes common aliases."""
    if combo and not combo.strip():
        return ["space"]
    combo = combo.strip()
    if combo == "+":
        return ["+"]
    parts = [p.strip() for p in combo.split("+")] if len(combo) > 1 else [combo]
    if combo.endswith("++"):  # "ctrl++" -> ctrl and plus
        parts = [p for p in parts if p] + ["+"]
    keys = []
    for p in parts:
        if not p:
            continue
        low = p.lower()
        keys.append(_KEY_ALIASES.get(low, low))
    return keys or [combo]
