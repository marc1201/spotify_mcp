"""FastMCP server: OAuthProxy(Spotify) + tool registration + health route + uvicorn entrypoint.

The one load-bearing config value is `base_url`/`issuer_url` — it MUST be the exact public origin
claude.ai talks to (https://mcp.example.com), or OAuth discovery/DCR/PKCE breaks.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import OAuthProxy
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
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

    # The version travels in the MCP initialize handshake; bump it with every tool-surface change
    # so clients that cache a connector's tool list see that it changed.
    mcp = FastMCP("Spotify", instructions=INSTRUCTIONS, auth=auth, version=__version__)
    register_all(mcp)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "spotify-mcp"})

    return mcp.http_app(), s


class BindError(RuntimeError):
    """The process environment names a listening socket this process cannot use."""


def bind_kwargs(
    env: Mapping[str, str], *, port: int | None = None, pid: int | None = None
) -> dict[str, object]:
    """Where uvicorn listens, from the environment (pure; same shape as the 1201-assistant
    connector, docs/ACCESS-BOUNDARIES-STEP1.md "Step 1b"):

    - ``LISTEN_FDS`` set → systemd socket activation (spotify-mcp.socket): ``{"fd": 3}``.
      ``LISTEN_PID`` must equal this process's pid (``pid``, default ``os.getpid()``) and exactly
      one fd must be passed, else ``BindError`` — never fall back to a port when a socket was meant.
    - ``SPOTIFY_MCP_SOCKET`` set → ``{"uds": <absolute path>}``.
    - else ``{"host": "127.0.0.1", "port": <port>}`` (``port``, default ``PORT`` or 8080).
    """
    listen_fds = (env.get("LISTEN_FDS") or "").strip()
    if listen_fds:
        own = str(os.getpid() if pid is None else pid)
        if (env.get("LISTEN_PID") or "").strip() != own:
            raise BindError("LISTEN_FDS is set but LISTEN_PID is not this process")
        if listen_fds != "1":
            raise BindError(f"LISTEN_FDS={listen_fds!r}: exactly one listening socket is expected")
        return {"fd": 3}
    uds = (env.get("SPOTIFY_MCP_SOCKET") or "").strip()
    if uds:
        if not os.path.isabs(uds):
            raise BindError("SPOTIFY_MCP_SOCKET must be an absolute path")
        return {"uds": uds}
    if port is None:
        try:
            port = int((env.get("PORT") or "8080").strip())
        except ValueError as e:
            raise BindError("PORT must be an integer") from e
    return {"host": "127.0.0.1", "port": port}


def describe_bind(kw: Mapping[str, object]) -> str:
    if "fd" in kw:
        return f"socket-activation fd {kw['fd']}"
    if "uds" in kw:
        return f"unix:{kw['uds']}"
    return f"{kw.get('host')}:{kw.get('port')}"


def serve(app, s: Settings, env: Mapping[str, str] | None = None) -> None:
    """Run uvicorn on whatever bind_kwargs picks. /healthz answers on all three binds."""
    kw = bind_kwargs(os.environ if env is None else env, port=s.port)
    log.info("spotify-mcp listening on %s (public: %s)", describe_bind(kw), s.base_url)
    uvicorn.run(app, log_level="info", timeout_keep_alive=120, **kw)


def main() -> None:
    app, s = build_app()
    serve(app, s)
