from __future__ import annotations

import os
import secrets
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

_env_loaded = False


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


def data_dir() -> Path | None:
    value = os.environ.get("RAVEN_DATA_DIR")
    return Path(value).expanduser().resolve() if value else None


def env_file() -> Path:
    data = data_dir()
    default = data / "raven.env" if data else REPO_ROOT / ".env"
    return _env_path("RAVEN_ENV_FILE", default)


def artifacts_dir() -> Path:
    data = data_dir()
    default = data / "artifacts" if data else REPO_ROOT / "artifacts"
    return _env_path("RAVEN_ARTIFACTS_DIR", default)


def logs_dir() -> Path:
    return artifacts_dir() / "logs"


def specs_dir() -> Path:
    return _env_path("RAVEN_SPECS_DIR", REPO_ROOT / "specs")


def pairing_token() -> str:
    """RAVEN_TOKEN, else a random token kept beside the artifacts so paired phones survive restarts."""
    if token := (os.environ.get("RAVEN_TOKEN") or "").strip():
        return token
    path = (data_dir() or REPO_ROOT / "artifacts") / "pairing-token"
    try:
        if path.is_file() and (saved := path.read_text().strip()):
            return saved
        path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(24)
        path.write_text(token)
        path.chmod(0o600)
        return token
    except OSError:
        return secrets.token_urlsafe(24)


def load_env() -> None:
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    try:
        lines = env_file().read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
