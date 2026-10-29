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
    assert "11:00" in body
    assert "15-minute" in body
    assert "grok-artist" in body
    assert "4 turns/day" in body
    assert "8 turns" not in body


def test_watch_page_uses_crisp_pixels(client):
    response = client.get("/watch")
    assert response.status_code == 200
    assert "Agent Pixel" in response.text
    assert 'src="/static/mascot.png"' in response.text
    assert "image-rendering: pixelated" in response.text
    assert "image-rendering: crisp-edges" in response.text
    assert "256" in response.text
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
        assert state["current_agent_id"] == "claude"
        assert len(state["palette"]) == 16
        assert len(state["slices"]) == 12
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
