"""Gladia live STT session creation.

POST https://api.gladia.io/v2/live  (x-gladia-key) -> {"id", "url"}; the client then
opens a WebSocket to `url` and streams raw PCM s16le 16 kHz mono frames."""

from __future__ import annotations

import logging
import os

from raven_core.events import VoiceSession
from raven_core.integrations import http_client

log = logging.getLogger("raven.voice")

GLADIA_LIVE_URL = "https://api.gladia.io/v2/live"


class VoiceError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def gladia_body() -> dict:
    return {
        "model": "solaria-1",
        "encoding": "wav/pcm",
        "sample_rate": 16000,
        "bit_depth": 16,
        "channels": 1,
        "language_config": {"languages": [], "code_switching": True},
        "messages_config": {
            "receive_partial_transcripts": True,
            "receive_final_transcripts": True,
        },
    }


async def create_voice_session() -> VoiceSession:
    key = os.environ.get("GLADIA_API_KEY")
    if not key:
        raise VoiceError(503, "GLADIA_API_KEY not configured")
    try:
        async with http_client(timeout=20) as client:
            r = await client.post(
                GLADIA_LIVE_URL,
                json=gladia_body(),
                headers={"x-gladia-key": key, "Content-Type": "application/json"},
            )
    except Exception as e:
        raise VoiceError(502, f"Gladia unreachable: {type(e).__name__}") from e
    if r.status_code >= 400:
        log.warning("gladia init failed: HTTP %s", r.status_code)
        raise VoiceError(502, f"Gladia error {r.status_code}: {r.text[:300]}")
    data = r.json()
    return VoiceSession(id=data["id"], url=data["url"])
