"""Search & lookup tools."""

from __future__ import annotations

from fastmcp import FastMCP

from ..client import get_client


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def search(
        q: str,
        types: list[str],
        limit: int = 10,
        market: str | None = None,
        offset: int = 0,
    ) -> dict:
        """Search the Spotify catalog. `types` is a list from: track, album, artist, playlist,
        show, episode, audiobook. `limit` per type is capped at 10 (restricted apps) or 50 (full).
        Returns a dict keyed by plural type (tracks, albums, ...)."""
        return await get_client().search(q, types, limit=limit, market=market, offset=offset)

    @mcp.tool
    async def get_item(item_type: str, id_or_uri: str, market: str | None = None) -> dict | None:
        """Get one item by id/URI. item_type: track, album, artist, playlist, show, episode,
        audiobook, chapter."""
        return await get_client().get_item(item_type, id_or_uri, market=market)

    @mcp.tool
    async def get_items_batch(item_type: str, ids: list[str]) -> list:
        """Get several items at once (FULL-access apps only). item_type: track, album, artist,
        episode, show, audiobook, chapter. On restricted apps, use get_item per id instead."""
        return await get_client().get_items_batch(item_type, ids)

    @mcp.tool
    async def album_tracks(album_id: str, limit: int = 50, offset: int = 0) -> list:
        """List the tracks on an album."""
        return await get_client().album_tracks(album_id, limit=limit, offset=offset)

    @mcp.tool
    async def artist_albums(
        artist_id: str, include_groups: str | None = None, limit: int = 50, offset: int = 0
    ) -> list:
        """List an artist's albums. include_groups is a comma list from album,single,appears_on,
        compilation."""
        return await get_client().artist_albums(
            artist_id, include_groups=include_groups, limit=limit, offset=offset
        )

    @mcp.tool
    async def artist_top_tracks(artist_id: str, market: str = "from_token") -> list:
        """An artist's top tracks (FULL-access apps only)."""
        return await get_client().artist_top_tracks(artist_id, market=market)
