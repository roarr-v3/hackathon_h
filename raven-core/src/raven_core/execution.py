from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from raven_core.events import ExecutionProfile


class ExecutionProfiles:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._profile: ExecutionProfile | None = None

    def _load(self) -> ExecutionProfile:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {
                "h_mode": os.environ.get("RAVEN_H_MODE") or "auto",
                "model": os.environ.get("HAI_MODEL") or "holo4-27b",
                "linux_space": os.environ.get("RAVEN_LINUX_SANDBOX") or "raven-linux-apple",
                "planner": os.environ.get("RAVEN_PLANNER_MODE") or "local",
            }
        except (OSError, ValueError) as error:
            raise ValueError("Cannot read saved execution settings; repair the execution profile before running tests") from error
        try:
            return ExecutionProfile.model_validate(data)
        except ValueError as error:
            raise ValueError("Invalid execution profile; repair the saved settings before running tests") from error

    def snapshot(self, profile: ExecutionProfile | None = None, *, model: str | None = None) -> ExecutionProfile:
        if profile is None:
            if self._profile is None:
                self._profile = self._load()
            profile = self._profile
        data = profile.model_dump(mode="json")
        if model is not None:
            data["model"] = model
        return ExecutionProfile.model_validate(data)

    def save(self, profile: ExecutionProfile) -> ExecutionProfile:
        saved = ExecutionProfile.model_validate(profile.model_dump(mode="json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                handle.write(saved.model_dump_json(indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._profile = saved
        return self.snapshot()
