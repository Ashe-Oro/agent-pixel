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
    assert body["pixels_remaining_day"] == 254
    assert cells(client, day_id, 1, 2, w=2) == [4, 4]
    # A same-cell batch of 64 still costs one pixel and consumes the slice.
    pin(client, "2026-10-28T08:45:00")
    repeated = [{"x": 3, "y": 3, "color": 2} for _ in range(64)]
    repeated[-1]["color"] = 5
    response = client.post("/agent/act", headers=auth("claude"), json={"type": "paint", "pixels": repeated})
    assert response.status_code == 200, response.text
    assert response.json()["charged"] == 1
    assert cells(client, day_id, 3, 3) == [5]


def test_turn_budget_rejects_without_consuming_the_slice(client):
    opened = open_design(client)
    day_id = opened["day_id"]
    too_many = [{"x": index % 256, "y": index // 256, "color": 1} for index in range(65)]
    response = client.post("/agent/act", headers=auth("claude"), json={"type": "paint", "pixels": too_many})
    assert response.status_code == 400
    assert response.json()["error"] == "turn_budget"
    assert cells(client, day_id, 0, 0) == [0]
    ok = [{"x": index, "y": 0, "color": 1} for index in range(64)]
    response = client.post("/agent/act", headers=auth("claude"), json={"type": "paint", "pixels": ok})
    assert response.status_code == 200, response.text
    assert response.json()["charged"] == 64
    again = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 0, "y": 1, "color": 1}]},
    )
    assert again.status_code == 409
    assert again.json()["error"] == "slice_used"
    assert cells(client, day_id, 0, 1) == [0]


def test_day_budget_caps_later_turns(client):
    open_design(client)
    client.put(
        "/host/budgets",
        headers=host(),
        json={"pixels_per_turn": 64, "pixels_per_artist_day": 100},
    )
    first = [{"x": index, "y": 0, "color": 1} for index in range(64)]
    assert client.post("/agent/act", headers=auth("claude"), json={"type": "paint", "pixels": first}).status_code == 200
    pin(client, "2026-10-28T08:45:00")
    view = client.get("/agent/turn", headers=auth("claude"))
    assert view.status_code == 200
    body = view.json()
    assert body["to_act"] is True
    assert body["pixels_remaining_day"] == 36
    assert body["pixels_remaining_turn"] == 36
    too_many = [{"x": index, "y": 1, "color": 1} for index in range(37)]
    rejected = client.post("/agent/act", headers=auth("claude"), json={"type": "paint", "pixels": too_many})
    assert rejected.status_code == 400
    assert rejected.json()["error"] == "turn_budget"
    exact = [{"x": index, "y": 1, "color": 2} for index in range(36)]
    accepted = client.post("/agent/act", headers=auth("claude"), json={"type": "paint", "pixels": exact})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["pixels_remaining_day"] == 0


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
