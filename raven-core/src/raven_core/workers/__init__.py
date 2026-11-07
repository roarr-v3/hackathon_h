"""Platform workers. `get_worker(platform)` is the only entry point the server needs.

Env:
  RAVEN_FAKE_WORKERS   "1"/"true"/"all" -> every platform is a FakeWorker;
                       or a comma list, e.g. "windows,macos", to fake only those.
  RAVEN_LINUX_SANDBOX  local Cua sandbox name for linux  (default raven-linux)
  RAVEN_MACOS_SANDBOX  local Cua sandbox name for macos  (default raven-mac)
  WINDOWS_WORKER_HOST  Windows PC running cua-spacesd: "100.x.y.z", "host:3211" or "http://host:3211"
  WINDOWS_WORKER_TOKEN its CUA_ENV_TOKEN (falls back to CUA_ENV_TOKEN)
"""

from __future__ import annotations

import os

from raven_core.events import Platform
from raven_core.workers.base import ShellResult, Worker, WorkerUnavailable, png_size
from raven_core.workers.fake import FakeWorker

__all__ = ["Worker", "WorkerUnavailable", "ShellResult", "FakeWorker", "get_worker", "png_size", "reset_workers"]

_cache: dict[tuple[str, str | None], Worker] = {}


def _faked(platform: Platform) -> bool:
    v = os.environ.get("RAVEN_FAKE_WORKERS", "").strip().lower()
    if not v or v in ("0", "false", "no"):
        return False
    if v in ("1", "true", "yes", "all", "*"):
        return True
    return platform.value in {p.strip() for p in v.split(",")}


def _windows_url() -> str:
    host = os.environ.get("WINDOWS_WORKER_HOST", "").strip()
    if not host:
        raise WorkerUnavailable("WINDOWS_WORKER_HOST is not set (see workers/windows/README.md)")
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    if host.count(":") < 2:  # no explicit port
        host = host.rstrip("/") + ":3211"
    return host


def _host_available(platform: Platform) -> bool:
    from raven_core.hosts import get_host

    try:
        get_host(platform)
        return True
    except WorkerUnavailable:
        return False


def get_worker(platform: Platform, name: str | None = None) -> Worker:
    """Return a (cached) Worker for `platform`. Connection happens lazily on first call."""
    from raven_core.engine.holo import load_env

    load_env()
    platform = Platform(platform)
    key = (platform.value, name)
    if key in _cache:
        return _cache[key]
    if _faked(platform):
        w: Worker = FakeWorker(platform, name)
    elif os.environ.get("RAVEN_H_MODE", "local-runtime") != "models-loop" and _host_available(platform):
        from raven_core.hosts import get_host

        w = get_host(platform)  # type: ignore[assignment]  # H's desktop agent runs inside it
    else:
        from raven_core.workers.cua import CuaWorker

        if platform == Platform.linux:
            w = CuaWorker(platform, name or os.environ.get("RAVEN_LINUX_SANDBOX", "raven-linux"))
        elif platform == Platform.macos:
            w = CuaWorker(platform, name or os.environ.get("RAVEN_MACOS_SANDBOX", "raven-mac"))
        elif platform == Platform.windows:
            token = os.environ.get("WINDOWS_WORKER_TOKEN") or os.environ.get("CUA_ENV_TOKEN")
            w = CuaWorker(platform, name or "windows-pc", url=_windows_url(), token=token)
        else:
            raise WorkerUnavailable(f"no worker implementation for {platform.value} (use RAVEN_FAKE_WORKERS={platform.value})")
    _cache[key] = w
    return w


def reset_workers() -> None:
    """Forget cached workers (tests / after env changes)."""
    _cache.clear()
