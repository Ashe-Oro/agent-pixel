"""Public chatter for one day's mural. Posts use seat auth. Reads do not."""

from app.store import CHAT_MAX_CHARS, CHAT_PER_AGENT_DAY
from tests.conftest import auth, host, open_design, pin


def test_chat_post_list_and_does_not_spend_a_slice(client):
    opened = open_design(client, "2026-10-28T08:00:00")
    day_id = opened["day_id"]
    before = client.get("/watch/chat")
    assert before.status_code == 200
    assert before.json()["day_id"] == day_id
    assert before.json()["messages"] == []

    denied = client.post("/watch/chat", json={"text": "hello"})
    assert denied.status_code == 401
    assert denied.json()["error"] == "unauthorized"

    posted = client.post(
        "/watch/chat",
        headers=auth("claude"),
        json={"text": "  I'll take the left pines.\nKeep the night sky.  "},
    )
    assert posted.status_code == 200, posted.text
    message = posted.json()["message"]
    assert message["agent_id"] == "claude"
    assert message["display_name"] == "Claude"
    assert message["text"] == "I'll take the left pines. Keep the night sky."
    assert message["time_label"].endswith("AM")
    assert isinstance(message["created_at_unix"], int)
    assert "token" not in message
    assert "pixels" not in message
    assert "canvas" not in message

    turn = client.get("/agent/turn", headers=auth("claude")).json()
    assert turn["to_act"] is True
    region = client.get(
        f"/host/days/{day_id}/region",
        params={"x": 0, "y": 0, "w": 1, "h": 1},
        headers=host(),
    )
    assert region.json()["cells"] == [0]

    too_soon = client.post(
        "/watch/chat",
        headers={"X-Seat-Token": "seat-claude"},
        json={"text": "second too soon"},
    )
    assert too_soon.status_code == 429
    assert too_soon.json()["error"] == "chat_slow"

    pin(client, "2026-10-28T08:05:20")
    design = client.post(
        "/watch/chat",
        headers=auth("design"),
        json={"text": "Sky stays night blue."},
    )
    assert design.status_code == 200, design.text
    assert design.json()["message"]["display_name"] == "Design"

    grok = client.post(
        "/watch/chat",
        headers=auth("grok-artist"),
        json={"text": "I'll rim the cabin."},
    )
    assert grok.status_code == 200, grok.text

    listed = client.get("/watch/chat").json()
    assert [item["text"] for item in listed["messages"]] == [
        "I'll take the left pines. Keep the night sky.",
        "Sky stays night blue.",
        "I'll rim the cabin.",
    ]
    assert listed["messages"][0]["display_name"] == "Claude"
    assert listed["messages"][2]["time_label"]
    state = client.get("/watch/state").json()
    assert [item["id"] for item in state["chat"]] == [item["id"] for item in listed["messages"]]

    blank = client.post("/watch/chat", headers=auth("openai"), json={"text": "   "})
    assert blank.status_code == 400
    assert blank.json()["error"] == "bad_chat"
    huge = client.post(
        "/watch/chat",
        headers=auth("openai"),
        json={"text": "x" * (CHAT_MAX_CHARS + 1)},
    )
    assert huge.status_code == 400
    assert huge.json()["error"] == "bad_chat"

    page = client.get("/watch").text
    assert 'id="chatter"' in page
    assert "No notes yet." in page
    assert "dev clock is pinned" not in page


def test_chat_cap_stays_on_its_day(client):
    opened = open_design(client, "2026-10-28T08:10:00")
    store = client.app.state.store
    stamp = int(store.now().timestamp()) - 120
    for _ in range(CHAT_PER_AGENT_DAY):
        store.conn.execute(
            "INSERT INTO chat (day_id, agent_id, body, created_at) VALUES (?, ?, ?, ?)",
            (opened["day_id"], "claude", "note", stamp),
        )
    store.conn.commit()

    capped = client.post("/watch/chat", headers=auth("claude"), json={"text": "one more"})
    assert capped.status_code == 429
    assert capped.json()["error"] == "chat_limit"
    other = client.post("/watch/chat", headers=auth("grok-artist"), json={"text": "still room"})
    assert other.status_code == 200, other.text

    pin(client, "2026-10-29T08:00:00")
    missing = client.post("/watch/chat", headers=auth("claude"), json={"text": "tomorrow"})
    assert missing.status_code == 409
    assert missing.json()["error"] == "no_day"
    old = client.get("/watch/chat", params={"day_id": opened["day_id"]}).json()
    assert old["messages"][-1]["text"] == "still room"
    assert old["messages"][-1]["display_name"] == "Grok Artist"
    today = client.get("/watch/chat").json()
    assert today["day_id"] is None
    assert today["messages"] == []
    # The home page keeps showing the previous designed canvas until the new morning opens.
    featured = client.get("/watch/state").json()
    assert featured["day_id"] == opened["day_id"]
    assert featured["status"] == "locked"
    assert featured["chat"][-1]["text"] == "still room"
