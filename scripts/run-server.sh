#!/usr/bin/env bash
# systemd ExecStart wrapper for the MCP server. Loads non-secret config + gpg secrets, then execs
# the server. No `set -x` (would echo secrets).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
set -a
# shellcheck disable=SC1091
. "$ROOT/config.env"          # SPOTIFY_CLIENT_ID, BASE_URL, PORT, SPOTIFY_ALLOWED_USER_IDS
set +a
# shellcheck disable=SC1091
. "$ROOT/scripts/secrets.sh"  # SPOTIFY_CLIENT_SECRET, FASTMCP_JWT_KEY (+ CF_TUNNEL_TOKEN)

exec "$ROOT/.venv/bin/python" -m spotify_mcp
