"""Pure-function unit tests — no network, no secrets. Locks in the security-critical id parsing
and the defensive normalizers (which must tolerate fields the RESTRICTED regime strips)."""

from __future__ import annotations

import pytest

from spotify_mcp import models
from spotify_mcp.client import parse_id, split_ids_and_uris, spotify_uri
from spotify_mcp.errors import SpotifyBadRequest
from spotify_mcp.regime import Regime, playlist_items_segment

ID = "4iV5W9uYEdYUVa79Axb7Rh"


@pytest.mark.parametrize(
    "raw,expected",
    [
        (ID, ID),
        (f"spotify:track:{ID}", ID),
        (f"https://open.spotify.com/track/{ID}?si=abcd", ID),
        ("some.user_name", "some.user_name"),  # user ids may contain . and _
    ],
)
def test_parse_id_accepts_valid(raw, expected):
    assert parse_id(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "../../users/victim",
        f"spotify:track:../../playlists/{ID}/followers",
        "a/b",
        "",
        "..",
        ".",
        "with space",
        "-",
    ],
)
def test_parse_id_rejects_traversal_and_junk(bad):
    with pytest.raises(SpotifyBadRequest):
        parse_id(bad)


def test_spotify_uri_and_split():
    assert spotify_uri("track", ID) == f"spotify:track:{ID}"
    assert spotify_uri("track", f"spotify:track:{ID}") == f"spotify:track:{ID}"
    ids, uris = split_ids_and_uris("album", [ID, f"spotify:album:{ID}"])
    assert ids == [ID, ID]
    assert uris == [f"spotify:album:{ID}", f"spotify:album:{ID}"]


def test_regime_segment():
    assert playlist_items_segment(Regime.FULL) == "tracks"
    assert playlist_items_segment(Regime.RESTRICTED) == "items"


def test_models_are_defensive():
    # RESTRICTED strips popularity/available_markets/etc — normalizers must use .get(), never KeyError.
    assert models.track(None) is None
    assert models.track({"name": "t", "artists": [{"name": "a"}]})["artists"] == ["a"]
    assert models.playback(None) == {"active": False}
    pb = models.playback(
        {"is_playing": True, "item": {"type": "track", "name": "t", "artists": []}}
    )
    assert pb["active"] is True
    assert pb["item"]["name"] == "t"
    assert models.device({"id": "d", "name": "n", "type": "Computer"})["id"] == "d"
    # item() dispatches on type
    assert models.item({"type": "artist", "name": "A"})["name"] == "A"


def test_remove_playlist_items_request_shape_by_regime():
    """Regression: the remove DELETE body key is `tracks` (full /tracks) vs `items` (restricted
    /items) — same {"uri": ...} object shape, both in the request body (confirmed vs the API ref)."""
    import asyncio

    import httpx

    from spotify_mcp.client import SpotifyClient
    from spotify_mcp.regime import Regime

    client = SpotifyClient(httpx.AsyncClient())
    captured: dict = {}

    async def fake_json(method, path, **kw):
        captured.clear()
        captured.update(method=method, path=path, json=kw.get("json"), params=kw.get("params"))
        return {"snapshot_id": "s"}

    client._json = fake_json  # type: ignore[method-assign]

    client._regime = Regime.RESTRICTED
    asyncio.run(client.remove_playlist_items("PL", ["spotify:track:abc"]))
    assert captured["path"].endswith("/items")
    assert captured["json"] == {"items": [{"uri": "spotify:track:abc"}]}
    assert captured["params"] is None

    client._regime = Regime.FULL
    asyncio.run(client.remove_playlist_items("PL", ["spotify:track:abc"]))
    assert captured["path"].endswith("/tracks")
    assert captured["json"] == {"tracks": [{"uri": "spotify:track:abc"}]}
    assert captured["params"] is None


def test_library_and_follow_use_query_uris_in_restricted():
    """Restricted /me/library save/remove/follow pass uris as a comma-separated QUERY param
    (not a JSON body), per the 2026 consolidated endpoints."""
    import asyncio

    import httpx

    from spotify_mcp.client import SpotifyClient
    from spotify_mcp.regime import Regime

    client = SpotifyClient(httpx.AsyncClient())
    client._regime = Regime.RESTRICTED
    captured: dict = {}

    async def fake_json(method, path, **kw):
        captured.clear()
        captured.update(method=method, path=path, json=kw.get("json"), params=kw.get("params"))
        return None

    client._json = fake_json  # type: ignore[method-assign]

    asyncio.run(client.save_to_library("track", ["spotify:track:abc"]))
    assert (captured["method"], captured["path"]) == ("PUT", "/me/library")
    assert captured["json"] is None and captured["params"] == {"uris": "spotify:track:abc"}

    asyncio.run(client.remove_from_library("track", ["spotify:track:abc"]))
    assert captured["method"] == "DELETE" and captured["params"] == {"uris": "spotify:track:abc"}

    asyncio.run(client.follow("artist", ["spotify:artist:xyz"]))
    assert captured["method"] == "PUT" and captured["params"] == {"uris": "spotify:artist:xyz"}

    asyncio.run(client.follow_playlist("PL"))
    assert (captured["method"], captured["path"]) == ("PUT", "/me/library")
    assert captured["params"] == {"uris": "spotify:playlist:PL"}
