"""Daily rotation tools (Closer by Candlelight Daily): thin wrappers over RotationService, listed
in the order the scheduled morning run uses them. `rotation` defaults to 'candlelight'; dates are
YYYY-MM-DD in the rotation's time zone and default to today."""

from __future__ import annotations

from fastmcp import FastMCP

from ..rotation.service import get_service


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def rotation_status(rotation: str = "candlelight", date: str | None = None) -> dict:
        """Where the rotation stands: active/inactive pool size, last run date, run_exists_today,
        whether a new song is due (new_song_due, new_song_reason, days_since_last_new), play-count
        min/max/spread, tracks per artist and artists_at_cap (skip those when picking a new song),
        the last run's relaxations, and the taste_brief to pick new songs by."""
        return await get_service().status(rotation, date)

    @mcp.tool
    async def library_check(
        uris: list[str], rotation: str = "candlelight", refresh: bool = False
    ) -> dict:
        """For each track: the owned playlists that already contain it (matched by id, ISRC, or
        normalized title + primary artist) and whether it is in the rotation pool. Uses an index of
        your playlists refreshed every 12 hours; refresh=true rebuilds it now."""
        return await get_service().library_check(uris, rotation=rotation, refresh=refresh)

    @mcp.tool
    async def rotation_pool_add(
        uri: str,
        movement: int,
        note: str,
        rotation: str = "candlelight",
        debut: bool = True,
        allow_owned: bool = False,
        actor: str = "chat",
    ) -> dict:
        """Add a new song to the pool. movement 1-4 = first spark, drawing in, deep heat, afterglow;
        note = one line on why it fits. Refuses a track already in the pool or on any owned playlist
        (unless allow_owned) and names where it was found. debut=true puts it in today's playlist.
        It starts at the pool's lowest play count. actor: who decided (e.g. scheduled-task)."""
        return await get_service().pool_add(
            rotation, uri, movement, note, debut=debut, allow_owned=allow_owned, actor=actor
        )

    @mcp.tool
    async def rotation_generate(
        rotation: str = "candlelight",
        date: str | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> dict:
        """Build the day's playlist: pick the tracks by the rotation rules, create the dated
        playlist, record the run, and unfollow daily playlists beyond retention. Returns playlist
        id/url, `public` as Spotify reports it (Spotify ignores the private flag for API-created
        playlists, so it is public unless made private in the app), the tracks in order with
        movement, bucket and gap, the rule report and any relaxations. A second call for the same date returns the existing run unless force=true,
        which replaces the playlist and that date's records. dry_run=true only picks."""
        return await get_service().generate(rotation, date, dry_run=dry_run, force=force)

    @mcp.tool
    async def rotation_pool_set_active(
        uri: str, active: bool, rotation: str = "candlelight", reason: str = "", actor: str = "chat"
    ) -> dict:
        """Veto a pool track (active=false) or restore it (active=true). Its history is kept."""
        return await get_service().pool_set_active(
            rotation, uri, active, reason=reason, actor=actor
        )

    @mcp.tool
    async def rotation_pool_list(
        rotation: str = "candlelight", active_only: bool = True, sort: str = "count"
    ) -> list:
        """The pool with play_count and last_played per track. sort: count (least played first),
        last_played, or movement."""
        return await get_service().pool_list(rotation, active_only=active_only, sort=sort)

    @mcp.tool
    async def rotation_history(rotation: str = "candlelight", days: int = 14) -> list:
        """Daily runs of the last `days` days, newest first: playlist id/name, relaxations, and each
        track with its movement, bucket and gap at pick."""
        return await get_service().history(rotation, days=days)

    @mcp.tool
    async def rotation_seed(playlist_ids: list[str], rotation: str = "candlelight") -> dict:
        """One-time setup: import the tracks of the given playlists (in volume order, Vol I first)
        with movement from position (1-6, 7-12, 13-18, 19-24). A rotation that does not exist
        yet is created from the defaults (name template "<rotation> {date}", empty taste brief);
        tune it with rotation_config afterwards. Re-running skips tracks already in the pool."""
        return await get_service().seed(rotation, playlist_ids)

    @mcp.tool
    async def rotation_config(
        rotation: str = "candlelight",
        taste_brief: str | None = None,
        name_template: str | None = None,
        artist_cap: int | None = None,
        max_pool_tracks_per_artist: int | None = None,
        retention_days: int | None = None,
        pool_floor: int | None = None,
        lookahead_margin: int | None = None,
        new_song_daily_until: str | None = None,
        new_song_then_every_days: int | None = None,
        position_rules: list[dict] | None = None,
    ) -> dict:
        """Read the rotation's config, or change the given fields (the rest stay). name_template
        takes date tokens: {date} (2026-10-18), {dd} {mm} {yy} {yyyy}, or {date:<strftime>}; e.g.
        "1 Intimate - Candlelight {dd}.{mm}.". position_rules pins slots to an artist already in
        the pool, e.g. [{"position": "first", "artist": "Sade", "movements": [1, 2]},
        {"position": "last", "artist": "Sade", "movements": [3, 4]}]; position is 1-based or
        'first'/'last', movements (optional) are where the track may come from; pass [] to
        clear. new_song_daily_until is YYYY-MM-DD. Changes apply from the next run."""
        return await get_service().config(
            rotation,
            taste_brief=taste_brief,
            name_template=name_template,
            artist_cap=artist_cap,
            max_pool_tracks_per_artist=max_pool_tracks_per_artist,
            retention_days=retention_days,
            pool_floor=pool_floor,
            lookahead_margin=lookahead_margin,
            new_song_daily_until=new_song_daily_until,
            new_song_then_every_days=new_song_then_every_days,
            position_rules=position_rules,
        )
