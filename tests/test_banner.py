"""Pause banner on the homepage only, and the /paused note."""

OTHER_PAGES = ("/watch", "/canvas/1", "/paused")
BANNER = "Agent Pixel is on pause."
LINK = 'href="/paused">Read why →</a>'

NOTE_LINES = (
    "Agent Pixel was an experiment: a new AI-made pixel-art mural every day, painted in public on a 256×256 canvas with a strict 16-color palette. We held it to one test. Would someone seeing it for the first time look at it and think \"wow\"?",
    "After about 40 canvases, the honest answer is no. So we're pausing the project to rethink it.",
    "Several AI artists sharing one canvas.",
    "Each artist's work kept pulling against the others, and the pieces rarely came together as one picture.",
    "A single AI artist.",
    "The canvases became much more coherent, and a torii gate framing the setting sun was our best piece. But building an image a few pixels at a time was slow, and the big picture often got lost in the details.",
    "Generating the whole image with code.",
    "It was fast, and the pixels came out clean, but the result looked computed rather than crafted, with stair-stepped skies, flat shapes and ruler-straight lines.",
    "Clean isn't the same as good.",
    "We got reliable at crisp pixels and subjects you could recognize at a glance. That never quite became beautiful.",
    "Progress can be misleading.",
    '"Better than last week" felt like a win even while the work was still far from the bar.',
    "The craft is in the details.",
    "The best pixel art we studied, whether hand-drawn, coded or AI-generated, comes from rich color shifts, irregular clusters of shading and carefully placed edges.",
    "The format may be the real limit.",
    "One still image a day, under tight constraints, may not be interesting enough on its own, even when it's well made.",
    "The site and every past canvas will stay up. If Agent Pixel returns, it will probably look different: a new format, more time and care put into each piece, or both.",
    "Thanks for following along.",
)


def test_banner_shows_only_on_homepage_when_paused(client, monkeypatch):
    monkeypatch.setenv("TILE_DAILY_PAUSED", "yes")
    home = client.get("/")
    assert home.status_code == 200
    assert home.headers["content-type"].startswith("text/html")
    assert home.headers["cache-control"] == "no-store"
    assert BANNER in home.text
    assert LINK in home.text
    assert 'class="pause-banner"' in home.text
    for path in OTHER_PAGES:
        page = client.get(path)
        assert page.status_code == 200, path
        assert "Read why" not in page.text
        assert 'class="pause-banner"' not in page.text
    # / and /watch share watch.html. The path, not the file, decides.
    watch = client.get("/watch").text
    assert watch == home.text.replace(
        '<p class="pause-banner">Agent Pixel is on pause. <a href="/paused">Read why →</a></p>',
        "",
    )


def test_banner_is_absent_on_homepage_when_not_paused(client, monkeypatch):
    monkeypatch.delenv("TILE_DAILY_PAUSED", raising=False)
    home = client.get("/")
    assert home.status_code == 200
    assert BANNER not in home.text
    assert "Read why" not in home.text
    assert 'class="pause-banner"' not in home.text
    monkeypatch.setenv("TILE_DAILY_PAUSED", "OFF")
    assert "Read why" not in client.get("/").text
    monkeypatch.setenv("TILE_DAILY_PAUSED", "1")
    assert LINK in client.get("/").text
    assert "Read why" not in client.get("/watch").text
    assert "Read why" not in client.get("/canvas/3").text
    assert "Read why" not in client.get("/paused").text
    monkeypatch.setenv("TILE_DAILY_PAUSED", "0")
    assert "Read why" not in client.get("/").text


def test_paused_page_renders_the_note(client, monkeypatch):
    monkeypatch.delenv("TILE_DAILY_PAUSED", raising=False)
    page = client.get("/paused")
    assert page.status_code == 200
    body = page.text
    assert "<h1>Agent Pixel is on pause</h1>" in body
    assert "<h2>What we tried</h2>" in body
    assert "<h2>What we learned</h2>" in body
    assert "<h2>What's next</h2>" in body
    for line in NOTE_LINES:
        assert line in body
    assert 'href="/#gallery"' in body
    assert "Back to the gallery" in body
    assert "Ross" not in body
    assert "TILE_" not in body
    assert "Read why" not in body
    monkeypatch.setenv("TILE_DAILY_PAUSED", "1")
    assert client.get("/paused").text == body
