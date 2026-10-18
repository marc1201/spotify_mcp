"""Tool registration. Each module exposes register(mcp); register_all wires them all."""

from __future__ import annotations

from fastmcp import FastMCP

from . import follow, library, playback, playlists, profile, rotation, search


def register_all(mcp: FastMCP) -> None:
    for module in (playback, search, playlists, library, follow, profile, rotation):
        module.register(mcp)
