"""Addresses a phone can use to reach this machine: LAN IP, Bonjour name, Tailscale IP."""

from __future__ import annotations

import contextlib
import socket
import subprocess
import sys

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def is_local(host: str | None) -> bool:
    return host in LOOPBACK


def _text(argv: list[str]) -> str:
    from raven_core.machines import tool_env

    with contextlib.suppress(OSError, subprocess.SubprocessError):
        return subprocess.run(argv, capture_output=True, text=True, timeout=2, env=tool_env()).stdout.strip()
    return ""


def pairing_urls(port: int) -> list[str]:
    hosts: list[str] = []
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))
        hosts.append(s.getsockname()[0])
    if sys.platform == "darwin" and (name := _text(["scutil", "--get", "LocalHostName"])):
        hosts.append(f"{name}.local")
    hosts += _text(["tailscale", "ip", "-4"]).splitlines()[:1]
    return [f"http://{h}:{port}" for h in dict.fromkeys(hosts) if h and not h.startswith("127.")]
