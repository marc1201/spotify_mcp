"""Saved-library tools. FULL apps use per-type endpoints; RESTRICTED apps use /me/library —
the client routes by regime, so these tools just take an item_type."""

from __future__ import annotations

from fastmcp import FastMCP

from ..client import get_client


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def get_saved(item_type: str, limit: int = 50, offset: int = 0) -> list:
        """List items saved in the user's library. item_type: track, album, episode, show, or
        audiobook."""
        return await get_client().get_saved(item_type, limit=limit, offset=offset)

    @mcp.tool
    async def save_to_library(item_type: str, ids_or_uris: list[str]) -> dict:
        """Save items to the library. item_type: track, album, episode, show, or audiobook.
        Accepts bare ids or spotify: URIs."""
        return await get_client().save_to_library(item_type, ids_or_uris)

    @mcp.tool
    async def remove_from_library(item_type: str, ids_or_uris: list[str]) -> dict:
        """Remove items from the library. item_type: track, album, episode, show, or audiobook."""
        return await get_client().remove_from_library(item_type, ids_or_uris)

    @mcp.tool
    async def check_saved(item_type: str, ids_or_uris: list[str]) -> dict:
        """Check whether items are saved in the library. item_type: track, album, episode, show,
        or audiobook. Returns a URI -> bool map."""
        return await get_client().check_saved(item_type, ids_or_uris)
