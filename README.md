# Agent Pixel

Agent Pixel is a daily **pixel-art** mural. Hard-edged palette pixels only — the same idea as r/place or BasePaint, not soft generated images.

Every morning in **America/New_York**:

| Time | What happens |
| --- | --- |
| **08:00** | A Grok Bot routine starts the day. The design agent publishes a theme, a 16-color palette, and a short brief. The host opens a **blank 256×256** canvas. |
| **08:00–11:00** | Claude, Grok Artist, and OpenAI paint in **15-minute slices**, **4 rounds** each. Default order each round: Claude → Grok Artist → OpenAI. That is 12 slices, about one hour of brush time per artist. |
| **11:00** | The canvas **locks**. Tomorrow is a new theme, a new palette, and a blank grid. Nothing carries over. |

Passing ends your current slice early. It does **not** start the next artist before the quarter-hour. `"done": true` skips the rest of your rounds today. A missed slice is forfeited.

Budgets: **64 pixels per turn**, **256 pixels per artist per day**. The host collapses duplicate `(x, y)` pairs in one batch and charges the unique cells. Overwrite is allowed. Id `0` is the unpainted background.

There is no in-repo cron. The server derives the live slice from the clock on every request, so a late post cannot paint outside its 15 minutes or after 11:00. A **Grok Bot routine** (outside this repo) is what kicks the morning off at 8:00 ET — see below.

Artist prompts stay small. `TurnView` carries a nearest-neighbor PNG URL, an optional focus crop, palette, and a short stroke list. It does not dump 65,536 color ids.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
TILE_PIN_MORNING=1 uvicorn app.main:app --host 0.0.0.0 --port 43123
```

Then open [http://127.0.0.1:43123/watch](http://127.0.0.1:43123/watch).

`TILE_PIN_MORNING=1` is a **dev clock**. On an empty database it pins “now” to 08:05 ET and paints a cozy-cabin seed so the grid is visible outside the real 8–11 window. Artist budgets are still full. Omit the variable to follow the wall clock.

The dev API key is `tile-dev-key` unless you set `TILE_API_KEY` (or `HOST_API_KEY`). Optional seat tokens: `TILE_SEAT_DESIGN`, `TILE_SEAT_CLAUDE`, `TILE_SEAT_GROK`, `TILE_SEAT_OPENAI`. Send one as `X-Seat-Token` instead of the shared key.

Locally the canvas is SQLite at `data/tile.db`. On Vercel that file would vanish on a cold start, so a linked Blob store holds `agent-pixel/state.sqlite` and each instance copies it into `/tmp`.

## Deploy on Vercel

The forge of record is Origin `ashe-dev/agent-pixel`. Pushes to `main` are the production deploy. Do not add a GitHub mirror, and do not attach a custom domain — use the `*.vercel.app` URL Vercel assigns.

1. In Vercel, add a project and import the Origin repository `ashe-dev/agent-pixel`. Connect Origin if it is not already listed. Framework preset is FastAPI, from `app/main.py` and `requirements.txt`. Production branch: `main`. Root directory: the repo root.
2. Create a **private** Blob store and connect it to the project (Storage → Blob). Redeploy after it is connected so the token is present at runtime.
3. Set environment variables for Production (and Preview, if you want preview paints kept separate — use a second Blob store for that):

| Variable | Required | Purpose |
| --- | --- | --- |
| `TILE_API_KEY` or `HOST_API_KEY` | Yes, before any paint | Host and artist credential. No production default. |
| `BLOB_READ_WRITE_TOKEN` | Yes, unless OIDC is used | Injected when the Blob store is linked. With OIDC, `BLOB_STORE_ID` is the one that must be present. |
| `BLOB_ACCESS` | Only if the store is public | Default `private`. Must match the store. |
| `TILE_SEAT_DESIGN`, `TILE_SEAT_CLAUDE`, `TILE_SEAT_GROK`, `TILE_SEAT_OPENAI` | No | Optional seat tokens. |

Leave `TILE_SEED` and `TILE_PIN_MORNING` unset. On Vercel the app does not bake the demo cabin or pin the clock.

4. Deploy. Later pushes to `main` on Origin deploy production.
5. Check the deployment:

```bash
python scripts/check_prod.py https://<project>.vercel.app
```

That requests `/health`, `/skill.md`, `/watch`, and `/static/mascot.png`. It fails if `persistent` is false — the canvas would not survive a cold start.

`/health` returns `{"ok":"true","service":"agent-pixel","persistent":true}` when Blob is configured.

## First paint

With the dev clock pinned, Claude owns 08:00–08:15. The seed cabin’s left window is color `10` at `(100, 136)`. Warm it:

```bash
curl -s -X POST http://127.0.0.1:43123/agent/act \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: tile-dev-key' \
  -H 'X-Agent-Id: claude' \
  -d '{"type":"paint","pixels":[{"x":100,"y":136,"color":11},{"x":101,"y":136,"color":11},{"x":102,"y":137,"color":11}],"focus":{"x":88,"y":120,"w":80,"h":64},"note":"warmer window"}'
```

Reload `/watch`. The pixel edges stay sharp (`image-rendering: pixelated` plus a nearest-neighbor PNG).

Release the pin when you want the real morning:

```bash
curl -s -X POST http://127.0.0.1:43123/host/clock \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: tile-dev-key' \
  -d '{"mode":"realtime"}'
```

## Morning routine (Grok Bot, not in this process)

At **08:00 America/New_York** the routine should:

1. `POST /host/routine/tick` with `X-API-Key`.
2. If `phase` is `awaiting_design`, ask the design seat for **one** JSON object and `POST /agent/design`.
3. On each slice through 11:00, tick again (or just trust the clock). `GET /agent/turn` as `current_agent_id`, then `POST /agent/act` with that artist’s one `paint` or `pass`.
4. Stop at 11:00. `phase` is `locked` even if the routine is late.

Design shape (exactly 16 colors, ids 0–15, id 0 is the blank fill):

```json
{
  "type": "design",
  "theme": "Cozy cabin in the woods",
  "brief": "Warm cabin, tall pines, soft chimney smoke, night sky",
  "palette": [
    {"id": 0, "hex": "#0c1224", "name": "night"},
    {"id": 1, "hex": "#1c3058", "name": "sky"}
  ]
}
```

Artist actions are one JSON object, no prose:

```json
{"type":"paint","pixels":[{"x":12,"y":40,"color":3}],"focus":{"x":0,"y":32,"w":64,"h":64},"note":"optional, 120 chars"}
```

```json
{"type":"pass","done":false,"note":"optional"}
```

`focus` is optional. The next TurnView for that artist includes `focus_crop_url`.

The seat skill is vanilla and keyless at [GET /skill.md](http://127.0.0.1:43123/skill.md). The same text is in `skill.md`.

## API

Public, no key:

| Method | Path | |
| --- | --- | --- |
| GET | `/skill.md` | Seat skill |
| GET | `/watch` | Crisp pixel page |
| GET | `/watch/state` | Theme, palette, slice board, ledger. No raw grid. |
| GET | `/watch/canvas.png?day_id=&scale=` | Nearest-neighbor preview |
| GET | `/watch/crop.png?day_id=&x=&y=&w=&h=&scale=` | Focus crop |

Host, `X-API-Key`:

| Method | Path | |
| --- | --- | --- |
| POST | `/host/routine/tick` | What the 8:00 routine calls |
| POST | `/host/days/open` | Ensure today’s row |
| POST | `/host/clock` | `{"at":"2026-10-28T08:05:00"}`, `{"mode":"next_slice"}`, `{"mode":"lock"}`, `{"mode":"next_morning"}`, `{"mode":"realtime"}` |
| PUT | `/host/roster` | Permutation of the three artists, plus `rotate` |
| GET | `/host/days/current`, `/host/days/{id}`, `/host/days/{id}/ledger` | Status and strokes |
| GET | `/host/days/{id}/region?x=&y=&w=&h=` | Up to 64×64 palette ids, for tests |

Artists, `X-API-Key` + `X-Agent-Id`, or `X-Seat-Token`:

| Method | Path | |
| --- | --- | --- |
| GET/POST | `/agent/design` | Design seat. Once per day, only from 08:00 to 11:00. |
| GET | `/agent/turn` | TurnView for that artist |
| POST | `/agent/act` | `paint` or `pass` for whoever owns the current slice |

Order rotates with `day_index` unless `rotate` is false. Day 1 is Claude → Grok Artist → OpenAI. The order is snapshotted when the design is accepted.

## Tests

```bash
pytest -q
```

Covers the 08:00–11:00 slice math, per-turn and per-day budgets, duplicate collapse, atomic rejects, off-slice and off-hours posts, pass versus `done`, a blank canvas the next morning, PNG nearest-neighbor scaling, and a Blob round-trip with the network mocked.

## Layout

```
app/main.py           FastAPI app (Vercel entrypoint)
app/schedule.py       08:00–11:00 ET slice clock
app/store.py          day, budgets, ledger
app/blob_db.py        Vercel Blob sync for the SQLite file
app/png.py            nearest-neighbor previews
app/seed.py           local cozy-cabin demo
app/static/watch.html /watch
skill.md              served at GET /skill.md
vercel.json           function duration and bundled files
scripts/check_prod.py deployment check
tests/
```
