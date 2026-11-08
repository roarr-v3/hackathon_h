"""Where an agent runs: on this machine (bare metal) when it is the target OS, else in a VM.

A prompt that asks for a VM/sandbox/container wins, one that asks for this machine forces bare
metal, and RAVEN_PLACEMENT (auto | bare-metal | vm) sets the default. iOS runs in the Simulator
on a Mac.
"""

from __future__ import annotations

import os
import platform as _platform
import re
import sys

from raven_core.events import ExecutionProfile, Placement, Platform
from raven_core.workers.base import WorkerUnavailable

_VM_RE = re.compile(
    r"\b(vms?|virtual[\s-]?machines?|sandbox(?:ed|es)?|containers?|lume|isolat(?:ed|ion))\b", re.I
)
_BARE_RE = re.compile(
    r"\b(bare[\s-]?metal|this\s+(?:mac|macbook|machine|computer|pc|laptop|screen)|"
    r"my\s+(?:mac|macbook|machine|computer|pc|laptop|screen|desktop)|locally|on\s+the\s+host|host\s+machine)\b",
    re.I,
)


def host_platform() -> Platform | None:
    return {"darwin": Platform.macos, "linux": Platform.linux, "win32": Platform.windows}.get(sys.platform)


def default_platform() -> Platform:
    return Platform(os.environ.get("RAVEN_DEFAULT_PLATFORM") or (host_platform() or Platform.linux).value)


def host_arch() -> str:
    return _platform.machine() or "unknown"


def describe_host() -> str:
    if host_platform() == Platform.macos:
        return f"this Mac (macOS {_platform.mac_ver()[0]}, {host_arch()})"
    return f"this machine ({_platform.system()} {_platform.release()}, {host_arch()})"


def requested(text: str | None) -> Placement:
    """What the prompt asks for: vm, bare-metal, or auto when it says neither."""
    if not text:
        return Placement.auto
    if _VM_RE.search(text):
        return Placement.vm
    if _BARE_RE.search(text):
        return Placement.bare_metal
    return Placement.auto


def can_run_bare_metal(platform: Platform) -> bool:
    host = host_platform()
    return platform != Platform.windows and (
        Platform(platform) == host or (Platform(platform) == Platform.ios and host == Platform.macos)
    )


def default_placement() -> Placement:
    try:
        return Placement((os.environ.get("RAVEN_PLACEMENT") or "auto").strip().lower())
    except ValueError:
        return Placement.auto


def resolve(
    platform: Platform,
    wanted: Placement | str = Placement.auto,
    execution: ExecutionProfile | None = None,
) -> Placement:
    """bare-metal or vm for this agent; raises when bare metal is demanded for another OS."""
    platform, wanted = Platform(platform), Placement(wanted)
    if execution is not None:
        # Profiles are explicit, not hints from a prompt or an old RAVEN_PLACEMENT.
        # Native Mac/DeviceHub must never secretly use a VM.
        if platform in (Platform.macos, Platform.ios):
            if host_platform() != Platform.macos:
                raise WorkerUnavailable(f"{platform.value} requires a native Mac for this execution profile")
            return Placement.bare_metal
        return Placement.vm
    if wanted == Placement.auto:
        wanted = default_placement()
    if wanted == Placement.vm:
        return Placement.vm
    if can_run_bare_metal(platform):
        return Placement.bare_metal
    if wanted == Placement.bare_metal:
        raise WorkerUnavailable(
            f"{platform.value} can't run bare metal on {describe_host()}; ask for a VM instead"
        )
    return Placement.vm
