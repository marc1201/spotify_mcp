"""Compact normalizers — trim Spotify's verbose JSON to the fields useful to an LLM, keeping
tool outputs small and token-cheap. All field access is defensive (.get()) because the RESTRICTED
regime strips fields like popularity / available_markets / explicit and /me email/country/product.
"""

from __future__ import annotations

from typing import Any

Json = dict[str, Any]


def _names(arr: list[Json] | None) -> list[str]:
    return [a.get("name", "") for a in (arr or [])]


def artist(a: Json | None) -> Json | None:
    if not a:
        return None
    out = {"name": a.get("name"), "id": a.get("id"), "uri": a.get("uri")}
    if a.get("genres"):
        out["genres"] = a["genres"]
    if a.get("popularity") is not None:
        out["popularity"] = a["popularity"]
    if (a.get("followers") or {}).get("total") is not None:
        out["followers"] = a["followers"]["total"]
    return out


def album(a: Json | None) -> Json | None:
    if not a:
        return None
    return {
        "name": a.get("name"),
        "id": a.get("id"),
        "uri": a.get("uri"),
        "artists": _names(a.get("artists")),
        "release_date": a.get("release_date"),
        "total_tracks": a.get("total_tracks"),
        "album_type": a.get("album_type"),
    }


def track(t: Json | None) -> Json | None:
    if not t:
        return None
    out = {
        "name": t.get("name"),
        "id": t.get("id"),
        "uri": t.get("uri"),
        "artists": _names(t.get("artists")),
        "album": (t.get("album") or {}).get("name"),
        "duration_ms": t.get("duration_ms"),
    }
    if t.get("explicit") is not None:
        out["explicit"] = t["explicit"]
    if t.get("popularity") is not None:
        out["popularity"] = t["popularity"]
    return out


def episode(e: Json | None) -> Json | None:
    if not e:
        return None
    return {
        "name": e.get("name"),
        "id": e.get("id"),
        "uri": e.get("uri"),
        "show": (e.get("show") or {}).get("name"),
        "duration_ms": e.get("duration_ms"),
        "release_date": e.get("release_date"),
    }


def show(s: Json | None) -> Json | None:
    if not s:
        return None
    return {
        "name": s.get("name"),
        "id": s.get("id"),
        "uri": s.get("uri"),
        "publisher": s.get("publisher"),
        "total_episodes": s.get("total_episodes"),
    }


def playlist(p: Json | None) -> Json | None:
    if not p:
        return None
    tracks = p.get("tracks") or p.get("items") or {}
    return {
        "name": p.get("name"),
        "id": p.get("id"),
        "uri": p.get("uri"),
        "owner": (p.get("owner") or {}).get("display_name") or (p.get("owner") or {}).get("id"),
        "public": p.get("public"),
        "collaborative": p.get("collaborative"),
        "description": p.get("description"),
        "tracks": tracks.get("total") if isinstance(tracks, dict) else None,
    }


def device(d: Json | None) -> Json | None:
    if not d:
        return None
    return {
        "id": d.get("id"),
        "name": d.get("name"),
        "type": d.get("type"),
        "is_active": d.get("is_active"),
        "volume_percent": d.get("volume_percent"),
        "supports_volume": d.get("supports_volume"),
    }


def item(d: Json | None) -> Json | None:
    """Dispatch a heterogeneous object (search/queue mixes types) to the right normalizer."""
    if not d:
        return None
    kind = d.get("type")
    return {
        "track": track,
        "episode": episode,
        "artist": artist,
        "album": album,
        "show": show,
        "playlist": playlist,
    }.get(kind, lambda x: {"type": kind, "name": x.get("name"), "uri": x.get("uri")})(d)


def playback(state: Json | None) -> Json:
    """Normalize GET /me/player (or currently-playing). Empty body => nothing active."""
    if not state:
        return {"active": False}
    return {
        "active": True,
        "is_playing": state.get("is_playing"),
        "progress_ms": state.get("progress_ms"),
        "repeat_state": state.get("repeat_state"),
        "shuffle_state": state.get("shuffle_state"),
        "device": device(state.get("device")),
        "context": (state.get("context") or {}).get("uri"),
        "item": item(state.get("item")),
    }
