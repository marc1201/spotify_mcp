# spotify-mcp

A private, full-feature **Spotify MCP server** for use as a **claude.ai custom connector**. Runs on
the VPS, exposed at `https://mcp.example.com/mcp` via a Cloudflare Tunnel. One Spotify login both
authenticates the connector and authorizes the Spotify API (FastMCP `OAuthProxy` wrapping Spotify).

## Vibe-coding disclaimer

Full disclosure: this was **vibe-coded** — designed and written end-to-end by **Claude** (Anthropic's
Claude Code, Opus 4.8) under human direction, not hand-typed by a person. The usual caveat applies:
**review it before you trust it** (especially the auth and secret-handling paths), and treat it as a
personal, single-user tool rather than audited production software.

It wasn't unchecked, though. The build ran behind explicit quality gates — all committed under
`.claude/agents/`, so you can re-run them yourself:

- **`security-reviewer`** — audited secret hygiene (nothing secret on argv/stdout, the gpg idiom, no
  `set -x`) and OAuth/connector security (base_url/issuer correctness, pinned redirect URIs, PKCE,
  127.0.0.1-only bind, SSRF guard on pagination). Its findings were applied — a path-traversal guard
  on Spotify IDs, hashing the token-cache key, a KEY-injection guard in `set-secret.sh`, scope trimming.
- **`qa`** — `ruff` lint + format, an import / tool-registration smoke (all 53 tools), `pytest`, and a
  server boot-and-probe (401 + full OAuth discovery on a throwaway port).
- **`dedup-reviewer`** — duplication review; applied the consolidations it found, kept the rest.

Beyond the agents: Spotify's API behaviour (including the Feb-2026 dev-mode restrictions) and the
FastMCP auth surface were verified against primary sources / introspected on the pinned version
*before* coding; there are unit tests for the security-critical ID parsing and the response
normalizers; and the OAuth handshake was verified end-to-end against the live endpoint.

None of this makes AI-written code infallible — it just means it was held to a bar. Audit accordingly.

## Architecture

```
claude.ai ──OAuth(DCR+PKCE)──▶ mcp.example.com/mcp ──OAuthProxy──▶ accounts.spotify.com (login+consent)
   (holds FastMCP JWT)               │ (holds Spotify tokens, auto-refreshes)
   tool calls ─────────────────────▶ ┘ ──Bearer Spotify token──▶ api.spotify.com/v1/*
```

- **Server:** Python + FastMCP (`>=2.14,<3`), uvicorn on `127.0.0.1:8080` (reachable only via the tunnel).
- **53 tools:** playback, queue, devices, search/lookup, playlists, library, follow, profile, top items,
  and the daily rotation (below).
- **Regime auto-detect:** Spotify's 2026 dev-mode split (FULL vs RESTRICTED endpoint sets) is detected
  on first authenticated call; library/follow/playlist paths route automatically. FULL-only tools
  (`get_items_batch`, `artist_top_tracks`, `get_user_profile`, `browse`) return a clear message on
  restricted apps.

## Restricted dev-mode notes

This app runs in Spotify's **restricted** 2026 dev-mode regime (auto-detected on the first
authenticated call). The server routes to the consolidated endpoints and the request shapes the
current Web API reference specifies — several of which differ non-obviously from the classic API.
Every endpoint was audited against the 2026 reference; the verified restricted shapes are:

| Operation | Restricted request |
|---|---|
| Add playlist items | `POST /playlists/{id}/items` — body `{"uris":[...]}` |
| Remove playlist items | `DELETE /playlists/{id}/items` — body `{"items":[{"uri":...}]}` (key renamed from `tracks`) |
| Reorder playlist items | `PUT /playlists/{id}/items` — body `{range_start, insert_before, range_length}` |
| Create playlist | `POST /me/playlists` |
| Save/remove library, follow/unfollow | `PUT`/`DELETE /me/library` — `uris` as a **comma-separated query param** (max 40), not a body |
| Check saved / following | `GET /me/library/contains?uris=…` |
| Search | `limit` capped at 10 |

Full-access-only tools (`get_items_batch`, `artist_top_tracks`, `get_user_profile`, `browse`) return a
clear "unavailable on restricted apps" message. The HTTP client logs `method/path/body/response` on any
Spotify error (`journalctl --user -u spotify-mcp`), and `tests/` pins these request shapes per regime.

## Daily rotation (Closer by Candlelight Daily)

A small rotation engine builds a private 24-track playlist every morning from a stored song pool,
following six rules (no repeats from the last two days, caps on recently played tracks, a rested
minimum, one new song a day until the end of 2026 and then weekly, equal distribution) plus the
Candlelight structure: four movements of six, at most three tracks per artist, never the same
artist twice in a row. The spec and rule definitions live in Marc's requirements doc; the picking
algorithm is `rotation/engine.py`, a port of the reference `rotation_v2.py`.

Layering: `tools/rotation.py` (thin) → `rotation/service.py` (the operations) → `rotation/store.py`
(sqlite, `ROTATION_DB`, default `data/rotation.db`) + `rotation/engine.py` (pure selection) + the
`SpotifyClient` for every Spotify call. `daily_tracks` is the source of truth: play counts and gaps
are derived from it, dry runs are never stored, and a day's records are written in one transaction
after Spotify succeeded.

| Tool | Purpose |
|---|---|
| `rotation_status` | pool size, last run, `run_exists_today`, `new_song_due` + why, play-count spread, artists at the pool cap, `taste_brief` |
| `library_check` | which owned playlists already hold a track (id, ISRC, or normalized title + artist) and whether it is in the pool; uses a 12-hour index |
| `rotation_pool_add` | add a new song with a movement and a note; refuses duplicates and tracks already on an owned playlist |
| `rotation_generate` | pick, create the dated private playlist, record the run, apply retention; idempotent per date, `force` replaces, `dry_run` only picks |
| `rotation_pool_set_active` | veto or restore a track (history kept) |
| `rotation_pool_list`, `rotation_history` | the pool with counts; the last N runs with buckets |
| `rotation_seed` | one-time import from the volume playlists, movement from position |
| `rotation_config` | read or change taste brief, name template (`{date}`, `{dd}.{mm}.`), artist cap, retention, pool floor, schedule, and position rules such as first and last slot by Sade |

Setup, once, from any chat with the connector: `rotation_seed` with the six volume playlist ids in
order (Vol I first), then `rotation_pool_set_active(false)` for any track to leave out, then
`rotation_generate(dry_run=true)` to see the first day. The scheduled task below does the rest.

The sqlite file is the only copy of the history. Copy it now and then:
`.venv/bin/python -c "import sqlite3; sqlite3.connect('data/rotation.db').backup(sqlite3.connect('data/rotation.bak.db'))"`.

### Scheduled task

A claude.ai scheduled task at 06:50 Lisbon (`CRON_TZ=Europe/Lisbon 50 6 * * *`) with the SMCP
connector. In that environment the tools are named `mcp__SMCP__<tool>`. The task reads catalog
text it does not control, so restrict its allowed tools to `me`, `search`, `library_check` and the
`rotation_*` tools; it needs nothing else. Prompt:

```
Daily Closer by Candlelight. Load the SMCP tools with ToolSearch first; they are named mcp__SMCP__<tool>.
Use only these tools: me, search, library_check, rotation_status, rotation_pool_add, rotation_generate.

1. Call me. If it fails, stop and report: "SMCP needs re-authorization, today's Candlelight playlist was not created."
2. Call rotation_status with rotation "candlelight". If run_exists_today is true, stop and report that today's
   playlist already exists.
3. If new_song_due is true, add one new song:
   - Pick a track that fits taste_brief exactly, in its original album version, by an artist that is not in
     artists_at_cap. Find it with search (types ["track"]) and use the track uri from the result.
   - Call library_check with that uri. If in_pool is true or playlists is not empty, pick a different track.
   - Call rotation_pool_add with the uri, movement 1-4 (1 first spark, 2 drawing in, 3 deep heat, 4 afterglow),
     a one-line note on why it fits, and actor "scheduled-task".
   - If three candidates in a row are refused, skip the new song and say so in the report.
4. Call rotation_generate with rotation "candlelight" and no other arguments.
5. Report in at most four lines: the playlist name and url; the new song as "Artist - Title: note" or
   "no new song"; relaxations, or "no relaxations"; and a last line "Needs attention: yes" only if a step
   failed, a relaxation was logged, or no new song could be added, otherwise "Needs attention: no".
   Plain text, no em dashes, no emojis, no tables.
```

## One-time setup

1. **Spotify app** (developer.spotify.com → your app → Settings):
   - Add Redirect URI **exactly**: `https://mcp.example.com/auth/callback`
   - Copy the **Client ID** into `config.env` (`SPOTIFY_CLIENT_ID=`). It is not a secret.
   - You (Premium) are the app owner; dev mode allows up to 5 allowlisted users.

2. **Secrets** (gpg-encrypted to `you@example.com`, never echoed):
   ```bash
   scripts/set-secret.sh SPOTIFY_CLIENT_SECRET     # paste the dashboard secret (hidden input)
   scripts/set-secret.sh FASTMCP_JWT_KEY           # paste a long random string (openssl rand -base64 48)
   scripts/set-secret.sh CF_TUNNEL_TOKEN           # the Cloudflare tunnel token (step 3)
   ```

3. **Cloudflare Tunnel** (remotely-managed, token model):
   - Create a tunnel (dashboard: Zero Trust → Networks → Tunnels → Create; or `scripts/cf-provision.sh`
     with an API token). Add public hostname `mcp.example.com` → `http://localhost:8080`.
   - Store its token via `set-secret.sh CF_TUNNEL_TOKEN`.
   - `cloudflared` is a rootless static binary in `~/.local/bin`.

4. **systemd --user services:**
   ```bash
   loginctl enable-linger "$USER"          # one-time; survives logout/reboot (may need sudo)
   mkdir -p ~/.config/systemd/user
   ln -sf ~/projects/spotify_mcp/systemd/spotify-mcp.service    ~/.config/systemd/user/
   ln -sf ~/projects/spotify_mcp/systemd/spotify-tunnel.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable spotify-mcp.service spotify-tunnel.service
   scripts/up.sh                            # warms gpg-agent, starts both
   ```

## Day-to-day

- `scripts/up.sh` — warm gpg-agent + (re)start both services. **Run this after a reboot** (a cold
  gpg-agent can't decrypt the secrets unattended — by design; no passphrase is stored).
- `scripts/down.sh` — stop both.
- Logs: `journalctl --user -u spotify-mcp -f` and `... -u spotify-tunnel -f`.

## Connect in claude.ai

Settings → Connectors → **Add custom connector** → URL `https://mcp.example.com/mcp` → Connect → log in
to Spotify + consent once. Works from web, desktop, and the iOS app afterward.

## Verify

```bash
curl -s https://mcp.example.com/healthz
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://mcp.example.com/mcp        # 401
curl -s https://mcp.example.com/.well-known/oauth-authorization-server | jq .issuer  # https://mcp.example.com/
```
All advertised OAuth URLs must be `https://mcp.example.com` — never `http`, `127.0.0.1`, or `:8080`.

## Security notes

- Secrets live only in `secrets.env.gpg` (asymmetric to your key) and are decrypted transiently into
  the service env; nothing secret is written in plaintext, passed on argv, or printed. No `set -x` in
  secret-touching scripts.
- uvicorn binds `127.0.0.1` only; the public surface is the Cloudflare-fronted HTTPS tunnel.
- The connector is gated by Spotify login + an optional `SPOTIFY_ALLOWED_USER_IDS` allowlist (lock it
  to your own Spotify user id after the first `me` call) + Spotify dev-mode's 5-user cap.

See [SECURITY.md](SECURITY.md) for the security policy and how to report issues.

## License

[MIT](LICENSE) © 2026 marc1201.
