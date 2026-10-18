#!/usr/bin/env bash
# systemd ExecStart wrapper for the MCP server. Loads non-secret config, then (vault mode only) the
# gpg secrets, then execs the server. No `set -x` (would echo secrets).
#
# Credentials mode: when CREDENTIALS_DIRECTORY is set (the system unit spotify-mcp.service with
# LoadCredentialEncrypted=), the secrets are files under it and spotify_mcp.settings reads them —
# scripts/secrets.sh is NOT sourced and config.env is optional (the unit's EnvironmentFile= carries
# BASE_URL, SPOTIFY_ALLOWED_USER_IDS).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -n "${CREDENTIALS_DIRECTORY:-}" ]; then
  if [ -r "$ROOT/config.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/config.env"
    set +a
  fi
else
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/config.env"          # SPOTIFY_CLIENT_ID, BASE_URL, PORT, SPOTIFY_ALLOWED_USER_IDS
  set +a
  # shellcheck disable=SC1091
  . "$ROOT/scripts/secrets.sh"  # SPOTIFY_CLIENT_SECRET, FASTMCP_JWT_KEY (+ CF_TUNNEL_TOKEN)
fi

exec "$ROOT/.venv/bin/python" -m spotify_mcp
