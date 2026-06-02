"""Spotify HTTP error classification. Pure stdlib (no FastMCP import) so it stays a leaf module.

client.py raises these for internal control flow (e.g. catching SpotifyNotFound) and converts
uncaught ones into FastMCP ToolError with the actionable `.user_message` at its public boundary.
"""

from __future__ import annotations

import httpx

# Spotify 403 "reason" codes worth a tailored, actionable message.
_REASON_HELP = {
    "NO_ACTIVE_DEVICE": (
        "No active Spotify device. Open Spotify on a phone/desktop/web player (or pass device_id "
        "from list_devices) and retry."
    ),
    "PREMIUM_REQUIRED": "Spotify Premium is required for playback control.",
    "ALREADY_PAUSED": "Playback is already paused.",
    "NOT_PAUSED": "Playback is not paused.",
    "UNKNOWN": "Spotify could not complete the player command.",
}


class SpotifyError(Exception):
    """Base for all Spotify API errors."""

    def __init__(self, message: str, *, status: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.reason = reason

    @property
    def user_message(self) -> str:
        return f"Spotify error{f' {self.status}' if self.status else ''}: {self.message}"


class SpotifyAuthError(SpotifyError):
    @property
    def user_message(self) -> str:
        return "Spotify rejected the access token (401). Reconnect the Spotify connector in Claude."


class SpotifyForbidden(SpotifyError):
    @property
    def user_message(self) -> str:
        if self.reason and self.reason in _REASON_HELP:
            return _REASON_HELP[self.reason]
        return f"Spotify refused the request (403): {self.message}"


class SpotifyNotFound(SpotifyError):
    @property
    def user_message(self) -> str:
        return f"Not found (404): {self.message}"


class SpotifyBadRequest(SpotifyError):
    @property
    def user_message(self) -> str:
        return f"Bad request (400): {self.message}"


class SpotifyRateLimited(SpotifyError):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited; retry after {retry_after:g}s", status=429)
        self.retry_after = retry_after

    @property
    def user_message(self) -> str:
        return f"Spotify rate limit hit. Wait ~{self.retry_after:g}s and retry."


def normalize_http_error(resp: httpx.Response) -> SpotifyError:
    """Map a non-2xx Spotify response to the right SpotifyError subclass."""
    message = resp.text[:300]
    reason: str | None = None
    try:
        err = resp.json().get("error")
        if isinstance(err, dict):
            message = err.get("message") or message
            reason = err.get("reason")
        elif isinstance(err, str):
            # token-endpoint style: {"error": "...", "error_description": "..."}
            message = resp.json().get("error_description") or err
    except Exception:
        pass

    code = resp.status_code
    if code == 401:
        return SpotifyAuthError(message, status=401, reason=reason)
    if code == 403:
        return SpotifyForbidden(message, status=403, reason=reason)
    if code == 404:
        return SpotifyNotFound(message, status=404, reason=reason)
    if code == 400:
        return SpotifyBadRequest(message, status=400, reason=reason)
    return SpotifyError(message, status=code, reason=reason)
