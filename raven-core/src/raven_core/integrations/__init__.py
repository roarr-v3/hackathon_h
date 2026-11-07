"""Third-party integrations (Linkup research, Devin sessions, Gladia voice).

All outbound HTTP goes through `http_client()` so tests can swap the transport:
    monkeypatch.setattr(raven_core.integrations, "TRANSPORT", httpx.MockTransport(handler))
"""

from __future__ import annotations

import httpx

TRANSPORT: httpx.AsyncBaseTransport | None = None


def http_client(timeout: float = 30.0) -> httpx.AsyncClient:
    if TRANSPORT is not None:
        return httpx.AsyncClient(transport=TRANSPORT, timeout=timeout)
    return httpx.AsyncClient(timeout=timeout)
