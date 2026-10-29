"""Process settings. The dev API key is a local default, not a production secret."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent

# Seat id → env suffix. grok-artist has no hyphen in the variable name.
WAKE_ENV = (
    ("claude", "CLAUDE"),
    ("grok-artist", "GROK_ARTIST"),
    ("openai", "OPENAI"),
    ("ross", "ROSS"),
    ("host", "HOST"),
)


def resolve_api_key() -> str:
    """Production must set TILE_API_KEY or HOST_API_KEY. The dev default is local only."""
    key = os.environ.get("TILE_API_KEY", "").strip() or os.environ.get("HOST_API_KEY", "").strip()
    if key:
        return key
    if os.environ.get("VERCEL"):
        return ""
    return "tile-dev-key"


def clean_base_url(value: str) -> str:
    """Origin only. Userinfo, path, and query are dropped so a wake cannot echo them."""
    text = value.strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return ""
    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{parts.scheme}://{host}"


def resolve_public_base_url() -> str:
    """Explicit origin, else the Vercel production host, else this deployment."""
    explicit = os.environ.get("AP_PUBLIC_BASE_URL", "").strip()
    if explicit:
        return clean_base_url(explicit)
    production = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL", "").strip()
    if production:
        return clean_base_url(production)
    deployment = os.environ.get("VERCEL_URL", "").strip()
    if deployment:
        return clean_base_url(deployment)
    return ""


def resolve_wakes() -> dict[str, tuple[str, str]]:
    """URL and sender key per seat. A seat with no URL is omitted."""
    hooks: dict[str, tuple[str, str]] = {}
    for seat, suffix in WAKE_ENV:
        url = os.environ.get(f"AP_WAKE_URL_{suffix}", "").strip()
        key = os.environ.get(f"AP_WAKE_KEY_{suffix}", "").strip()
        if url:
            hooks[seat] = (url, key)
    return hooks


@dataclass(frozen=True)
class Settings:
    db_path: Path
    api_key: str
    seed: bool
    pin_morning: bool
    skill_path: Path
    seat_tokens: dict[str, str]
    cron_secret: str = ""
    public_base_url: str = ""
    wakes: dict[str, tuple[str, str]] = field(default_factory=dict)

    def __repr__(self) -> str:
        seats = ",".join(sorted(self.wakes))
        return (
            f"Settings(db_path={self.db_path!r}, seed={self.seed}, "
            f"pin_morning={self.pin_morning}, wake_seats={seats!r})"
        )

    @classmethod
    def from_env(cls) -> Settings:
        seats: dict[str, str] = {}
        for agent, env_name in (
            ("design", "TILE_SEAT_DESIGN"),
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
            cron_secret=os.environ.get("CRON_SECRET", "").strip(),
            public_base_url=resolve_public_base_url(),
            wakes=resolve_wakes(),
        )
