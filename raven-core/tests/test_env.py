from __future__ import annotations

import os

import pytest

import raven_core.env as env


@pytest.fixture(autouse=True)
def reset_load_env_flag():
    env._env_loaded = False
    yield
    env._env_loaded = False


def test_data_dir_sets_default_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("RAVEN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RAVEN_ARTIFACTS_DIR", raising=False)
    monkeypatch.delenv("RAVEN_ENV_FILE", raising=False)

    assert env.data_dir() == tmp_path.resolve()
    assert env.artifacts_dir() == tmp_path / "artifacts"
    assert env.logs_dir() == tmp_path / "artifacts" / "logs"
    assert env.env_file() == tmp_path / "raven.env"


def test_artifacts_and_env_file_override_data_dir(monkeypatch, tmp_path):
    data = tmp_path / "data"
    artifacts = tmp_path / "custom-artifacts"
    env_file = tmp_path / "custom.env"
    monkeypatch.setenv("RAVEN_DATA_DIR", str(data))
    monkeypatch.setenv("RAVEN_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("RAVEN_ENV_FILE", str(env_file))

    assert env.artifacts_dir() == artifacts.resolve()
    assert env.env_file() == env_file.resolve()


def test_load_env_parses_file_without_overriding(monkeypatch, tmp_path):
    path = tmp_path / "raven.env"
    path.write_text(
        '# ignored\n'
        'export RAVEN_TEST_ENV_QUOTED="value with spaces"\n'
        'RAVEN_TEST_ENV_EMPTY=\n'
        'RAVEN_TEST_ENV_EXISTING=from-file\n'
    )
    monkeypatch.setenv("RAVEN_ENV_FILE", str(path))
    monkeypatch.delenv("RAVEN_TEST_ENV_QUOTED", raising=False)
    monkeypatch.delenv("RAVEN_TEST_ENV_EMPTY", raising=False)
    monkeypatch.setenv("RAVEN_TEST_ENV_EXISTING", "already-set")

    env.load_env()

    assert os.environ["RAVEN_TEST_ENV_QUOTED"] == "value with spaces"
    assert "RAVEN_TEST_ENV_EMPTY" not in os.environ
    assert os.environ["RAVEN_TEST_ENV_EXISTING"] == "already-set"


def test_specs_dir_defaults_to_repo_root(monkeypatch):
    monkeypatch.delenv("RAVEN_SPECS_DIR", raising=False)

    assert env.specs_dir() == env.REPO_ROOT / "specs"
