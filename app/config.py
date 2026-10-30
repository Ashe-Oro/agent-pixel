"""Process settings. The dev API key is a local default, not a production secret."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def resolve_api_key() -> str:
    """Production must set TILE_API_KEY or HOST_API_KEY. The dev default is local only."""
    key = os.environ.get("TILE_API_KEY", "").strip() or os.environ.get("HOST_API_KEY", "").strip()
    if key:
        return key
    if os.environ.get("VERCEL"):
        return ""
    return "tile-dev-key"


@dataclass(frozen=True)
class Settings:
    db_path: Path
    api_key: str
    seed: bool
    pin_morning: bool
    skill_path: Path
    seat_tokens: dict[str, str]

    @classmethod
    def from_env(cls) -> Settings:
        seats: dict[str, str] = {}
        for agent, env_name in (
            ("design", "TILE_SEAT_DESIGN"),
            ("ross", "TILE_SEAT_ROSS"),
            ("claude", "TILE_SEAT_CLAUDE"),
            ("grok-artist", "TILE_SEAT_GROK"),
            ("openai", "TILE_SEAT_OPENAI"),
        ):
            token = os.environ.get(env_name, "").strip()
            if token:
                seats[token] = agent
        on_vercel = bool(os.environ.get("VERCEL"))
        seed_default = "0" if on_vercel else "1"
        return cls(
            db_path=Path(os.environ.get("TILE_DB_PATH", ROOT / "data" / "tile.db")),
            api_key=resolve_api_key(),
            seed=os.environ.get("TILE_SEED", seed_default) != "0",
            pin_morning=os.environ.get("TILE_PIN_MORNING", "0") == "1",
            skill_path=Path(os.environ.get("TILE_SKILL_PATH", ROOT / "skill.md")),
            seat_tokens=seats,
        )
