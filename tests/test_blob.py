from pathlib import Path

from fastapi.testclient import TestClient

from app import blob_db
from app.config import ROOT, Settings, resolve_api_key
from app.main import create_app
from tests.conftest import DESIGN, auth, host


def test_production_api_key_is_not_the_dev_default(monkeypatch):
    monkeypatch.delenv("TILE_API_KEY", raising=False)
    monkeypatch.delenv("HOST_API_KEY", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    assert resolve_api_key() == "tile-dev-key"
    monkeypatch.setenv("VERCEL", "1")
    assert resolve_api_key() == ""
    monkeypatch.setenv("HOST_API_KEY", "prod-secret")
    assert resolve_api_key() == "prod-secret"
    monkeypatch.setenv("TILE_API_KEY", "tile-secret")
    assert resolve_api_key() == "tile-secret"


def test_paint_survives_a_new_instance(tmp_path, monkeypatch):
    state = {"body": None, "etag": None, "n": 0}

    def download(_name):
        if state["body"] is None:
            raise blob_db.BlobMissing()
        return state["body"], state["etag"]

    def stat(_name):
        if state["body"] is None:
            raise blob_db.BlobMissing()
        return state["etag"]

    def upload(_name, body, if_match=None):
        if state["body"] is not None and if_match != state["etag"]:
            raise blob_db.BlobConflict()
        state["n"] += 1
        state["body"] = bytes(body)
        state["etag"] = f"e{state['n']}"
        return state["etag"]

    monkeypatch.setattr(blob_db, "configured", lambda: True)
    monkeypatch.setattr(blob_db, "download", download)
    monkeypatch.setattr(blob_db, "stat", stat)
    monkeypatch.setattr(blob_db, "upload", upload)

    path = Path("/tmp/agent-pixel.sqlite")
    path.unlink(missing_ok=True)

    def make_app():
        return create_app(
            Settings(
                db_path=tmp_path / "ignored.db",
                api_key="test-key",
                seed=False,
                pin_morning=False,
                skill_path=ROOT / "skill.md",
                seat_tokens={},
            )
        )

    first = make_app()
    try:
        with TestClient(first) as client:
            assert client.get("/health").json()["persistent"] is True
            client.post("/host/clock", json={"at": "2026-10-28T08:00:00"}, headers=host())
            created = client.post("/agent/design", json=DESIGN, headers=auth("design"))
            assert created.status_code == 200, created.text
            day_id = created.json()["day_id"]
            painted = client.post(
                "/agent/act",
                headers=auth("claude"),
                json={"type": "paint", "pixels": [{"x": 4, "y": 5, "color": 3}]},
            )
            assert painted.status_code == 200, painted.text
    finally:
        first.state.store.close()
        path.unlink(missing_ok=True)

    assert state["body"]
    second = make_app()
    try:
        with TestClient(second) as client:
            region = client.get(
                f"/host/days/{day_id}/region",
                params={"x": 4, "y": 5, "w": 1, "h": 1},
                headers=host(),
            )
            assert region.status_code == 200, region.text
            assert region.json()["cells"] == [3]
    finally:
        second.state.store.close()
        path.unlink(missing_ok=True)
