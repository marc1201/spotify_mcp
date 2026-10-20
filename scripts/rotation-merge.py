#!/usr/bin/env python3
"""rotation-merge — bring one rotation back from another copy of the rotation database.

    rotation-merge --from <db> --into <db> --rotation <id> [--fill-others] [--apply]

`--from` is a database file (a snapshot, a backup, or a `rotation.db` copied TOGETHER with its
`-wal` and `-shm`: opening it replays the WAL into the copy). Without `--apply` the script only
prints what both files hold. With `--apply`, in ONE transaction on `--into`: a `VACUUM INTO` copy of
the target is written first (`<into>.before-merge-<stamp>.bak.db`), the named rotation is replaced
wholesale from the source (config, pool, runs, tracks, events; pool rows that exist only in the
target are kept when they are new), and with `--fill-others` every other rotation gains the rows the
target lacks (runs, tracks, pool rows by primary key; events after the source's oldest run) while
keeping everything the target already has. Stdlib only; runs as the service user or in its group.
Written 2026-10-20 for the Wehmut recovery after a WAL-less copy at the VPS cutover.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys

TABLES = (("rotations", "id"), ("pool", "rotation_id"), ("daily_runs", "rotation_id"), ("daily_tracks", "rotation_id"))


def describe(conn: sqlite3.Connection, label: str, schema: str = "main") -> None:
    print(label)
    for (rid,) in conn.execute(f"SELECT id FROM {schema}.rotations ORDER BY id"):
        pool = conn.execute(f"SELECT count(*) FROM {schema}.pool WHERE rotation_id=? AND active=1", (rid,)).fetchone()[0]
        runs = conn.execute(
            f"SELECT count(*), min(run_date), max(run_date) FROM {schema}.daily_runs WHERE rotation_id=?", (rid,)
        ).fetchone()
        print(f"  {rid}: {pool} active songs, {runs[0]} runs {runs[1] or '-'}..{runs[2] or '-'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rotation-merge")
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--into", dest="dst", required=True)
    ap.add_argument("--rotation", required=True)
    ap.add_argument("--fill-others", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    rid = a.rotation

    into = sqlite3.connect(a.dst)
    into.execute("ATTACH DATABASE ? AS src", (a.src,))
    describe(into, f"source {a.src}", "src")
    describe(into, f"target {a.dst}", "main")
    if into.execute("SELECT 1 FROM src.rotations WHERE id=?", (rid,)).fetchone() is None:
        print(f"rotation {rid!r} is not in the source — nothing to merge", file=sys.stderr)
        return 2
    if not a.apply:
        print("dry run — pass --apply to merge")
        return 0

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{a.dst}.before-merge-{stamp}.bak.db"
    into.execute("VACUUM INTO ?", (backup,))
    print("target copied to", backup)
    oldest = into.execute("SELECT min(run_date) FROM src.daily_runs").fetchone()[0] or "0000"
    into.execute("BEGIN IMMEDIATE")
    try:
        into.execute("CREATE TEMP TABLE keep AS SELECT * FROM main.pool WHERE rotation_id=?", (rid,))
        for table, col in TABLES + (("pool_events", "rotation_id"),):
            into.execute(f"DELETE FROM main.{table} WHERE {col}=?", (rid,))
        for table, col in TABLES:
            into.execute(f"INSERT INTO main.{table} SELECT * FROM src.{table} WHERE {col}=?", (rid,))
        into.execute(
            "INSERT INTO main.pool_events (rotation_id, track_id, event, at, actor, reason) "
            "SELECT rotation_id, track_id, event, at, actor, reason FROM src.pool_events WHERE rotation_id=?",
            (rid,),
        )
        kept = into.execute("INSERT OR IGNORE INTO main.pool SELECT * FROM keep").rowcount
        if a.fill_others:
            for table in ("pool", "daily_runs", "daily_tracks"):
                into.execute(f"INSERT OR IGNORE INTO main.{table} SELECT * FROM src.{table} WHERE rotation_id<>?", (rid,))
            into.execute(
                "INSERT INTO main.pool_events (rotation_id, track_id, event, at, actor, reason) "
                "SELECT rotation_id, track_id, event, at, actor, reason FROM src.pool_events "
                "WHERE rotation_id<>? AND at >= ?",
                (rid, oldest),
            )
        into.execute("COMMIT")
    except Exception:
        into.execute("ROLLBACK")
        raise
    print(f"merged {rid!r}; {kept} target-only pool rows kept")
    describe(into, f"target after merge {a.dst}", "main")
    into.execute("DETACH DATABASE src")
    into.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    into.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
