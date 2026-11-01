from app.config import ROOT
from app.seed import DEMO_WINDOW, blank_canvas, draw_cabin
from tests.conftest import host


def test_skill_matches_contract(client):
    response = client.get("/skill.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    body = response.text
    assert body.startswith("# Agent Pixel\n")
    assert body == (ROOT / "skill.md").read_text(encoding="utf-8")
    assert "256×256" in body
    assert "08:00" in body
    assert "7200" in body
    assert "09:30" not in body
    assert "08:54" not in body
    assert "3 minutes" in body
    assert "Ross" not in body
    assert "TILE_SEAT_ROSS" in body
    assert "seat `ross`" in body
    assert "16384" in body
    assert "TILE_SOLO_WINDOW_SECONDS" in body
    assert "canvas_ends_at_unix" in body
    assert "seconds_left_canvas" in body
    assert "POST /agent/finish" in body
    assert "2×2" in body
    assert "canvas_url" in body


def test_watch_page_uses_crisp_pixels(client):
    response = client.get("/watch")
    assert response.status_code == 200
    page = response.text
    assert "Agent Pixel" in page
    assert 'src="/static/mascot.png"' in page
    assert "image-rendering: pixelated" in page
    assert "image-rendering: crisp-edges" in page
    assert 'id="slice-prev"' in page
    assert 'id="slice-next"' in page
    assert "overflow-x: auto" in page
    assert "repeat(3, minmax(0, 1fr))" not in page
    assert "2 hours" in page
    assert "90 minutes" not in page
    assert "54 minutes" not in page
    assert "An AI artist paints this canvas" in page
    assert "The artist" in page
    assert "Select all shows every slice" in page
    assert "Ross" not in page
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "Ross" not in schema.text
    assert "timeLeft" in page
    assert "m left" in page
    assert "turnLine" in page
    assert "of ${esc(roundTotal)}" in page
    assert "of 4" not in page
    assert "slices.dense" in page
    assert "256" in page
    assert 'data-canvas-outline="256"' in page
    assert "border: 1px solid #ffd36a" in page
    assert "object-fit: cover" not in page
    start = page.index("grid-bounds")
    chunk = page[start:page.index('id="caption"')]
    assert 'id="mural"' in chunk
    assert 'id="placeholder"' in chunk
    assert "aspect-ratio: 1" in page
    mascot = client.get("/static/mascot.png")
    assert mascot.status_code == 200
    assert mascot.headers["content-type"].startswith("image/png")
    assert mascot.content[:8] == b"\x89PNG\r\n\x1a\n"
    state = client.get("/watch/state")
    assert state.status_code == 200
    assert state.json()["status"] == "no_day"


def test_demo_window_pixel_stays_put():
    canvas = blank_canvas()
    draw_cabin(canvas)
    x, y = DEMO_WINDOW
    assert canvas[y * 256 + x] == 10


def test_seeded_morning_is_paintable(tmp_path):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    app = create_app(
        Settings(
            db_path=tmp_path / "demo.db",
            api_key="test-key",
            seed=True,
            pin_morning=True,
            skill_path=ROOT / "skill.md",
            seat_tokens={},
        )
    )
    with TestClient(app) as client:
        state = client.get("/watch/state").json()
        assert state["status"] == "painting"
        assert state["demo_seed"] is True
        assert state["theme"] == "Cozy cabin in the woods"
        assert state["solo"] is True
        assert state["current_agent_id"] == "ross"
        assert state["turn"] == 1
        assert state["turns"] is None
        assert state["seconds_left_canvas"] > 0
        assert state["canvas_ends_at_unix"]
        assert len(state["palette"]) == 16
        assert len(state["slices"]) == 1
        assert state["schedule"]["slices"] is None
        assert state["schedule"]["locks"] == "10:00"
        assert state["schedule"]["turns_per_artist"] is None
        assert state["schedule"]["solo_window_seconds"] == 7200
        assert state["pixels_remaining_turn"] == 16384
        assert state["schedule"]["pixels_per_turn"] == 16384
        assert state["schedule"]["pixels_per_artist_day"] is None
        assert all(artist["pixels_remaining_day"] is None for artist in state["artists"])
        assert [artist["agent_id"] for artist in state["artists"]] == ["ross"]
        region = client.get(
            f"/host/days/{state['day_id']}/region",
            params={"x": 100, "y": 136, "w": 1, "h": 1},
            headers=host(),
        )
        assert region.json()["cells"] == [10]
        preview = client.get(state["preview_url"])
        assert preview.status_code == 200
        assert preview.content[:8] == b"\x89PNG\r\n\x1a\n"
    app.state.store.close()
