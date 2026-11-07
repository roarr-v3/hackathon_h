from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# src/raven_core/server/config.py -> parents[4] == repo root
REPO_ROOT = Path(__file__).resolve().parents[4]


def load_env() -> None:
    """Load repo-root .env (never overrides real env vars). Safe to call often."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(REPO_ROOT / ".env", override=False)


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser().resolve() if v else default


@dataclass
class Settings:
    token: str | None = field(default_factory=lambda: os.environ.get("RAVEN_TOKEN") or None)
    port: int = field(default_factory=lambda: int(os.environ.get("RAVEN_PORT") or 8787))
    artifacts_dir: Path = field(
        default_factory=lambda: _env_path("RAVEN_ARTIFACTS_DIR", REPO_ROOT / "artifacts")
    )
    specs_dir: Path = field(default_factory=lambda: _env_path("RAVEN_SPECS_DIR", REPO_ROOT / "specs"))
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
