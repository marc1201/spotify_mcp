"""Spotify dev-mode API regime.

A Feb-Mar 2026 Spotify change split dev-mode apps into two endpoint regimes:

- FULL        — the long-standing endpoint surface (per-type library/follow endpoints,
                /playlists/{id}/tracks, batch "get several", browse, artist top-tracks, …).
- RESTRICTED  — new/migrated dev-mode apps: library + follow consolidated into /me/library,
                playlist items at /playlists/{id}/items, no batch/browse/top-tracks/users.

Player endpoints (/me/player/*) are identical in both. This module is the ONLY home of the
regime *vocabulary*; the routing logic that consumes it lives in client.py. Tools never see it.
"""

from __future__ import annotations

from enum import StrEnum


class Regime(StrEnum):
    FULL = "full"
    RESTRICTED = "restricted"


class RestrictedUnsupported(Exception):
    """A FULL-only operation was invoked while the app is in the RESTRICTED regime."""

    def __init__(self, op: str, alternative: str = ""):
        self.op = op
        self.alternative = alternative
        super().__init__(op)


def playlist_items_segment(regime: Regime) -> str:
    """Path segment for a playlist's items endpoint (differs by regime)."""
    return "tracks" if regime is Regime.FULL else "items"
