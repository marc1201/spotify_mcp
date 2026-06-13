"""Async Spotify Web API client.

Responsibilities (the ONLY place regime-routing lives):
- pull the user's Spotify bearer token fresh from FastMCP per request (never stored),
- detect the dev-mode regime once (lazily, on first authed call) and route FULL vs RESTRICTED paths,
- retry on 429 (honoring Retry-After) and transient 5xx,
- normalize errors and convert them to FastMCP ToolError with actionable text at the public boundary.

Tools call the high-level methods here; they never branch on regime or build raw HTTP.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Iterable, Sequence
from typing import Any

import httpx
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from . import models
from .errors import (
    SpotifyAuthError,
    SpotifyBadRequest,
    SpotifyError,
    SpotifyRateLimited,
    normalize_http_error,
)
from .regime import Regime, RestrictedUnsupported, playlist_items_segment

API = "https://api.spotify.com/v1"

Json = dict[str, Any]


# --------------------------------------------------------------------------- module helpers


_ID_EXTRA = frozenset("._~-")


def parse_id(id_or_uri: str) -> str:
    """Extract a bare id from a bare id, `spotify:type:id` URI, or open.spotify.com URL.

    Rejects ids containing path separators (e.g. '../../users/x'), closing a traversal /
    host-confusion vector when the id is interpolated into an endpoint path template.
    """
    s = str(id_or_uri).strip()
    if s.startswith("spotify:"):
        s = s.split(":")[-1]
    elif "open.spotify.com/" in s:
        s = s.rstrip("/").split("/")[-1].split("?")[0]
    if not any(c.isalnum() for c in s) or not all(c.isalnum() or c in _ID_EXTRA for c in s):
        raise SpotifyBadRequest(f"invalid Spotify id/uri: {id_or_uri!r}", status=400)
    return s


def spotify_uri(item_type: str, id_or_uri: str) -> str:
    s = str(id_or_uri).strip()
    return s if s.startswith("spotify:") else f"spotify:{item_type}:{parse_id(s)}"


def split_ids_and_uris(item_type: str, items: Iterable[str]) -> tuple[list[str], list[str]]:
    items = list(items)
    return [parse_id(x) for x in items], [spotify_uri(item_type, x) for x in items]


def _plural(item_type: str) -> str:
    return f"{item_type}s"


def _dev(device_id: str | None) -> Json | None:
    return {"device_id": device_id} if device_id else None


def _snapshot(data: Json | None) -> Json:
    return {"ok": True, "snapshot_id": (data or {}).get("snapshot_id")}


def _assert_spotify_url(url: str) -> None:
    # SSRF guard: pagination follows server-provided `next` URLs — they must stay on the API host.
    if not url.startswith("https://api.spotify.com/"):
        raise SpotifyError(f"refusing non-Spotify URL: {url[:64]}")


def _api(fn):
    """Convert internal Spotify errors into clean, actionable FastMCP ToolErrors at the boundary."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except RestrictedUnsupported as exc:
            msg = f"'{exc.op}' is unavailable: this Spotify app is in restricted dev-mode."
            if exc.alternative:
                msg += f" {exc.alternative}"
            raise ToolError(msg) from None
        except ToolError:
            raise
        except SpotifyError as exc:
            raise ToolError(exc.user_message) from None

    return wrapper


# --------------------------------------------------------------------------- process-wide holder

_CLIENT: SpotifyClient | None = None


def set_client(client: SpotifyClient) -> None:
    global _CLIENT
    _CLIENT = client


def get_client() -> SpotifyClient:
    """Return the process-wide client (set in the server lifespan; lazily created as a fallback)."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = SpotifyClient(
            httpx.AsyncClient(http2=True, timeout=httpx.Timeout(20.0, read=60.0))
        )
    return _CLIENT


# --------------------------------------------------------------------------- the client


class SpotifyClient:
    def __init__(self, http: httpx.AsyncClient):
        self._http = http
        self._regime: Regime | None = None
        self._regime_lock = asyncio.Lock()

    # ---- low level ----------------------------------------------------------

    def _token(self) -> str:
        tok = get_access_token()
        if tok is None or not tok.token:
            raise SpotifyAuthError("no access token in request context", status=401)
        return tok.token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Json | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: Json | None = None,
        expected: Sequence[int] = (200, 201, 202, 204),
    ) -> httpx.Response:
        if path.startswith("http"):
            _assert_spotify_url(path)
            url = path
        else:
            url = f"{API}{path}"
        hdrs = {"Authorization": f"Bearer {self._token()}"}
        if headers:
            hdrs.update(headers)
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        for attempt in range(4):
            resp = await self._http.request(
                method, url, params=params, json=json, content=content, headers=hdrs
            )
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", "1"))
                if attempt < 3 and wait <= 10:
                    await asyncio.sleep(wait + 0.25)
                    continue
                raise SpotifyRateLimited(wait)
            if resp.status_code in (500, 502, 503) and attempt < 3:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code in expected:
                return resp
            raise normalize_http_error(resp)
        raise SpotifyError("exhausted retries contacting Spotify")

    async def _json(self, method: str, path: str, **kw) -> Any:
        resp = await self._request(method, path, **kw)
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except Exception:
            return None

    # ---- regime -------------------------------------------------------------

    async def regime(self) -> Regime:
        if self._regime is not None:
            return self._regime
        async with self._regime_lock:
            if self._regime is None:
                # /markets exists only on FULL apps; 403/404 => RESTRICTED.
                resp = await self._request("GET", "/markets", expected=(200, 403, 404))
                self._regime = Regime.FULL if resp.status_code == 200 else Regime.RESTRICTED
            return self._regime

    async def _ensure_full(self, op: str, alternative: str = "") -> None:
        if await self.regime() is not Regime.FULL:
            raise RestrictedUnsupported(op, alternative)

    async def _me_id(self) -> str:
        # The verifier already resolved the Spotify user id onto the token (per-request, correct
        # per-user). Prefer it; fall back to /me only if the token carries no subject.
        tok = get_access_token()
        if tok and tok.subject:
            return tok.subject
        uid = (await self._json("GET", "/me") or {}).get("id")
        if not uid:
            raise SpotifyError("could not resolve current user id")
        return uid

    # ---- player (identical in both regimes) --------------------------------

    @_api
    async def playback_state(self) -> Json:
        return models.playback(await self._json("GET", "/me/player"))

    @_api
    async def currently_playing(self) -> Json:
        return models.playback(await self._json("GET", "/me/player/currently-playing"))

    @_api
    async def devices(self) -> list[Json | None]:
        data = await self._json("GET", "/me/player/devices")
        return [models.device(d) for d in (data or {}).get("devices", [])]

    @_api
    async def transfer(self, device_id: str, play: bool = True) -> Json:
        await self._json("PUT", "/me/player", json={"device_ids": [device_id], "play": play})
        return {"ok": True, "transferred_to": device_id}

    @_api
    async def play(
        self,
        device_id: str | None = None,
        uris: list[str] | None = None,
        context_uri: str | None = None,
        offset_position: int | None = None,
        position_ms: int | None = None,
    ) -> Json:
        body: Json = {}
        if uris:
            body["uris"] = uris
        if context_uri:
            body["context_uri"] = context_uri
        if offset_position is not None:
            body["offset"] = {"position": offset_position}
        if position_ms is not None:
            body["position_ms"] = position_ms
        await self._json("PUT", "/me/player/play", params=_dev(device_id), json=body or None)
        return {"ok": True}

    @_api
    async def pause(self, device_id: str | None = None) -> Json:
        await self._json("PUT", "/me/player/pause", params=_dev(device_id))
        return {"ok": True}

    @_api
    async def next(self, device_id: str | None = None) -> Json:
        await self._json("POST", "/me/player/next", params=_dev(device_id))
        return {"ok": True}

    @_api
    async def previous(self, device_id: str | None = None) -> Json:
        await self._json("POST", "/me/player/previous", params=_dev(device_id))
        return {"ok": True}

    @_api
    async def seek(self, position_ms: int, device_id: str | None = None) -> Json:
        params = {"position_ms": position_ms, "device_id": device_id}
        await self._json("PUT", "/me/player/seek", params=params)
        return {"ok": True, "position_ms": position_ms}

    @_api
    async def set_volume(self, volume_percent: int, device_id: str | None = None) -> Json:
        vol = max(0, min(100, volume_percent))
        await self._json(
            "PUT", "/me/player/volume", params={"volume_percent": vol, "device_id": device_id}
        )
        return {"ok": True, "volume_percent": vol}

    @_api
    async def set_repeat(self, state: str, device_id: str | None = None) -> Json:
        if state not in ("track", "context", "off"):
            raise SpotifyBadRequest("repeat state must be track|context|off", status=400)
        await self._json(
            "PUT", "/me/player/repeat", params={"state": state, "device_id": device_id}
        )
        return {"ok": True, "repeat": state}

    @_api
    async def set_shuffle(self, state: bool, device_id: str | None = None) -> Json:
        flag = "true" if state else "false"
        await self._json(
            "PUT", "/me/player/shuffle", params={"state": flag, "device_id": device_id}
        )
        return {"ok": True, "shuffle": state}

    @_api
    async def queue(self) -> Json:
        data = await self._json("GET", "/me/player/queue")
        if not data:
            return {"currently_playing": None, "queue": []}
        return {
            "currently_playing": models.item(data.get("currently_playing")),
            "queue": [models.item(x) for x in data.get("queue", [])],
        }

    @_api
    async def add_to_queue(self, uri: str, device_id: str | None = None) -> Json:
        await self._json("POST", "/me/player/queue", params={"uri": uri, "device_id": device_id})
        return {"ok": True, "queued": uri}

    @_api
    async def recently_played(
        self, limit: int = 20, after: int | None = None, before: int | None = None
    ) -> list[Json]:
        params = {"limit": min(50, max(1, limit)), "after": after, "before": before}
        data = await self._json("GET", "/me/player/recently-played", params=params)
        return [
            {"track": models.track(x.get("track")), "played_at": x.get("played_at")}
            for x in (data or {}).get("items", [])
        ]

    # ---- search & lookup ----------------------------------------------------

    @_api
    async def search(
        self, q: str, types: list[str], limit: int = 10, market: str | None = None, offset: int = 0
    ) -> Json:
        cap = 50 if await self.regime() is Regime.FULL else 10
        params = {
            "q": q,
            "type": ",".join(types),
            "limit": min(cap, max(1, limit)),
            "offset": offset,
        }
        if market:
            params["market"] = market
        data = await self._json("GET", "/search", params=params) or {}
        out: Json = {}
        for t in types:
            block = data.get(f"{t}s") or {}
            out[f"{t}s"] = [models.item(x) for x in block.get("items", []) if x]
        return out

    @_api
    async def get_item(
        self, item_type: str, id_or_uri: str, market: str | None = None
    ) -> Json | None:
        params = {"market": market} if market else None
        data = await self._json(
            "GET", f"/{_plural(item_type)}/{parse_id(id_or_uri)}", params=params
        )
        return models.item(data)

    @_api
    async def get_items_batch(self, item_type: str, ids: list[str]) -> list[Json | None]:
        await self._ensure_full("get_items_batch", "Fetch items one at a time with get_item.")
        idlist = [parse_id(x) for x in ids]
        data = await self._json("GET", f"/{_plural(item_type)}", params={"ids": ",".join(idlist)})
        return [models.item(x) for x in (data or {}).get(_plural(item_type), []) if x]

    @_api
    async def album_tracks(
        self, album_id: str, limit: int = 50, offset: int = 0, market: str | None = None
    ) -> list[Json | None]:
        params = {"limit": min(50, limit), "offset": offset, "market": market}
        data = await self._json("GET", f"/albums/{parse_id(album_id)}/tracks", params=params)
        return [models.track(x) for x in (data or {}).get("items", [])]

    @_api
    async def artist_albums(
        self, artist_id: str, include_groups: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[Json | None]:
        params = {"limit": min(50, limit), "offset": offset, "include_groups": include_groups}
        data = await self._json("GET", f"/artists/{parse_id(artist_id)}/albums", params=params)
        return [models.album(x) for x in (data or {}).get("items", [])]

    @_api
    async def artist_top_tracks(
        self, artist_id: str, market: str = "from_token"
    ) -> list[Json | None]:
        await self._ensure_full("artist_top_tracks", "Use search to find an artist's tracks.")
        data = await self._json(
            "GET", f"/artists/{parse_id(artist_id)}/top-tracks", params={"market": market}
        )
        return [models.track(x) for x in (data or {}).get("tracks", [])]

    # ---- playlists ----------------------------------------------------------

    async def _playlist_items_url(self, playlist_id: str) -> str:
        """Regime-aware URL for a playlist's items/tracks endpoint."""
        seg = playlist_items_segment(await self.regime())
        return f"/playlists/{parse_id(playlist_id)}/{seg}"

    @_api
    async def my_playlists(self, limit: int = 50, offset: int = 0) -> list[Json | None]:
        data = await self._json(
            "GET", "/me/playlists", params={"limit": min(50, limit), "offset": offset}
        )
        return [models.playlist(x) for x in (data or {}).get("items", [])]

    @_api
    async def get_playlist(self, playlist_id: str, market: str | None = None) -> Json | None:
        params = {"market": market} if market else None
        return models.playlist(
            await self._json("GET", f"/playlists/{parse_id(playlist_id)}", params=params)
        )

    @_api
    async def playlist_items(
        self, playlist_id: str, limit: int = 50, offset: int = 0, market: str | None = None
    ) -> list[Json]:
        params = {"limit": min(50, limit), "offset": offset, "market": market}
        data = await self._json("GET", await self._playlist_items_url(playlist_id), params=params)
        out = []
        for row in (data or {}).get("items", []):
            obj = row.get("track") or row.get("item") or row
            out.append({"added_at": row.get("added_at"), "item": models.item(obj)})
        return out

    @_api
    async def create_playlist(
        self,
        name: str,
        public: bool = False,
        collaborative: bool = False,
        description: str | None = None,
    ) -> Json | None:
        body: Json = {"name": name, "public": public, "collaborative": collaborative}
        if description:
            body["description"] = description
        if await self.regime() is Regime.FULL:
            data = await self._json("POST", f"/users/{await self._me_id()}/playlists", json=body)
        else:
            data = await self._json("POST", "/me/playlists", json=body)
        return models.playlist(data)

    @_api
    async def add_playlist_items(
        self, playlist_id: str, uris: list[str], position: int | None = None
    ) -> Json:
        body: Json = {"uris": uris}
        if position is not None:
            body["position"] = position
        data = await self._json("POST", await self._playlist_items_url(playlist_id), json=body)
        return _snapshot(data)

    @_api
    async def remove_playlist_items(self, playlist_id: str, uris: list[str]) -> Json:
        # Body shape differs by regime: classic /tracks expects {"tracks":[{"uri":...}]},
        # restricted /items expects {"uris":[...]} (the same shape add_playlist_items uses).
        if await self.regime() is Regime.FULL:
            body: Json = {"tracks": [{"uri": u} for u in uris]}
        else:
            body = {"uris": uris}
        data = await self._json("DELETE", await self._playlist_items_url(playlist_id), json=body)
        return _snapshot(data)

    @_api
    async def reorder_playlist_items(
        self, playlist_id: str, range_start: int, insert_before: int, range_length: int = 1
    ) -> Json:
        body = {
            "range_start": range_start,
            "insert_before": insert_before,
            "range_length": range_length,
        }
        data = await self._json("PUT", await self._playlist_items_url(playlist_id), json=body)
        return _snapshot(data)

    @_api
    async def set_playlist_details(
        self,
        playlist_id: str,
        name: str | None = None,
        public: bool | None = None,
        collaborative: bool | None = None,
        description: str | None = None,
    ) -> Json:
        body = {
            k: v
            for k, v in {
                "name": name,
                "public": public,
                "collaborative": collaborative,
                "description": description,
            }.items()
            if v is not None
        }
        if not body:
            return {"ok": True, "note": "nothing to change"}
        await self._json("PUT", f"/playlists/{parse_id(playlist_id)}", json=body)
        return {"ok": True}

    @_api
    async def upload_playlist_cover(self, playlist_id: str, image_base64: str) -> Json:
        await self._request(
            "PUT",
            f"/playlists/{parse_id(playlist_id)}/images",
            content=image_base64.encode(),
            headers={"Content-Type": "image/jpeg"},
        )
        return {"ok": True}

    # ---- library (FULL: per-type; RESTRICTED: consolidated /me/library) -----

    @_api
    async def get_saved(
        self, item_type: str, limit: int = 50, offset: int = 0, market: str | None = None
    ) -> list[Json]:
        params = {"limit": min(50, limit), "offset": offset, "market": market}
        data = await self._json("GET", f"/me/{_plural(item_type)}", params=params)
        out = []
        for row in (data or {}).get("items", []):
            obj = row.get(item_type) if isinstance(row, dict) and row.get(item_type) else row
            out.append(
                {
                    "added_at": row.get("added_at") if isinstance(row, dict) else None,
                    "item": models.item(obj),
                }
            )
        return out

    @_api
    async def save_to_library(self, item_type: str, items: list[str]) -> Json:
        ids, uris = split_ids_and_uris(item_type, items)
        if await self.regime() is Regime.FULL:
            await self._json("PUT", f"/me/{_plural(item_type)}", json={"ids": ids})
        else:
            await self._json("PUT", "/me/library", json={"uris": uris})
        return {"ok": True, "saved": len(uris)}

    @_api
    async def remove_from_library(self, item_type: str, items: list[str]) -> Json:
        ids, uris = split_ids_and_uris(item_type, items)
        if await self.regime() is Regime.FULL:
            await self._json("DELETE", f"/me/{_plural(item_type)}", json={"ids": ids})
        else:
            await self._json("DELETE", "/me/library", json={"uris": uris})
        return {"ok": True, "removed": len(uris)}

    @_api
    async def check_saved(self, item_type: str, items: list[str]) -> Json:
        ids, uris = split_ids_and_uris(item_type, items)
        if await self.regime() is Regime.FULL:
            res = await self._json(
                "GET", f"/me/{_plural(item_type)}/contains", params={"ids": ",".join(ids)}
            )
        else:
            res = await self._json("GET", "/me/library/contains", params={"uris": ",".join(uris)})
        return dict(zip(uris, res or [], strict=False))

    # ---- follow -------------------------------------------------------------

    @_api
    async def followed_artists(
        self, limit: int = 50, after: str | None = None
    ) -> list[Json | None]:
        params = {"type": "artist", "limit": min(50, limit), "after": after}
        data = await self._json("GET", "/me/following", params=params)
        return [models.artist(a) for a in ((data or {}).get("artists") or {}).get("items", [])]

    @_api
    async def follow(self, item_type: str, items: list[str]) -> Json:
        ids, uris = split_ids_and_uris(item_type, items)
        if await self.regime() is Regime.FULL:
            await self._json("PUT", "/me/following", params={"type": item_type}, json={"ids": ids})
        else:
            await self._json("PUT", "/me/library", json={"uris": uris})
        return {"ok": True}

    @_api
    async def unfollow(self, item_type: str, items: list[str]) -> Json:
        ids, uris = split_ids_and_uris(item_type, items)
        if await self.regime() is Regime.FULL:
            await self._json(
                "DELETE", "/me/following", params={"type": item_type}, json={"ids": ids}
            )
        else:
            await self._json("DELETE", "/me/library", json={"uris": uris})
        return {"ok": True}

    @_api
    async def check_following(self, item_type: str, items: list[str]) -> Json:
        ids, uris = split_ids_and_uris(item_type, items)
        if await self.regime() is Regime.FULL:
            res = await self._json(
                "GET", "/me/following/contains", params={"type": item_type, "ids": ",".join(ids)}
            )
        else:
            res = await self._json("GET", "/me/library/contains", params={"uris": ",".join(uris)})
        return dict(zip(uris, res or [], strict=False))

    @_api
    async def follow_playlist(self, playlist_id: str, public: bool = True) -> Json:
        pid = parse_id(playlist_id)
        if await self.regime() is Regime.FULL:
            await self._json("PUT", f"/playlists/{pid}/followers", json={"public": public})
        else:
            await self._json("PUT", "/me/library", json={"uris": [f"spotify:playlist:{pid}"]})
        return {"ok": True}

    @_api
    async def unfollow_playlist(self, playlist_id: str) -> Json:
        pid = parse_id(playlist_id)
        if await self.regime() is Regime.FULL:
            await self._json("DELETE", f"/playlists/{pid}/followers")
        else:
            await self._json("DELETE", "/me/library", json={"uris": [f"spotify:playlist:{pid}"]})
        return {"ok": True}

    # ---- profile & personalization -----------------------------------------

    @_api
    async def me(self) -> Json:
        d = await self._json("GET", "/me") or {}
        return {
            "id": d.get("id"),
            "display_name": d.get("display_name"),
            "uri": d.get("uri"),
            "product": d.get("product"),
            "country": d.get("country"),
            "followers": (d.get("followers") or {}).get("total"),
        }

    @_api
    async def top_items(
        self, item_type: str, time_range: str = "medium_term", limit: int = 20, offset: int = 0
    ) -> list[Json | None]:
        if item_type not in ("artists", "tracks"):
            raise SpotifyBadRequest("top item_type must be 'artists' or 'tracks'", status=400)
        params = {"time_range": time_range, "limit": min(50, limit), "offset": offset}
        data = await self._json("GET", f"/me/top/{item_type}", params=params)
        norm = models.artist if item_type == "artists" else models.track
        return [norm(x) for x in (data or {}).get("items", [])]

    @_api
    async def user_profile(self, user_id: str) -> Json:
        await self._ensure_full("get_user_profile", "Only your own profile (me) is available.")
        d = await self._json("GET", f"/users/{parse_id(user_id)}") or {}
        return {
            "id": d.get("id"),
            "display_name": d.get("display_name"),
            "uri": d.get("uri"),
            "followers": (d.get("followers") or {}).get("total"),
        }

    @_api
    async def browse(
        self, kind: str, limit: int = 20, offset: int = 0, country: str | None = None
    ) -> list[Json]:
        await self._ensure_full("browse", "Browse is unavailable; use search instead.")
        params = {"limit": min(50, limit), "offset": offset, "country": country}
        if kind == "new_releases":
            data = await self._json("GET", "/browse/new-releases", params=params)
            return [models.album(a) for a in ((data or {}).get("albums") or {}).get("items", [])]
        if kind == "categories":
            data = await self._json("GET", "/browse/categories", params=params)
            cats = ((data or {}).get("categories") or {}).get("items", [])
            return [{"id": c.get("id"), "name": c.get("name")} for c in cats]
        raise SpotifyBadRequest(f"unknown browse kind {kind!r}", status=400)
