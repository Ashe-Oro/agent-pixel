"""HTTP surface for Agent Pixel.

Public: /skill.md, /watch, /watch/state, crisp PNGs.
Host key: open a day, move the dev clock, read the ledger.
Artist seats: design, TurnView, one paint or pass.
"""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app.config import Settings
from app.png import render_png
from app.schedule import GRID, SLICE_COUNT
from app.store import KNOWN_AGENTS, ApiError, Store

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC = STATIC_DIR / "watch.html"


def _parse_slices(raw: str | None) -> list[int] | None:
    """Parse ``slices=0,2,5``. Missing or blank means the full painting."""
    if raw is None or not raw.strip():
        return None
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) > SLICE_COUNT * 2:
        raise ApiError(400, "bad_slices", "slices lists at most 24 indices.")
    if any(part == "" for part in parts):
        raise ApiError(400, "bad_slices", "slices must be comma-separated indices from 0 to 11.")
    indices: list[int] = []
    for part in parts:
        if len(part) > 2 or not part.isdigit():
            raise ApiError(400, "bad_slices", "slices must be comma-separated indices from 0 to 11.")
        indices.append(int(part))
    return indices


def _key_ok(got: str, expected: str) -> bool:
    if not got or not expected:
        return False
    left = got.encode()
    right = expected.encode()
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings)
    if settings.seed:
        store.seed_demo()

    app = FastAPI(
        title="Agent Pixel",
        version="1.0.0",
        summary="A fresh 256×256 pixel mural every morning, painted in 3-minute slices.",
    )
    app.state.store = store
    app.state.settings = settings

    @app.exception_handler(ApiError)
    async def on_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            {"error": exc.error, "message": exc.message},
            status_code=exc.status,
        )

    def host_guard(request: Request) -> None:
        if not _key_ok(request.headers.get("x-api-key", ""), settings.api_key):
            raise ApiError(401, "unauthorized", "Host routes need a valid X-API-Key.")

    def agent_id(request: Request) -> str:
        token = request.headers.get("x-seat-token", "").strip()
        named = request.headers.get("x-agent-id", "").strip()
        if token:
            agent = settings.seat_tokens.get(token)
            if not agent:
                raise ApiError(403, "unauthorized", "Unknown seat token.")
            if named and named != agent:
                raise ApiError(403, "unauthorized", "Seat token does not match X-Agent-Id.")
            return agent
        if not _key_ok(request.headers.get("x-api-key", ""), settings.api_key):
            raise ApiError(
                401,
                "unauthorized",
                "Send X-API-Key and X-Agent-Id, or a seat token.",
            )
        if named not in KNOWN_AGENTS:
            raise ApiError(
                401,
                "unauthorized",
                "X-Agent-Id must be design, claude, grok-artist, or openai.",
            )
        return named

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": "true",
            "service": "agent-pixel",
            "persistent": store.persistent,
        }

    @app.get("/", include_in_schema=False)
    def root() -> FileResponse:
        return FileResponse(STATIC)

    @app.get("/canvas/{day_id}", include_in_schema=False)
    def canvas_page(day_id: int) -> FileResponse:
        return FileResponse(STATIC)

    @app.get("/skill.md")
    def skill_md() -> Response:
        path = settings.skill_path
        if not path.is_file():
            raise ApiError(404, "no_skill", "skill.md is not in the repo.")
        return Response(path.read_bytes(), media_type="text/markdown; charset=utf-8")

    @app.get("/watch", include_in_schema=False)
    def watch() -> FileResponse:
        return FileResponse(STATIC)

    @app.get("/static/mascot.png", include_in_schema=False)
    def mascot_png() -> FileResponse:
        return FileResponse(STATIC_DIR / "mascot.png", media_type="image/png")

    @app.get("/watch/state")
    def watch_state(day_id: int | None = None) -> dict[str, Any]:
        return store.watch_state(day_id=day_id)

    @app.get("/watch/gallery")
    def watch_gallery() -> dict[str, Any]:
        return store.gallery()

    @app.get("/watch/chat")
    def watch_chat(day_id: int | None = None) -> dict[str, Any]:
        return store.list_chat(day_id)

    @app.post("/watch/chat")
    def post_chat(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return store.post_chat(agent_id(request), body.get("text"))

    @app.get("/watch/canvas.png")
    def canvas_png(day_id: int, scale: int = 3, slices: str | None = None) -> Response:
        canvas, palette = store.canvas_for_slices(day_id, _parse_slices(slices))
        payload = render_png(canvas, palette, scale=scale)
        return Response(payload, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/watch/crop.png")
    def crop_png(day_id: int, x: int, y: int, w: int, h: int, scale: int = 4) -> Response:
        if w < 1 or h < 1 or x < 0 or y < 0 or x + w > GRID or y + h > GRID:
            raise ApiError(400, "bad_focus", "Crop is outside the 256×256 canvas.")
        canvas, palette = store.canvas_of(day_id)
        payload = render_png(canvas, palette, scale=scale, crop=(x, y, w, h))
        return Response(payload, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/host/days/open")
    def open_day(request: Request) -> dict[str, Any]:
        host_guard(request)
        return store.open_today()

    @app.post("/host/canvas/new")
    def new_canvas(request: Request) -> dict[str, Any]:
        host_guard(request)
        return store.open_bonus()

    @app.delete("/host/days/{day_id}")
    def delete_day(day_id: int, request: Request, force: bool = False) -> dict[str, Any]:
        host_guard(request)
        return store.delete_day(day_id, force=force)

    @app.post("/host/routine/tick")
    def routine_tick(request: Request) -> dict[str, Any]:
        host_guard(request)
        return store.routine_tick()

    @app.post("/host/clock")
    def clock(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        host_guard(request)
        return store.adjust_clock(body)

    @app.put("/host/roster")
    def roster(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        host_guard(request)
        order = body.get("order")
        if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
            raise ApiError(400, "bad_roster", "order must be a list of agent ids.")
        rotate = body.get("rotate", True)
        if not isinstance(rotate, bool):
            raise ApiError(400, "bad_roster", "rotate must be a boolean.")
        return store.set_roster(order, rotate)

    @app.put("/host/budgets")
    def budgets(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        host_guard(request)
        per_turn = body.get("pixels_per_turn", 0)
        per_day = body.get("pixels_per_artist_day", 0)
        if per_turn is None:
            per_turn = 0
        if per_day is None:
            per_day = 0
        if type(per_turn) is not int or type(per_day) is not int or per_turn != 0 or per_day != 0:
            raise ApiError(
                400,
                "bad_budget",
                "Pixel caps are off. One paint can cover the whole 256×256 grid. Pass 0 or omit both fields.",
            )
        return store.set_budgets()

    @app.get("/host/days/current")
    def current_day(request: Request) -> dict[str, Any]:
        host_guard(request)
        return store.watch_state()

    @app.get("/host/days/{day_id}")
    def one_day(day_id: int, request: Request) -> dict[str, Any]:
        host_guard(request)
        return store.watch_state(day_id=day_id)

    @app.get("/host/days/{day_id}/ledger")
    def ledger(day_id: int, request: Request) -> dict[str, Any]:
        host_guard(request)
        return store.ledger(day_id)

    @app.get("/host/days/{day_id}/region")
    def region(day_id: int, request: Request, x: int = 0, y: int = 0, w: int = 1, h: int = 1) -> dict[str, Any]:
        host_guard(request)
        return store.region(day_id, x, y, w, h)

    @app.get("/agent/design")
    def design_view(request: Request) -> dict[str, Any]:
        return store.design_view(agent_id(request))

    @app.post("/agent/design")
    def design(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return store.submit_design(agent_id(request), body)

    @app.get("/agent/turn")
    def turn(request: Request) -> dict[str, Any]:
        return store.turn_view(agent_id(request))

    @app.post("/agent/act")
    def act(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return store.act(agent_id(request), body)

    return app


app = create_app()
