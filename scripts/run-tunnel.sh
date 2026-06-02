#!/usr/bin/env bash
# systemd ExecStart wrapper for the Cloudflare tunnel. The tunnel token is gpg-managed and passed
# via the TUNNEL_TOKEN env var (cloudflared reads it), keeping it off argv / /proc/*/cmdline.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/scripts/secrets.sh"
: "${CF_TUNNEL_TOKEN:?CF_TUNNEL_TOKEN not set — add it with: scripts/set-secret.sh CF_TUNNEL_TOKEN}"

export TUNNEL_TOKEN="$CF_TUNNEL_TOKEN"
exec "$HOME/.local/bin/cloudflared" tunnel --no-autoupdate run
