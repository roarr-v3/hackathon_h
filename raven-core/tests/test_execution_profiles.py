import json

import pytest

from raven_core.events import ExecutionProfile, HMode, Platform
from raven_core.execution import ExecutionProfiles


def test_profile_defaults_and_per_platform_modes():
    profile = ExecutionProfile()
    assert profile.mode_for(Platform.linux) == HMode.agents_api
    assert profile.mode_for(Platform.windows) == HMode.agents_api
    assert profile.mode_for(Platform.macos) == HMode.local_runtime
    assert profile.mode_for(Platform.ios) == HMode.local_runtime
    override = profile.model_copy(update={"platform_modes": {Platform.linux: HMode.models_loop}})
    assert override.mode_for(Platform.linux) == HMode.models_loop


def test_profiles_persist_without_mutating_snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_H_MODE", "models-loop")
    monkeypatch.setenv("HAI_MODEL", "holo4-35b-a3b")
    store = ExecutionProfiles(tmp_path / "execution.json")
    original = store.snapshot()
    assert original.h_mode == HMode.models_loop
    assert original.model == "holo4-35b-a3b"
    changed = original.model_copy(update={"linux_target": "devin-vm", "h_mode": HMode.agents_api})
    store.save(changed)
    assert original.linux_target == "apple-container"
    assert original.h_mode == HMode.models_loop
    assert ExecutionProfiles(store.path).snapshot() == changed
    assert json.loads(store.path.read_text())["linux_target"] == "devin-vm"
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_profile_snapshot_copies_nested_overrides(tmp_path):
    store = ExecutionProfiles(tmp_path / "execution.json")
    first = store.snapshot()
    first.platform_modes[Platform.linux] = HMode.models_loop
    assert Platform.linux not in store.snapshot().platform_modes


def test_invalid_saved_profile_does_not_silently_change_targets(tmp_path):
    path = tmp_path / "execution.json"
    path.write_text('{"linux_target":"docker"}')
    with pytest.raises(ValueError, match="execution"):
        ExecutionProfiles(path).snapshot()


def test_space_name_cannot_contain_shell_arguments():
    with pytest.raises(ValueError):
        ExecutionProfile(linux_space="raven-linux; echo bad")


def test_explicit_profile_is_copied_and_model_override_is_validated(tmp_path):
    store = ExecutionProfiles(tmp_path / "execution.json")
    explicit = ExecutionProfile(platform_modes={Platform.linux: HMode.models_loop})
    snapshot = store.snapshot(explicit, model="holo4-35b-a3b")
    assert explicit.model == "holo4-27b"
    assert snapshot.model == "holo4-35b-a3b"
    explicit.platform_modes.clear()
    assert snapshot.mode_for(Platform.linux) == HMode.models_loop
    with pytest.raises(ValueError):
        store.snapshot(model="invalid model\n")
