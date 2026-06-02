"""Player tools — identical in both API regimes. Modify actions need Premium + an active device."""

from __future__ import annotations

from fastmcp import FastMCP

from ..client import get_client


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def playback_state() -> dict:
        """Current playback state: active device, current track, progress, shuffle/repeat. Returns
        {"active": false} if nothing is playing on any device."""
        return await get_client().playback_state()

    @mcp.tool
    async def currently_playing() -> dict:
        """The item currently playing (lighter than playback_state)."""
        return await get_client().currently_playing()

    @mcp.tool
    async def list_devices() -> list:
        """List the user's available Spotify Connect devices and which is active. Use a device's
        `id` for the device_id parameter of other player tools."""
        return await get_client().devices()

    @mcp.tool
    async def transfer_playback(device_id: str, play: bool = True) -> dict:
        """Transfer playback to a device (from list_devices). play=True resumes on it."""
        return await get_client().transfer(device_id, play)

    @mcp.tool
    async def play(
        uris: list[str] | None = None,
        context_uri: str | None = None,
        offset_position: int | None = None,
        position_ms: int | None = None,
        device_id: str | None = None,
    ) -> dict:
        """Start or resume playback. Pass `uris` (a list of track URIs) OR a `context_uri`
        (album/playlist/artist URI). `offset_position` picks an index within the context;
        `position_ms` seeks within the first track. With no args, resumes current playback."""
        return await get_client().play(
            device_id=device_id,
            uris=uris,
            context_uri=context_uri,
            offset_position=offset_position,
            position_ms=position_ms,
        )

    @mcp.tool
    async def pause(device_id: str | None = None) -> dict:
        """Pause playback."""
        return await get_client().pause(device_id)

    @mcp.tool
    async def next_track(device_id: str | None = None) -> dict:
        """Skip to the next track."""
        return await get_client().next(device_id)

    @mcp.tool
    async def previous_track(device_id: str | None = None) -> dict:
        """Skip to the previous track."""
        return await get_client().previous(device_id)

    @mcp.tool
    async def seek(position_ms: int, device_id: str | None = None) -> dict:
        """Seek to a position (milliseconds) in the current track."""
        return await get_client().seek(position_ms, device_id)

    @mcp.tool
    async def set_volume(volume_percent: int, device_id: str | None = None) -> dict:
        """Set playback volume (0-100)."""
        return await get_client().set_volume(volume_percent, device_id)

    @mcp.tool
    async def set_repeat(state: str, device_id: str | None = None) -> dict:
        """Set repeat mode: 'track', 'context', or 'off'."""
        return await get_client().set_repeat(state, device_id)

    @mcp.tool
    async def set_shuffle(state: bool, device_id: str | None = None) -> dict:
        """Turn shuffle on (True) or off (False)."""
        return await get_client().set_shuffle(state, device_id)

    @mcp.tool
    async def get_queue() -> dict:
        """The user's playback queue and the currently playing item."""
        return await get_client().queue()

    @mcp.tool
    async def add_to_queue(uri: str, device_id: str | None = None) -> dict:
        """Add a track or episode URI to the end of the playback queue."""
        return await get_client().add_to_queue(uri, device_id)

    @mcp.tool
    async def recently_played(limit: int = 20) -> list:
        """The most recently played tracks (up to 50)."""
        return await get_client().recently_played(limit=limit)
