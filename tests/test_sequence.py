"""Canvases are a sequence. The open one is the newest that has not locked.

Leftover bonus rows, archived blanks, and past dates must not decide which
canvas cron, turn, and watch agree on. The 08:00 cron opens one morning
canvas, and waits if a one-off is still running.
"""

from tests.conftest import DESIGN, auth, host, pin
from tests.test_wakes import CRON, install_poster, payloads, set_hook


def _design(client, theme):
    body = dict(DESIGN)
    body["theme"] = theme
    response = client.post("/agent/design", headers=auth("design"), json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _bonus(client, stamp, theme):
    pin(client, stamp)
    opened = client.post("/host/canvas/new", headers=host())
    assert opened.status_code == 200, opened.text
    _design(client, theme)
    return opened.json()["day"]["day_id"]


def test_mixed_history_does_not_confuse_the_next_morning(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    seen = install_poster(monkeypatch)
    _bonus(client, "2026-10-25T08:00:00", "October 25")
    _bonus(client, "2026-10-26T08:00:00", "October 26")
    _bonus(client, "2026-10-27T08:00:00", "October 27")

    pin(client, "2026-10-29T08:00:00")
    blank = client.get("/cron/tick", headers=CRON)
    assert blank.status_code == 200, blank.text
    assert blank.json()["phase"] == "awaiting_design"
    blank_id = blank.json()["day"]["day_id"]

    fourteen = _bonus(client, "2026-10-29T14:00:00", "Fourteen")
    assert fourteen != blank_id
    fifteen = _bonus(client, "2026-10-29T14:10:00", "Fifteen")
    sixteen = _bonus(client, "2026-10-29T14:20:00", "Sixteen")
    removed = client.delete(f"/host/days/{fifteen}", headers=host())
    assert removed.status_code == 200, removed.text

    # Label the archived blank and the newest painting as Oct 30. Selection
    # must ignore those labels.
    client.app.state.store.conn.execute(
        "UPDATE days SET ny_date = '2026-10-30' WHERE id IN (?, ?)",
        (blank_id, sixteen),
    )

    pin(client, "2026-10-29T18:00:00")
    evening = client.get("/cron/tick", headers=CRON)
    assert evening.status_code == 200, evening.text
    assert evening.json()["phase"] == "locked"
    assert evening.json()["day"]["day_id"] == sixteen
    assert evening.json()["day"]["archived"] is False
    watch = client.get("/watch/state").json()
    turn = client.get("/agent/turn", headers=auth("claude")).json()
    assert watch["day_id"] == sixteen
    assert watch["status"] == "locked"
    assert turn["day_id"] == sixteen
    assert turn["to_act"] is False

    pin(client, "2026-10-30T08:00:00")
    before = client.app.state.store.conn.execute("SELECT COUNT(*) AS n FROM days").fetchone()["n"]
    opened = client.get("/cron/tick", headers=CRON)
    assert opened.status_code == 200, opened.text
    assert opened.json()["phase"] == "awaiting_design"
    new_id = opened.json()["day"]["day_id"]
    assert new_id not in (blank_id, fourteen, fifteen, sixteen)
    again = client.get("/cron/tick", headers=CRON)
    assert again.json()["day"]["day_id"] == new_id
    third = client.post("/host/routine/tick", headers=host())
    assert third.json()["day"]["day_id"] == new_id
    after = client.app.state.store.conn.execute("SELECT COUNT(*) AS n FROM days").fetchone()["n"]
    assert after == before + 1

    watch = client.get("/watch/state").json()
    assert watch["day_id"] == new_id
    assert watch["status"] == "awaiting_design"
    for agent in ("claude", "grok-artist", "openai"):
        set_hook(client, agent, ["slice_open"], url=f"http://127.0.0.1:9/{agent}")
    designed = _design(client, "October 30")
    assert designed["day_id"] == new_id
    turn = client.get("/agent/turn", headers=auth(designed["first_agent_id"])).json()
    assert turn["day_id"] == new_id
    assert turn["to_act"] is True
    assert turn["slice_index"] == 0
    assert [item["day_id"] for item in payloads(seen)] == [new_id]
    assert payloads(seen)[0]["seat"] == designed["first_agent_id"]
    assert payloads(seen)[0]["event"] == "slice_open"
    client.get("/cron/tick", headers=CRON)
    assert len(seen) == 1


def test_morning_waits_until_a_running_one_off_locks(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    pin(client, "2026-10-30T07:50:00")
    opened = client.post("/host/canvas/new", headers=host())
    assert opened.status_code == 200, opened.text
    one_off = opened.json()["day"]["day_id"]
    _design(client, "Still going at eight")

    pin(client, "2026-10-30T08:00:00")
    first = client.get("/cron/tick", headers=CRON)
    assert first.status_code == 200, first.text
    assert first.json()["day"]["day_id"] == one_off
    assert first.json()["phase"] == "slice"
    assert client.get("/cron/tick", headers=CRON).json()["day"]["day_id"] == one_off
    assert client.get("/watch/state").json()["day_id"] == one_off
    assert client.get("/agent/turn", headers=auth("claude")).json()["day_id"] == one_off

    # 07:50 plus twelve 3-minute slices is 08:26.
    pin(client, "2026-10-30T08:26:00")
    started = client.get("/cron/tick", headers=CRON)
    assert started.status_code == 200, started.text
    assert started.json()["phase"] == "awaiting_design"
    new_id = started.json()["day"]["day_id"]
    assert new_id != one_off
    assert client.get("/cron/tick", headers=CRON).json()["day"]["day_id"] == new_id
    assert client.get("/watch/state").json()["day_id"] == new_id
    assert client.get("/agent/turn", headers=auth("claude")).json()["day_id"] == new_id
    assert client.get("/agent/turn", headers=auth("claude")).json()["to_act"] is False
