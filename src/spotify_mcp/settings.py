"""Settings: the systemd credential store first, the environment second. Loaded lazily in
server.build_app() (never at import), so `import spotify_mcp` stays side-effect-free and needs
no secrets.

`secret(name)` reads `$CREDENTIALS_DIRECTORY/<name>` when the unit was started with
`LoadCredentialEncrypted=<name>` (the system unit spotify-mcp.service) and falls back to the
environment, which is what config.env + scripts/secrets.sh export today. Credential IDs are global
file names, so they are prefixed (`SPOTIFY_…`); the environment fallback also accepts today's bare
`FASTMCP_JWT_KEY`. Nothing in this module ever puts a value on stdout, stderr or into an exception
message: names only."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

# Credential ID -> environment names that may carry the same value, in lookup order (closed set).
CREDENTIALS: dict[str, tuple[str, ...]] = {
    "SPOTIFY_CLIENT_ID": ("SPOTIFY_CLIENT_ID",),
    "SPOTIFY_CLIENT_SECRET": ("SPOTIFY_CLIENT_SECRET",),
    "SPOTIFY_FASTMCP_JWT_KEY": ("SPOTIFY_FASTMCP_JWT_KEY", "FASTMCP_JWT_KEY"),
}


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


def check_name(name: object) -> str:
    """The credential name, or `SettingsError` — anchored pattern, never a path."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise SettingsError(f"invalid credential name {str(name)[:80]!r}")
    return name


def credentials_dir(env: Mapping[str, str] | None = None) -> Path | None:
    """`$CREDENTIALS_DIRECTORY` when systemd set it (an absolute path), else None."""
    e = os.environ if env is None else env
    raw = (e.get("CREDENTIALS_DIRECTORY") or "").strip()
    if not raw or not os.path.isabs(raw):
        return None
    return Path(raw)


def secret(name: str, env: Mapping[str, str] | None = None) -> str:
    """The value of credential `name`: the credential file (stripped) when the directory is set
    and the file exists, else the first non-empty environment name mapped to it in CREDENTIALS
    (or the name itself when it is not a listed credential); "" when nothing holds a value."""
    name = check_name(name)
    e = os.environ if env is None else env
    d = credentials_dir(e)
    if d is not None:
        try:
            with open(d / name, encoding="utf-8") as fh:
                val = fh.read().strip()
            if val:
                return val
        except (FileNotFoundError, NotADirectoryError):
            pass  # not loaded for this unit — the environment may still carry it
    for env_name in CREDENTIALS.get(name, (name,)):
        val = (e.get(env_name) or "").strip()
        if val:
            return val
    return ""


def _require(name: str, env: Mapping[str, str] | None) -> str:
    val = secret(name, env)
    if not val:
        env_names = " / ".join(CREDENTIALS.get(name, (name,)))
        raise SettingsError(
            f"Missing credential {name!r}: neither $CREDENTIALS_DIRECTORY/{name} nor the "
            f"environment ({env_names}) holds a value. Source config.env and scripts/secrets.sh "
            "before starting (see README), or load it in the unit with LoadCredentialEncrypted=."
        )
    return val


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    base_url = (e.get("BASE_URL") or "https://mcp.example.com").strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise SettingsError(f"BASE_URL must be an https:// origin, got {base_url!r}")
    allowed = (e.get("SPOTIFY_ALLOWED_USER_IDS") or "").split()
    try:
        port = int((e.get("PORT") or "8080").strip())
    except ValueError as exc:
        raise SettingsError("PORT must be an integer") from exc
    return Settings(
        spotify_client_id=_require("SPOTIFY_CLIENT_ID", e),
        spotify_client_secret=_require("SPOTIFY_CLIENT_SECRET", e),
        base_url=base_url,
        port=port,
        allowed_user_ids=frozenset(a for a in allowed if a),
        jwt_signing_key=_require("SPOTIFY_FASTMCP_JWT_KEY", e),
    )
