"""Rotation configs. The stored row for a rotation is created from these at seed time and then
edited through the rotation_config tool; BASE holds the structural defaults every rotation gets
unless its row overrides them. Percent rules are turned into counts from track_count at run time
(caps round down, minimums round up: 20% of 24 -> cap 4, minimum 5)."""

from __future__ import annotations

from typing import Any

BASE: dict[str, Any] = {
    "track_count": 24,
    "movements": 4,  # 1 = no arc: per_movement is the whole list and track movements are ignored
    "per_movement": 6,
    "period_days": 1,  # 1 = daily, 7 = weekly; every gap rule counts in periods
    "cap_3_4_pct": 20,  # R2: at most this share with gap 3-4
    "cap_3_5_pct": 50,  # R3: at most this share with gap 3-5
    "min_fresh_pct": 20,  # R4: at least this share rested (gap 8+ or never played)
    "new_song_daily_until": "2026-12-31",  # R5: one new song every day up to and including this date
    "new_song_then_every_days": 7,  # R5 afterwards: one new song in every rolling window of this many days
    "artist_cap": 3,  # tracks per artist per daily playlist
    "max_pool_tracks_per_artist": 5,  # the new-song picker skips artists at or above this
    "lookahead_margin": 1,  # rested tracks kept in reserve for tomorrow beyond min_fresh
    "pool_floor": 144,  # 6 x track_count; below this new_song_due fires every day
    "mode": "dated",  # a new dated playlist each day ("rolling" is reserved for v2)
    "retention_days": 7,  # keep the newest N daily playlists on Spotify
    "timezone": "Europe/Lisbon",
    "market": "PT",
    "library_index_ttl_hours": 12,
    # Position rules: [{"position": 1 | "first" | "last", "artist": "Sade"}, ...]. That slot of
    # every daily playlist holds a track by that artist; unmet rules are logged as relaxations.
    "position_rules": [],
}

CANDLELIGHT_TASTE_BRIEF = """\
Closer by Candlelight: slow-burn intimate music, four movements (first spark, drawing in, deep heat, afterglow).
Lanes: Sade-style soul; quiet storm and classic soul (Marvin Gaye, Teddy Pendergrass, Anita Baker, Barry White,
Roberta Flack); neo-soul (D'Angelo, Erykah Badu, Jill Scott, Lauryn Hill); slow R&B with warm low voices
(Leon Bridges, Giveon, Jordan Rakei); trip-hop and dream-pop (Portishead, Massive Attack, Morcheeba, Hooverphonic,
Cigarettes After Sex, Beach House, Mazzy Star); jazz vocal (Norah Jones, Melody Gardot); late-night songwriters
(Nick Cave, Leonard Cohen, Cat Power, Tracy Chapman); occasional Brazilian (Ceu, Marisa Monte).
Hard vocal filter: no thin, airy, breathy-whispery, pitched-up, strained or wailing falsetto voices.
Rejected before: FKA twigs, Sabrina Claudio, dvsn, Frank Ocean "Self Control", Maxwell "This Woman's Work",
Miguel "Adorn", Massive Attack "Angel". Avoid Bon Iver, Sufjan Stevens, Cocteau Twins, Minnie Riperton,
Smokey Robinson, Curtis Mayfield, Chris Isaak.
Nothing fast, no hip-hop. Fan favourites over overplayed radio staples. Original album versions."""

ROTATIONS: dict[str, dict[str, Any]] = {
    "candlelight": {
        **BASE,
        "name_template": "1 Intimate - Closer by Candlelight - Daily {date}",
        "taste_brief": CANDLELIGHT_TASTE_BRIEF,
    },
}


def config_for(rotation_id: str) -> dict[str, Any]:
    """The starting config of a rotation: its named defaults, or BASE with a plain name template
    and an empty taste brief for a rotation that only exists in the database."""
    if rotation_id in ROTATIONS:
        return ROTATIONS[rotation_id]
    return {**BASE, "name_template": f"{rotation_id} {{date}}", "taste_brief": ""}


# Fields rotation_config may change from chat. Everything else is structural and lives here.
EDITABLE: tuple[str, ...] = (
    "taste_brief",
    "name_template",
    "artist_cap",
    "max_pool_tracks_per_artist",
    "retention_days",
    "pool_floor",
    "lookahead_margin",
    "new_song_daily_until",
    "new_song_then_every_days",
    "position_rules",
    "track_count",
    "movements",
    "per_movement",
    "period_days",
)
