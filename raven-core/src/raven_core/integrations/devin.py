"""Devin API: open a session asking Devin to fix a failure.

API v1: POST https://api.devin.ai/v1/sessions  (Authorization: Bearer $DEVIN_API_KEY)
body {"prompt": str, "idempotent": bool} -> {"session_id", "url", "is_new_session"}
Override base with DEVIN_API_BASE (e.g. for a proxy)."""

from __future__ import annotations

import logging
import os
from typing import Any

from raven_core.integrations import http_client

log = logging.getLogger("raven.devin")


def available() -> bool:
    return bool(os.environ.get("DEVIN_API_KEY"))


async def create_session(prompt: str) -> dict[str, Any] | None:
    """Return {"session_id", "url"} or None if no key / failure."""
    key = os.environ.get("DEVIN_API_KEY")
    if not key:
        return None
    base = (os.environ.get("DEVIN_API_BASE") or "https://api.devin.ai/v1").rstrip("/")
    try:
        async with http_client(timeout=60) as client:
            r = await client.post(
                f"{base}/sessions",
                json={"prompt": prompt, "idempotent": False},
                headers={"Authorization": f"Bearer {key}"},
            )
        if r.status_code >= 400:
            log.warning("devin create session failed: HTTP %s %s", r.status_code, r.text[:200])
            return None
        data = r.json()
    except Exception as e:
        log.warning("devin create session error: %s", type(e).__name__)
        return None
    sid = data.get("session_id") or data.get("id")
    url = data.get("url") or (f"https://app.devin.ai/sessions/{sid.removeprefix('devin-')}" if sid else None)
    return {"session_id": sid, "url": url}


def fix_prompt(report_markdown: str) -> str:
    return (
        "Raven (a cross-platform test runner driving computer-use agents on macOS, Linux, "
        "Windows and iOS) found a failing cross-platform test of the bitchat clients "
        "(Nostr geohash channels; no Bluetooth available in the test VMs).\n\n"
        "Investigate the failure below, find the root cause in the relevant bitchat client "
        "(e.g. github.com/permissionlesstech/bitchat or the bitchat-tui client), and open a PR "
        "with a fix. If it is a test/environment problem rather than an app bug, explain what "
        "to change in the test instead.\n\n"
        "---\n\n" + report_markdown
    )
