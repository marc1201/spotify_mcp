"""Pure tests for the selection engine: bucket boundaries, determinism, and the spec's acceptance
simulation (a year of daily runs with new songs; R1-R4 must hold every single day)."""

from __future__ import annotations

import collections
import copy
import datetime as dt
import random

from spotify_mcp.rotation import engine

CFG = dict(
    size=24, per_movement=6, cap_3_4=4, cap_3_5=12, min_fresh=5, artist_cap=3, lookahead_margin=1
)


def build_pool(seed: int = 1, inactive_artists: tuple[str, ...] = ()) -> list[dict]:
    """143 tracks over six volumes (vol 3 has 23), an artist mix like the real Candlelight pool."""
    r = random.Random(seed)
    artists = (
        ["Sade"] * 15
        + ["Cigarettes After Sex"] * 11
        + ["Beach House"] * 6
        + ["Mazzy Star"] * 6
        + ["Portishead"] * 6
        + ["Hooverphonic"] * 6
        + ["Massive Attack"] * 4
        + ["D'Angelo"] * 4
        + ["FKA twigs"] * 2
        + ["Sabrina Claudio"] * 3
    )
    i = 0
    while len(artists) < 143:
        artists += [f"Artist {i}"] * r.choice([1, 1, 2, 2, 3, 4])
        i += 1
    artists = artists[:143]
    r.shuffle(artists)
    pool, k = [], 0
    for vol in range(1, 7):
        for pos in range(1, (23 if vol == 3 else 24) + 1):
            a = artists[k]
            k += 1
            pool.append(
                dict(
                    id=f"t{k:03d}",
                    artist=a,
                    move=min(4, (pos - 1) // 6 + 1),
                    src_pos=pos,
                    count=0,
                    last=None,
                    active=a not in inactive_artists,
                )
            )
    return pool


def test_bucket_boundaries():
    assert [engine.bucket_of(g) for g in (1, 2, 3, 4, 5, 6, 7, 8, 30)] == [
        "X",
        "X",
        "A",
        "A",
        "B",
        "C6",
        "C7",
        "F",
        "F",
    ]
    assert engine.bucket_of(engine.NEVER_PLAYED) == "F"
    assert engine.gap_of({"last": None}, dt.date(2026, 1, 1)) == engine.NEVER_PLAYED
    assert engine.gap_of({"last": dt.date(2026, 1, 1)}, dt.date(2026, 1, 4)) == 3


def test_same_seed_is_independent_of_pool_order():
    today = dt.date(2026, 11, 1)
    pool = build_pool()
    r = random.Random(5)
    for s in pool:
        s["count"] = r.randint(0, 3)
        s["last"] = today - dt.timedelta(days=r.randint(1, 12))
    shuffled = copy.deepcopy(pool)
    random.Random(9).shuffle(shuffled)
    a = engine.select(pool, today, random.Random("candlelight:2026-11-01"), CFG)
    b = engine.select(shuffled, today, random.Random("candlelight:2026-11-01"), CFG)
    assert [s["id"] for s in a.tracks] == [s["id"] for s in b.tracks]
    assert a.movements == b.movements and a.report == b.report


def simulate(days: int, new_songs: bool = True, inactive: tuple[str, ...] = (), cfg=CFG, rules=()):
    start = dt.date(2026, 10, 19)
    pool = build_pool(inactive_artists=inactive)
    r = random.Random(42)
    last_new = start - dt.timedelta(days=1)
    fails: collections.Counter[str] = collections.Counter()
    relax_days = 0
    spreads = []
    nid = 1000
    for d in range(days):
        today = start + dt.timedelta(days=d)
        active = [s for s in pool if s["active"]]
        due = new_songs and (
            today <= dt.date(2026, 12, 31) or (today - last_new).days >= 6 or len(active) < 144
        )
        debut: list[str] = []
        if due:
            per_artist = collections.Counter(s["artist"] for s in active)
            cands = [
                a for a in [f"New {j}" for j in range(60)] + ["Portishead"] if per_artist[a] < 5
            ]
            nid += 1
            pool.append(
                dict(
                    id=f"n{nid}",
                    artist=r.choice(cands),
                    move=r.randint(1, 4),
                    src_pos=99,
                    count=min(s["count"] for s in active),
                    last=None,
                    active=True,
                )
            )
            debut, last_new = [f"n{nid}"], today
        sel = engine.select(
            pool, today, random.Random(f"candlelight:{today}"), cfg, debut, position_rules=rules
        )
        chosen = sel.tracks
        for rule in rules:
            at = chosen[rule["position"] - 1]
            if at["artist"] != rule["artist"] or sel.pinned.get(rule["position"]) != at["id"]:
                fails["pin"] += 1
        gaps = [engine.gap_of(s, today) for s in chosen]
        if any(g <= 2 for g in gaps):
            fails["R1"] += 1
        if sum(g in (3, 4) for g in gaps) > cfg["cap_3_4"]:
            fails["R2"] += 1
        if sum(g in (3, 4, 5) for g in gaps) > cfg["cap_3_5"]:
            fails["R3"] += 1
        if sum(g >= 8 for g in gaps) < cfg["min_fresh"]:
            fails["R4"] += 1
        if len(chosen) != cfg["size"]:
            fails["short"] += 1
        if max(collections.Counter(s["artist"] for s in chosen).values()) > cfg["artist_cap"]:
            fails["artist"] += 1
        if any(a["artist"] == b["artist"] for a, b in zip(chosen, chosen[1:], strict=False)):
            fails["adjacent"] += 1
        if set(debut) - {s["id"] for s in chosen}:
            fails["debut"] += 1
        moves = collections.Counter(sel.movements[s["id"]] for s in chosen)
        if not sel.relaxations and any(moves[m] != cfg["per_movement"] for m in range(1, 5)):
            fails["movement"] += 1
        relax_days += bool(sel.relaxations)
        for s in chosen:
            s["count"] += 1
            s["last"] = today
        if d >= 30:
            counts = [s["count"] for s in pool if s["active"]]
            spreads.append(max(counts) - min(counts))
    return dict(fails), relax_days, max(spreads, default=0)


def test_year_of_daily_runs_holds_every_rule():
    fails, relax_days, _spread = simulate(439)
    assert fails == {}
    assert relax_days == 0


def test_year_with_vocal_filter_tracks_inactive():
    fails, _relax, _spread = simulate(439, inactive=("FKA twigs", "Sabrina Claudio"))
    assert fails == {}


def test_no_new_songs_only_borrows_movements():
    fails, relax_days, _spread = simulate(365, new_songs=False)
    assert fails == {}
    assert 0 < relax_days < 60  # fallbacks happen, rules never break


def test_order_separates_artists_and_keeps_movement_sequence():
    chosen = [
        dict(id="a1", artist="Sade", move=1, src_pos=1),
        dict(id="a2", artist="Sade", move=1, src_pos=2),
        dict(id="a3", artist="Sade", move=1, src_pos=3),
        dict(id="b1", artist="Beach House", move=1, src_pos=4),
        dict(id="c1", artist="Portishead", move=1, src_pos=5),
        dict(id="d1", artist="Mazzy Star", move=1, src_pos=6),
        dict(id="e1", artist="Sade", move=2, src_pos=7),
        dict(id="f1", artist="Cat Power", move=2, src_pos=8),
    ]
    out = engine.order(chosen, {s["id"]: s["move"] for s in chosen})
    assert [s["id"] for s in out][:6] != ["a1", "a2", "a3", "b1", "c1", "d1"]
    assert all(a["artist"] != b["artist"] for a, b in zip(out, out[1:], strict=False))
    assert [s["move"] for s in out] == sorted(s["move"] for s in out)


def test_position_rules_pin_first_and_last_every_day():
    pool = build_pool()
    assert any(s["artist"] == "Sade" and s["move"] in (1, 2) for s in pool)
    assert any(s["artist"] == "Sade" and s["move"] in (3, 4) for s in pool)
    rules = [
        {"position": 1, "artist": "Sade", "movements": [1, 2]},
        {"position": 24, "artist": "Sade", "movements": [3, 4]},
    ]
    fails, relax_days, _spread = simulate(120, rules=rules)
    assert fails == {}  # includes the pin check and the adjacency check (slots 2 and 23 not Sade)
    assert relax_days == 0


def test_position_rule_borrows_or_relaxes_when_it_cannot_be_met():
    pool = build_pool()
    today = dt.date(2026, 11, 1)
    lonely = next(
        s for s in pool if s["move"] == 2 and sum(t["artist"] == s["artist"] for t in pool) == 1
    )
    rule = {"position": 1, "artist": lonely["artist"]}
    sel = engine.select(pool, today, random.Random(1), CFG, position_rules=[rule])
    assert sel.tracks[0]["id"] == lonely["id"] and sel.movements[lonely["id"]] == 1
    assert sel.relaxations == [f"position 1: {lonely['artist']} borrowed from movement 2"]
    sel = engine.select(
        pool, today, random.Random(1), CFG, position_rules=[{**rule, "movements": [1, 2]}]
    )
    assert sel.tracks[0]["id"] == lonely["id"] and sel.relaxations == []
    sel = engine.select(
        pool, today, random.Random(1), CFG, position_rules=[{"position": 24, "artist": "Nobody"}]
    )
    assert len(sel.tracks) == 24 and sel.pinned == {}
    assert sel.relaxations == ["position 24: no eligible Nobody track"]
