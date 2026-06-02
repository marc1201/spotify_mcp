"""Follow tools. FULL apps use /me/following + /playlists/{id}/followers; RESTRICTED apps use
/me/library — routed in the client."""

from __future__ import annotations

from fastmcp import FastMCP

from ..client import get_client


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def followed_artists(limit: int = 50, after: str | None = None) -> list:
        """List artists the user follows. `after` is the last artist id from a previous page."""
        return await get_client().followed_artists(limit=limit, after=after)

    @mcp.tool
    async def follow(item_type: str, ids_or_uris: list[str]) -> dict:
        """Follow artists or users. item_type: 'artist' or 'user'."""
        return await get_client().follow(item_type, ids_or_uris)

    @mcp.tool
    async def unfollow(item_type: str, ids_or_uris: list[str]) -> dict:
        """Unfollow artists or users. item_type: 'artist' or 'user'."""
        return await get_client().unfollow(item_type, ids_or_uris)

    @mcp.tool
    async def check_following(item_type: str, ids_or_uris: list[str]) -> dict:
        """Check whether the user follows the given artists/users. Returns a URI -> bool map."""
        return await get_client().check_following(item_type, ids_or_uris)

    @mcp.tool
    async def follow_playlist(playlist_id: str, public: bool = True) -> dict:
        """Follow (subscribe to) a playlist."""
        return await get_client().follow_playlist(playlist_id, public=public)

    @mcp.tool
    async def unfollow_playlist(playlist_id: str) -> dict:
        """Unfollow a playlist."""
        return await get_client().unfollow_playlist(playlist_id)
