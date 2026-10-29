from io import BytesIO

from PIL import Image

from tests.conftest import auth, host, open_design, pin


def cells(client, day_id, x, y, w=1, h=1):
    response = client.get(
        f"/host/days/{day_id}/region",
        params={"x": x, "y": y, "w": w, "h": h},
        headers=host(),
    )
    assert response.status_code == 200, response.text
    return response.json()["cells"]


def test_duplicate_collapse_charges_unique_cells_and_last_wins(client):
    opened = open_design(client)
    day_id = opened["day_id"]
    response = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={
            "type": "paint",
            "pixels": [
                {"x": 1, "y": 2, "color": 1},
                {"x": 1, "y": 2, "color": 4},
                {"x": 2, "y": 2, "color": 4},
            ],
            "note": "two cells",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["charged"] == 2
    assert body["pixels_remaining_day"] is None
    assert body["pixels_remaining_turn"] == 0
    assert cells(client, day_id, 1, 2, w=2) == [4, 4]
    # A same-cell batch of 64 still costs one pixel and consumes the slice.
    pin(client, "2026-10-28T08:06:00")
    repeated = [{"x": 3, "y": 3, "color": 2} for _ in range(64)]
    repeated[-1]["color"] = 5
    response = client.post("/agent/act", headers=auth("claude"), json={"type": "paint", "pixels": repeated})
    assert response.status_code == 200, response.text
    assert response.json()["charged"] == 1
    assert cells(client, day_id, 3, 3) == [5]


def test_open_slice_has_no_pixel_cap_and_can_cover_the_grid(client):
    opened = open_design(client)
    day_id = opened["day_id"]
    view = client.get("/agent/turn", headers=auth("claude")).json()
    assert view["to_act"] is True
    assert view["pixels_remaining_turn"] is None
    assert view["pixels_remaining_day"] is None
    state = client.get("/watch/state").json()
    assert state["pixels_remaining_turn"] is None
    assert state["schedule"]["pixels_per_turn"] is None
    assert state["schedule"]["pixels_per_artist_day"] is None
    assert state["artists"][0]["pixels_remaining_day"] is None

    denied = client.put(
        "/host/budgets",
        headers=host(),
        json={"pixels_per_turn": 64, "pixels_per_artist_day": 256},
    )
    assert denied.status_code == 400
    assert denied.json()["error"] == "bad_budget"
    cleared = client.put("/host/budgets", headers=host(), json={})
    assert cleared.status_code == 200
    assert cleared.json() == {"pixels_per_turn": None, "pixels_per_artist_day": None}

    # The old turn cap was 64 and the old day cap was 256. Both must stay open.
    wide = [{"x": index % 256, "y": index // 256, "color": 1} for index in range(300)]
    first = client.post("/agent/act", headers=auth("claude"), json={"type": "paint", "pixels": wide})
    assert first.status_code == 200, first.text
    assert first.json()["charged"] == 300
    assert first.json()["pixels_remaining_day"] is None
    assert first.json()["pixels_remaining_turn"] == 0
    again = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 0, "y": 2, "color": 1}]},
    )
    assert again.status_code == 409
    assert again.json()["error"] == "slice_used"
    assert cells(client, day_id, 0, 2) == [0]

    # A later slice can still paint every cell, including ones already used today.
    pin(client, "2026-10-28T08:06:00")
    later = client.get("/agent/turn", headers=auth("claude")).json()
    assert later["to_act"] is True
    assert later["pixels_remaining_turn"] is None
    assert later["pixels_remaining_day"] is None
    full = [{"x": index % 256, "y": index // 256, "color": 4} for index in range(256 * 256)]
    covered = client.post("/agent/act", headers=auth("claude"), json={"type": "paint", "pixels": full})
    assert covered.status_code == 200, covered.text
    assert covered.json()["charged"] == 256 * 256
    assert cells(client, day_id, 0, 0) == [4]
    assert cells(client, day_id, 255, 0) == [4]
    assert cells(client, day_id, 43, 1) == [4]
    assert cells(client, day_id, 0, 255) == [4]
    assert cells(client, day_id, 255, 255) == [4]

    grok = client.get("/agent/turn", headers=auth("grok-artist")).json()
    assert grok["to_act"] is True
    assert grok["pixels_remaining_turn"] is None
    latest = grok["recent_strokes"][0]
    assert latest["pixel_count"] == 256 * 256
    assert latest["pixels_omitted"] is True
    assert "pixels" not in latest
    _assert_no_full_grid(grok)


def test_legacy_budget_columns_do_not_cap_a_new_process(tmp_path):
    from fastapi.testclient import TestClient

    from app.config import ROOT, Settings
    from app.main import create_app

    db_path = tmp_path / "legacy.db"
    settings = Settings(
        db_path=db_path,
        api_key="test-key",
        seed=False,
        pin_morning=False,
        skill_path=ROOT / "skill.md",
        seat_tokens={},
    )
    app = create_app(settings)
    with TestClient(app) as first:
        open_design(first)
        app.state.store.conn.execute(
            "UPDATE settings SET pixels_per_turn = 64, pixels_per_artist_day = 256 WHERE id = 1"
        )
    app.state.store.close()

    reopened = create_app(settings)
    with TestClient(reopened) as client:
        stored = reopened.state.store.conn.execute(
            "SELECT pixels_per_turn, pixels_per_artist_day FROM settings WHERE id = 1"
        ).fetchone()
        assert stored["pixels_per_turn"] == 0
        assert stored["pixels_per_artist_day"] == 0
        view = client.get("/agent/turn", headers=auth("claude")).json()
        assert view["pixels_remaining_turn"] is None
        assert view["pixels_remaining_day"] is None
        batch = [{"x": index, "y": 4, "color": 2} for index in range(90)]
        painted = client.post("/agent/act", headers=auth("claude"), json={"type": "paint", "pixels": batch})
        assert painted.status_code == 200, painted.text
        assert painted.json()["charged"] == 90
    reopened.state.store.close()


def test_out_of_bounds_and_bad_color_are_atomic(client):
    opened = open_design(client)
    day_id = opened["day_id"]
    response = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 1, "y": 1, "color": 1}, {"x": 256, "y": 0, "color": 1}]},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "out_of_bounds"
    assert cells(client, day_id, 1, 1) == [0]
    response = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 1, "y": 1, "color": 99}]},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "bad_color"
    response = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 4, "y": 4, "color": 3}], "note": "x" * 121},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "bad_note"
    response = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 4, "y": 4, "color": 3}], "note": "kept"},
    )
    assert response.status_code == 200, response.text
    assert cells(client, day_id, 4, 4) == [3]


def test_canvas_png_is_the_full_grid_without_a_baked_frame(client):
    opened = open_design(client)
    preview = client.get(f"/watch/canvas.png?day_id={opened['day_id']}&scale=1")
    assert preview.status_code == 200
    image = Image.open(BytesIO(preview.content))
    assert image.size == (256, 256)
    background = image.getpixel((0, 0))
    assert image.getpixel((255, 0)) == background
    assert image.getpixel((0, 255)) == background
    assert image.getpixel((255, 255)) == background
    scaled = client.get(f"/watch/canvas.png?day_id={opened['day_id']}&scale=3")
    scaled_image = Image.open(BytesIO(scaled.content))
    assert scaled_image.size == (768, 768)


def test_preview_png_is_nearest_neighbor(client):
    opened = open_design(client, "2026-10-28T08:00:00")
    # Repaint id 1 as pure red via a fresh palette by using color 1 from the test palette.
    # The fixture palette's id 1 is not guaranteed red, so compare against the rendered swatch.
    client.post(
        "/agent/act",
        headers=auth("claude"),
        json={
            "type": "paint",
            "pixels": [{"x": 2, "y": 3, "color": 1}],
            "focus": {"x": 0, "y": 0, "w": 8, "h": 8},
        },
    )
    view = client.get("/agent/turn", headers=auth("claude"))
    # Slice is consumed, so this view is informational. Crop URL was stored for next time;
    # fetch the preview directly.
    preview = client.get(f"/watch/canvas.png?day_id={opened['day_id']}&scale=2")
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("image/png")
    image = Image.open(BytesIO(preview.content))
    assert image.size == (512, 512)
    red = image.getpixel((4, 6))  # (2, 3) scaled by 2
    assert image.getpixel((5, 6)) == red
    assert image.getpixel((4, 7)) == red
    assert image.getpixel((5, 7)) == red
    assert image.getpixel((0, 0)) != red
    crop = client.get(f"/watch/crop.png?day_id={opened['day_id']}&x=0&y=0&w=8&h=8&scale=4")
    assert crop.status_code == 200
    cropped = Image.open(BytesIO(crop.content))
    assert cropped.size == (32, 32)
    turn = view.json()
    assert "canvas" not in turn
    assert turn["preview_url"].endswith(f"day_id={opened['day_id']}&scale=3")
    assert turn["focus_crop_url"]
    _assert_no_full_grid(turn)


def _assert_no_full_grid(payload):
    def walk(node):
        if isinstance(node, list):
            assert len(node) < 1000
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            assert "canvas" not in node
            for value in node.values():
                walk(value)

    walk(payload)
