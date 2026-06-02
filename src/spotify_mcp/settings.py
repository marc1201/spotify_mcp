"""Environment-driven settings. Loaded lazily in server.build_app() (never at import),
so `import spotify_mcp` stays side-effect-free and needs no secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    spotify_client_id: str
    spotify_client_secret: str
    base_url: str
    port: int
    allowed_user_ids: frozenset[str]
    jwt_signing_key: str


def _require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise SettingsError(
            f"Missing required env var {key!r}. "
            "Source config.env and scripts/secrets.sh before starting (see README)."
        )
    return val


def load_settings() -> Settings:
    base_url = os.environ.get("BASE_URL", "https://mcp.example.com").strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise SettingsError(f"BASE_URL must be an https:// origin, got {base_url!r}")
    allowed = os.environ.get("SPOTIFY_ALLOWED_USER_IDS", "").split()
    return Settings(
        spotify_client_id=_require("SPOTIFY_CLIENT_ID"),
        spotify_client_secret=_require("SPOTIFY_CLIENT_SECRET"),
        base_url=base_url,
        port=int(os.environ.get("PORT", "8080")),
        allowed_user_ids=frozenset(a for a in allowed if a),
        jwt_signing_key=_require("FASTMCP_JWT_KEY"),
    )
