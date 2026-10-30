"""Public slice filters on /watch/canvas.png.

None selected, a blank slices query, and every slice index return the stored
painting. A subset replays only those ledger strokes onto background id 0.
"""

from io import BytesIO

from PIL import Image

from app.config import ROOT
from app.schedule import configured_slice_count
from app.seed import DEMO_WINDOW
from tests.conftest import auth, host, open_design, pin


def cells(client, day_id, x, y, w=1, h=1):
    response = client.get(
        f"/host/days/{day_id}/region",
        params={"x": x, "y": y, "w": w, "h": h},
        headers=host(),
    )
    assert response.status_code == 200, response.text
    return response.json()["cells"]


def paint(client, agent, pixels, stamp=None):
    if stamp:
        pin(client, stamp)
    response = client.post(
        "/agent/act",
        headers=auth(agent),
        json={"type": "paint", "pixels": pixels},
    )
    assert response.status_code == 200, response.text
    return response.json()


def rgb(hex_value):
    raw = hex_value.removeprefix("#")
    return tuple(int(raw[i : i + 2], 16) for i in (0, 2, 4))


def palette_colors(client):
    state = client.get("/watch/state").json()
    return {swatch["id"]: rgb(swatch["hex"]) for swatch in state["palette"]}


def canvas_image(client, day_id, slices=None, scale=1):
    params = {"day_id": day_id, "scale": scale}
    if slices is not None:
        params["slices"] = slices
    response = client.get("/watch/canvas.png", params=params)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["cache-control"] == "no-store"
    assert "x-api-key" not in {key.lower() for key in response.headers}
    image = Image.open(BytesIO(response.content)).convert("RGB")
    return image


def test_watch_slice_cards_are_layer_toggles(client):
    page = client.get("/watch").text
    assert 'type="button"' in page
    assert "aria-pressed" in page
    assert "data-index" in page
    assert "Add layer" in page
    assert "Showing" in page
    assert "selected.has" in page
    assert "&slices=" in page
    assert 'data-canvas-outline="256"' in page
    assert "border: 1px solid #ffd36a" in page
    assert "image-rendering: pixelated" in page
    assert "dev clock pinned" not in page
    assert "dev clock is pinned" not in page
    assert "/host/clock" not in page
    assert "data.brief" not in page
    assert "director_note" not in page


def test_watch_slice_strip_is_a_carousel(client):
    """Layer chips scroll in slice order for any count the API returns."""
    page = client.get("/watch").text
    assert 'id="select-all"' in page
    assert 'id="select-none"' in page
    assert 'id="slice-prev"' in page
    assert 'id="slice-next"' in page
    assert 'id="slices"' in page
    assert "overflow-x: auto" in page
    assert "ArrowRight" in page
    assert "ArrowLeft" in page
    assert "round-break" in page
    assert "artistInitial" in page
    assert "anchorIndex" in page
    assert "scrollLeft" in page
    assert "slices.map" in page
    assert "repeat(3, minmax(0, 1fr))" not in page
    script = page.split("<script>", 1)[1]
    board = script.split("function paintBoard", 1)[1].split("function syncSliceActions", 1)[0]
    assert "18" not in board
    assert "12" not in board


def test_subset_replay_and_default_full_view(client):
    opened = open_design(client, "2026-10-28T08:00:00")
    day_id = opened["day_id"]
    paint(
        client,
        "claude",
        [
            {"x": 10, "y": 10, "color": 1},
            {"x": 11, "y": 10, "color": 2},
        ],
    )
    paint(
        client,
        "grok-artist",
        [
            {"x": 10, "y": 10, "color": 4},
            {"x": 12, "y": 10, "color": 5},
        ],
    )
    paint(
        client,
        "openai",
        [
            {"x": 10, "y": 10, "color": 0},
            {"x": 13, "y": 10, "color": 6},
        ],
    )
    colors = palette_colors(client)

    full = canvas_image(client, day_id)
    assert full.size == (256, 256)
    assert full.getpixel((10, 10)) == colors[0]
    assert full.getpixel((11, 10)) == colors[2]
    assert full.getpixel((12, 10)) == colors[5]
    assert full.getpixel((13, 10)) == colors[6]
    assert full.getpixel((0, 0)) == colors[0]

    every = ",".join(str(index) for index in range(configured_slice_count()))
    assert full.tobytes() == canvas_image(client, day_id, slices=every).tobytes()
    assert full.tobytes() == canvas_image(client, day_id, slices="").tobytes()
    assert full.tobytes() == canvas_image(client, day_id, slices="  ").tobytes()

    only_claude = canvas_image(client, day_id, slices="0")
    assert only_claude.getpixel((10, 10)) == colors[1]
    assert only_claude.getpixel((11, 10)) == colors[2]
    assert only_claude.getpixel((12, 10)) == colors[0]
    assert only_claude.getpixel((13, 10)) == colors[0]
    assert only_claude.tobytes() != full.tobytes()

    only_grok = canvas_image(client, day_id, slices="1")
    assert only_grok.getpixel((10, 10)) == colors[4]
    assert only_grok.getpixel((11, 10)) == colors[0]
    assert only_grok.getpixel((12, 10)) == colors[5]
    assert only_grok.getpixel((13, 10)) == colors[0]

    claude_and_grok = canvas_image(client, day_id, slices="0,1")
    assert claude_and_grok.getpixel((10, 10)) == colors[4]
    assert claude_and_grok.getpixel((11, 10)) == colors[2]
    assert claude_and_grok.getpixel((12, 10)) == colors[5]
    assert claude_and_grok.getpixel((13, 10)) == colors[0]
    assert claude_and_grok.tobytes() == canvas_image(client, day_id, slices="1, 0").tobytes()
    assert only_claude.tobytes() == canvas_image(client, day_id, slices="0,0").tobytes()

    claude_and_openai = canvas_image(client, day_id, slices="0,2")
    assert claude_and_openai.getpixel((10, 10)) == colors[0]
    assert claude_and_openai.getpixel((11, 10)) == colors[2]
    assert claude_and_openai.getpixel((12, 10)) == colors[0]
    assert claude_and_openai.getpixel((13, 10)) == colors[6]

    only_openai = canvas_image(client, day_id, slices="2")
    assert only_openai.getpixel((10, 10)) == colors[0]
    assert only_openai.getpixel((11, 10)) == colors[0]
    assert only_openai.getpixel((13, 10)) == colors[6]

    untouched = canvas_image(client, day_id, slices="11")
    assert untouched.getpixel((10, 10)) == colors[0]
    assert untouched.getpixel((11, 10)) == colors[0]
    assert untouched.getpixel((12, 10)) == colors[0]
    assert untouched.getpixel((13, 10)) == colors[0]

    zoom = canvas_image(client, day_id, slices="0", scale=2)
    assert zoom.size == (512, 512)
    assert zoom.getpixel((20, 20)) == colors[1]
    assert zoom.getpixel((21, 20)) == colors[1]
    assert zoom.getpixel((20, 21)) == colors[1]
    assert zoom.getpixel((21, 21)) == colors[1]
    assert zoom.getpixel((22, 20)) == colors[2]
    assert zoom.getpixel((19, 20)) == colors[0]

    assert cells(client, day_id, 10, 10) == [0]
    assert cells(client, day_id, 11, 10) == [2]
    assert cells(client, day_id, 12, 10) == [5]
    assert cells(client, day_id, 13, 10) == [6]

    for bad in ("18", "nope", "-1", "0,1,", "0,99"):
        response = client.get("/watch/canvas.png", params={"day_id": day_id, "slices": bad})
        assert response.status_code == 400, bad
        assert response.json()["error"] == "bad_slices"

    assert client.get(f"/host/days/{day_id}/ledger").status_code == 401
    state = client.get("/watch/state").json()
    for row in state["slices"]:
        assert "pixels" not in row
    for stroke in state["recent_strokes"]:
        assert "pixels" not in stroke


def test_passed_slice_filters_to_background(client):
    opened = open_design(client, "2026-10-28T08:00:00")
    day_id = opened["day_id"]
    passed = client.post("/agent/act", headers=auth("claude"), json={"type": "pass", "note": "yield"})
    assert passed.status_code == 200, passed.text
    paint(client, "grok-artist", [{"x": 4, "y": 4, "color": 3}])
    colors = palette_colors(client)

    full = canvas_image(client, day_id)
    blank = canvas_image(client, day_id, slices="0")
    grok = canvas_image(client, day_id, slices="1")
    assert full.getpixel((4, 4)) == colors[3]
    assert blank.getpixel((4, 4)) == colors[0]
    assert grok.getpixel((4, 4)) == colors[3]
    assert blank.getpixel((0, 0)) == colors[0]


def test_seeded_cabin_stays_on_the_full_view_only(tmp_path):
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
        day_id = state["day_id"]
        painted = client.post(
            "/agent/act",
            headers=auth("claude"),
            json={"type": "paint", "pixels": [{"x": 1, "y": 1, "color": 11}]},
        )
        assert painted.status_code == 200, painted.text
        colors = palette_colors(client)
        window_x, window_y = DEMO_WINDOW
        full = canvas_image(client, day_id)
        one = canvas_image(client, day_id, slices="0")
        every = canvas_image(client, day_id, slices=",".join(str(index) for index in range(configured_slice_count())))
        assert full.getpixel((window_x, window_y)) == colors[10]
        assert full.getpixel((1, 1)) == colors[11]
        assert one.getpixel((1, 1)) == colors[11]
        assert one.getpixel((window_x, window_y)) == colors[0]
        assert every.tobytes() == full.tobytes()
        assert cells(client, day_id, window_x, window_y) == [10]
        assert cells(client, day_id, 1, 1) == [11]
    app.state.store.close()
