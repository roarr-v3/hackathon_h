from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from raven_core.events import ExecutionProfile
from raven_core.workers.base import WorkerUnavailable

__all__ = ["list_devices", "prepare_device"]

_COMMAND_TIMEOUT = 30
_BOOT_TIMEOUT = 120
_CLEANUP_TIMEOUT = 5
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9:-]{0,127}")
_SIMULATOR_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_PHYSICAL_UDID = re.compile(r"(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{16}|[0-9a-fA-F]{40})")
_XCODE_HELP = (
    "Select a full Xcode 27 or later installation in Xcode > Settings > Locations, "
    "or set DEVELOPER_DIR to its Contents/Developer directory; Command Line Tools alone are insufficient."
)


@dataclass(frozen=True)
class _Device:
    id: str
    name: str
    kind: Literal["simulator", "device"]
    aliases: frozenset[str]
    reason: str | None = None
    screen_url: str | None = None

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "available": self.reason is None}


def _require_mac() -> None:
    if sys.platform != "darwin":
        raise WorkerUnavailable("Native iOS Device Hub preparation requires macOS with Xcode 27 or later.")


def _kill_spawned(spawn: asyncio.Task) -> None:
    if spawn.cancelled():
        return
    try:
        process = spawn.result()
    except Exception:
        return
    if process.returncode is None:
        with suppress(ProcessLookupError, OSError):
            process.kill()


async def _cleanup(spawn: asyncio.Task) -> None:
    try:
        process = await asyncio.wait_for(asyncio.shield(spawn), _CLEANUP_TIMEOUT)
    except TimeoutError:
        spawn.add_done_callback(_kill_spawned)
        spawn.cancel()
        return
    except (asyncio.CancelledError, Exception):
        return
    _kill_spawned(spawn)
    with suppress(TimeoutError, ProcessLookupError, OSError):
        await asyncio.wait_for(process.wait(), _CLEANUP_TIMEOUT)


async def _run(*argv: str, developer_dir: str | None = None, timeout: float = _COMMAND_TIMEOUT) -> bytes:
    env = {
        key: value for key in ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "DEVELOPER_DIR")
        if (value := os.environ.get(key)) is not None
    }
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    if developer_dir is not None:
        env["DEVELOPER_DIR"] = developer_dir
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env,
    ))
    try:
        async with asyncio.timeout(timeout):
            process = await asyncio.shield(spawn)
            stdout, _ = await process.communicate()
    except BaseException as exc:
        cleanup = asyncio.create_task(_cleanup(spawn))
        cancelled = isinstance(exc, asyncio.CancelledError)
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError from None
        if isinstance(exc, TimeoutError):
            raise WorkerUnavailable(
                f"Device Hub command timed out after {timeout:g}s: {' '.join(argv)}. "
                "Check the selected target in Device Hub and retry."
            ) from None
        if isinstance(exc, OSError):
            raise WorkerUnavailable(f"Cannot execute {Path(argv[0]).name}. {_XCODE_HELP}") from None
        raise
    if process.returncode != 0:
        raise WorkerUnavailable(
            f"Device Hub command failed (exit {process.returncode}): {' '.join(argv)}. "
            f"{_XCODE_HELP} For a physical device, connect, pair, trust, unlock, and enable Developer Mode."
        )
    return stdout


async def _toolchain() -> tuple[str, str]:
    _require_mac()
    raw = await _run("/usr/bin/xcode-select", "--print-path")
    try:
        developer = Path(raw.decode("utf-8").strip())
    except UnicodeError:
        raise WorkerUnavailable(f"Cannot determine the active Xcode installation. {_XCODE_HELP}") from None
    app = developer.parent / "Applications" / "DeviceHub.app"
    if not developer.is_absolute() or developer.name != "Developer" or not app.is_dir():
        raise WorkerUnavailable(f"Device Hub is missing from the active Xcode installation. {_XCODE_HELP}")
    return str(developer), str(app)


def _json(raw: bytes, source: str) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise WorkerUnavailable(f"{source} returned invalid JSON; check the selected Xcode installation.") from None
    if not isinstance(value, dict):
        raise WorkerUnavailable(f"{source} returned an unsupported JSON format; update Xcode and retry.")
    return value


async def _core(developer: str, *args: str) -> dict:
    data = _json(await _run(
        "/usr/bin/xcrun", "devicectl", *args, "--timeout", "25", "--json-output", "-",
        developer_dir=developer,
    ), "devicectl")
    info, result = data.get("info"), data.get("result")
    if not isinstance(info, dict) or info.get("outcome") != "success" or data.get("error"):
        raise WorkerUnavailable(
            "devicectl could not verify the target. Check Xcode, device availability, pairing, trust, and Developer Mode."
        )
    if not isinstance(result, dict):
        raise WorkerUnavailable("devicectl returned no structured result; update Xcode and retry.")
    return result


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or value.casefold() in {"booted", "all"}:
        raise WorkerUnavailable("Xcode returned an invalid device identifier; refusing an unpinned target.")
    return value


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _properties(raw: dict, category: str, legacy: str) -> dict:
    modern = _mapping(_mapping(raw.get("properties")).get(category))
    return _mapping(raw.get(legacy)) | modern


def _device_property(raw: dict, key: str) -> Any:
    properties = _mapping(raw.get("properties"))
    values = [properties[key]] if key in properties else []
    values.extend(group[key] for group in properties.values() if isinstance(group, dict) and key in group)
    if values:
        if any(value != values[0] for value in values):
            raise WorkerUnavailable(f"devicectl returned conflicting {key} properties; refusing an ambiguous target.")
        return values[0]
    return _mapping(raw.get("deviceProperties")).get(key)


def _lower(value: Any) -> str:
    return value.casefold() if isinstance(value, str) else ""


async def _simulators(developer: str) -> list[_Device]:
    data = _json(await _run(
        "/usr/bin/xcrun", "simctl", "list", "devices", "--json", developer_dir=developer,
    ), "simctl")
    groups = data.get("devices")
    if not isinstance(groups, dict):
        raise WorkerUnavailable("simctl returned no device inventory; check Xcode and installed iOS runtimes.")
    devices = []
    for runtime, entries in groups.items():
        if not re.fullmatch(r"com\.apple\.CoreSimulator\.SimRuntime\.(?:iOS|iPadOS)-.+", runtime):
            continue
        if not isinstance(entries, list):
            raise WorkerUnavailable("simctl returned an unsupported iOS inventory; update Xcode and retry.")
        for raw in entries:
            if not isinstance(raw, dict):
                raise WorkerUnavailable("simctl returned an invalid device record; refusing an ambiguous target.")
            identifier = _identifier(raw.get("udid"))
            if not _SIMULATOR_ID.fullmatch(identifier):
                raise WorkerUnavailable("simctl returned a non-UUID device identifier; refusing an unpinned target.")
            reason = None
            if raw.get("isAvailable") is not True:
                reason = "Simulator is unavailable. Install its iOS runtime in Xcode > Settings > Components."
            elif raw.get("state") not in {"Booted", "Shutdown"}:
                reason = "Simulator is not ready. Wait for its current boot or shutdown to finish, then retry."
            devices.append(_Device(
                identifier, raw.get("name") if isinstance(raw.get("name"), str) else identifier,
                "simulator", frozenset({identifier.casefold()}), reason,
            ))
    return devices


def _core_device(raw: dict, simulator_ids: set[str]) -> _Device | None:
    hardware = _properties(raw, "hardware", "hardwareProperties")
    connection = _properties(raw, "connection", "connectionProperties")
    if _lower(hardware.get("platform")) not in {"ios", "ipados"}:
        return None
    identifier = _identifier(raw.get("identifier"))
    if not _SIMULATOR_ID.fullmatch(identifier):
        raise WorkerUnavailable("devicectl returned a non-UUID CoreDevice identifier; refusing an unpinned target.")
    udid = _identifier(hardware["udid"]) if hardware.get("udid") else None
    aliases = frozenset(value.casefold() for value in (identifier, udid) if value)
    reality = _lower(hardware.get("reality"))
    transport = _lower(connection.get("transportType"))
    state = _mapping(_mapping(raw.get("properties")).get("state"))
    visibility = _lower(raw.get("visibilityClass") or state.get("visibilityClass"))
    simulated = (
        reality == "simulated" or transport == "samemachine" or visibility == "simulators"
        or bool(aliases & simulator_ids)
    )
    if simulated and reality == "physical":
        raise WorkerUnavailable("devicectl returned conflicting physical/simulator identities; refusing the target.")
    if simulated:
        kind = "simulator"
    elif reality == "physical" or (
        not reality and udid and _PHYSICAL_UDID.fullmatch(udid) and transport in {"wired", "localnetwork"}
    ):
        kind = "device"
    else:
        raise WorkerUnavailable(
            "Cannot verify whether an iOS target is physical or simulated. Refresh it in Device Hub and retry."
        )
    reason = None
    booted = _lower(_device_property(raw, "bootState")) == "booted"
    if kind == "device":
        if _lower(connection.get("pairingState")) != "paired":
            reason = "Physical device is not paired/authorized. Pair it in Device Hub and approve Trust on the device."
        elif _lower(_device_property(raw, "developerModeStatus")) != "enabled":
            reason = "Physical device is not authorized for development. Enable Developer Mode on the device and retry."
        elif (
            _lower(connection.get("state")) != "available" if "state" in connection
            else _lower(connection.get("tunnelState")) != "connected"
        ):
            reason = "Physical device is unavailable. Connect it to this Mac, unlock it, and check its status in Device Hub."
    if reason is None and not booted:
        reason = "Device is not booted or its state cannot be verified. Start/unlock the selected target in Device Hub."
    name = _device_property(raw, "name")
    url = _device_property(raw, "screenViewingURL")
    return _Device(
        udid or identifier, name if isinstance(name, str) else udid or identifier, kind, aliases,
        reason, url if isinstance(url, str) else None,
    )


async def _inventory(developer: str) -> list[_Device]:
    devices = await _simulators(developer)
    simulator_ids = {device.id.casefold() for device in devices}
    simulator_indexes = {device.id.casefold(): index for index, device in enumerate(devices)}
    result = await _core(developer, "list", "devices")
    entries = result.get("devices")
    if not isinstance(entries, list):
        raise WorkerUnavailable("devicectl returned no device inventory; check Xcode and device pairing.")
    core_aliases: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise WorkerUnavailable("devicectl returned an invalid device record; refusing an ambiguous target.")
        device = _core_device(raw, simulator_ids)
        if device is None:
            continue
        if core_aliases & device.aliases:
            raise WorkerUnavailable("Xcode returned duplicate/ambiguous device identifiers; refresh Device Hub and retry.")
        core_aliases.update(device.aliases)
        if device.kind == "device":
            devices.append(device)
        elif device.id.casefold() in simulator_indexes:
            index = simulator_indexes[device.id.casefold()]
            devices[index] = replace(devices[index], aliases=devices[index].aliases | device.aliases)
    seen: set[str] = set()
    for device in devices:
        if seen & device.aliases:
            raise WorkerUnavailable("Xcode returned duplicate/ambiguous device identifiers; refresh Device Hub and retry.")
        seen.update(device.aliases)
    return sorted(devices, key=lambda device: (device.kind != "simulator", device.name.casefold(), device.id.casefold()))


def _select(devices: list[_Device], execution: ExecutionProfile) -> _Device:
    if execution.ios_device_id is not None:
        matches = [device for device in devices if execution.ios_device_id.casefold() in device.aliases]
        if len(matches) != 1:
            raise WorkerUnavailable(
                "Requested ios_device_id was not found uniquely in the iOS inventory. "
                "Use an exact ID from list_devices(); pair physical devices in Device Hub first."
            )
        selected = matches[0]
        if selected.kind != execution.ios_target:
            raise WorkerUnavailable(
                f"Requested ios_device_id is a {selected.kind}, not ios_target={execution.ios_target}; "
                "choose a matching target and ID."
            )
    elif execution.ios_target == "device":
        phones = [device for device in devices if device.kind == "device"]
        available = [device for device in phones if device.reason is None]
        if not available:
            paired = ", ".join(f"{device.name} ({device.reason})" for device in phones) or "none paired"
            raise WorkerUnavailable(
                "No physical iPhone is available: connect and unlock one, trust this Mac and enable Developer Mode "
                f"(paired: {paired}). Simulators are used only when Simulator is selected."
            )
        selected = ([device for device in available if "iphone" in device.name.casefold()] or available)[0]
    else:
        matches = [device for device in devices if device.kind == "simulator" and device.reason is None]
        if not matches:
            raise WorkerUnavailable(
                "No available iOS simulator. Install an iOS runtime and add a simulator in Device Hub. "
                "Physical devices are never selected as a fallback."
            )
        selected = matches[0]
    if selected.reason:
        raise WorkerUnavailable(f"{selected.id}: {selected.reason}")
    return selected


def _screen_url(device: _Device) -> str:
    url = device.screen_url
    try:
        parts = urlsplit(url or "")
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
        valid = (
            isinstance(url, str) and not any(ord(char) <= 32 for char in url)
            and parts.scheme == "devices" and parts.netloc == "device" and parts.path == "/open"
            and not parts.fragment and set(query) == {"id"} and len(query["id"]) == 1
            and query["id"][0].casefold() in device.aliases
        )
    except ValueError:
        valid = False
    if not valid:
        raise WorkerUnavailable(
            "Device Hub did not provide a supported screenViewingURL matching the selected device ID. "
            "Update Xcode and check View Screen for that target; refusing to open an arbitrary foreground device."
        )
    return url


async def list_devices() -> list[dict]:
    developer, _ = await _toolchain()
    return [device.public() for device in await _inventory(developer)]


async def prepare_device(execution: ExecutionProfile) -> dict:
    _require_mac()
    developer, app = await _toolchain()
    devices = await _inventory(developer)
    selected = _select(devices, execution)
    if selected.kind == "simulator":
        await _run(
            "/usr/bin/xcrun", "simctl", "bootstatus", selected.id, "-b",
            developer_dir=developer, timeout=_BOOT_TIMEOUT,
        )
    result = await _core(developer, "device", "info", "details", "--device", selected.id)
    raw = result.get("device", result)
    current = _core_device(
        _mapping(raw), {selected.id.casefold()} if selected.kind == "simulator" else set(),
    )
    if (
        current is None or current.kind != selected.kind or current.id.casefold() != selected.id.casefold()
        or not selected.aliases <= current.aliases
        or any(device is not selected and device.aliases & current.aliases for device in devices)
    ):
        raise WorkerUnavailable("Device identity/type changed during preparation; refusing to open a different target.")
    if current.reason:
        raise WorkerUnavailable(f"{selected.id}: {current.reason}")
    url = _screen_url(current)
    if selected.kind == "device":
        lock = await _core(developer, "device", "info", "lockState", "--device", selected.id)
        if _lower(lock.get("deviceIdentifier")) not in current.aliases:
            raise WorkerUnavailable("Device lock-state response belongs to a different target; refusing to open it.")
        if lock.get("passcodeRequired") is not False or lock.get("unlockedSinceBoot") is not True:
            raise WorkerUnavailable(
                "Selected physical device is locked or authorization cannot be verified. Unlock it manually and retry."
            )
    await _run("/usr/bin/open", "-a", app, "-u", url, developer_dir=developer)
    return {"id": selected.id, "name": current.name, "kind": selected.kind, "available": True}
