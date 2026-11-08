"""Resolve once, prepare once, then retain that worker for setup AND execution.

Explicit profiles are independent of process-global H/host/placement defaults.
Legacy callers retain bare-metal and explicit macOS VM placement; no route starts
a container engine implicitly. Only an eligible Apple preflight can switch to a
Devin Linux VM, once, with an announcement.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
from typing import Any, Awaitable, Callable

from raven_core.events import ExecutionProfile, HMode, Placement, Platform
from raven_core.workers.base import ShellResult, Worker, WorkerUnavailable, png_size
from raven_core.workers.fake import FakeWorker

log = logging.getLogger(__name__)
Say = Callable[[str], Awaitable[None]]
_cache: dict[tuple[str, str | None, str, str], Any] = {}


def _profile_key(execution: ExecutionProfile) -> str:
    # Frozen pydantic models containing dicts are not hashable. Include ALL fields,
    # even overrides for other platforms, and snapshot mutable nested mappings.
    return json.dumps(execution.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _faked(platform: Platform) -> bool:
    value = os.environ.get("RAVEN_FAKE_WORKERS", "").strip().lower()
    return value in ("1", "true", "yes", "all", "*") or platform.value in {p.strip() for p in value.split(",")}


def _legacy_mode(platform: Platform) -> HMode:
    value = os.environ.get("RAVEN_H_MODE") or "auto"
    try:
        return ExecutionProfile(h_mode=HMode(value)).mode_for(platform)
    except ValueError as exc:
        raise WorkerUnavailable("RAVEN_H_MODE must be auto, local-runtime, agents-api or models-loop") from exc


def _bare_metal_worker(platform: Platform, mode: HMode) -> Any:
    if mode != HMode.models_loop:
        from raven_core.hosts import LocalHost

        return LocalHost(platform, mode=mode)
    if sys.platform == "darwin":
        from raven_core.workers.local_mac import LocalMacWorker

        return LocalMacWorker(platform)
    raise WorkerUnavailable("bare-metal Models fallback is implemented only on macOS; select H's official harness")


def _devin_worker(platform: Platform, mode: HMode, name: str | None = None) -> Any:
    from raven_core.hosts.devin import DevinHost
    from raven_core.hosts.remote import NativeRemoteWorker

    host = DevinHost(platform, name=name, mode=mode)
    return NativeRemoteWorker(host) if mode == HMode.models_loop else host


def get_worker(
    platform: Platform,
    name: str | None = None,
    placement: Placement | str = Placement.auto,
    execution: ExecutionProfile | None = None,
) -> Worker:
    """Pure selection after env loading; no connection, preflight or provisioning."""
    from raven_core.env import load_env
    from raven_core.hosts.capabilities import CapabilityCategory as Category, CapabilityError
    from raven_core.placement import resolve

    load_env()
    platform = Platform(platform)
    snapshot = execution.model_copy(deep=True) if execution is not None else None
    mode = snapshot.mode_for(platform) if snapshot else _legacy_mode(platform)
    try:
        where = None if _faked(platform) else resolve(platform, placement, execution=snapshot)
    except WorkerUnavailable as exc:
        if snapshot is not None:
            raise CapabilityError(Category.configuration, str(exc)) from exc
        raise
    profile_key = _profile_key(snapshot) if snapshot else json.dumps({
        "legacy_mode": mode.value,
        "host": os.environ.get("RAVEN_HOST_LINUX", "") if platform == Platform.linux else "",
        "linux_space": os.environ.get("RAVEN_LINUX_SANDBOX", "raven-linux-apple"),
        "macos_space": os.environ.get("RAVEN_MACOS_SANDBOX", "raven-mac"),
    }, sort_keys=True)
    key = (platform.value, name, where.value if where else "fake", profile_key)
    existing = _cache.get(key)
    if existing is not None and not getattr(existing, "_closed", False) and not getattr(getattr(existing, "host", None), "_closed", False):
        return existing

    if where is None:
        worker: Any = FakeWorker(platform, name)
    elif where == Placement.bare_metal:
        worker = _bare_metal_worker(platform, mode)
    elif platform == Platform.linux:
        target = snapshot.linux_target if snapshot else os.environ.get("RAVEN_HOST_LINUX", "apple-container")
        if target in ("devin-vm", "devin-cloud"):
            worker = _devin_worker(platform, mode, name)
        elif target in ("", "cua-local", "apple-container"):
            space = name or (snapshot.linux_space if snapshot else os.environ.get("RAVEN_LINUX_SANDBOX", "raven-linux-apple"))
            if mode == HMode.models_loop:
                from raven_core.workers.cua import CuaWorker

                worker = CuaWorker(platform, space)
            else:
                from raven_core.hosts.cua import CuaHost

                worker = CuaHost(platform, space, mode=mode)
        else:
            raise CapabilityError(Category.configuration, "Unsupported Linux execution target; choose apple-container or devin-vm")
    elif platform == Platform.windows:
        if snapshot is None:
            from raven_core.hosts.devin import inspect_devin

            readiness = inspect_devin(platform)
            if readiness["status"] == "blocked":
                raise CapabilityError(Category.configuration, readiness["detail"])
        worker = _devin_worker(platform, mode, name)
    elif platform == Platform.macos:
        space = name or os.environ.get("RAVEN_MACOS_SANDBOX", "raven-mac")
        if mode == HMode.models_loop:
            from raven_core.workers.cua import CuaWorker

            worker = CuaWorker(platform, space)
        else:
            from raven_core.hosts.cua import CuaHost

            worker = CuaHost(platform, space, mode=mode)
    else:
        raise WorkerUnavailable("iOS runs natively through DeviceHub on a Mac; there is no iOS VM")
    if snapshot is not None:
        worker._raven_execution_key = _profile_key(snapshot)
    _cache[key] = worker
    return worker


async def prepare_worker(worker: Worker, *, execution: ExecutionProfile, say: Say | None = None) -> Worker:
    """Idempotent preflight. A fallback is resolved before any suite setup, never mid-test."""
    from raven_core.hosts import h_environment
    from raven_core.hosts.capabilities import CapabilityCategory as Category, CapabilityError

    if isinstance(worker, FakeWorker):
        return worker
    key = _profile_key(execution)
    if getattr(worker, "_raven_execution_key", key) != key:
        raise CapabilityError(Category.configuration, "Worker belongs to another execution profile; resolve a new worker")
    lock = getattr(worker, "_raven_prepare_lock", None)
    if lock is None:
        lock = worker._raven_prepare_lock = asyncio.Lock()  # type: ignore[attr-defined]

    async def announce(text: str) -> None:
        log.info("%s", text)
        if say:
            await say(text)

    async def prepare(selected: Any) -> None:
        if selected.platform == Platform.ios:
            if sys.platform != "darwin":
                raise CapabilityError(Category.configuration, "DeviceHub iOS preparation requires macOS")
            try:
                from raven_core.workers.device_hub import prepare_device
            except ImportError as exc:
                raise CapabilityError(Category.dependency, "The DeviceHub iOS adapter is not installed") from exc
            try:
                selected.device = await prepare_device(execution)
            except WorkerUnavailable as exc:
                raise CapabilityError(Category.desktop_unavailable, str(exc)) from None
        fn = getattr(selected, "prepare", None) or getattr(selected, "preflight", None)
        if fn is not None:
            try:
                await fn()
            except CapabilityError:
                raise
            except WorkerUnavailable as exc:
                # Third-party/legacy adapters do not get to trigger Linux fallback
                # by throwing an unclassified error (including H auth/latency).
                category = Category.desktop_unavailable if selected.platform in (Platform.macos, Platform.ios) else Category.transport
                detail = str(exc) if selected.platform in (Platform.macos, Platform.ios) else "Worker preflight is unavailable; no automatic target switch"
                raise CapabilityError(category, detail) from None

    async with lock:
        if resolved := getattr(worker, "_raven_resolved_worker", None):
            if getattr(resolved, "_closed", False) or getattr(getattr(resolved, "host", None), "_closed", False):
                raise CapabilityError(Category.configuration, "This execution worker was closed; resolve a new worker before setup")
            return resolved
        if error := getattr(worker, "_raven_prepare_error", None):
            raise error
        # Do this before considering any paid fallback. Missing H authentication is
        # not a machine-capability failure, in any mode (including Models).
        h_environment()
        await announce(f"Preparing {worker.platform.value} target `{worker.name}` ({execution.mode_for(worker.platform).value})…")
        try:
            await prepare(worker)
            resolved = worker
        except CapabilityError as exc:
            eligible = (
                worker.platform == Platform.linux
                and getattr(worker, "target", None) == "apple-container"
                and execution.linux_target == "apple-container"
                and execution.linux_fallback
                and exc.allows_linux_fallback
            )
            if not eligible:
                raise
            await announce(
                f"Local Linux preflight blocked: {exc.detail} "
                "Falling back once to a Devin Linux VM; H mode is unchanged. No further target fallback will be attempted."
            )
            resolved = _devin_worker(Platform.linux, execution.mode_for(Platform.linux))
            resolved._raven_execution_key = key
            try:
                await prepare(resolved)
            except BaseException as fallback_error:
                # A later call must not allocate another VM following a failed attempt.
                # Cancellation cleanup belongs to the Devin adapter.
                if isinstance(fallback_error, Exception):
                    worker._raven_prepare_error = fallback_error  # type: ignore[attr-defined]
                raise
        worker._raven_resolved_worker = resolved  # type: ignore[attr-defined]
        resolved._raven_resolved_worker = resolved
        resolved._raven_prepared = True
        for cache_key, cached in list(_cache.items()):
            if cached is worker:
                _cache[cache_key] = resolved
        return resolved


async def inspect_targets(execution: ExecutionProfile) -> list[dict]:
    """Read-only inventory. Never prepare, resume, create, open a viewer, or call a model.

    ``ready`` means a matching worker passed preflight in this process. Configured
    but unverified capabilities are ``unchecked``; missing prerequisites are
    ``blocked``. No environment values, tokens, command output or connection URLs.
    """
    from raven_core import machines
    from raven_core.hosts.capabilities import CapabilityError
    from raven_core.hosts.devin import inspect_devin

    key = _profile_key(execution)
    result: list[dict] = []
    for platform in Platform:
        target = (
            execution.linux_target if platform == Platform.linux else
            execution.windows_target if platform == Platform.windows else
            f"devicehub-{execution.ios_target}" if platform == Platform.ios else "native"
        )
        row = {"platform": platform.value, "target": target, "status": "unchecked", "detail": "Preflight has not run"}
        prepared = next((w for w in _cache.values()
                         if w.platform == platform and getattr(w, "_raven_execution_key", None) == key
                         and getattr(w, "_raven_prepared", False)
                         and not getattr(w, "_closed", False) and not getattr(getattr(w, "host", None), "_closed", False)), None)
        if prepared is not None:
            row.update(target=getattr(prepared, "target", target), status="ready",
                       detail="Selected worker passed local capability preflight; H authentication/model health is not implied")
        elif _faked(platform):
            row.update(detail="Fake-worker override is active; no real target has been checked")
        elif not os.environ.get("HAI_API_KEY"):
            row.update(status="blocked", detail="HAI_API_KEY is not configured")
        elif target == "devin-vm":
            row.update(inspect_devin(platform))
        elif platform == Platform.linux:
            if sys.platform != "darwin":
                row.update(status="blocked", detail="Apple Containerization needs a Mac; one announced Linux VM fallback may be used at preflight")
            else:
                try:
                    info = await machines.space_info(execution.linux_space)
                    if info is None:
                        row.update(status="blocked", detail=f"Existing Space `{execution.linux_space}` is missing; inspection does not create it")
                    else:
                        machines.validate_space(info, platform)
                        row.update(detail=f"Apple Containerization Space `{execution.linux_space}` is {info.get('state')}; desktop and H runtime not yet verified")
                        if info.get("state") not in ("running", "ready"):
                            row["status"] = "blocked"
                except CapabilityError as exc:
                    row.update(status="blocked", detail=exc.detail)
        elif sys.platform != "darwin":
            row.update(status="blocked", detail="This target requires a native Mac")
        elif platform == Platform.ios:
            try:
                from raven_core.workers.device_hub import list_devices

                devices = await list_devices()
                matching = [d for d in devices if d.get("kind") == execution.ios_target and (
                    not execution.ios_device_id or str(d.get("id", "")).casefold() == execution.ios_device_id.casefold()
                )]
                if not matching:
                    row.update(status="blocked", detail="Selected iOS device was not found by DeviceHub")
                elif not any(d.get("available") is True for d in matching):
                    row.update(status="blocked", detail="Selected iOS device is unavailable; check pairing, trust, unlock and Developer Mode")
                else:
                    row.update(detail="DeviceHub inventory checked; selected device and GUI preparation have not run")
            except (ImportError, WorkerUnavailable):
                row.update(status="blocked", detail="DeviceHub inventory is unavailable; install/configure DeviceHub on this Mac")
        else:
            try:
                from raven_core.workers.local_mac import missing_mac_permissions

                missing = await asyncio.to_thread(missing_mac_permissions, prompt=False)
                if missing:
                    row.update(status="blocked", detail="Native Mac needs " + " and ".join(missing))
                elif importlib.util.find_spec("hai_agents") is None:
                    row.update(status="blocked", detail="Install Raven's hai-agents[desktop] runtime")
                else:
                    row.update(detail="Native Mac permissions present; selected H runtime and desktop preflight not yet checked")
            except (ImportError, OSError):
                row.update(status="blocked", detail="Cannot inspect native macOS desktop permissions")
        result.append(row)
    return result


def reset_workers() -> None:
    """Forget selection/preflight caches; never delete, stop, or provision machines."""
    _cache.clear()


__all__ = [
    "Worker", "WorkerUnavailable", "ShellResult", "FakeWorker", "get_worker",
    "prepare_worker", "inspect_targets", "png_size", "reset_workers",
]
