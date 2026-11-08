from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_real_side_effects(monkeypatch):
    monkeypatch.setenv("RAVEN_ACP_DAEMON", "0")
    monkeypatch.setenv("RAVEN_MANAGE_MACHINES", "0")
    monkeypatch.setenv("RAVEN_SHOW_VM", "0")
