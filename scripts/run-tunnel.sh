#!/usr/bin/env bash
# systemd ExecStart wrapper for the Cloudflare tunnel.
#
# Credentials mode (the system unit spotify-tunnel.service, User=tunnel): CREDENTIALS_DIRECTORY is
# set and cloudflared reads the run-token itself from the credential FILE
# $CREDENTIALS_DIRECTORY/SPOTIFY_CF_TUNNEL_TOKEN (`--token-file`) — secrets.sh is not sourced,
# nothing in the environment, nothing on argv but a path.
# Vault mode (today's user install): scripts/secrets.sh exports CF_TUNNEL_TOKEN, handed over as
# TUNNEL_TOKEN (cloudflared reads it), keeping it off argv / /proc/*/cmdline. No set -x.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="${HOME:-/nonexistent}/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

TOKEN_FILE=""
if [ -n "${CREDENTIALS_DIRECTORY:-}" ]; then
  case "$CREDENTIALS_DIRECTORY" in
    /*) ;;
    *) echo "run-tunnel: CREDENTIALS_DIRECTORY must be an absolute path" >&2; exit 1 ;;
  esac
  TOKEN_FILE="$CREDENTIALS_DIRECTORY/SPOTIFY_CF_TUNNEL_TOKEN"
  if [ ! -r "$TOKEN_FILE" ]; then
    echo "run-tunnel: credential SPOTIFY_CF_TUNNEL_TOKEN not loaded (LoadCredentialEncrypted= in the unit)" >&2
    exit 1
  fi
else
  # shellcheck disable=SC1091
  . "$ROOT/scripts/secrets.sh"
  : "${CF_TUNNEL_TOKEN:?CF_TUNNEL_TOKEN not set — add it with: scripts/set-secret.sh CF_TUNNEL_TOKEN}"
  export TUNNEL_TOKEN="$CF_TUNNEL_TOKEN"
fi

# Today's install: the user-local binary; the release install (User=tunnel) finds it on PATH.
CLOUDFLARED="${HOME:-/nonexistent}/.local/bin/cloudflared"
[ -x "$CLOUDFLARED" ] || CLOUDFLARED="$(command -v cloudflared || true)"
[ -n "$CLOUDFLARED" ] || { echo "run-tunnel: cloudflared not found" >&2; exit 1; }
if [ -n "$TOKEN_FILE" ]; then
  exec "$CLOUDFLARED" tunnel --no-autoupdate run --token-file "$TOKEN_FILE"
fi
exec "$CLOUDFLARED" tunnel --no-autoupdate run
