"""Selection algorithm for the daily rotation (spec steps 1-8). Pure Python, no I/O.

select() picks the day's tracks and order() sequences them. Ported from Marc's reference
rotation_v2.py with two additions: the pool is sorted by track id before the seeded tiebreak is
drawn, so the result depends only on (pool contents, date, seed) and never on the order rows came
out of the database; and position rules ("slot 1 must be Sade") reserve their track first and pin
it in the final order.

A pool track is a dict: id, artist (primary artist), move (1-4), src_pos (order inside its
movement), count (effective play count), last (date of its last daily playlist, or None), active.
Nothing here mutates the pool; the caller persists the result.

Gap = days since the track's last daily playlist (never played counts as rested). Buckets:
  X   gap 1-2          excluded (R1, never relaxed)
  A   gap 3-4          capped by R2 and R3
  B   gap 5            capped by R3
  C6  gap 6            free
  C7  gap 7            free, but it is tomorrow's rested supply (the lookahead protects it)
  F   gap 8+ or never  rested (R4 needs min_fresh of these)
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Collection, Iterable
from random import Random
from typing import Any, NamedTuple

Track = dict[str, Any]
Pins = dict[str, tuple[int, int]]  # track id -> (movement, slot index inside that movement)

NEVER_PLAYED = 10**6  # larger than any real gap


class Selection(NamedTuple):
    tracks: list[Track]  # in playing order
    movements: dict[str, int]  # track id -> movement it plays in (differs from its own if borrowed)
    report: dict[str, int]  # R2_gap_3_4, R3_gap_3_5, R4_rested, artist_max
    relaxations: list[str]
    pinned: dict[int, str]  # final 1-based position -> track id placed there by a position rule


def gap_of(track: Track, today: dt.date) -> int:
    last = track.get("last")
    return NEVER_PLAYED if last is None else (today - last).days


def bucket_of(gap: int) -> str:
    if gap <= 2:
        return "X"
    if gap <= 4:
        return "A"
    if gap == 5:
        return "B"
    if gap == 6:
        return "C6"
    if gap == 7:
        return "C7"
    return "F"


def select(
    pool: Iterable[Track],
    today: dt.date,
    rng: Random,
    cfg: dict[str, int],
    debut_ids: Collection[str] = (),
    position_rules: Iterable[dict[str, Any]] = (),
) -> Selection:
    """Pick cfg['size'] tracks for `today`. cfg keys: size, movements (default 4), per_movement,
    cap_3_4, cap_3_5, min_fresh, artist_cap, lookahead_margin. `rng` must be seeded by the caller with the rotation
    and the date so that a dry run and the real run agree. Each position rule is a dict
    {position: 1-based slot, artist: primary artist, movements?: the movements its track may come
    from (default: the slot's own)}; its track is reserved before anything else, least played
    first, and a rule that cannot be met is logged in relaxations, never an error."""
    pool = sorted(pool, key=lambda s: s["id"])
    size, per_movement = cfg["size"], cfg["per_movement"]
    n_movements = cfg.get("movements", 4)
    cap_34, cap_345, min_fresh = cfg["cap_3_4"], cfg["cap_3_5"], cfg["min_fresh"]
    artist_cap, margin = cfg["artist_cap"], cfg["lookahead_margin"]

    def bucket(s: Track) -> str:
        return bucket_of(gap_of(s, today))

    elig = [s for s in pool if s["active"] and bucket(s) != "X"]
    jitter = {s["id"]: rng.random() for s in elig}

    def key(s: Track) -> tuple[int, int, float]:
        # Priority: least played, then longest gap, then the seeded random tiebreak.
        return (s["count"], -min(gap_of(s, today), 999), jitter[s["id"]])

    supply = sum(1 for s in elig if bucket(s) in ("C7", "F"))  # tomorrow's rested candidates
    chosen: list[Track] = []
    slot: dict[str, int] = {}
    pins: Pins = {}
    per_move: Counter[int] = Counter()
    per_artist: Counter[str] = Counter()
    per_bucket: Counter[str] = Counter()
    used_supply = 0
    relaxations: list[str] = []

    def ok(s: Track, relax_move: bool = False, protect: bool = True) -> bool:
        b = bucket(s)
        if s["id"] in slot or per_artist[s["artist"]] >= artist_cap:
            return False
        if not relax_move and per_move[s["move"]] >= per_movement:
            return False
        if b == "A" and (per_bucket["A"] >= cap_34 or per_bucket["A"] + per_bucket["B"] >= cap_345):
            return False
        if b == "B" and per_bucket["A"] + per_bucket["B"] >= cap_345:
            return False
        if protect and b in ("C7", "F") and supply - used_supply - 1 < min_fresh + margin:
            # Lookahead: leave enough rested tracks for tomorrow, unless today's own quota needs it.
            if not (b == "F" and per_bucket["F"] < min_fresh):
                return False
        return True

    def add(s: Track, move: int | None = None) -> None:
        nonlocal used_supply
        chosen.append(s)
        slot[s["id"]] = move or s["move"]
        per_move[slot[s["id"]]] += 1
        per_artist[s["artist"]] += 1
        per_bucket[bucket(s)] += 1
        if bucket(s) in ("C7", "F"):
            used_supply += 1

    def fill(cands: Iterable[Track], stop=None, **kw: bool) -> None:
        for s in sorted(cands, key=key):
            if len(chosen) >= size or (stop is not None and stop()):
                break
            if ok(s, **kw):
                add(s)

    def of(*buckets: str) -> list[Track]:
        return [s for s in elig if bucket(s) in buckets]

    for rule in position_rules:  # 2. position rules reserve their slot's track first
        pos, artist = int(rule["position"]), str(rule["artist"])
        m, local = (pos - 1) // per_movement + 1, (pos - 1) % per_movement
        allowed = set(rule.get("movements") or [m])  # movements the track may come from
        cands = sorted((s for s in elig if s["artist"].casefold() == artist.casefold()), key=key)
        own = [s for s in cands if s["move"] in allowed]
        pick = next((s for s in own if ok(s, relax_move=True)), None) or next(
            (s for s in own if ok(s, relax_move=True, protect=False)), None
        )
        if pick is None:
            pick = next((s for s in cands if ok(s, relax_move=True, protect=False)), None)
            if pick is not None:
                relaxations.append(
                    f"position {pos}: {artist} borrowed from movement {pick['move']}"
                )
        if pick is None:
            relaxations.append(f"position {pos}: no eligible {artist} track")
            continue
        add(pick, move=m)
        pins[pick["id"]] = (m, local)
    for s in elig:  # 3. debuts
        if s["id"] in debut_ids and ok(s, protect=False):
            add(s)
    fill(of("F"), stop=lambda: per_bucket["F"] >= min_fresh)  # 4. rested quota (R4)
    fill(of("A"))  # 5. short gaps up to the caps (R2, R3)
    fill(of("B"))
    fill(of("C6"))  # 6. the rest; gap 7 and rested only while tomorrow stays covered
    fill(of("C7", "F"))
    if len(chosen) < size:  # 7. fallbacks, each logged
        before = len(chosen)
        fill(elig, protect=False)
        if len(chosen) > before:
            relaxations.append("lookahead")
    if len(chosen) < size:
        for m in sorted(range(1, n_movements + 1), key=lambda m: per_move[m]):
            while per_move[m] < per_movement and len(chosen) < size:
                c = next(
                    (s for s in sorted(elig, key=key) if ok(s, relax_move=True, protect=False)),
                    None,
                )
                if c is None:
                    break
                add(c, move=m)
                relaxations.append(f"movement {m} borrowed {c['id']}")
    if per_bucket["F"] < min_fresh:
        relaxations.append(f"R4 only {per_bucket['F']} rested")
    if len(chosen) < size:
        relaxations.append(f"short list: {len(chosen)} of {size}")
    report = {
        "R2_gap_3_4": per_bucket["A"],
        "R3_gap_3_5": per_bucket["A"] + per_bucket["B"],
        "R4_rested": per_bucket["F"],
        "artist_max": max(per_artist.values(), default=0),
    }
    ordered = order(chosen, slot, pins, n_movements)
    pinned = {i + 1: s["id"] for i, s in enumerate(ordered) if s["id"] in pins}
    return Selection(ordered, slot, report, relaxations, pinned)


def order(
    chosen: list[Track], slot: dict[str, int], pins: Pins | None = None, movements: int = 4
) -> list[Track]:
    """Movements in order; inside a movement, source order unless that would repeat an artist, with
    pinned tracks fixed at their slot. Each free slot takes the earliest track whose artist differs
    from both neighbours, preferring the artist with the most tracks still waiting, so repeats are
    spread out instead of piling up at the end."""
    pins = pins or {}
    out: list[Track] = []
    for m in range(1, movements + 1):
        tracks = [s for s in chosen if slot[s["id"]] == m]
        n = len(tracks)
        fixed: dict[int, Track] = {}
        for s in tracks:
            if s["id"] not in pins:
                continue
            i = min(pins[s["id"]][1], n - 1)
            if (
                i in fixed
            ):  # a short movement squeezed two pins together: take the nearest free slot
                i = next(j for j in [*range(i - 1, -1, -1), *range(i + 1, n)] if j not in fixed)
            fixed[i] = s
        left = sorted((s for s in tracks if s["id"] not in pins), key=lambda s: s.get("src_pos", 0))
        seq: list[Track] = []
        for i in range(n):
            if i in fixed:
                seq.append(fixed[i])
                continue
            prev = seq[-1]["artist"] if seq else (out[-1]["artist"] if out else None)
            nxt = fixed[i + 1]["artist"] if (i + 1) in fixed else None
            waiting = Counter(s["artist"] for s in left)
            options = (
                [s for s in left if s["artist"] != prev and s["artist"] != nxt]
                or [s for s in left if s["artist"] != prev]
                or left
            )
            most = max(waiting[s["artist"]] for s in options)
            pick = (
                next(s for s in options if waiting[s["artist"]] == most) if most > 1 else options[0]
            )
            left.remove(pick)
            seq.append(pick)
        out.extend(seq)
    return out
