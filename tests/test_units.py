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


def test_remove_playlist_items_body_shape_by_regime():
    """Regression: restricted /items DELETE needs {"uris":[...]} (not the classic {"tracks":[...]})."""
    import asyncio

    import httpx

    from spotify_mcp.client import SpotifyClient
    from spotify_mcp.regime import Regime

    client = SpotifyClient(httpx.AsyncClient())
    captured: dict = {}

    async def fake_json(method, path, **kw):
        captured.update(method=method, path=path, json=kw.get("json"))
        return {"snapshot_id": "s"}

    client._json = fake_json  # type: ignore[method-assign]

    client._regime = Regime.RESTRICTED
    asyncio.run(client.remove_playlist_items("PL", ["spotify:track:abc"]))
    assert captured["path"].endswith("/items")
    assert captured["json"] == {"uris": ["spotify:track:abc"]}

    client._regime = Regime.FULL
    asyncio.run(client.remove_playlist_items("PL", ["spotify:track:abc"]))
    assert captured["path"].endswith("/tracks")
    assert captured["json"] == {"tracks": [{"uri": "spotify:track:abc"}]}
