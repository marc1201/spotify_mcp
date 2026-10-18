"""The rotation operations behind tools/rotation.py: one class, one method per tool.

Every Spotify call goes through SpotifyClient (so the regime stays invisible here), every read or
write of rotation state goes through Store, and the picking is engine.select(). "Today" is the
rotation's local date (Europe/Lisbon). Public methods sit behind the client's _api boundary (plus
sqlite errors) so nothing but a clean ToolError leaves a rotation tool, and the writing ones hold
a per-rotation lock so a scheduled run and a manual one cannot interleave."""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import logging
import random
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo

from fastmcp.exceptions import ToolError

from ..client import SpotifyClient, _api, get_client, parse_id
from . import defaults, engine
from .store import Store, get_store

log = logging.getLogger("spotify_mcp")

Json = dict[str, Any]

ROMAN = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X")
PAGE = 50
DESCRIPTION_MAX = 300  # Spotify's playlist description limit
NEW_SRC_POSITION = 99  # new songs sort after the seeded ones inside their movement
BUCKET_LABEL = {"C6": "C", "C7": "C"}  # stored vocabulary: A, B, C, F, debut
# Caps on model-supplied values: a runaway or poisoned value must not persist unbounded, and the
# actor name is logged, so it is kept to a plain token.
ACTOR_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
NOTE_MAX = 300
TASTE_BRIEF_MAX = 4000
NAME_TEMPLATE_MAX = 100
LIBRARY_CHECK_MAX = 25
HISTORY_DAYS_MAX = 365
DATE_TOKENS = re.compile(
    r"\{(date|dd|mm|yy|yyyy)(?::([^{}]+))?\}"
)  # {date}, {dd}.{mm}., {date:%d.%m.}
TOKEN_FORMAT = {"dd": "%d", "mm": "%m", "yy": "%y", "yyyy": "%Y"}


def norm_key(title: str, artist: str) -> str:
    """Normalized 'title|primary artist' so remasters, relinked ids and feat. credits still match."""

    def norm(s: str) -> str:
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
        s = re.sub(r"\s*[(\[].*?[)\]]", "", s)  # (feat. X), [Remastered]
        s = re.sub(r"\s+-\s+.*$", "", s)  # " - Remastered 2021", " - uncut"
        return re.sub(r"[^a-z0-9]+", " ", s).strip()

    return f"{norm(title)}|{norm(artist)}"


def engine_config(cfg: Json) -> dict[str, int]:
    """Percent rules -> counts for the engine. Caps round down, minimums round up."""
    n = cfg["track_count"]
    return {
        "size": n,
        "movements": cfg["movements"],
        "per_movement": cfg["per_movement"],
        "cap_3_4": cfg["cap_3_4_pct"] * n // 100,
        "cap_3_5": cfg["cap_3_5_pct"] * n // 100,
        "min_fresh": -(-cfg["min_fresh_pct"] * n // 100),
        "artist_cap": cfg["artist_cap"],
        "lookahead_margin": cfg["lookahead_margin"],
    }


def _boundary(fn):
    """The client's _api boundary plus sqlite errors: only ToolError leaves a rotation tool."""
    inner = _api(fn)

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await inner(*args, **kwargs)
        except sqlite3.Error as exc:
            raise ToolError(f"rotation store error: {exc}") from None

    return wrapper


class RotationService:
    def __init__(
        self,
        store: Store,
        client: SpotifyClient,
        now: Callable[[dt.tzinfo], dt.datetime] | None = None,
    ):
        self._store = store
        self._client = client
        self._now = now or dt.datetime.now  # injectable clock, for tests
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- helpers ------------------------------------------------------------

    def _lock(self, rotation: str) -> asyncio.Lock:
        return self._locks.setdefault(rotation, asyncio.Lock())

    def _cfg(self, rotation: str) -> Json:
        stored = self._store.get_config(rotation)
        if stored is None:
            raise ToolError(f"rotation {rotation!r} is not set up yet: run rotation_seed first.")
        return {**defaults.BASE, **stored}

    def _today(self, cfg: Json) -> dt.date:
        return self._now(ZoneInfo(cfg["timezone"])).date()

    def _date(self, cfg: Json, date: str | None) -> dt.date:
        if not date:
            return self._today(cfg)
        try:
            return dt.date.fromisoformat(date)
        except ValueError:
            raise ToolError(f"date must be YYYY-MM-DD, got {date!r}") from None

    @staticmethod
    def _actor(actor: str) -> str:
        if not ACTOR_RE.match(actor or ""):
            raise ToolError("actor must be 1-32 characters of a-z, 0-9, '-' or '_'")
        return actor

    @staticmethod
    def _text(value: str | None, what: str, limit: int) -> str:
        value = (value or "").strip()
        if len(value) > limit:
            raise ToolError(f"{what} must be at most {limit} characters")
        return value

    @staticmethod
    def _name(cfg: Json, day: dt.date) -> str:
        """Render name_template: {date} is the ISO date, {dd} {mm} {yy} {yyyy} its parts, and
        {date:%d.%m.} any strftime pattern. Plain substitution, never str.format: the template is
        editable from chat."""

        def render(m: re.Match[str]) -> str:
            token, fmt = m.group(1), m.group(2)
            if token == "date":
                return day.strftime(fmt) if fmt else day.isoformat()
            return day.strftime(TOKEN_FORMAT[token])

        return DATE_TOKENS.sub(render, cfg["name_template"])

    def _validate_name_template(self, value: Any) -> str:
        t = self._text(str(value), "name_template", NAME_TEMPLATE_MAX)
        if not DATE_TOKENS.search(t) or not DATE_TOKENS.sub("", t).strip():
            raise ToolError(
                "name_template needs a date token ({date}, {dd}, {mm}, {yyyy} or "
                "{date:<strftime>}) plus a fixed name"
            )
        try:
            rendered = self._name({"name_template": t}, dt.date(2026, 1, 31))
        except ValueError as exc:
            raise ToolError(f"name_template date format is invalid: {exc}") from None
        if rendered == DATE_TOKENS.sub("", t):
            raise ToolError("name_template date format renders nothing")
        return t

    def _validate_position_rules(self, cfg: Json, rotation: str, value: Any) -> list[Json]:
        if not isinstance(value, list):
            raise ToolError(
                "position_rules must be a list of {position, artist} objects; [] clears"
            )
        pool_artists = {
            r["primary_artist"].casefold(): r["primary_artist"] for r in self._store.pool(rotation)
        }
        size, n_moves = cfg["track_count"], cfg["movements"]
        out: list[Json] = []
        seen: set[int] = set()
        for rule in value:
            if not isinstance(rule, dict) or "position" not in rule or "artist" not in rule:
                raise ToolError(
                    "each position rule needs position (1-based, 'first' or 'last') and artist"
                )
            pos = rule["position"]
            if isinstance(pos, str):
                pos = {"first": 1, "last": size}.get(pos.strip().lower(), pos)
                if isinstance(pos, str) and pos.strip().isdigit():
                    pos = int(pos)
            if isinstance(pos, bool) or not isinstance(pos, int) or not 1 <= pos <= size:
                raise ToolError(
                    f"position must be 1-{size}, 'first' or 'last', got {rule['position']!r}"
                )
            if pos in seen:
                raise ToolError(f"position {pos} is given twice")
            seen.add(pos)
            artist = str(rule["artist"]).strip()
            if artist.casefold() not in pool_artists:
                raise ToolError(
                    f"no pool track by {artist!r}; the artist must already be in the pool"
                )
            entry: Json = {"position": pos, "artist": pool_artists[artist.casefold()]}
            moves = rule.get("movements")
            if moves is not None:
                good = (
                    isinstance(moves, list)
                    and moves
                    and all(
                        isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= n_moves
                        for x in moves
                    )
                )
                if not good:
                    raise ToolError(f"movements must be a non-empty list of numbers 1-{n_moves}")
                entry["movements"] = sorted(set(moves))
            out.append(entry)
        return sorted(out, key=lambda r: r["position"])

    @staticmethod
    def _marker(d: str) -> str:
        """The start of every description this service writes; identifies our playlists."""
        return f"{d} | R2 "

    def _pool_view(self, rotation: str, before: str | None, active_only: bool) -> list[Json]:
        """Pool rows plus play_count / last_played derived from the runs before `before`."""
        stats = self._store.play_stats(rotation, before)
        out = []
        for row in self._store.pool(rotation, active_only=active_only):
            n, last = stats.get(row["track_id"], (0, None))
            row["play_count"] = row["count_offset"] + n
            row["last_played"] = last
            out.append(row)
        return out

    @staticmethod
    def _engine_track(row: Json) -> engine.Track:
        last = row["last_played"]
        return {
            "id": row["track_id"],
            "artist": row["primary_artist"],
            "move": row["movement"],
            "src_pos": row["src_position"],
            "count": row["play_count"],
            "last": dt.date.fromisoformat(last) if last else None,
            "active": row["active"],
        }

    async def _all_playlist_items(self, playlist_id: str) -> list[Json]:
        items: list[Json] = []
        offset = 0
        while True:
            page = await self._client.playlist_items(playlist_id, limit=PAGE, offset=offset)
            items.extend(page)
            if len(page) < PAGE:
                return items
            offset += PAGE

    async def _owned_playlists(self) -> list[Json]:
        me = (await self._client.me())["id"]
        out: list[Json] = []
        offset = 0
        while True:
            page = await self._client.my_playlists(limit=PAGE, offset=offset)
            if page and all(p.get("owner_id") is None for p in page):
                # Never fail open: without owner ids every playlist would look like someone else's.
                raise ToolError("Spotify returned playlists without owner ids; cannot tell yours")
            out.extend(p for p in page if p and p.get("owner_id") == me)
            if len(page) < PAGE:
                return out
            offset += PAGE

    async def _unfollow_quietly(self, playlist_id: str, why: str) -> str | None:
        """Best-effort unfollow: returns the error text instead of raising, so a stale or missing
        playlist never fails the day's run."""
        try:
            await self._client.unfollow_playlist(playlist_id)
        except ToolError as exc:
            log.warning("rotation: could not unfollow %s (%s): %s", playlist_id, why, exc)
            return str(exc)
        return None

    async def _ensure_library_index(self, cfg: Json, refresh: bool) -> str:
        """Rebuild the owned-playlists index when asked or when older than the TTL."""
        at = self._store.library_index_refreshed_at()
        if at and not refresh:
            age = dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(at)
            if age < dt.timedelta(hours=cfg["library_index_ttl_hours"]):
                return at
        rows = []
        for pl in await self._owned_playlists():
            for item in await self._all_playlist_items(pl["id"]):
                t = item.get("item") or {}
                if not t.get("id") or not t.get("artists"):
                    continue
                rows.append(
                    {
                        "playlist_id": pl["id"],
                        "playlist_name": pl["name"],
                        "track_id": t["id"],
                        "isrc": t.get("isrc"),
                        "norm_key": norm_key(t["name"], t["artists"][0]),
                    }
                )
        self._store.replace_library_index(rows)
        log.info("rotation: library index rebuilt, %d tracks", len(rows))
        return self._store.library_index_refreshed_at() or ""

    def _check_track(self, rotation: str, track: Json) -> Json:
        artists = track.get("artists") or ["Unknown"]
        key = norm_key(track["name"], artists[0])
        hit = self._store.pool_match(rotation, track["id"], track.get("isrc"), key)
        return {
            "uri": track.get("uri"),
            "title": track.get("name"),
            "artist": artists[0],
            "in_pool": hit is not None,
            "pool_match": (
                {
                    "title": hit["title"],
                    "artist": hit["primary_artist"],
                    "source": hit["source"],
                    "active": hit["active"],
                }
                if hit
                else None
            ),
            "playlists": self._store.library_matches(track["id"], track.get("isrc"), key),
        }

    @staticmethod
    def _new_song_due(
        cfg: Json, day: dt.date, active: int, last_new: str | None
    ) -> tuple[bool, str]:
        last = dt.date.fromisoformat(last_new) if last_new else None
        if last == day:
            return False, "a new song was already added today"
        if active < cfg["pool_floor"]:
            return True, f"active pool {active} is below the floor of {cfg['pool_floor']}"
        if day <= dt.date.fromisoformat(cfg["new_song_daily_until"]):
            return True, f"one new song every day until {cfg['new_song_daily_until']}"
        every = cfg["new_song_then_every_days"]
        since = (day - last).days if last else None
        if since is None:
            return True, "no new song yet"
        if since >= every - 1:  # the window is `every` days including the day it was added
            return True, f"{since} days since the last new song; one is due every {every} days"
        return False, f"next new song due in {every - 1 - since} days"

    def _description(self, report: Json, new_today: list[Json]) -> str:
        r2, r3, r4 = report["R2_gap_3_4"], report["R3_gap_3_5"], report["R4_rested"]
        parts = [
            self._marker(report["date"]) + f"{r2['value']}/{r2['max']}",
            f"R3 {r3['value']}/{r3['max']}",
            f"R4 {r4['value']}>={r4['min']}",
            f"spread {report['R6_count_spread']}",
        ]
        if report["relaxations"]:
            parts.append("relaxed: " + "; ".join(report["relaxations"]))
        parts.extend(f"New today: {t['artist']} - {t['title']}" for t in new_today)
        return " | ".join(parts)[:DESCRIPTION_MAX]

    def _run_result(self, rotation: str, run: Json) -> Json:
        pid = run["playlist_id"]
        return {
            "rotation": rotation,
            "date": run["run_date"],
            "dry_run": False,
            "existing": True,
            "note": "a run for this date already exists; pass force=true to replace it",
            "playlist_id": pid,
            "playlist_url": f"https://open.spotify.com/playlist/{pid}",
            "playlist_name": run["playlist_name"],
            "tracks": [
                {
                    "position": t["position"],
                    "uri": t["uri"],
                    "title": t["title"],
                    "artist": t["primary_artist"],
                    "movement": t["movement"],
                    "bucket": t["bucket"],
                    "gap": t["gap_at_pick"],
                }
                for t in self._store.run_tracks(rotation, run["run_date"])
            ],
            "rule_report": run["rule_report"],
            "relaxations": run["relaxations"],
        }

    async def _apply_retention(self, rotation: str, cfg: Json) -> Json:
        removed, errors = [], []
        for run in self._store.runs_beyond_retention(rotation, cfg["retention_days"]):
            err = await self._unfollow_quietly(run["playlist_id"], f"retention {run['run_date']}")
            if err and "404" not in err:  # already gone counts as removed
                errors.append({"date": run["run_date"], "error": err})
                continue
            self._store.mark_playlist_removed(rotation, run["run_date"])
            removed.append(run["playlist_name"])
        return {"removed": removed, "errors": errors}

    # ---- tools (in the order the morning run uses them) ---------------------

    @_boundary
    async def status(self, rotation: str, date: str | None = None) -> Json:
        cfg = self._cfg(rotation)
        day = self._date(cfg, date)
        pool = self._pool_view(rotation, before=None, active_only=False)
        active = [r for r in pool if r["active"]]
        counts = [r["play_count"] for r in active]
        last_run = self._store.last_run(rotation)
        last_new = self._store.last_new_song_date(rotation)
        due, why = self._new_song_due(cfg, day, len(active), last_new)
        per_artist = Counter(r["primary_artist"] for r in active)
        cap = cfg["max_pool_tracks_per_artist"]
        return {
            "rotation": rotation,
            "date": day.isoformat(),
            "pool": {"active": len(active), "inactive": len(pool) - len(active)},
            "pool_floor": cfg["pool_floor"],
            "last_run_date": last_run["run_date"] if last_run else None,
            "run_exists_today": self._store.run(rotation, day.isoformat()) is not None,
            "new_song_due": due,
            "new_song_reason": why,
            "days_since_last_new": (day - dt.date.fromisoformat(last_new)).days
            if last_new
            else None,
            "play_count": {
                "min": min(counts, default=0),
                "max": max(counts, default=0),
                "spread": max(counts, default=0) - min(counts, default=0),
            },
            "artists_at_cap": sorted(a for a, n in per_artist.items() if n >= cap),
            "tracks_per_artist": dict(per_artist.most_common()),
            "last_relaxations": last_run["relaxations"] if last_run else [],
            "taste_brief": cfg["taste_brief"],
        }

    @_boundary
    async def library_check(
        self, uris: list[str], rotation: str = "candlelight", refresh: bool = False
    ) -> Json:
        cfg = self._cfg(rotation)
        if not uris:
            raise ToolError("pass at least one track uri")
        if len(uris) > LIBRARY_CHECK_MAX:
            raise ToolError(f"at most {LIBRARY_CHECK_MAX} uris per call")
        refreshed = await self._ensure_library_index(cfg, refresh)
        checked = []
        for uri in uris:
            track = await self._client.get_item("track", uri, market=cfg["market"])
            if not track:
                raise ToolError(f"track not found: {uri}")
            checked.append(self._check_track(rotation, track))
        return {"tracks": checked, "index_refreshed_at": refreshed}

    @_boundary
    async def pool_add(
        self,
        rotation: str,
        uri: str,
        movement: int,
        note: str,
        debut: bool = True,
        allow_owned: bool = False,
        actor: str = "chat",
    ) -> Json:
        async with self._lock(rotation):
            cfg = self._cfg(rotation)
            actor = self._actor(actor)
            if not 1 <= movement <= cfg["movements"]:
                raise ToolError(f"movement must be between 1 and {cfg['movements']}")
            note = self._text(note, "note", NOTE_MAX)
            if not note:
                raise ToolError("note must say in one line why the track fits the rotation")
            track = await self._client.get_item("track", uri, market=cfg["market"])
            if not track:
                raise ToolError(f"track not found: {uri}")
            if track.get("is_playable") is False:
                raise ToolError(f"{track['name']} is not playable in market {cfg['market']}")
            await self._ensure_library_index(cfg, refresh=False)
            check = self._check_track(rotation, track)
            if check["in_pool"]:
                m = check["pool_match"]
                state = "active" if m["active"] else "inactive"
                raise ToolError(
                    f"already in the pool as {m['artist']} - {m['title']} ({m['source']}, {state})"
                )
            if check["playlists"] and not allow_owned:
                names = ", ".join(p["playlist_name"] for p in check["playlists"])
                raise ToolError(
                    f"{track['name']} is already on your playlist(s): {names}. "
                    "Pick another track, or pass allow_owned=true to add it anyway."
                )
            today = self._today(cfg).isoformat()
            active = self._pool_view(rotation, before=None, active_only=True)
            artists = track.get("artists") or ["Unknown"]
            row = {
                "track_id": track["id"],
                "uri": track["uri"],
                "title": track["name"],
                "artists": artists,
                "primary_artist": artists[0],
                "isrc": track.get("isrc"),
                "duration_ms": track.get("duration_ms"),
                "movement": movement,
                "src_position": NEW_SRC_POSITION,
                "source": "new",
                "added_on": today,
                "debut_on": today if debut else None,
                "active": True,
                "count_offset": min((r["play_count"] for r in active), default=0),
                "note": note,
                "title_key": norm_key(track["name"], artists[0]),
            }
            self._store.add_pool_track(rotation, row)
            self._store.add_event(rotation, track["id"], "added", actor, note)
            log.info("rotation %s: added %s - %s (%s)", rotation, artists[0], track["name"], actor)
            return {**row, "play_count": row["count_offset"], "last_played": None}

    @_boundary
    async def generate(
        self, rotation: str, date: str | None = None, dry_run: bool = False, force: bool = False
    ) -> Json:
        async with self._lock(rotation):
            return await self._generate(rotation, date, dry_run, force)

    async def _generate(self, rotation: str, date: str | None, dry_run: bool, force: bool) -> Json:
        cfg = self._cfg(rotation)
        day = self._date(cfg, date)
        d = day.isoformat()
        today = self._today(cfg)
        if not dry_run and day > today:
            raise ToolError(f"real runs are for today ({today}); use dry_run=true to preview {d}")
        if not dry_run and day < today and not force:
            raise ToolError(f"{d} is in the past; pass force=true to regenerate it")
        existing = self._store.run(rotation, d)
        if existing and not force and not dry_run:
            return self._run_result(rotation, existing)

        pool = self._pool_view(rotation, before=d, active_only=True)
        if not pool:
            raise ToolError("the pool is empty: run rotation_seed first")
        debut_ids = {r["track_id"] for r in pool if r["debut_on"] == d}
        ecfg = engine_config(cfg)
        rng = random.Random(f"{rotation}:{d}")  # same seed => dry run and real run agree
        rules = cfg.get("position_rules") or []
        sel = engine.select(
            [self._engine_track(r) for r in pool], day, rng, ecfg, debut_ids, position_rules=rules
        )

        by_id = {r["track_id"]: r for r in pool}
        tracks = []
        for pos, t in enumerate(sel.tracks, 1):
            row = by_id[t["id"]]
            gap = engine.gap_of(t, day)
            bucket = engine.bucket_of(gap)
            tracks.append(
                {
                    "position": pos,
                    "track_id": t["id"],
                    "uri": row["uri"],
                    "title": row["title"],
                    "artist": row["primary_artist"],
                    "movement": sel.movements[t["id"]],
                    "bucket": "debut" if t["id"] in debut_ids else BUCKET_LABEL.get(bucket, bucket),
                    "gap": None if gap == engine.NEVER_PLAYED else gap,
                }
            )
        chosen = {t["track_id"] for t in tracks}
        counts_after = [r["play_count"] + (r["track_id"] in chosen) for r in pool]
        last_new = self._store.last_new_song_date(rotation)
        report = {
            "date": d,
            "tracks": len(tracks),
            "R1_gap_1_2": sum(1 for t in tracks if t["gap"] is not None and t["gap"] <= 2),
            "R2_gap_3_4": {"value": sel.report["R2_gap_3_4"], "max": ecfg["cap_3_4"]},
            "R3_gap_3_5": {"value": sel.report["R3_gap_3_5"], "max": ecfg["cap_3_5"]},
            "R4_rested": {"value": sel.report["R4_rested"], "min": ecfg["min_fresh"]},
            "R5_days_since_new": (day - dt.date.fromisoformat(last_new)).days if last_new else None,
            "R6_count_spread": max(counts_after, default=0) - min(counts_after, default=0),
            "artist_max": sel.report["artist_max"],
            "relaxations": sel.relaxations,
        }
        if rules:
            by_pos = {t["position"]: t for t in tracks}
            report["position_rules"] = []
            for r in rules:
                t = by_pos.get(r["position"])
                met = t is not None and t["artist"].casefold() == r["artist"].casefold()
                report["position_rules"].append(
                    {
                        "position": r["position"],
                        "artist": r["artist"],
                        "met": met,
                        "track": t["title"] if t else None,
                        "from_movement": by_id[t["track_id"]]["movement"] if t else None,
                    }
                )
        name = self._name(cfg, day)
        description = self._description(report, [t for t in tracks if t["bucket"] == "debut"])
        result = {
            "rotation": rotation,
            "date": d,
            "dry_run": dry_run,
            "playlist_name": name,
            "description": description,
            "tracks": tracks,
            "rule_report": report,
            "relaxations": sel.relaxations,
        }
        if dry_run:
            return result

        # Spotify first. The store is written only once the playlist exists, in one transaction
        # that also drops the replaced records of a forced run, so history is never lost half-way.
        created = await self._client.create_playlist(name, public=False, description=description)
        pid = created["id"]
        await self._client.add_playlist_items(pid, [t["uri"] for t in tracks])
        # Set privacy again after the fact: the create-time flag has been seen to not stick.
        await self._client.set_playlist_details(pid, public=False, description=description)
        self._store.record_run(
            rotation,
            {
                "run_date": d,
                "playlist_id": pid,
                "playlist_name": name,
                "rule_report": report,
                "relaxations": sel.relaxations,
            },
            [
                {
                    "position": t["position"],
                    "track_id": t["track_id"],
                    "movement": t["movement"],
                    "gap_at_pick": t["gap"],
                    "bucket": t["bucket"],
                }
                for t in tracks
            ],
            replace=existing is not None,
        )
        log.info(
            "rotation %s: created %s (%s), relaxations=%r", rotation, name, pid, sel.relaxations
        )
        # Now the playlist of a forced run, and any orphan of an attempt that failed after creating
        # its playlist. Only playlists this service wrote are touched: same name AND our description
        # marker, never the one just created.
        replaced = []
        if existing and existing["playlist_id"] != pid:
            await self._unfollow_quietly(existing["playlist_id"], "forced regeneration")
            replaced.append(existing["playlist_id"])
        marker = self._marker(d)
        for pl in await self._owned_playlists():
            if pl["id"] == pid or pl["id"] in replaced or pl["name"] != name:
                continue
            if (pl.get("description") or "").startswith(marker):
                await self._unfollow_quietly(pl["id"], "orphan from an earlier attempt")
        retention = await self._apply_retention(rotation, cfg)
        return {
            **result,
            "playlist_id": pid,
            "playlist_url": f"https://open.spotify.com/playlist/{pid}",
            "retention": retention,
        }

    @_boundary
    async def pool_set_active(
        self, rotation: str, uri: str, active: bool, reason: str = "", actor: str = "chat"
    ) -> Json:
        async with self._lock(rotation):
            self._cfg(rotation)
            actor = self._actor(actor)
            reason = self._text(reason, "reason", NOTE_MAX)
            tid = parse_id(uri)
            row = self._store.pool_track(rotation, tid)
            if row is None:
                raise ToolError(f"{uri} is not in the {rotation} pool")
            if row["active"] == active:
                return {**row, "changed": False}
            self._store.set_active(rotation, tid, active)
            event = "reactivated" if active else "deactivated"
            self._store.add_event(rotation, tid, event, actor, reason or None)
            log.info(
                "rotation %s: %s %s - %s (%s)",
                rotation,
                event,
                row["primary_artist"],
                row["title"],
                actor,
            )
            return {**self._store.pool_track(rotation, tid), "changed": True}

    @_boundary
    async def pool_list(
        self, rotation: str, active_only: bool = True, sort: str = "count"
    ) -> list[Json]:
        self._cfg(rotation)
        keys = {
            "count": lambda r: (r["play_count"], r["last_played"] or "", r["title"]),
            "last_played": lambda r: (r["last_played"] or "", r["play_count"], r["title"]),
            "movement": lambda r: (r["movement"], r["src_position"], r["title"]),
        }
        if sort not in keys:
            raise ToolError("sort must be count, last_played or movement")
        rows = sorted(
            self._pool_view(rotation, before=None, active_only=active_only), key=keys[sort]
        )
        return [
            {
                "uri": r["uri"],
                "title": r["title"],
                "artist": r["primary_artist"],
                "movement": r["movement"],
                "source": r["source"],
                "active": r["active"],
                "play_count": r["play_count"],
                "last_played": r["last_played"],
                "added_on": r["added_on"],
                "note": r["note"],
            }
            for r in rows
        ]

    @_boundary
    async def history(self, rotation: str, days: int = 14) -> list[Json]:
        cfg = self._cfg(rotation)
        days = min(max(0, days), HISTORY_DAYS_MAX)
        since = (self._today(cfg) - dt.timedelta(days=days)).isoformat()
        out = []
        for run in self._store.runs_since(rotation, since):
            out.append(
                {
                    "date": run["run_date"],
                    "playlist_id": run["playlist_id"],
                    "playlist_name": run["playlist_name"],
                    "playlist_removed": run["playlist_removed_at"] is not None,
                    "relaxations": run["relaxations"],
                    "tracks": [
                        {
                            "position": t["position"],
                            "title": t["title"],
                            "artist": t["primary_artist"],
                            "movement": t["movement"],
                            "bucket": t["bucket"],
                            "gap": t["gap_at_pick"],
                        }
                        for t in self._store.run_tracks(rotation, run["run_date"])
                    ],
                }
            )
        return out

    @_boundary
    async def seed(self, rotation: str, playlist_ids: list[str], actor: str = "chat") -> Json:
        async with self._lock(rotation):
            actor = self._actor(actor)
            if self._store.get_config(rotation) is None:
                # A new rotation needs no code: it starts from BASE (or its named defaults) and
                # is tuned afterwards with rotation_config.
                self._store.put_config(rotation, dict(defaults.config_for(rotation)))
            cfg = self._cfg(rotation)
            today = self._today(cfg).isoformat()
            active = self._pool_view(rotation, before=None, active_only=True)
            offset = min((r["play_count"] for r in active), default=0)
            imported = 0
            skipped: list[Json] = []
            for i, pid in enumerate(playlist_ids):
                source = f"Vol {ROMAN[i]}" if i < len(ROMAN) else f"playlist {i + 1}"
                for pos, item in enumerate(await self._all_playlist_items(pid), 1):
                    t = item.get("item") or {}
                    if not t.get("id") or not t.get("artists"):
                        skipped.append({"source": source, "position": pos, "reason": "unavailable"})
                        continue
                    key = norm_key(t["name"], t["artists"][0])
                    dup = self._store.pool_match(rotation, t["id"], t.get("isrc"), key)
                    if dup:
                        skipped.append(
                            {
                                "source": source,
                                "position": pos,
                                "title": t["name"],
                                "reason": f"already in the pool from {dup['source']}",
                            }
                        )
                        continue
                    self._store.add_pool_track(
                        rotation,
                        {
                            "track_id": t["id"],
                            "uri": t["uri"],
                            "title": t["name"],
                            "artists": t["artists"],
                            "primary_artist": t["artists"][0],
                            "isrc": t.get("isrc"),
                            "duration_ms": t.get("duration_ms"),
                            "movement": min(cfg["movements"], (pos - 1) // cfg["per_movement"] + 1),
                            "src_position": pos,
                            "source": source,
                            "added_on": today,
                            "debut_on": None,
                            "active": True,
                            "count_offset": offset,
                            "note": None,
                            "title_key": key,
                        },
                    )
                    self._store.add_event(
                        rotation, t["id"], "added", actor, f"seeded from {source}"
                    )
                    imported += 1
            pool = self._store.pool(rotation)
            log.info("rotation %s: seeded %d tracks (%d skipped)", rotation, imported, len(skipped))
            return {
                "imported": imported,
                "skipped": skipped,
                "pool_size": len(pool),
                "by_movement": dict(sorted(Counter(r["movement"] for r in pool).items())),
            }

    @_boundary
    async def config(self, rotation: str, **updates: Any) -> Json:
        async with self._lock(rotation):
            cfg = self._cfg(rotation)
            changes = {k: v for k, v in updates.items() if v is not None}
            unknown = sorted(set(changes) - set(defaults.EDITABLE))
            if unknown:
                raise ToolError(f"not editable: {', '.join(unknown)}")
            for key in ("artist_cap", "max_pool_tracks_per_artist", "retention_days", "pool_floor"):
                if key in changes and (not isinstance(changes[key], int) or changes[key] < 1):
                    raise ToolError(f"{key} must be a positive integer")
            if "new_song_then_every_days" in changes and changes["new_song_then_every_days"] < 1:
                raise ToolError("new_song_then_every_days must be a positive integer")
            if "lookahead_margin" in changes and changes["lookahead_margin"] < 0:
                raise ToolError("lookahead_margin must be 0 or more")
            if "taste_brief" in changes:
                changes["taste_brief"] = self._text(
                    changes["taste_brief"], "taste_brief", TASTE_BRIEF_MAX
                )
                if not changes["taste_brief"]:
                    raise ToolError("taste_brief must not be empty")
            if "name_template" in changes:
                changes["name_template"] = self._validate_name_template(changes["name_template"])
            if "position_rules" in changes:
                changes["position_rules"] = self._validate_position_rules(
                    cfg, rotation, changes["position_rules"]
                )
            if "new_song_daily_until" in changes:
                self._date(cfg, str(changes["new_song_daily_until"]))
            if changes:
                stored = self._store.get_config(rotation) or {}
                stored.update(changes)
                self._store.put_config(rotation, stored)
                cfg.update(changes)
                log.info("rotation %s: config changed %s", rotation, sorted(changes))
            return {"rotation": rotation, "changed": sorted(changes), "config": cfg}


# --------------------------------------------------------------------------- process-wide holder

_SERVICE: RotationService | None = None


def get_service() -> RotationService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = RotationService(get_store(), get_client())
    return _SERVICE
