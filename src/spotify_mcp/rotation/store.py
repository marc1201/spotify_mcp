"""SQLite persistence for rotations: stdlib sqlite3, one file, parameterized SQL only. No Spotify,
no FastMCP, so it stays a leaf module like errors.py.

daily_tracks is the source of truth for everything play-related. A track's play count and last
appearance are derived from it on every read (plus the count_offset a new song starts with), never
cached, so force-regenerating or deactivating a track can never leave a stale counter. Dry runs
are not stored. Dates are ISO strings in the rotation's time zone; timestamps are UTC ISO."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

Row = dict[str, Any]

DEFAULT_PATH = "data/rotation.db"  # relative to the working directory (the repo root under systemd)

SCHEMA = """
CREATE TABLE IF NOT EXISTS rotations (
    id TEXT PRIMARY KEY,
    config TEXT NOT NULL                 -- JSON, see defaults.py
);
CREATE TABLE IF NOT EXISTS pool (
    rotation_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    uri TEXT NOT NULL,
    title TEXT NOT NULL,
    artists TEXT NOT NULL,               -- JSON list
    primary_artist TEXT NOT NULL,
    isrc TEXT,
    duration_ms INTEGER,
    movement INTEGER NOT NULL,
    src_position INTEGER NOT NULL,
    source TEXT NOT NULL,                -- 'Vol I' .. 'Vol VI' or 'new'
    added_on TEXT NOT NULL,
    debut_on TEXT,                       -- the date it must appear in the daily playlist, if any
    active INTEGER NOT NULL DEFAULT 1,
    count_offset INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    title_key TEXT NOT NULL,             -- normalized 'title|primary artist' for dedup
    PRIMARY KEY (rotation_id, track_id)
);
CREATE INDEX IF NOT EXISTS pool_by_key ON pool (rotation_id, title_key);
CREATE TABLE IF NOT EXISTS daily_runs (
    rotation_id TEXT NOT NULL,
    run_date TEXT NOT NULL,
    playlist_id TEXT NOT NULL,
    playlist_name TEXT NOT NULL,
    rule_report TEXT NOT NULL,           -- JSON
    relaxations TEXT NOT NULL,           -- JSON list
    created_at TEXT NOT NULL,
    playlist_removed_at TEXT,            -- set when retention unfollowed the playlist
    PRIMARY KEY (rotation_id, run_date)
);
CREATE TABLE IF NOT EXISTS daily_tracks (
    rotation_id TEXT NOT NULL,
    run_date TEXT NOT NULL,
    position INTEGER NOT NULL,
    track_id TEXT NOT NULL,
    movement INTEGER NOT NULL,
    gap_at_pick INTEGER,                 -- NULL if the track had never played
    bucket TEXT NOT NULL,                -- A gap 3-4, B gap 5, C gap 6-7, F rested, debut
    PRIMARY KEY (rotation_id, run_date, position)
);
CREATE INDEX IF NOT EXISTS daily_tracks_by_track ON daily_tracks (rotation_id, track_id, run_date);
CREATE TABLE IF NOT EXISTS pool_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rotation_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    event TEXT NOT NULL,                 -- added, deactivated, reactivated
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS library_index (
    playlist_id TEXT NOT NULL,
    playlist_name TEXT NOT NULL,
    track_id TEXT NOT NULL,
    isrc TEXT,
    norm_key TEXT NOT NULL,
    PRIMARY KEY (playlist_id, track_id)
);
CREATE INDEX IF NOT EXISTS library_index_by_key ON library_index (norm_key);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: str = ":memory:"):
        self._path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        if path != ":memory:":
            os.chmod(path, 0o600)  # listening history is private; -wal/-shm inherit this mode
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def snapshot(self, label: str) -> str | None:
        """Copy the whole database next to itself (rotation.<utc stamp>.<label>.bak.db) before a
        destructive operation. None for an in-memory store."""
        if self._path == ":memory:":
            return None
        src = Path(self._path)
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        dest = src.with_name(f"{src.stem}.{stamp}.{label}.bak.db")
        self._db.execute("VACUUM INTO ?", (str(dest),))
        os.chmod(dest, 0o600)
        return str(dest)

    def get_config(self, rotation_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT config FROM rotations WHERE id=?", (rotation_id,)).fetchone()
        return json.loads(row["config"]) if row else None

    def put_config(self, rotation_id: str, config: dict[str, Any]) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO rotations (id, config) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET config=excluded.config",
                (rotation_id, json.dumps(config)),
            )

    # ---- pool ---------------------------------------------------------------

    @staticmethod
    def _pool_row(r: sqlite3.Row) -> Row:
        d = dict(r)
        d["artists"] = json.loads(d["artists"])
        d["active"] = bool(d["active"])
        return d

    def pool(self, rotation_id: str, active_only: bool = False) -> list[Row]:
        sql = "SELECT * FROM pool WHERE rotation_id=?"
        if active_only:
            sql += " AND active=1"
        rows = self._db.execute(sql + " ORDER BY track_id", (rotation_id,)).fetchall()
        return [self._pool_row(r) for r in rows]

    def pool_track(self, rotation_id: str, track_id: str) -> Row | None:
        r = self._db.execute(
            "SELECT * FROM pool WHERE rotation_id=? AND track_id=?", (rotation_id, track_id)
        ).fetchone()
        return self._pool_row(r) if r else None

    def pool_match(
        self, rotation_id: str, track_id: str, isrc: str | None, norm_key: str
    ) -> Row | None:
        """The pool row that is the same song: by id, ISRC, or normalized title + primary artist."""
        r = self._db.execute(
            "SELECT p.* FROM pool p WHERE p.rotation_id=? AND (p.track_id=? "
            "OR (p.isrc IS NOT NULL AND p.isrc=?) OR p.title_key=?) LIMIT 1",
            (rotation_id, track_id, isrc, norm_key),
        ).fetchone()
        return self._pool_row(r) if r else None

    @staticmethod
    def _pool_params(rotation_id: str, row: Row) -> tuple[Any, ...]:
        return (
            rotation_id,
            row["track_id"],
            row["uri"],
            row["title"],
            json.dumps(row["artists"]),
            row["primary_artist"],
            row.get("isrc"),
            row.get("duration_ms"),
            row["movement"],
            row["src_position"],
            row["source"],
            row["added_on"],
            row.get("debut_on"),
            1 if row.get("active", True) else 0,
            row.get("count_offset", 0),
            row.get("note"),
            row["title_key"],
        )

    _POOL_INSERT = (
        "INSERT INTO pool (rotation_id, track_id, uri, title, artists, primary_artist, isrc, "
        "duration_ms, movement, src_position, source, added_on, debut_on, active, count_offset, "
        "note, title_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    _EVENT_INSERT = (
        "INSERT INTO pool_events (rotation_id, track_id, event, at, actor, reason) "
        "VALUES (?,?,?,?,?,?)"
    )

    def add_pool_track(self, rotation_id: str, row: Row) -> None:
        with self._db:
            self._db.execute(self._POOL_INSERT, self._pool_params(rotation_id, row))

    def add_pool_tracks(
        self,
        rotation_id: str,
        rows: list[Row],
        events: list[tuple[str, str, str, str | None]],
        clear: bool = False,
    ) -> dict[str, int]:
        """Insert many pool rows and their (track_id, event, actor, reason) events in ONE
        transaction, optionally wiping the rotation's pool and history first, so a failure
        half-way leaves the previous state untouched. Returns the rows cleared."""
        now = _now()
        with self._db:
            cleared = self._clear(rotation_id) if clear else {}
            self._db.executemany(
                self._POOL_INSERT, [self._pool_params(rotation_id, r) for r in rows]
            )
            self._db.executemany(
                self._EVENT_INSERT, [(rotation_id, t, e, now, a, r) for t, e, a, r in events]
            )
        return cleared

    def update_pool_track(self, rotation_id: str, track_id: str, changes: Row) -> None:
        """Change a track's movement and/or note."""
        sets = {"movement": "movement=?", "note": "note=?"}
        unknown = set(changes) - set(sets)
        if unknown:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        if not changes:
            return
        with self._db:
            self._db.execute(
                "UPDATE pool SET "
                + ", ".join(sets[k] for k in changes)
                + " WHERE rotation_id=? AND track_id=?",
                (*changes.values(), rotation_id, track_id),
            )

    def _clear(self, rotation_id: str) -> dict[str, int]:
        """Delete a rotation's pool, runs, tracks and events inside the caller's transaction."""
        return {
            "daily_tracks": self._db.execute(
                "DELETE FROM daily_tracks WHERE rotation_id=?", (rotation_id,)
            ).rowcount,
            "daily_runs": self._db.execute(
                "DELETE FROM daily_runs WHERE rotation_id=?", (rotation_id,)
            ).rowcount,
            "pool_events": self._db.execute(
                "DELETE FROM pool_events WHERE rotation_id=?", (rotation_id,)
            ).rowcount,
            "pool": self._db.execute(
                "DELETE FROM pool WHERE rotation_id=?", (rotation_id,)
            ).rowcount,
        }

    def clear_rotation_data(self, rotation_id: str) -> dict[str, int]:
        """Delete a rotation's pool, runs, tracks and events (not its config). Returns row counts."""
        with self._db:
            return self._clear(rotation_id)

    def delete_rotation(self, rotation_id: str) -> dict[str, int]:
        """Remove a rotation entirely, in one transaction."""
        with self._db:
            counts = self._clear(rotation_id)
            self._db.execute("DELETE FROM rotations WHERE id=?", (rotation_id,))
        return counts

    def counts(self, rotation_id: str) -> dict[str, int]:
        return {
            "pool": self._db.execute(
                "SELECT COUNT(*) FROM pool WHERE rotation_id=?", (rotation_id,)
            ).fetchone()[0],
            "daily_runs": self._db.execute(
                "SELECT COUNT(*) FROM daily_runs WHERE rotation_id=?", (rotation_id,)
            ).fetchone()[0],
        }

    def set_active(self, rotation_id: str, track_id: str, active: bool) -> None:
        with self._db:
            self._db.execute(
                "UPDATE pool SET active=? WHERE rotation_id=? AND track_id=?",
                (1 if active else 0, rotation_id, track_id),
            )

    def add_event(
        self, rotation_id: str, track_id: str, event: str, actor: str, reason: str | None
    ) -> None:
        with self._db:
            self._db.execute(
                self._EVENT_INSERT, (rotation_id, track_id, event, _now(), actor, reason)
            )

    def last_new_song_date(self, rotation_id: str) -> str | None:
        r = self._db.execute(
            "SELECT MAX(added_on) AS d FROM pool WHERE rotation_id=? AND source='new'",
            (rotation_id,),
        ).fetchone()
        return r["d"] if r else None

    # ---- runs ---------------------------------------------------------------

    def play_stats(self, rotation_id: str, before: str | None = None) -> dict[str, tuple[int, str]]:
        """track_id -> (appearances, last run date) over runs strictly before `before` (or all)."""
        sql = "SELECT track_id, COUNT(*) AS n, MAX(run_date) AS last FROM daily_tracks WHERE rotation_id=?"
        args: tuple[Any, ...] = (rotation_id,)
        if before:
            sql += " AND run_date<?"
            args += (before,)
        rows = self._db.execute(sql + " GROUP BY track_id", args).fetchall()
        return {r["track_id"]: (r["n"], r["last"]) for r in rows}

    @staticmethod
    def _run_row(r: sqlite3.Row) -> Row:
        d = dict(r)
        d["rule_report"] = json.loads(d["rule_report"])
        d["relaxations"] = json.loads(d["relaxations"])
        return d

    def run(self, rotation_id: str, run_date: str) -> Row | None:
        r = self._db.execute(
            "SELECT * FROM daily_runs WHERE rotation_id=? AND run_date=?", (rotation_id, run_date)
        ).fetchone()
        return self._run_row(r) if r else None

    def last_run(self, rotation_id: str) -> Row | None:
        r = self._db.execute(
            "SELECT * FROM daily_runs WHERE rotation_id=? ORDER BY run_date DESC LIMIT 1",
            (rotation_id,),
        ).fetchone()
        return self._run_row(r) if r else None

    def runs_since(self, rotation_id: str, since: str) -> list[Row]:
        rows = self._db.execute(
            "SELECT * FROM daily_runs WHERE rotation_id=? AND run_date>=? ORDER BY run_date DESC",
            (rotation_id, since),
        ).fetchall()
        return [self._run_row(r) for r in rows]

    def run_tracks(self, rotation_id: str, run_date: str) -> list[Row]:
        rows = self._db.execute(
            "SELECT d.position, d.track_id, d.movement, d.gap_at_pick, d.bucket, "
            "p.uri, p.title, p.primary_artist FROM daily_tracks d "
            "JOIN pool p ON p.rotation_id=d.rotation_id AND p.track_id=d.track_id "
            "WHERE d.rotation_id=? AND d.run_date=? ORDER BY d.position",
            (rotation_id, run_date),
        ).fetchall()
        return [dict(r) for r in rows]

    def record_run(
        self, rotation_id: str, run: Row, tracks: list[Row], replace: bool = False
    ) -> None:
        """Write the run and its tracks in one transaction; with replace, that date's previous
        records go in the same transaction, so history is never lost half-way."""
        with self._db:
            if replace:
                self._db.execute(
                    "DELETE FROM daily_tracks WHERE rotation_id=? AND run_date=?",
                    (rotation_id, run["run_date"]),
                )
                self._db.execute(
                    "DELETE FROM daily_runs WHERE rotation_id=? AND run_date=?",
                    (rotation_id, run["run_date"]),
                )
            self._db.execute(
                "INSERT INTO daily_runs (rotation_id, run_date, playlist_id, playlist_name, "
                "rule_report, relaxations, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    rotation_id,
                    run["run_date"],
                    run["playlist_id"],
                    run["playlist_name"],
                    json.dumps(run["rule_report"]),
                    json.dumps(run["relaxations"]),
                    _now(),
                ),
            )
            self._db.executemany(
                "INSERT INTO daily_tracks (rotation_id, run_date, position, track_id, movement, "
                "gap_at_pick, bucket) VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        rotation_id,
                        run["run_date"],
                        t["position"],
                        t["track_id"],
                        t["movement"],
                        t.get("gap_at_pick"),
                        t["bucket"],
                    )
                    for t in tracks
                ],
            )

    def delete_run(self, rotation_id: str, run_date: str) -> None:
        with self._db:
            self._db.execute(
                "DELETE FROM daily_tracks WHERE rotation_id=? AND run_date=?",
                (rotation_id, run_date),
            )
            self._db.execute(
                "DELETE FROM daily_runs WHERE rotation_id=? AND run_date=?", (rotation_id, run_date)
            )

    def runs_beyond_retention(self, rotation_id: str, keep_from: str) -> list[Row]:
        """Runs dated before `keep_from` whose playlist is still on Spotify, never the newest run."""
        rows = self._db.execute(
            "SELECT * FROM daily_runs WHERE rotation_id=? AND playlist_removed_at IS NULL "
            "AND run_date<? AND run_date<(SELECT MAX(run_date) FROM daily_runs WHERE rotation_id=?) "
            "ORDER BY run_date",
            (rotation_id, keep_from, rotation_id),
        ).fetchall()
        return [self._run_row(r) for r in rows]

    def mark_playlist_removed(self, rotation_id: str, run_date: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE daily_runs SET playlist_removed_at=? WHERE rotation_id=? AND run_date=?",
                (_now(), rotation_id, run_date),
            )

    # ---- library index ------------------------------------------------------

    def library_index_refreshed_at(self) -> str | None:
        r = self._db.execute(
            "SELECT value FROM meta WHERE key='library_index_refreshed_at'"
        ).fetchone()
        return r["value"] if r else None

    def replace_library_index(self, rows: list[Row]) -> None:
        with self._db:
            self._db.execute("DELETE FROM library_index")
            self._db.executemany(
                "INSERT OR IGNORE INTO library_index (playlist_id, playlist_name, track_id, isrc, "
                "norm_key) VALUES (?,?,?,?,?)",
                [
                    (
                        r["playlist_id"],
                        r["playlist_name"],
                        r["track_id"],
                        r.get("isrc"),
                        r["norm_key"],
                    )
                    for r in rows
                ],
            )
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES ('library_index_refreshed_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_now(),),
            )

    def library_matches(self, track_id: str, isrc: str | None, norm_key: str) -> list[Row]:
        """Owned playlists holding this song, each with how it matched (id, isrc, or title)."""
        rows = self._db.execute(
            "SELECT DISTINCT playlist_id, playlist_name, "
            "CASE WHEN track_id=? THEN 'id' WHEN isrc IS NOT NULL AND isrc=? THEN 'isrc' "
            "ELSE 'title' END AS matched FROM library_index "
            "WHERE track_id=? OR (isrc IS NOT NULL AND isrc=?) OR norm_key=? ORDER BY playlist_name",
            (track_id, isrc, track_id, isrc, norm_key),
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- process-wide holder

_STORE: Store | None = None


def get_store() -> Store:
    """The process-wide store, opened lazily at ROTATION_DB (default data/rotation.db)."""
    global _STORE
    if _STORE is None:
        _STORE = Store(os.environ.get("ROTATION_DB", "").strip() or DEFAULT_PATH)
    return _STORE
