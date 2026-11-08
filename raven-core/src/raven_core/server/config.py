from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from raven_core.env import REPO_ROOT, artifacts_dir, load_env, pairing_token, specs_dir


@dataclass
class Settings:
    token: str | None = field(default_factory=pairing_token)
    port: int = field(default_factory=lambda: int(os.environ.get("RAVEN_PORT") or 8787))
    artifacts_dir: Path = field(default_factory=artifacts_dir)
    specs_dir: Path = field(default_factory=specs_dir)
    auto_devin: bool = field(
        default_factory=lambda: os.environ.get("RAVEN_AUTO_DEVIN", "") in ("1", "true", "yes")
    )
    reports: bool = field(
        default_factory=lambda: os.environ.get("RAVEN_REPORTS", "1") not in ("0", "false", "no")
    )
    # Hard cap for a whole run (on top of the spec timeout handled by the runner).
    run_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("RAVEN_RUN_TIMEOUT_S") or 900)
    )
    planning_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("RAVEN_PLANNING_TIMEOUT_S") or 90)
    )
    prepare_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("RAVEN_PREPARE_TIMEOUT_S") or 300)
    )
    fix_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("RAVEN_FIX_TIMEOUT_S") or 300)
    )
