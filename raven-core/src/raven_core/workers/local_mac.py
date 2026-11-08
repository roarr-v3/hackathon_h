"""This Mac, bare metal: `screencapture` for screenshots, Quartz events for input, and a visible
agent pointer (`mac_pointer`) that glides to every action.

macOS lets the app that started Raven (Raven Agent, Terminal, ...) see the screen and post input
only once it is allowed in System Settings → Privacy & Security → Screen Recording and
Accessibility; `check_mac_permissions()` asks for both and names the app to turn on.
Text is typed as Unicode and shortcuts follow the current keyboard layout, so AZERTY works.
While an agent runs, Raven Agent's own window is hidden so the agent can't read earlier results.
Moving the mouse into a screen corner aborts the agent (pyautogui's failsafe).
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from raven_core.events import Platform
from raven_core.workers.base import ShellResult, WorkerUnavailable, parse_combo

log = logging.getLogger(__name__)

RAVEN_BUNDLE_ID = "app.raven.agent"
SHOT_MAX_SIDE = int(os.environ.get("RAVEN_LOCAL_SHOT_MAX") or 1280)
GLIDE_S = float(os.environ.get("RAVEN_POINTER_GLIDE_S") or 0.3)

MODIFIER_FLAGS = {"shift": 1 << 17, "ctrl": 1 << 18, "alt": 1 << 19, "cmd": 1 << 20, "fn": 1 << 23}
MODIFIER_KEYCODES = {"cmd": 55, "shift": 56, "alt": 58, "ctrl": 59, "fn": 63}
NAMED_KEYCODES = {
    "enter": 36, "return": 36, "tab": 48, "space": 49, "backspace": 51, "delete": 51, "escape": 53,
    "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
US_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
    "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31,
    "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
}


def _framework(name: str) -> ctypes.CDLL:
    return ctypes.cdll.LoadLibrary(f"/System/Library/Frameworks/{name}.framework/{name}")


def responsible_app() -> tuple[str, str | None, int]:
    """(name, bundle path, pid) of the app macOS holds responsible for this process; TCC asks about it."""
    try:
        fn = ctypes.CDLL("/usr/lib/libSystem.B.dylib").responsibility_get_pid_responsible_for_pid
        fn.restype, fn.argtypes = ctypes.c_int, [ctypes.c_int]
        pid = int(fn(os.getpid()))
        buf = ctypes.create_string_buffer(4096)
        if pid > 0 and ctypes.CDLL("/usr/lib/libproc.dylib").proc_pidpath(pid, buf, ctypes.c_uint32(len(buf))) > 0:
            path = buf.value.decode()
            if ".app/" in path:
                bundle = path[: path.index(".app/") + 4]
                return Path(bundle).stem, bundle, pid
            return Path(path).name, path, pid
    except (OSError, AttributeError):
        pass
    return "the app that started Raven (usually Raven Agent in your Applications folder)", None, 0


def missing_mac_permissions(prompt: bool = True) -> list[str]:
    """Screen Recording / Accessibility not yet granted; with `prompt`, macOS shows its dialogs."""
    cg = _framework("CoreGraphics")
    cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
    cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
    missing = []
    if not cg.CGPreflightScreenCaptureAccess():
        if prompt:
            cg.CGRequestScreenCaptureAccess()
        missing.append("Screen Recording")
    try:
        from ApplicationServices import (  # type: ignore[import-not-found]
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )

        trusted = bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt}))
    except ImportError:
        ax = _framework("ApplicationServices")
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        trusted = bool(ax.AXIsProcessTrusted())
    if not trusted:
        missing.append("Accessibility")
    return missing


def check_mac_permissions() -> None:
    missing = missing_mac_permissions()
    if missing:
        name, bundle, _ = responsible_app()
        where = f" ({bundle})" if bundle and not bundle.startswith("/Applications/") else ""
        raise WorkerUnavailable(
            f"macOS hasn't allowed {name}{where} to use {' and '.join(missing)}. Turn it on in System Settings → "
            "Privacy & Security (macOS may already be asking), choose Quit & Reopen if offered, then retry "
            "(or add 'in a vm' to the prompt to use a VM)."
        )


def log_permission_state() -> None:
    name, bundle, _ = responsible_app()
    missing = missing_mac_permissions(prompt=False)
    log.info(
        "bare-metal runs on this Mac act as %s (%s); missing permissions: %s",
        name, bundle or "?", ", ".join(missing) or "none",
    )


def hide_raven_window() -> list[Any]:
    """Hide Raven Agent while an agent drives this screen; returns the apps to unhide later."""
    from AppKit import NSWorkspace  # type: ignore[import-not-found]

    apps = [
        app for app in NSWorkspace.sharedWorkspace().runningApplications()
        if str(app.bundleIdentifier() or "").startswith(RAVEN_BUNDLE_ID)
    ]
    for app in apps:
        app.hide()
    return apps


def resolve_combo(combo: str, layout: Mapping[str, int]) -> tuple[list[str], int | None]:
    """(modifiers, keycode) for a shortcut; characters follow `layout` (character → keycode)."""
    keys = parse_combo(combo)
    modifiers = [k for k in keys if k in MODIFIER_FLAGS]
    rest = [k for k in keys if k not in MODIFIER_FLAGS]
    if not rest:
        return modifiers, None
    key = rest[-1]
    if key in NAMED_KEYCODES:
        return modifiers, NAMED_KEYCODES[key]
    if len(key) == 1 and (key in layout or key in US_KEYCODES):
        return modifiers, layout.get(key, US_KEYCODES.get(key))
    raise ValueError(f"unknown key {key!r} in {combo!r}")


def typing_plan(text: str) -> list[tuple[str, str]]:
    """Unicode runs, with Return/Tab presses for the line breaks and tabs between them."""
    plan: list[tuple[str, str]] = []
    for part in re.split(r"(\n|\t)", text):
        if part in ("\n", "\t"):
            plan.append(("key", "enter" if part == "\n" else "tab"))
        elif part:
            plan.append(("text", part))
    return plan


def layout_keycodes() -> dict[str, int]:
    """Character → keycode for the current keyboard layout (unshifted keys)."""
    import Quartz  # type: ignore[import-not-found]

    chars: dict[str, int] = {}
    for keycode in range(128):
        event = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
        _, char = Quartz.CGEventKeyboardGetUnicodeString(event, 4, None, None)
        if char and len(char) == 1 and char.isprintable() and not char.isspace():
            chars.setdefault(char.lower(), keycode)
    return chars


def _post_key(source: Any, keycode: int, down: bool, flags: int) -> None:
    import Quartz  # type: ignore[import-not-found]

    event = Quartz.CGEventCreateKeyboardEvent(source, keycode, down)
    Quartz.CGEventSetFlags(event, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def press_combo(modifiers: list[str], keycode: int | None) -> None:
    import Quartz  # type: ignore[import-not-found]

    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    flags = 0
    for m in modifiers:
        flags |= MODIFIER_FLAGS[m]
        _post_key(source, MODIFIER_KEYCODES[m], True, flags)
    if keycode is not None:
        _post_key(source, keycode, True, flags)
        time.sleep(0.02)
        _post_key(source, keycode, False, flags)
    for m in reversed(modifiers):
        flags &= ~MODIFIER_FLAGS[m]
        _post_key(source, MODIFIER_KEYCODES[m], False, flags)
    time.sleep(0.03)


def type_unicode(text: str, abort: threading.Event | None = None) -> None:
    import Quartz  # type: ignore[import-not-found]

    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for kind, value in typing_plan(text):
        if kind == "key":
            press_combo([], NAMED_KEYCODES[value])
            continue
        for char in value:
            if abort is not None and abort.is_set():
                return
            units = len(char.encode("utf-16-le")) // 2
            for down in (True, False):
                event = Quartz.CGEventCreateKeyboardEvent(source, 0, down)
                Quartz.CGEventSetFlags(event, 0)
                Quartz.CGEventKeyboardSetUnicodeString(event, units, char)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            time.sleep(0.008)


def _screencapture() -> bytes:
    fd, name = tempfile.mkstemp(prefix="raven-shot-", suffix=".png")
    os.close(fd)
    path = Path(name)
    try:
        subprocess.run(["/usr/sbin/screencapture", "-x", "-t", "png", str(path)], check=True, capture_output=True, timeout=30)
        subprocess.run(["/usr/bin/sips", "-Z", str(SHOT_MAX_SIDE), str(path)], check=True, capture_output=True, timeout=30)
        return path.read_bytes()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise WorkerUnavailable(f"screencapture failed: {e}") from e
    finally:
        path.unlink(missing_ok=True)


class AgentPointer:
    """The on-screen agent pointer (a `mac_pointer` helper process). Never fails the agent."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None

    def send(self, op: str, **data: Any) -> None:
        if os.environ.get("RAVEN_SHOW_POINTER", "1").strip().lower() in ("0", "false", "no", "off"):
            return
        try:
            if self._proc is None or self._proc.poll() is not None:
                self._proc = subprocess.Popen(
                    [sys.executable, "-m", "raven_core.workers.mac_pointer"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                )
            assert self._proc.stdin is not None
            self._proc.stdin.write((json.dumps({"op": op, **data}) + "\n").encode())
            self._proc.stdin.flush()
        except (OSError, ValueError) as e:
            log.warning("agent pointer unavailable: %s", e)
            self._proc = None

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()


class LocalMacWorker:
    """Worker protocol on this Mac's own screen (macOS, or the iOS Simulator window)."""

    bare_metal = True

    def __init__(self, platform: Platform = Platform.macos) -> None:
        self.platform = Platform(platform)
        self.name = "this-mac"
        self._checked = False
        self._pointer = AgentPointer()
        self._hidden: Any = None
        self._abort = threading.Event()

    def __repr__(self) -> str:
        return f"LocalMacWorker({self.platform.value}, this-mac)"

    async def preflight(self) -> None:
        if not self._checked:
            await asyncio.to_thread(check_mac_permissions)
            self._checked = True

    async def begin(self) -> None:
        self._abort.clear()
        self._hidden = await asyncio.to_thread(hide_raven_window)
        x, y = await asyncio.to_thread(lambda: tuple(self._gui().position()))
        self._pointer.send("show", x=int(x), y=int(y))

    async def finish(self) -> None:
        self._abort.set()
        self._pointer.send("hide")
        apps, self._hidden = self._hidden or [], None
        for app in apps:
            await asyncio.to_thread(app.unhide)

    @staticmethod
    def _gui() -> Any:
        import pyautogui  # type: ignore[import-untyped]

        pyautogui.PAUSE = 0.05
        return pyautogui

    async def _glide(self, x: int, y: int) -> None:
        self._pointer.send("move", x=x, y=y)
        gui = self._gui()
        await asyncio.to_thread(gui.moveTo, x, y, duration=GLIDE_S, tween=gui.easeInOutQuad)

    async def screenshot(self) -> bytes:
        return await asyncio.to_thread(_screencapture)

    async def size(self) -> tuple[int, int]:
        w, h = await asyncio.to_thread(lambda: tuple(self._gui().size()))
        return int(w), int(h)

    async def click(self, x: int, y: int, button: str = "left") -> None:
        await self._glide(int(x), int(y))
        self._pointer.send("click", x=int(x), y=int(y))
        await asyncio.to_thread(self._gui().click, int(x), int(y), button=button)

    async def double_click(self, x: int, y: int) -> None:
        await self._glide(int(x), int(y))
        self._pointer.send("click", x=int(x), y=int(y), count=2)
        await asyncio.to_thread(self._gui().doubleClick, int(x), int(y))

    async def type_text(self, text: str) -> None:
        self._pointer.send("typing")
        await asyncio.to_thread(type_unicode, text, self._abort)

    async def key(self, combo: str) -> None:
        modifiers, keycode = resolve_combo(combo, await asyncio.to_thread(layout_keycodes))
        self._pointer.send("typing")
        await asyncio.to_thread(press_combo, modifiers, keycode)

    async def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        await self._glide(int(x), int(y))

        def run() -> None:
            gui = self._gui()
            if dy:
                gui.scroll(-int(dy))
            if dx:
                gui.hscroll(-int(dx))

        await asyncio.to_thread(run)

    async def shell(self, cmd: str, timeout: int = 60) -> ShellResult:
        from raven_core.machines import local_shell

        return await local_shell(cmd, timeout)

    async def close(self) -> None:
        self._pointer.close()
