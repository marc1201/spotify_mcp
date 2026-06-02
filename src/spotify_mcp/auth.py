"""Token verification for the OAuthProxy.

Spotify access tokens are opaque (no JWT, no introspection endpoint), so we verify a token by
calling GET /v1/me. FastMCP's OAuthProxy has already validated its own issued JWT and decrypted
the stored upstream Spotify token before handing it here, so this is the "is this Spotify token
currently good, and is the user allowed" gate. Results are cached briefly (keyed by a hash of the
token, never the raw token) to avoid a /me round-trip on every MCP request — a refreshed token is a
new string => natural cache miss => re-verified.
"""

from __future__ import annotations

import hashlib
import logging
import time

import httpx
from fastmcp.server.auth.auth import AccessToken, TokenVerifier

log = logging.getLogger("spotify_mcp")

ME_URL = "https://api.spotify.com/v1/me"

# 16 scopes covering playback, playlists, library, follow, top items, recently-played, profile,
# playback position, and cover-image upload. Identity uses the Spotify user id, so user-read-email
# is intentionally NOT requested (keeps the consent surface minimal).
SPOTIFY_SCOPES: list[str] = [
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-read-recently-played",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
    "user-library-read",
    "user-library-modify",
    "user-follow-read",
    "user-follow-modify",
    "user-top-read",
    "user-read-private",
    "ugc-image-upload",
    "user-read-playback-position",
]


class SpotifyTokenVerifier(TokenVerifier):
    def __init__(
        self,
        allowlist: frozenset[str] | set[str] | None = None,
        scopes: list[str] | None = None,
        ttl_seconds: int = 120,
    ):
        super().__init__()
        self._allow = set(allowlist or ())
        self._scopes = scopes or SPOTIFY_SCOPES
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, AccessToken | None]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        now = time.monotonic()
        key = hashlib.sha256(token.encode()).hexdigest()  # index by hash, not the raw token
        cached = self._cache.get(key)
        if cached and cached[0] > now:
            return cached[1]

        result: AccessToken | None = None
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.get(ME_URL, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 200:
                me = resp.json()
                uid = me.get("id")
                if uid and (not self._allow or uid in self._allow):
                    result = AccessToken(
                        token=token,
                        client_id=uid,
                        scopes=self._scopes,
                        subject=uid,
                        expires_at=None,
                        claims={
                            "sub": uid,
                            "display_name": me.get("display_name"),
                            "product": me.get("product"),
                        },
                    )
                elif uid:
                    log.warning("denied Spotify user %r — not in SPOTIFY_ALLOWED_USER_IDS", uid)
        except Exception:
            result = None

        # Negative results cached briefly so a revoked/disallowed token fails fast but can recover.
        self._cache[key] = (now + (self._ttl if result else min(self._ttl, 15)), result)
        if len(self._cache) > 256:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        return result
