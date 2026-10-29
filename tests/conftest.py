import os

os.environ["TILE_DB_PATH"] = "/tmp/tile-import-sideeffect.db"
os.environ["TILE_SEED"] = "0"
os.environ["TILE_PIN_MORNING"] = "0"

import pytest
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.main import create_app

KEY = "test-key"


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        db_path=tmp_path / "tile.db",
        api_key=KEY,
        seed=False,
        pin_morning=False,
        skill_path=ROOT / "skill.md",
        seat_tokens={"seat-claude": "claude"},
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
    app.state.store.close()


def host():
    return {"X-API-Key": KEY}


def auth(agent: str):
    return {"X-API-Key": KEY, "X-Agent-Id": agent}


def palette():
    colors = []
    for index in range(16):
        red = index * 17
        green = 255 - index * 10
        blue = index * 8
        colors.append({"id": index, "hex": f"#{red:02x}{green:02x}{blue:02x}", "name": f"c{index}"})
    return colors


DESIGN = {
    "type": "design",
    "theme": "Cozy cabin in the woods",
    "brief": "Warm cabin, tall pines, soft chimney smoke, night sky",
    "palette": palette(),
}


def pin(client, stamp: str):
    response = client.post("/host/clock", json={"at": stamp}, headers=host())
    assert response.status_code == 200, response.text
    return response


def open_design(client, stamp: str = "2026-10-28T08:00:00"):
    pin(client, stamp)
    response = client.post("/agent/design", json=DESIGN, headers=auth("design"))
    assert response.status_code == 200, response.text
    return response.json()
