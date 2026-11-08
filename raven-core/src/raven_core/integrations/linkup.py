"""Linkup web search: research why a test failed (sourced answer + citations).

API: POST https://api.linkup.so/v1/search  (Authorization: Bearer $LINKUP_API_KEY)
body {"q", "depth": "standard"|"deep", "outputType": "sourcedAnswer"}
-> {"answer": str, "sources": [{"name", "url", "snippet"}]}
"""

from __future__ import annotations

import logging
import os
from typing import Any

from raven_core.integrations import http_client

log = logging.getLogger("raven.linkup")

LINKUP_URL = "https://api.linkup.so/v1/search"


def available() -> bool:
    return bool(os.environ.get("LINKUP_API_KEY"))


async def search(query: str, depth: str = "standard") -> dict[str, Any] | None:
    """Return {"query", "answer", "sources": [{name,url,snippet}]} or None (no key / error)."""
    key = os.environ.get("LINKUP_API_KEY")
    if not key:
        return None
    body = {"q": query[:400], "depth": depth, "outputType": "sourcedAnswer"}
    try:
        async with http_client(timeout=60) as client:
            r = await client.post(
                LINKUP_URL, json=body, headers={"Authorization": f"Bearer {key}"}
            )
        if r.status_code >= 400:
            log.warning("linkup search failed: HTTP %s", r.status_code)
            return None
        data = r.json()
    except Exception as e:  # network etc.
        log.warning("linkup search error: %s", type(e).__name__)
        return None
    sources = []
    for s in data.get("sources") or []:
        if isinstance(s, dict):
            sources.append(
                {
                    "name": s.get("name") or s.get("url") or "",
                    "url": s.get("url") or "",
                    "snippet": (s.get("snippet") or s.get("content") or "")[:500],
                }
            )
    return {"query": query, "answer": data.get("answer") or "", "sources": sources}


def failure_query(platform: str, category: str) -> str:
    from raven_core.events import Platform

    topic = {
        "blocked": "desktop automation setup accessibility permissions blocked",
        "execution": "desktop automation tool execution errors permissions",
        "timeout": "GUI automation timeout unchanged screen troubleshooting",
        "assertion": "GUI testing visible assertion failure reproduction",
        "cancelled": "GUI testing interrupted execution resume verification",
    }.get(category, "GUI testing failure troubleshooting")
    return f"{Platform(platform).value} {topic}"
