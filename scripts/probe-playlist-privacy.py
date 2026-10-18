"""Diagnostic: does Spotify honour `public: false` for this app? Run by hand, never by the server:

    . scripts/secrets.sh && .venv/bin/python scripts/probe-playlist-privacy.py [playlist_id]

Reads the live upstream Spotify token from FastMCP's encrypted OAuth store (needs FASTMCP_JWT_KEY
in the environment, which secrets.sh provides) and runs three experiments against the Web API:

  1. toggle `public` on the given playlist (default: the 17.10. daily) and read it back
  2. create a throwaway playlist with public=false (then with collaborative=true), read, delete
  3. create via the pre-2026 /users/{id}/playlists endpoint with public=false, read, delete

Prints only HTTP status codes, playlist ids/names and the public/collaborative flags. The token
never leaves the process and is never printed. No `set -x`, no logging of request headers.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import fastmcp.server.auth.oauth_proxy as proxy_mod
import httpx
from cryptography.fernet import Fernet
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.server.auth.oauth_proxy import UpstreamTokenSet
from key_value.aio.adapters.pydantic import PydanticAdapter
from key_value.aio.stores.disk import DiskStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

API = "https://api.spotify.com/v1"
COLLECTION = "mcp-upstream-tokens"
STORE_DIR = Path.home() / ".local/share/fastmcp/oauth-proxy"
DEFAULT_PLAYLIST = "3PWgu1jcp61S0ZaOFXYWPy"


def _fernet_from_env() -> Fernet:
    """The same two-step derivation OAuthProxy uses for its disk store."""
    src = Path(proxy_mod.__file__).read_text()
    salt = re.search(r'low_entropy_material=jwt_signing_key,\s*salt="([^"]+)"', src).group(1)
    signing = derive_jwt_key(low_entropy_material=os.environ["FASTMCP_JWT_KEY"], salt=salt)
    enc = derive_jwt_key(
        high_entropy_material=signing.decode(), salt="fastmcp-storage-encryption-key"
    )
    return Fernet(key=enc)


async def _live_token() -> UpstreamTokenSet:
    disk = DiskStore(directory=STORE_DIR)
    store = FernetEncryptionWrapper(key_value=disk, fernet=_fernet_from_env())
    adapter = PydanticAdapter[UpstreamTokenSet](
        key_value=store,
        pydantic_model=UpstreamTokenSet,
        default_collection=COLLECTION,
        raise_on_validation_error=True,
    )
    cache = next(v for v in vars(disk).values() if type(v).__name__ == "Cache")
    best: UpstreamTokenSet | None = None
    for raw in (str(k) for k in cache.iterkeys() if COLLECTION in str(k)):
        try:
            ts = await adapter.get(key=raw.split(COLLECTION, 1)[1].lstrip(":/|"))
        except Exception:  # an entry written under an older key; skip it
            continue
        if ts and (best is None or ts.expires_at > best.expires_at):
            best = ts
    if best is None:
        sys.exit("no decodable upstream token found")
    return best


def _fields(r: httpx.Response, *names: str) -> str:
    try:
        j = r.json()
    except Exception:
        return ""
    if isinstance(j, dict) and "error" in j:
        e = j["error"]
        return f"error={e.get('message') if isinstance(e, dict) else e}"
    return " ".join(f"{n}={j.get(n)!r}" for n in names) if isinstance(j, dict) else ""


def main() -> None:
    ts = asyncio.run(_live_token())
    left = int(ts.expires_at - time.time())
    print(
        f"token: expires in {left}s" + (" (expired: call any SMCP tool first)" if left < 0 else "")
    )
    c = httpx.Client(headers={"Authorization": f"Bearer {ts.access_token}"}, timeout=20)

    def get(pid: str) -> str:
        r = c.get(f"{API}/playlists/{pid}")
        return f"  GET -> {r.status_code} {_fields(r, 'public', 'collaborative', 'name')}"

    def put(pid: str, body: dict) -> str:
        r = c.put(f"{API}/playlists/{pid}", json=body)
        return f"  PUT {json.dumps(body)} -> {r.status_code} {_fields(r)}"

    def delete(pid: str) -> str:
        r = c.delete(f"{API}/me/library", params={"uris": f"spotify:playlist:{pid}"})
        return f"  DELETE from library -> {r.status_code} {_fields(r)}"

    def create(path: str, body: dict) -> str | None:
        r = c.post(f"{API}{path}", json=body)
        print(
            f"  POST {path} {json.dumps(body)} -> {r.status_code} {_fields(r, 'id', 'public', 'collaborative')}"
        )
        return r.json().get("id") if r.status_code in (200, 201) else None

    pid = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PLAYLIST
    print(f"\n1. toggle public on {pid}")
    print(get(pid))
    print(put(pid, {"public": True}))
    print(get(pid))
    print(put(pid, {"public": False}))
    print(get(pid))

    print("\n2a. create via /me/playlists with public=false")
    if new := create("/me/playlists", {"name": "SMCP privacy test", "public": False}):
        print(get(new))
        print(put(new, {"public": False}))
        print(get(new))
        print(delete(new))

    print("\n2b. create via /me/playlists with public=false and collaborative=true")
    if new := create(
        "/me/playlists", {"name": "SMCP privacy test 2", "public": False, "collaborative": True}
    ):
        print(get(new))
        print(put(new, {"collaborative": False}))
        print(get(new))
        print(delete(new))

    print("\n3. create via the old /users/{id}/playlists with public=false")
    me = c.get(f"{API}/me").json().get("id")
    if new := create(f"/users/{me}/playlists", {"name": "SMCP privacy test 3", "public": False}):
        print(get(new))
        print(delete(new))


if __name__ == "__main__":
    main()
