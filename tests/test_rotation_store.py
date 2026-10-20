"""Store tests on an in-memory sqlite: dedup matching, derived play stats, atomic run records,
retention selection, and the library index."""

from __future__ import annotations

import pytest

from spotify_mcp.rotation.store import Store


def row(tid: str, title: str = "Song", artist: str = "A", **kw) -> dict:
    base = dict(
        track_id=tid,
        uri=f"spotify:track:{tid}",
        title=title,
        artists=[artist],
        primary_artist=artist,
        isrc=kw.pop("isrc", None),
        duration_ms=1000,
        movement=1,
        src_position=1,
        source="Vol I",
        added_on="2026-10-19",
        debut_on=None,
        active=True,
        count_offset=0,
        note=None,
        title_key=f"{title.lower()}|{artist.lower()}",
    )
    base.update(kw)
    return base


def test_config_roundtrip_and_pool_matching():
    s = Store()
    assert s.get_config("x") is None
    s.put_config("x", {"a": 1})
    s.put_config("x", {"a": 2})
    assert s.get_config("x") == {"a": 2}
    s.add_pool_track("x", row("t1", "Cherish the Day", "Sade", isrc="ISRC1"))
    assert s.pool_match("x", "t1", None, "nope")["track_id"] == "t1"
    assert s.pool_match("x", "other", "ISRC1", "nope")["track_id"] == "t1"
    assert s.pool_match("x", "other", None, "cherish the day|sade")["track_id"] == "t1"
    assert s.pool_match("x", "other", "ISRC2", "nope") is None
    assert s.pool_match("y", "t1", None, "nope") is None  # rotations are separate


def test_play_stats_derive_from_runs_before_a_date():
    s = Store()
    for t in ("t1", "t2"):
        s.add_pool_track("x", row(t))
    run = lambda d, ids: s.record_run(  # noqa: E731
        "x",
        {
            "run_date": d,
            "playlist_id": f"PL{d}",
            "playlist_name": "n",
            "rule_report": {},
            "relaxations": [],
        },
        [
            {"position": i + 1, "track_id": t, "movement": 1, "gap_at_pick": None, "bucket": "F"}
            for i, t in enumerate(ids)
        ],
    )
    run("2026-10-19", ["t1"])
    run("2026-10-20", ["t1", "t2"])
    assert s.play_stats("x") == {"t1": (2, "2026-10-20"), "t2": (1, "2026-10-20")}
    assert s.play_stats("x", before="2026-10-20") == {"t1": (1, "2026-10-19")}
    assert s.play_stats("x", before="2026-10-19") == {}
    assert [t["track_id"] for t in s.run_tracks("x", "2026-10-20")] == ["t1", "t2"]
    s.delete_run("x", "2026-10-20")
    assert s.run("x", "2026-10-20") is None and s.play_stats("x") == {"t1": (1, "2026-10-19")}


def test_record_run_is_atomic():
    s = Store()
    s.add_pool_track("x", row("t1"))
    with pytest.raises(Exception):  # noqa: B017 - second track row violates NOT NULL
        s.record_run(
            "x",
            {
                "run_date": "d",
                "playlist_id": "p",
                "playlist_name": "n",
                "rule_report": {},
                "relaxations": [],
            },
            [
                {"position": 1, "track_id": "t1", "movement": 1, "bucket": "F"},
                {"position": 2, "track_id": None, "movement": 1, "bucket": "F"},
            ],
        )
    assert s.run("x", "d") is None
    assert s.run_tracks("x", "d") == []


def test_retention_keeps_the_newest_runs():
    s = Store()
    for d in range(1, 9):
        s.record_run(
            "x",
            {
                "run_date": f"2026-11-0{d}",
                "playlist_id": f"P{d}",
                "playlist_name": f"n{d}",
                "rule_report": {},
                "relaxations": [],
            },
            [],
        )
    beyond = s.runs_beyond_retention("x", keep_from="2026-11-02")
    assert [r["playlist_id"] for r in beyond] == ["P1"]
    s.mark_playlist_removed("x", "2026-11-01")
    assert s.runs_beyond_retention("x", keep_from="2026-11-02") == []
    # the newest run is never a candidate, however old the cut-off makes it
    assert [r["playlist_id"] for r in s.runs_beyond_retention("x", keep_from="2099-01-01")] == [
        "P2",
        "P3",
        "P4",
        "P5",
        "P6",
        "P7",
    ]
    assert s.last_run("x")["run_date"] == "2026-11-08"
    assert [r["run_date"] for r in s.runs_since("x", "2026-11-07")] == ["2026-11-08", "2026-11-07"]


def test_library_index_matches_by_id_isrc_or_title():
    s = Store()
    assert s.library_index_refreshed_at() is None
    s.replace_library_index(
        [
            {
                "playlist_id": "p1",
                "playlist_name": "Mood",
                "track_id": "a",
                "isrc": "I1",
                "norm_key": "k1",
            },
            {
                "playlist_id": "p2",
                "playlist_name": "Travel",
                "track_id": "b",
                "isrc": None,
                "norm_key": "k2",
            },
        ]
    )
    assert s.library_index_refreshed_at()
    assert [m["matched"] for m in s.library_matches("a", None, "zz")] == ["id"]
    assert [m["matched"] for m in s.library_matches("zz", "I1", "zz")] == ["isrc"]
    assert [(m["playlist_name"], m["matched"]) for m in s.library_matches("zz", None, "k2")] == [
        ("Travel", "title")
    ]
    assert s.library_matches("zz", None, "zz") == []
    s.replace_library_index([])
    assert s.library_matches("a", None, "zz") == []


def test_database_mode_follows_the_directory(tmp_path):
    """0600 in a private directory; 0660 when the directory is group-shared (setgid), so the
    window runner acting in the service's group can repair the file (VPS data root)."""
    import os
    import stat

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    Store(str(private / "rotation.db")).snapshot("t")
    modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in private.iterdir() if p.suffix == ".db"}
    assert modes and set(modes.values()) == {0o600}

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o770)
    os.chmod(shared, 0o2770)
    Store(str(shared / "rotation.db")).snapshot("t")
    modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in shared.iterdir() if p.suffix == ".db"}
    assert modes and set(modes.values()) == {0o660}
