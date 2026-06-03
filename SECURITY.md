# Security Policy

This is a personal, single-user hobby project (see the *Vibe-coding disclaimer* in the README). It is
**not** audited production software and ships with no warranty or response-time guarantee. Run it at
your own risk, and review the auth and secret-handling paths before trusting it.

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue for them.

- Preferred: GitHub **private vulnerability reporting** — the repository's *Security* tab →
  *Report a vulnerability*. (If it isn't enabled, the owner can turn it on under
  *Settings → Code security and analysis → Private vulnerability reporting*.)

Reports are acknowledged on a best-effort basis; as a hobby project there is no SLA.

## What to look at (security-sensitive surface)

- **OAuth & tokens** — `src/spotify_mcp/server.py`, `src/spotify_mcp/auth.py`: connector auth via
  FastMCP `OAuthProxy`, the Spotify-user allowlist (`SPOTIFY_ALLOWED_USER_IDS`), and token-cache
  behaviour (cached by a hash of the token, short TTL).
- **Secret handling** — `scripts/secrets.sh`, `scripts/set-secret.sh`, `scripts/cf-provision.sh`:
  gpg decrypt-on-source, nothing secret on argv/stdout, no `set -x` in secret-touching scripts.
- **Request building** — `src/spotify_mcp/client.py`: the SSRF guard on paginated `next` URLs and the
  Spotify-ID validation that blocks path traversal.

The repo ships a `security-reviewer` agent (`.claude/agents/security-reviewer.md`) that encodes these
checks — you can re-run it against any change.

## Good to know

- Secrets live only in a gpg-encrypted `secrets.env.gpg` (never committed) and your gitignored
  `config.env`. No credentials are present in the git history.
- The server binds `127.0.0.1` only; the public surface is solely the Cloudflare Tunnel (HTTPS).
- The connector authenticates the **Spotify** identity, not a Claude account — access is gated by the
  Spotify login plus the optional user-id allowlist plus Spotify dev-mode's own allowlist.
