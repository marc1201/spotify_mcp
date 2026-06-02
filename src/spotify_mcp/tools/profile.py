"""Profile & personalization tools."""

from __future__ import annotations

from fastmcp import FastMCP

from ..client import get_client


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def me() -> dict:
        """The current user's Spotify profile (id, display name, product tier where available)."""
        return await get_client().me()

    @mcp.tool
    async def top_items(item_type: str, time_range: str = "medium_term", limit: int = 20) -> list:
        """The user's top artists or tracks. item_type: 'artists' or 'tracks'. time_range:
        short_term (~4 weeks), medium_term (~6 months), or long_term (years)."""
        return await get_client().top_items(item_type, time_range=time_range, limit=limit)

    @mcp.tool
    async def get_user_profile(user_id: str) -> dict:
        """Get another user's public profile (FULL-access apps only)."""
        return await get_client().user_profile(user_id)

    @mcp.tool
    async def browse(
        kind: str, limit: int = 20, offset: int = 0, country: str | None = None
    ) -> list:
        """Browse editorial content (FULL-access apps only). kind: 'new_releases' or 'categories'."""
        return await get_client().browse(kind, limit=limit, offset=offset, country=country)
