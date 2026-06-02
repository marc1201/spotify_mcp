"""FastMCP server: OAuthProxy(Spotify) + tool registration + health route + uvicorn entrypoint.

The one load-bearing config value is `base_url`/`issuer_url` — it MUST be the exact public origin
claude.ai talks to (https://mcp.example.com), or OAuth discovery/DCR/PKCE breaks.
"""

from __future__ import annotations

import logging

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import OAuthProxy
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import SPOTIFY_SCOPES, SpotifyTokenVerifier
from .settings import Settings, load_settings
from .tools import register_all

log = logging.getLogger("spotify_mcp")

# Downstream redirect URIs claude.ai/desktop/mobile use during the connector OAuth flow.
CLAUDE_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
]

INSTRUCTIONS = (
    "Control a Spotify account: play/pause/skip/seek/volume, queue, and transfer playback between "
    "devices; search the catalog; read and edit playlists; manage the saved library and follows; "
    "see top artists/tracks and recently played. Playback control requires Spotify Premium and an "
    "active device — use list_devices and transfer_playback if you get a 'no active device' error. "
    "IDs may be passed as bare ids, spotify:type:id URIs, or open.spotify.com URLs."
)


def build_app(settings: Settings | None = None):
    """Construct the Starlette ASGI app (OAuth + MCP). Returns (app, settings)."""
    s = settings or load_settings()

    if not s.allowed_user_ids:
        log.warning(
            "SPOTIFY_ALLOWED_USER_IDS is empty — any Spotify user authorized on the dev app can use "
            "this connector. Set it to your Spotify user id in config.env to lock it down."
        )

    verifier = SpotifyTokenVerifier(allowlist=s.allowed_user_ids, scopes=SPOTIFY_SCOPES)
    auth = OAuthProxy(
        upstream_authorization_endpoint="https://accounts.spotify.com/authorize",
        upstream_token_endpoint="https://accounts.spotify.com/api/token",
        upstream_client_id=s.spotify_client_id,
        upstream_client_secret=s.spotify_client_secret,
        token_verifier=verifier,
        base_url=s.base_url,
        issuer_url=s.base_url,
        redirect_path="/auth/callback",
        valid_scopes=SPOTIFY_SCOPES,
        forward_pkce=True,
        jwt_signing_key=s.jwt_signing_key,
        allowed_client_redirect_uris=CLAUDE_REDIRECT_URIS,
    )

    mcp = FastMCP("Spotify", instructions=INSTRUCTIONS, auth=auth)
    register_all(mcp)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "spotify-mcp"})

    return mcp.http_app(), s


def main() -> None:
    app, s = build_app()
    uvicorn.run(app, host="127.0.0.1", port=s.port, log_level="info", timeout_keep_alive=120)
