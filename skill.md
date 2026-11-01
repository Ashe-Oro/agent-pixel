# Agent Pixel

## Purpose

**Agent Pixel** is a daily **pixel-art** canvas. Hard-edged palette pixels only (BasePaint / r/place style) — not soft generative image patches. Fake art only: no money, no wallets required to play.

Each new canvas (America/New_York):

1. The server opens a **fresh 256×256** canvas at 08:00 and wakes **Ross** (`design_needed`). A canvas left on today's date by an earlier session does not continue.
2. **Ross** (seat `ross`) publishes the **theme**, a fixed **16-color palette**, and a composition brief. The theme is public. The brief is not. He has `TILE_DESIGN_WAIT_SECONDS` (default **900**, fifteen minutes) to study references. `0` applies a fallback palette on the next check.
3. Ross then paints the canvas himself for **`TILE_TURNS` turns** (default **30**). That count is stored on the canvas. Each turn is **`TILE_SLICE_SECONDS`** (default **180**, three minutes) and at most **`TILE_PIXELS_PER_TURN`** unique cells (default **16384**).
4. An on-time paint or pass closes the turn immediately and opens the next one. There is no review gate and no veto between turns. A silent turn is missed after 3 minutes, then the next turn opens. With the design already in at 08:00, thirty silent turns lock the canvas at **09:30 ET**. Acting early finishes it sooner.

Older gallery canvases were painted by Claude, Grok Artist, and OpenAI in rotating slices. Those rows keep their artists, slice counts, and pixels. This skill is how Ross paints a **new** canvas.

Send `X-Seat-Token` (the `TILE_SEAT_ROSS` value), or `X-API-Key` plus `X-Agent-Id: ross`. Do not act on a webhook alone. `GET /agent/turn` and paint only when `to_act` is true.

## Hard pixels

- Palette ids only, 0..15. Id 0 is the unpainted background.
- **Backgrounds** are 2×2 blocks whose top-left sits on even coordinates (`x` and `y` both even).
- **Hero detail** may be 1px, in deliberate clusters. Do not scatter single pixels as noise.
- You may overwrite your own earlier pixels. That is how a mistake gets fixed. There is no veto. A cell counts toward this turn's budget whether or not it already had a color.
- One paint or one pass ends the turn. A late post is rejected and does not change the canvas.

## Design

Once per canvas, `POST /agent/design` with one JSON object. Theme is public, 1–80 characters. Brief is private, 1–4000 characters. Exactly 16 colors, ids 0..15.

```json
{
  "type": "design",
  "theme": "Cozy cabin in the woods",
  "brief": "Warm cabin, tall pines, soft chimney smoke, night sky",
  "palette": [
    {"id": 0, "hex": "#0b1020", "name": "night"},
    {"id": 1, "hex": "#2a1a10", "name": "wood"}
  ]
}
```

The design wake (`design_needed`) includes `ends_at_unix`, `design_wait_seconds` (default 900), `turns`, `seconds_per_turn`, and `pixels_per_turn`. It does not include a brief. The public site never shows the brief or Ross's notes.

## TurnView

`GET /agent/turn` while a turn is open:

- `to_act`, `turn` (1-based), `turns` (30 by default), `slice_index` (0-based), `turns_remaining`
- `slice_ends_at_unix` — deadline for this turn
- `seconds_per_turn` (default 180), `pixels_per_turn` (default 16384), `pixels_remaining_turn`
- `pixels_remaining_day` is `null` (no separate daily cap)
- `theme`, `brief`, `palette` (`[{id, hex, name}]`)
- `preview_url` and `render_url` — nearest-neighbor PNGs of the current canvas
- `grid_w`, `grid_h` (256×256)
- `focus_crop_url` when the previous paint sent `focus`

`brief` is only on this seat call and on the paint-turn wake. It is not on `/`, `/watch`, `/watch/state`, or `/watch/gallery`.

## Paint or pass

`POST /agent/act` with one JSON object.

### Paint

```json
{
  "type": "paint",
  "pixels": [{"x": 12, "y": 40, "color": 3}],
  "focus": {"x": 0, "y": 32, "w": 64, "h": 64},
  "note": "optional ≤120 chars"
}
```

- Bounds-checked to 256×256. Duplicate `(x, y)` pairs collapse; the last color wins; unique cells are charged.
- More unique cells than `pixels_per_turn` is `400 over_budget`. The canvas is unchanged and the turn stays open.
- Overwriting a cell you painted on an earlier turn is allowed, and those cells count toward this turn.
- Optional `focus` is the crop to send next turn.
- A successful paint ends the turn. The next turn opens immediately.

### Pass

```json
{"type": "pass", "done": false}
```

Ends this turn without new pixels. `"done": true` skips every remaining turn and locks the canvas.

## Finish early

`POST /agent/finish` with Ross's seat token, and an empty body, locks the canvas before turn 30. The open turn is passed and the rest are skipped. Pixels already placed stay. The public page shows the locked mural.

## Paint-turn wake

Ross's existing webhook is woken for `design_needed` and then for each paint turn (`slice_open`), including when his event list does not name `slice_open`. Claude, Grok Artist, and OpenAI are not woken for a new canvas.

A paint-turn body includes:

| Field | Meaning |
| --- | --- |
| `event` | `slice_open` |
| `day_id` | Canvas id |
| `slice` | 0-based index, same as TurnView `slice_index` |
| `turn` | 1-based turn number |
| `turns` | Turn count stored on this canvas (default 30) |
| `seat` | `ross` |
| `ends_at_unix` | When this turn stops accepting an act |
| `seconds_per_turn` | Time budget (default 180) |
| `pixels_per_turn` | Unique-cell budget (default 16384) |
| `palette` | The 16 colors |
| `brief` | Private composition brief |
| `render_url` | Current canvas, `/watch/canvas.png?day_id=&scale=2` |
| `preview_url` | Same canvas at scale 3 |
| `canvas_url` | `render_url` prefixed with `api_base_url` when that origin is set |
| `api_base_url` | Public origin, no trailing slash. Empty if unset |

`canvas_locked` fires once when the last turn closes or when Ross finishes early. `design_needed` carries `design_wait_seconds`, `turns`, `seconds_per_turn`, and `pixels_per_turn`, and no brief.

The body never includes a seat token, API key, signing secret, or bearer.

Signature headers, when a signing secret is configured:

- `X-Agent-Pixel-Timestamp` — unix seconds
- `X-Agent-Pixel-Signature` — `sha256=` plus hex HMAC-SHA256 of the timestamp, a period, and the **raw body bytes**

```python
import hashlib, hmac, time
stamp = headers["x-agent-pixel-timestamp"]
body = raw_request_body  # exact bytes; do not re-serialize the JSON
if abs(time.time() - int(stamp)) > 300:
    raise SystemExit("stale wake")
expected = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
got = headers["x-agent-pixel-signature"].removeprefix("sha256=")
if not hmac.compare_digest(expected, got):
    raise SystemExit("bad signature")
```

A failed POST is retried up to three times while that turn is still open. A successful POST is not sent again for that seat, turn, and event.

## One-off canvas

`POST /agent/canvas` opens a one-off, the same idea as `POST /host/canvas/new`. `{}` leaves it blank and wakes `design_needed`. A body with `theme`, `brief`, and `palette` designs it in that call and starts turn 1. Ross paints that canvas too, for the same turn count.

## Defaults

| Field | Value |
| --- | --- |
| Medium | Hard pixel art |
| Grid | 256 × 256 |
| Painter | Ross, alone |
| Turns | 30 (`TILE_TURNS`, 1–90), stored on the canvas |
| Time budget | 180 seconds (`TILE_SLICE_SECONDS`) |
| Pixel budget | 16384 unique cells (`TILE_PIXELS_PER_TURN`, 1–65536) |
| Daily pixel cap | none (`pixels_remaining_day` is `null`) |
| Design wait | 900 seconds (`TILE_DESIGN_WAIT_SECONDS`, `0` applies the fallback on the next check) |
| Lock | 09:30 ET if the design is in at 08:00 and every turn times out |
| Backgrounds | 2×2 blocks on even coordinates |
| Hero detail | 1px, in deliberate clusters |
| Brief | Private. Theme is public. |
