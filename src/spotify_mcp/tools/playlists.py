"""Playlist tools. Item paths differ by regime (/tracks vs /items) — handled in the client."""

from __future__ import annotations

from fastmcp import FastMCP

from ..client import get_client


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def my_playlists(limit: int = 50, offset: int = 0) -> list:
        """List the current user's playlists."""
        return await get_client().my_playlists(limit=limit, offset=offset)

    @mcp.tool
    async def get_playlist(playlist_id: str, market: str | None = None) -> dict | None:
        """Get a playlist's metadata (name, owner, track count, description)."""
        return await get_client().get_playlist(playlist_id, market=market)

    @mcp.tool
    async def playlist_items(
        playlist_id: str, limit: int = 50, offset: int = 0, market: str | None = None
    ) -> list:
        """List the tracks/episodes in a playlist."""
        return await get_client().playlist_items(
            playlist_id, limit=limit, offset=offset, market=market
        )

    @mcp.tool
    async def create_playlist(
        name: str,
        public: bool = False,
        collaborative: bool = False,
        description: str | None = None,
    ) -> dict | None:
        """Create a new playlist owned by the current user."""
        return await get_client().create_playlist(
            name, public=public, collaborative=collaborative, description=description
        )

    @mcp.tool
    async def add_playlist_items(
        playlist_id: str, uris: list[str], position: int | None = None
    ) -> dict:
        """Add tracks/episodes (by URI) to a playlist. position inserts at an index (default: end)."""
        return await get_client().add_playlist_items(playlist_id, uris, position=position)

    @mcp.tool
    async def remove_playlist_items(playlist_id: str, uris: list[str]) -> dict:
        """Remove all occurrences of the given track/episode URIs from a playlist."""
        return await get_client().remove_playlist_items(playlist_id, uris)

    @mcp.tool
    async def reorder_playlist_items(
        playlist_id: str, range_start: int, insert_before: int, range_length: int = 1
    ) -> dict:
        """Move a block of items within a playlist. Moves range_length items starting at
        range_start to before index insert_before."""
        return await get_client().reorder_playlist_items(
            playlist_id, range_start, insert_before, range_length=range_length
        )

    @mcp.tool
    async def set_playlist_details(
        playlist_id: str,
        name: str | None = None,
        public: bool | None = None,
        collaborative: bool | None = None,
        description: str | None = None,
    ) -> dict:
        """Change a playlist's name, visibility, collaborative flag, or description."""
        return await get_client().set_playlist_details(
            playlist_id,
            name=name,
            public=public,
            collaborative=collaborative,
            description=description,
        )

    @mcp.tool
    async def upload_playlist_cover(playlist_id: str, image_base64: str) -> dict:
        """Replace a playlist's cover image. image_base64 is a base64-encoded JPEG (no data: prefix)."""
        return await get_client().upload_playlist_cover(playlist_id, image_base64)
