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
- **`qa`** — `ruff` lint + format, an import / tool-registration smoke (all 44 tools), `pytest`, and a
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
- **44 tools:** playback, queue, devices, search/lookup, playlists, library, follow, profile, top items.
- **Regime auto-detect:** Spotify's 2026 dev-mode split (FULL vs RESTRICTED endpoint sets) is detected
  on first authenticated call; library/follow/playlist paths route automatically. FULL-only tools
  (`get_items_batch`, `artist_top_tracks`, `get_user_profile`, `browse`) return a clear message on
  restricted apps.

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
