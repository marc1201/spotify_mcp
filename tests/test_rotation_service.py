"""Service tests against a fake SpotifyClient that serves a 143-track catalog shaped like the six
Candlelight volumes and records every call. No network. The service gets an injectable clock so
real runs are tested on fixed dates."""

from __future__ import annotations

import asyncio
import datetime as dt
import random

import pytest
from fastmcp.exceptions import ToolError

from spotify_mcp.rotation import defaults
from spotify_mcp.rotation.service import RotationService, engine_config, norm_key
from spotify_mcp.rotation.store import Store

ME = "marc1201"
START = dt.date(2026, 10, 19)
VOLS = [f"vol{v}" for v in range(1, 7)]


class Clock:
    def __init__(self, day: dt.date):
        self.day = day

    def __call__(self, tz):
        return dt.datetime.combine(self.day, dt.time(7, 0), tzinfo=tz)

    def advance(self, days: int = 1) -> str:
        self.day += dt.timedelta(days=days)
        return self.day.isoformat()


def track(i: int, artist: str, title: str | None = None, isrc: str | None = None) -> dict:
    return {
        "name": title or f"Song {i}",
        "id": f"t{i:03d}",
        "uri": f"spotify:track:t{i:03d}",
        "artists": [artist],
        "album": "Album",
        "duration_ms": 240000,
        "isrc": isrc or f"ISRC{i:05d}",
    }


class FakeClient:
    """The subset of SpotifyClient the service uses, with a mutable playlist state."""

    def __init__(self, playlists: dict[str, dict]):
        self.playlists = playlists  # id -> {name, owner_id, items: [track], description?}
        self.catalog = {t["id"]: t for p in playlists.values() for t in p["items"]}
        self.calls: list[tuple] = []
        self.fail_next_create = False
        self.omit_owner_id = False
        self._n = 0

    async def me(self):
        self.calls.append(("me",))
        return {"id": ME}

    async def my_playlists(self, limit=50, offset=0):
        self.calls.append(("my_playlists", offset))
        rows = [
            {
                "id": k,
                "name": v["name"],
                "owner_id": None if self.omit_owner_id else v["owner_id"],
                "description": v.get("description"),
            }
            for k, v in self.playlists.items()
        ]
        return rows[offset : offset + limit]

    async def playlist_items(self, playlist_id, limit=50, offset=0, market=None):
        self.calls.append(("playlist_items", playlist_id, offset))
        items = [
            {"added_at": "2026-06-02T08:22:40Z", "item": t}
            for t in self.playlists[playlist_id]["items"]
        ]
        return items[offset : offset + limit]

    async def get_item(self, item_type, id_or_uri, market=None):
        self.calls.append(("get_item", id_or_uri, market))
        t = self.catalog.get(id_or_uri.split(":")[-1])
        if t is None:
            raise ToolError(f"Not found (404): {id_or_uri}")
        return dict(t)

    async def create_playlist(self, name, public=False, collaborative=False, description=None):
        self.calls.append(("create_playlist", name, public, description))
        if self.fail_next_create:
            self.fail_next_create = False
            raise ToolError("Spotify error 503: boom")
        self._n += 1
        pid = f"PL{self._n}"
        # Like the real Spotify API for app-created playlists: the public flag is ignored.
        self.playlists[pid] = {
            "name": name,
            "owner_id": ME,
            "items": [],
            "public": True,
            "description": description,
        }
        return {"id": pid, "name": name, "uri": f"spotify:playlist:{pid}", "public": public}

    async def add_playlist_items(self, playlist_id, uris, position=None):
        self.calls.append(("add_playlist_items", playlist_id, list(uris)))
        self.playlists[playlist_id]["items"].extend(self.catalog[u.split(":")[-1]] for u in uris)
        return {"ok": True, "snapshot_id": "s"}

    async def set_playlist_details(
        self, playlist_id, name=None, public=None, collaborative=None, description=None
    ):
        self.calls.append(("set_playlist_details", playlist_id, public, description))
        return {"ok": True}  # the public flag is ignored, as Spotify does

    async def get_playlist(self, playlist_id, market=None):
        self.calls.append(("get_playlist", playlist_id))
        p = self.playlists.get(playlist_id)
        if p is None:
            raise ToolError("Not found (404): playlist")
        return {
            "id": playlist_id,
            "name": p["name"],
            "owner_id": p["owner_id"],
            "public": p["public"],
        }

    async def unfollow_playlist(self, playlist_id):
        self.calls.append(("unfollow_playlist", playlist_id))
        if playlist_id not in self.playlists:
            raise ToolError("Not found (404): playlist")
        del self.playlists[playlist_id]
        return {"ok": True}

    def names(self, *methods):
        return [c for c in self.calls if c[0] in methods]

    def daily(self):
        return sorted(k for k, v in self.playlists.items() if "Daily" in v["name"])


def catalog() -> FakeClient:
    """Six volumes (vol 3 has 23 tracks) = 143 tracks, two other owned playlists, one followed
    playlist, plus a few catalog-only tracks that serve as new-song candidates."""
    r = random.Random(7)
    artists = (
        ["Sade"] * 15 + ["Cigarettes After Sex"] * 11 + ["Beach House"] * 6 + ["Portishead"] * 6
    )
    i = 0
    while len(artists) < 143:
        artists += [f"Artist {i}"] * r.choice([1, 2, 3, 4])
        i += 1
    artists = artists[:143]
    r.shuffle(artists)
    playlists, k = {}, 0
    for vol in range(1, 7):
        items = []
        for _pos in range(23 if vol == 3 else 24):
            k += 1
            items.append(track(k, artists[k - 1]))
        playlists[f"vol{vol}"] = {
            "name": f"Closer by Candlelight {vol}",
            "owner_id": ME,
            "items": items,
        }
    playlists["mood"] = {
        "name": "3 Mood - Weekend",
        "owner_id": ME,
        "items": [
            track(900, "Khruangbin", "Friday Morning"),
            track(901, "Al Green", "Simply Beautiful - Remastered"),
        ],
    }
    playlists["followed"] = {
        "name": "Someone else's",
        "owner_id": "other",
        "items": [track(950, "Other", "Elsewhere")],
    }
    fc = FakeClient(playlists)
    fc.catalog["t960"] = track(960, "Leon Bridges", "Beyond")  # free candidate
    fc.catalog["t961"] = track(
        961, "Al Green", "Simply Beautiful (2009 Remaster)", isrc="X"
    )  # = t901
    fc.catalog["t962"] = track(962, "Sade", "Cherish the Day")  # free candidate by a capped artist
    return fc


@pytest.fixture
def svc():
    client = catalog()
    clock = Clock(START)
    service = RotationService(Store(), client, now=clock)
    asyncio.run(service.seed("candlelight", VOLS))
    client.calls.clear()
    return service, client, clock


def run(coro):
    return asyncio.run(coro)


def test_norm_key_and_engine_config():
    assert norm_key("Pretty Wings - uncut", "Maxwell") == "pretty wings|maxwell"
    assert norm_key("Best Part (feat. H.E.R.)", "Daniel Caesar") == "best part|daniel caesar"
    assert norm_key("Céu", "Céu") == norm_key("Ceu", "ceu")
    assert (
        norm_key("Feel Like Makin' Love - 2026 Remaster", "Roberta Flack")
        == "feel like makin love|roberta flack"
    )
    assert engine_config(defaults.ROTATIONS["candlelight"]) == {
        "size": 24,
        "movements": 4,
        "per_movement": 6,
        "cap_3_4": 4,
        "cap_3_5": 12,
        "min_fresh": 5,
        "artist_cap": 3,
        "lookahead_margin": 1,
    }


def test_seed_imports_with_movements_and_skips_duplicates(svc):
    service, client, _ = svc
    pool = run(service.pool_list("candlelight", sort="movement"))
    assert len(pool) == 143
    assert {p["movement"] for p in pool} == {1, 2, 3, 4}
    assert sum(p["movement"] == 4 for p in pool) == 35  # 6 x 6, minus one: vol 3 has 23
    assert pool[0]["source"] == "Vol I"
    again = run(service.seed("candlelight", ["vol1"]))
    assert again["imported"] == 0 and len(again["skipped"]) == 24 and again["pool_size"] == 143
    assert run(service.config("candlelight"))["config"]["artist_cap"] == 3
    with pytest.raises(ToolError, match="actor"):
        run(service.seed("candlelight", ["vol1"], actor="Bad Actor!"))


def test_status_before_first_run(svc):
    service, _, _ = svc
    st = run(service.status("candlelight"))
    assert st["date"] == "2026-10-19"
    assert st["pool"] == {"active": 143, "inactive": 0}
    assert st["run_exists_today"] is False and st["last_run_date"] is None
    assert st["new_song_due"] is True  # below the floor, and in the daily period
    assert "Sade" in st["artists_at_cap"] and st["tracks_per_artist"]["Sade"] == 15
    assert st["play_count"] == {"min": 0, "max": 0, "spread": 0}
    assert "Closer by Candlelight" in st["taste_brief"]


def test_dry_run_is_deterministic_and_within_limits(svc):
    service, client, _ = svc
    a = run(service.generate("candlelight", "2026-11-30", dry_run=True))  # any date for a dry run
    b = run(service.generate("candlelight", "2026-11-30", dry_run=True))
    assert a["tracks"] == b["tracks"] and len(a["tracks"]) == 24
    assert [t["movement"] for t in a["tracks"]] == sorted(t["movement"] for t in a["tracks"])
    assert all(a["tracks"][i]["artist"] != a["tracks"][i + 1]["artist"] for i in range(23))
    r = a["rule_report"]
    assert r["R1_gap_1_2"] == 0 and r["R2_gap_3_4"]["value"] <= 4 and r["R3_gap_3_5"]["value"] <= 12
    assert r["R4_rested"]["value"] >= 5 and r["artist_max"] <= 3 and r["relaxations"] == []
    assert a["playlist_name"] == "1 Intimate - Closer by Candlelight - Daily 2026-11-30"
    assert len(a["description"]) <= 300 and a["description"].startswith("2026-11-30 | R2 ")
    assert not client.names("create_playlist", "add_playlist_items")
    assert run(service.status("candlelight", "2026-11-30"))["run_exists_today"] is False


def test_real_run_creates_playlist_reports_visibility_and_records(svc):
    service, client, _ = svc
    res = run(service.generate("candlelight"))
    assert res["date"] == "2026-10-19"
    assert res["playlist_id"] == "PL1" and res["playlist_url"].endswith("/PL1")
    create = client.names("create_playlist")[0]
    assert create[1] == res["playlist_name"] and create[2] is False
    assert client.names("add_playlist_items")[0][2] == [t["uri"] for t in res["tracks"]]
    assert client.names("set_playlist_details")[0][2] is False
    assert res["public"] is True  # Spotify ignored the flag; the result says so
    assert res["retention"] == {"removed": [], "errors": []}

    client.calls.clear()
    again = run(service.generate("candlelight"))
    assert again["existing"] is True and again["playlist_id"] == "PL1"
    assert [t["uri"] for t in again["tracks"]] == [t["uri"] for t in res["tracks"]]
    assert client.calls == []  # served from the store

    hist = run(service.history("candlelight", days=10**9))  # days is clamped, no overflow
    assert len(hist) == 1 and len(hist[0]["tracks"]) == 24 and hist[0]["playlist_id"] == "PL1"
    st = run(service.status("candlelight"))
    assert st["run_exists_today"] is True and st["play_count"]["max"] == 1


def test_real_runs_are_for_today_unless_forced(svc):
    service, client, clock = svc
    with pytest.raises(ToolError, match="real runs are for today"):
        run(service.generate("candlelight", "2026-10-20"))
    with pytest.raises(ToolError, match="in the past"):
        run(service.generate("candlelight", "2026-10-18"))
    past = run(service.generate("candlelight", "2026-10-18", force=True))
    assert past["date"] == "2026-10-18" and client.daily() == ["PL1"]
    clock.advance()
    today = run(service.generate("candlelight"))
    assert today["date"] == "2026-10-20" and client.daily() == ["PL1", "PL2"]


def test_next_day_excludes_yesterday_and_force_replaces(svc):
    service, client, clock = svc
    d1 = run(service.generate("candlelight"))
    clock.advance()
    d2 = run(service.generate("candlelight"))
    assert not {t["uri"] for t in d1["tracks"]} & {t["uri"] for t in d2["tracks"]}
    client.calls.clear()
    forced = run(service.generate("candlelight", force=True))
    assert forced["playlist_id"] != d2["playlist_id"]
    assert ("unfollow_playlist", d2["playlist_id"]) in client.calls
    assert d2["playlist_id"] not in client.playlists
    assert [t["uri"] for t in forced["tracks"]] == [
        t["uri"] for t in d2["tracks"]
    ]  # same seed, same pool
    hist = run(service.history("candlelight"))
    assert [h["playlist_id"] for h in hist] == [forced["playlist_id"], d1["playlist_id"]]


def test_force_keeps_history_when_spotify_fails(svc):
    service, client, _ = svc
    first = run(service.generate("candlelight"))
    client.fail_next_create = True
    with pytest.raises(ToolError, match="503"):
        run(service.generate("candlelight", force=True))
    assert run(service.generate("candlelight"))["playlist_id"] == first["playlist_id"]
    assert first["playlist_id"] in client.playlists
    assert len(run(service.history("candlelight"))) == 1


def test_retention_keeps_seven_dated_playlists(svc):
    service, client, clock = svc
    run(service.generate("candlelight"))
    for _ in range(7):  # eight days in total
        clock.advance()
        run(service.generate("candlelight"))
    assert len(client.daily()) == 7
    assert client.names("unfollow_playlist") == [("unfollow_playlist", "PL1")]
    assert all(v in client.playlists for v in VOLS + ["mood", "followed"])
    assert run(service.history("candlelight", days=400))[-1]["playlist_removed"] is True


def test_orphan_cleanup_only_touches_our_own_playlists(svc):
    service, client, _ = svc
    name = "1 Intimate - Closer by Candlelight - Daily 2026-10-19"
    client.playlists["orphan"] = {
        "name": name,
        "owner_id": ME,
        "items": [],
        "description": "2026-10-19 | R2 4/4 | R3 ...",
    }
    client.playlists["decoy"] = {
        "name": name,
        "owner_id": ME,
        "items": [],
        "description": "Marc's own list",
    }
    client.playlists["theirs"] = {
        "name": name,
        "owner_id": "other",
        "items": [],
        "description": "2026-10-19 | R2 ",
    }
    res = run(service.generate("candlelight"))
    assert res["playlist_id"] == "PL1"
    assert client.names("unfollow_playlist") == [("unfollow_playlist", "orphan")]
    assert (
        "orphan" not in client.playlists
        and "decoy" in client.playlists
        and "theirs" in client.playlists
    )


def test_missing_owner_ids_fail_closed(svc):
    service, client, _ = svc
    client.omit_owner_id = True
    with pytest.raises(ToolError, match="owner ids"):
        run(service.library_check(["t960"]))
    with pytest.raises(ToolError, match="owner ids"):
        run(service.pool_add("candlelight", "t960", 2, "x"))
    assert "t960" not in {p["uri"].split(":")[-1] for p in run(service.pool_list("candlelight"))}


def test_library_check_matches_owned_playlists_and_pool(svc):
    service, client, _ = svc
    res = run(service.library_check(["spotify:track:t001", "t961", "spotify:track:t960"]))
    pool_track, remaster, free = res["tracks"]
    assert pool_track["in_pool"] is True and pool_track["pool_match"]["source"] == "Vol I"
    assert [p["playlist_name"] for p in pool_track["playlists"]] == ["Closer by Candlelight 1"]
    assert remaster["in_pool"] is False
    assert [(p["playlist_name"], p["matched"]) for p in remaster["playlists"]] == [
        ("3 Mood - Weekend", "title")
    ]
    assert free["in_pool"] is False and free["playlists"] == []
    assert res["index_refreshed_at"]
    assert len(client.names("playlist_items")) == 7  # six volumes + mood, never the followed one
    client.calls.clear()
    run(service.library_check(["t960"]))
    assert not client.names("playlist_items")  # index is fresh, no rescan
    with pytest.raises(ToolError, match="at most 25"):
        run(service.library_check(["t960"] * 26))
    with pytest.raises(ToolError, match="at least one"):
        run(service.library_check([]))


def test_pool_add_validates_and_starts_at_lowest_count(svc):
    service, client, clock = svc
    with pytest.raises(ToolError, match="movement"):
        run(service.pool_add("candlelight", "t960", 5, "warm low voice"))
    with pytest.raises(ToolError, match="note"):
        run(service.pool_add("candlelight", "t960", 2, "  "))
    with pytest.raises(ToolError, match="at most 300"):
        run(service.pool_add("candlelight", "t960", 2, "x" * 301))
    with pytest.raises(ToolError, match="actor"):
        run(service.pool_add("candlelight", "t960", 2, "fits", actor="line\nbreak"))
    with pytest.raises(ToolError, match="already in the pool"):
        run(service.pool_add("candlelight", "t001", 2, "x"))
    with pytest.raises(ToolError, match="3 Mood - Weekend"):
        run(service.pool_add("candlelight", "t961", 2, "x"))
    with pytest.raises(ToolError, match="Not found"):
        run(service.pool_add("candlelight", "t999", 2, "x"))
    run(service.generate("candlelight"))  # some tracks now have count 1
    added = run(
        service.pool_add("candlelight", "t960", 2, "warm low voice", actor="scheduled-task")
    )
    assert (
        added["source"] == "new" and added["play_count"] == 0 and added["debut_on"] == "2026-10-19"
    )
    assert (
        run(service.pool_add("candlelight", "t961", 2, "x", allow_owned=True))["title_key"]
        == "simply beautiful|al green"
    )
    st = run(service.status("candlelight"))
    assert st["new_song_due"] is False and st["new_song_reason"].startswith(
        "a new song was already added"
    )
    assert st["days_since_last_new"] == 0
    clock.advance()
    assert run(service.status("candlelight"))["days_since_last_new"] == 1


def test_debut_plays_today_and_deactivated_never_plays(svc):
    service, client, _ = svc
    run(service.pool_add("candlelight", "t960", 3, "fits"))
    res = run(service.generate("candlelight", dry_run=True))
    debut = [t for t in res["tracks"] if t["bucket"] == "debut"]
    assert [t["uri"] for t in debut] == ["spotify:track:t960"] and debut[0]["movement"] == 3
    assert "New today: Leon Bridges - Beyond" in res["description"]
    off = run(
        service.pool_set_active("candlelight", "spotify:track:t960", False, reason="too airy")
    )
    assert off["active"] is False and off["changed"] is True
    assert run(service.pool_set_active("candlelight", "t960", False))["changed"] is False
    res2 = run(service.generate("candlelight", dry_run=True))
    assert "spotify:track:t960" not in {t["uri"] for t in res2["tracks"]}
    assert run(service.status("candlelight"))["pool"]["inactive"] == 1
    assert len(run(service.pool_list("candlelight", active_only=False))) == 144
    with pytest.raises(ToolError, match="not in the candlelight pool"):
        run(service.pool_set_active("candlelight", "t999", True))
    with pytest.raises(ToolError, match="invalid Spotify id"):
        run(service.pool_set_active("candlelight", "../x", True))


def test_new_song_schedule(svc):
    service, _, _ = svc
    cfg = run(service.config("candlelight"))["config"]
    due = RotationService._new_song_due
    d = dt.date
    assert due(cfg, d(2026, 12, 31), 150, None) == (True, "one new song every day until 2026-12-31")
    assert due(cfg, d(2026, 12, 31), 150, "2026-12-31")[0] is False
    assert due(cfg, d(2027, 1, 5), 150, "2027-01-01")[0] is False  # 4 days: not yet
    assert due(cfg, d(2027, 1, 7), 150, "2027-01-01")[0] is True  # 6 days: due
    assert due(cfg, d(2027, 1, 5), 140, "2027-01-01") == (
        True,
        "active pool 140 is below the floor of 144",
    )
    assert due(cfg, d(2027, 1, 5), 150, None) == (True, "no new song yet")


def test_config_updates_and_validation(svc):
    service, _, _ = svc
    with pytest.raises(ToolError, match="positive"):
        run(service.config("candlelight", artist_cap=0))
    for bad in ("no date", "{date:>999999999}", "{date} {date}", "{date}", "x" * 95 + "{date}"):
        with pytest.raises(ToolError, match="name_template"):
            run(service.config("candlelight", name_template=bad))
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        run(service.config("candlelight", new_song_daily_until="soon"))
    with pytest.raises(ToolError, match="at most 4000"):
        run(service.config("candlelight", taste_brief="x" * 4001))
    res = run(
        service.config(
            "candlelight",
            artist_cap=2,
            taste_brief="Only Sade.",
            name_template="Daily {date} (test)",
        )
    )
    assert res["changed"] == ["artist_cap", "name_template", "taste_brief"]
    again = run(service.config("candlelight"))["config"]
    assert (
        again["artist_cap"] == 2
        and again["taste_brief"] == "Only Sade."
        and again["retention_days"] == 7
    )
    assert run(service.status("candlelight"))["taste_brief"] == "Only Sade."
    dry = run(service.generate("candlelight", dry_run=True))
    assert (
        dry["rule_report"]["artist_max"] <= 2 and dry["playlist_name"] == "Daily 2026-10-19 (test)"
    )


def test_unknown_rotation_points_to_seed_and_seed_creates_it():
    service = RotationService(Store(), catalog(), now=Clock(START))
    with pytest.raises(ToolError, match="rotation_seed"):
        run(service.status("gute-nacht"))
    seeded = run(service.seed("gute-nacht", ["mood"]))
    assert seeded["imported"] == 2
    cfg = run(service.config("gute-nacht"))["config"]
    assert cfg["name_template"] == "gute-nacht {date}" and cfg["taste_brief"] == ""
    assert cfg["track_count"] == 24 and cfg["position_rules"] == []
    assert run(service.status("gute-nacht"))["pool"] == {"active": 2, "inactive": 0}
    renamed = run(service.config("gute-nacht", name_template="Gute Nacht {dd}.{mm}."))
    assert renamed["config"]["name_template"] == "Gute Nacht {dd}.{mm}."


def test_position_rules_and_date_format(svc):
    service, client, _ = svc
    with pytest.raises(ToolError, match="no pool track"):
        run(service.config("candlelight", position_rules=[{"position": 1, "artist": "Nobody"}]))
    with pytest.raises(ToolError, match="position must be"):
        run(service.config("candlelight", position_rules=[{"position": 0, "artist": "Sade"}]))
    with pytest.raises(ToolError, match="twice"):
        run(
            service.config(
                "candlelight",
                position_rules=[
                    {"position": "first", "artist": "Sade"},
                    {"position": 1, "artist": "Sade"},
                ],
            )
        )
    with pytest.raises(ToolError, match="needs position"):
        run(service.config("candlelight", position_rules=[{"artist": "Sade"}]))
    with pytest.raises(ToolError, match="movements must be"):
        run(
            service.config(
                "candlelight", position_rules=[{"position": 1, "artist": "Sade", "movements": [0]}]
            )
        )
    with pytest.raises(ToolError, match="name_template"):
        run(service.config("candlelight", name_template="Daily"))
    res = run(
        service.config(
            "candlelight",
            position_rules=[
                {"position": "last", "artist": "sade", "movements": [4, 3]},
                {"position": "first", "artist": "Sade", "movements": [1, 2]},
            ],
            name_template="1 Intimate - Candlelight {dd}.{mm}.",
        )
    )
    assert res["config"]["position_rules"] == [
        {"position": 1, "artist": "Sade", "movements": [1, 2]},
        {"position": 24, "artist": "Sade", "movements": [3, 4]},
    ]
    dry = run(service.generate("candlelight", dry_run=True))
    assert dry["playlist_name"] == "1 Intimate - Candlelight 19.10."
    assert dry["tracks"][0]["artist"] == "Sade" and dry["tracks"][-1]["artist"] == "Sade"
    assert dry["tracks"][1]["artist"] != "Sade" and dry["tracks"][-2]["artist"] != "Sade"
    assert dry["rule_report"]["artist_max"] <= 3 and dry["relaxations"] == []
    pr = dry["rule_report"]["position_rules"]
    assert [(r["position"], r["met"]) for r in pr] == [(1, True), (24, True)]
    assert pr[0]["from_movement"] in (1, 2) and pr[1]["from_movement"] in (3, 4)
    real = run(service.generate("candlelight"))
    assert client.names("create_playlist")[0][1] == "1 Intimate - Candlelight 19.10."
    assert real["tracks"][0]["artist"] == "Sade" and real["tracks"][-1]["artist"] == "Sade"
    for template, expected in (
        ("Candlelight {date}", "Candlelight 2026-10-19"),
        ("Candlelight {date:%d.%m.}", "Candlelight 19.10."),
        ("CL {yyyy}-{mm}-{dd} {yy}", "CL 2026-10-19 26"),
    ):
        run(service.config("candlelight", name_template=template))
        assert run(service.generate("candlelight", dry_run=True))["playlist_name"] == expected
    cleared = run(service.config("candlelight", position_rules=[]))
    assert cleared["changed"] == ["position_rules"] and cleared["config"]["position_rules"] == []
    assert (
        "position_rules"
        not in run(service.generate("candlelight", "2026-12-01", dry_run=True))["rule_report"]
    )
