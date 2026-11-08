from fastapi.testclient import TestClient

from raven_core.events import ExecutionProfile
from raven_core.server import Raven, create_app
from raven_core.server.config import Settings
from raven_core.store import Store


def make_service(tmp_path):
    return Raven(Settings(token=None, artifacts_dir=tmp_path / "artifacts", specs_dir=tmp_path), Store())


def test_execution_settings_api_persists_defaults(tmp_path):
    raven = make_service(tmp_path)
    with TestClient(create_app(raven)) as client:
        initial = client.get("/execution")
        assert initial.status_code == 200
        assert len(initial.json()["targets"]) == 4
        selected = ExecutionProfile(linux_target="devin-vm", model="holo4-35b-a3b")
        result = client.put("/execution", json=selected.model_dump(mode="json"))
        assert result.status_code == 200
        assert result.json()["profile"]["model"] == "holo4-35b-a3b"
    with TestClient(create_app(make_service(tmp_path))) as client:
        assert client.get("/execution").json()["profile"]["linux_target"] == "devin-vm"


def test_execution_settings_reject_unsupported_backend(tmp_path):
    with TestClient(create_app(make_service(tmp_path))) as client:
        result = client.put("/execution", json={"linux_target": "docker"})
        assert result.status_code == 422


def test_loading_execution_settings_does_not_probe_or_create_workers(tmp_path, monkeypatch):
    raven = make_service(tmp_path)
    raven.get_worker = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("unexpected worker access"))
    with TestClient(create_app(raven)) as client:
        result = client.get("/execution")
        assert result.status_code == 200
        assert all(target["status"] == "unchecked" for target in result.json()["targets"])
