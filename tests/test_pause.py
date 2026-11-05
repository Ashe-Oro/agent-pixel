"""TILE_DAILY_PAUSED skips the automatic morning canvas and its wakes."""

from app.schedule import daily_paused
from tests.conftest import DESIGN, auth, host, pin
from tests.test_wakes import CRON, install_poster, payloads, set_hook


def test_daily_paused_is_read_at_call_time(monkeypatch):
    monkeypatch.delenv("TILE_DAILY_PAUSED", raising=False)
    assert daily_paused() is False
    for raw in ("1", "true", "TRUE", "yes", "Yes", "on", "ON", "  on  "):
        monkeypatch.setenv("TILE_DAILY_PAUSED", raw)
        assert daily_paused() is True, raw
    for raw in ("", "0", "false", "FALSE", "no", "off", "OFF", "2", "truthy"):
        monkeypatch.setenv("TILE_DAILY_PAUSED", raw)
        assert daily_paused() is False, raw
    monkeypatch.setenv("TILE_DAILY_PAUSED", "1")
    assert daily_paused() is True
    monkeypatch.setenv("TILE_DAILY_PAUSED", "off")
    assert daily_paused() is False


def test_paused_tick_at_0800_creates_no_canvas_and_sends_no_wakes(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setenv("TILE_PUBLIC_BASE_URL", "https://pixel.example")
    monkeypatch.setenv("TILE_DAILY_PAUSED", "yes")
    seen = install_poster(monkeypatch)
    set_hook(client, "ross", ["design_needed", "slice_open", "canvas_locked"])
    set_hook(client, "design", ["awaiting_design"])

    pin(client, "2026-10-28T08:00:00")
    opened = client.get("/cron/tick", headers=CRON)
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["paused"] is True
    assert body["phase"] == "paused"
    assert body["day"] is None
    assert body["wakes"] == []
    assert seen == []

    host_tick = client.post("/host/routine/tick", headers=host())
    assert host_tick.status_code == 200, host_tick.text
    assert host_tick.json()["paused"] is True
    assert host_tick.json()["day"] is None
    assert seen == []

    state = client.get("/watch/state").json()
    assert state["status"] == "no_day"
    assert state["daily_paused"] is True
    assert client.get("/watch").status_code == 200
    assert client.get("/watch/gallery").json() == {"canvases": []}
    days = client.app.state.store.conn.execute("SELECT COUNT(*) AS n FROM days").fetchone()["n"]
    assert days == 0


def test_unpaused_tick_at_0800_opens_the_morning_and_wakes(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setenv("TILE_PUBLIC_BASE_URL", "https://pixel.example")
    monkeypatch.setenv("TILE_DAILY_PAUSED", "false")
    seen = install_poster(monkeypatch)
    set_hook(client, "ross", ["design_needed", "canvas_locked"])

    pin(client, "2026-10-28T07:59:00")
    early = client.get("/cron/tick", headers=CRON)
    assert early.json()["paused"] is False
    assert early.json()["phase"] == "before_open"
    assert early.json()["wakes"] == []
    assert client.get("/watch/state").json()["daily_paused"] is False

    pin(client, "2026-10-28T08:00:00")
    opened = client.get("/cron/tick", headers=CRON)
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["paused"] is False
    assert body["phase"] == "awaiting_design"
    assert body["day"]["day_id"]
    assert [item["event"] for item in body["wakes"]] == ["design_needed"]
    assert payloads(seen)[0]["event"] == "design_needed"
    assert client.get("/watch/state").json()["status"] == "awaiting_design"
    assert client.get("/watch/state").json()["daily_paused"] is False

    again = client.get("/cron/tick", headers=CRON).json()
    assert again["day"]["day_id"] == body["day"]["day_id"]
    assert again["paused"] is False
    assert len(seen) == 1


def test_pause_is_read_on_each_tick(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setenv("TILE_PUBLIC_BASE_URL", "https://pixel.example")
    seen = install_poster(monkeypatch)
    set_hook(client, "ross", ["design_needed"])
    pin(client, "2026-10-28T08:00:00")

    monkeypatch.setenv("TILE_DAILY_PAUSED", "1")
    paused = client.post("/host/routine/tick", headers=host()).json()
    assert paused["paused"] is True
    assert paused["day"] is None
    assert seen == []
    assert client.get("/watch/state").json()["daily_paused"] is True

    monkeypatch.setenv("TILE_DAILY_PAUSED", "0")
    opened = client.get("/cron/tick", headers=CRON).json()
    assert opened["paused"] is False
    assert opened["phase"] == "awaiting_design"
    assert opened["wakes"][0]["event"] == "design_needed"
    assert client.get("/watch/state").json()["daily_paused"] is False


def test_pause_leaves_an_open_canvas_alone(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setenv("TILE_PUBLIC_BASE_URL", "https://pixel.example")
    seen = install_poster(monkeypatch)
    set_hook(client, "ross", ["design_needed", "slice_open", "canvas_locked"])

    pin(client, "2026-10-28T08:00:00")
    opened = client.get("/cron/tick", headers=CRON).json()
    day_id = opened["day"]["day_id"]
    assert opened["paused"] is False
    posted = client.post("/agent/design", headers=auth("ross"), json=DESIGN)
    assert posted.status_code == 200, posted.text
    seen.clear()

    monkeypatch.setenv("TILE_DAILY_PAUSED", "on")
    held = client.get("/cron/tick", headers=CRON).json()
    assert held["paused"] is True
    assert held["day"]["day_id"] == day_id
    assert held["phase"] == "slice"
    painted = client.post(
        "/agent/act",
        headers=auth("ross"),
        json={"type": "paint", "pixels": [{"x": 4, "y": 5, "color": 3}]},
    )
    assert painted.status_code == 200, painted.text
    seen.clear()

    pin(client, "2026-10-29T08:00:00")
    nxt = client.get("/cron/tick", headers=CRON)
    assert nxt.status_code == 200, nxt.text
    body = nxt.json()
    assert body["paused"] is True
    assert body["phase"] == "locked"
    assert body["day"]["day_id"] == day_id
    assert body["wakes"] == []
    assert seen == []

    state = client.get("/watch/state").json()
    assert state["day_id"] == day_id
    assert state["status"] == "locked"
    assert state["theme"] == DESIGN["theme"]
    assert state["daily_paused"] is True
    gallery = client.get("/watch/gallery").json()["canvases"]
    assert [item["day_id"] for item in gallery] == [day_id]
    region = client.get(
        f"/host/days/{day_id}/region",
        params={"x": 4, "y": 5, "w": 1, "h": 1},
        headers=host(),
    )
    assert region.json()["cells"] == [3]
    days = client.app.state.store.conn.execute("SELECT COUNT(*) AS n FROM days").fetchone()["n"]
    assert days == 1
