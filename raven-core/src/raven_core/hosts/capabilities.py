"""Backend preflight failures, separate from H/model errors and application verdicts."""

from __future__ import annotations

from enum import StrEnum

from raven_core.workers.base import WorkerUnavailable


class CapabilityCategory(StrEnum):
    unsupported_runtime = "unsupported-runtime"
    desktop_unavailable = "desktop-unavailable"
    configuration = "configuration"
    dependency = "dependency"
    authentication = "authentication"
    access_denied = "access-denied"
    transport = "transport"
    bootstrap = "bootstrap"
    protocol = "protocol"
    timeout = "timeout"


class CapabilityError(WorkerUnavailable):
    """A safe, actionable backend failure, not an application verdict."""

    def __init__(self, category: CapabilityCategory | str, detail: str):
        self.category = CapabilityCategory(category)
        self.detail = detail
        super().__init__(detail)

    @property
    def allows_linux_fallback(self) -> bool:
        return self.category in (
            CapabilityCategory.unsupported_runtime,
            CapabilityCategory.desktop_unavailable,
        )


def command_failure(operation: str, output: str = "", *, local_desktop: bool = False) -> CapabilityError:
    """Conservative classification for infrastructure probes, NEVER H/model/test output.

    Do not echo arbitrary response bodies: CLIs and HTTP errors can contain credentials.
    Unknown failures cannot trigger a paid target switch.
    """
    text = output.lower()
    if any(s in text for s in ("unauthorized", "unauthenticated", "401", "not logged in", "not authenticated", "log in", "login required")):
        return CapabilityError(CapabilityCategory.authentication, f"{operation}: authentication is required; sign in and retry.")
    if any(s in text for s in ("forbidden", "403", "permission denied", "access denied", "not authorized")):
        return CapabilityError(CapabilityCategory.access_denied, f"{operation}: access was denied; check account and machine permissions.")
    if local_desktop and any(s in text for s in (
        "connection refused", "no route to host", "spacesd not available", "spacesdnotavailable",
        "desktop is not ready", "display is not available", "no display", "not running",
    )):
        return CapabilityError(CapabilityCategory.desktop_unavailable, f"{operation}: the local desktop is unavailable.")
    return CapabilityError(CapabilityCategory.transport, f"{operation} failed; check the machine/CLI connection (no automatic target switch).")


__all__ = ["CapabilityCategory", "CapabilityError"]
