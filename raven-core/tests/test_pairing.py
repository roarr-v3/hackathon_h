"""Phones on the LAN need the pairing token; local processes (desktop app, ACP bridge) do not."""

from __future__ import annotations

import stat
from pathlib import Path

from fastapi.testclient import TestClient

import raven_core.env as env
from raven_core.server import create_app
from raven_core.server.config import Settings
from raven_core.server.service import Raven
from raven_core.store import Store


def test_lan_clients_need_the_token_and_local_ones_do_not(tmp_path: Path):
    raven = Raven(settings=Settings(token="pair-me", artifacts_dir=tmp_path / "a", specs_dir=tmp_path / "s"), store=Store())
    app = create_app(raven)
    with TestClient(app, client=("192.168.1.20", 50000)) as phone:
        assert phone.get("/health").status_code == 200
        assert phone.get("/agents").status_code == 401
        assert phone.get("/agents", headers={"Authorization": "Bearer pair-me"}).status_code == 200
        assert phone.get("/agents?token=pair-me").status_code == 200
        assert phone.get("/pairing").status_code == 403
    with TestClient(app, client=("127.0.0.1", 50000)) as desktop:
        assert desktop.get("/agents").status_code == 200
        body = desktop.get("/pairing").json()
        assert body["token"] == "pair-me"
        assert all(u.startswith("http://") and not u.startswith("http://127.") for u in body["urls"])


def test_pairing_token_is_generated_once_and_kept_private(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("RAVEN_TOKEN", raising=False)
    monkeypatch.setenv("RAVEN_DATA_DIR", str(tmp_path))
    token = env.pairing_token()
    assert len(token) >= 24 and env.pairing_token() == token
    assert stat.S_IMODE((tmp_path / "pairing-token").stat().st_mode) == 0o600
    monkeypatch.setenv("RAVEN_TOKEN", "from-env")
    assert env.pairing_token() == "from-env"
