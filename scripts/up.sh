#!/usr/bin/env bash
# Warm the gpg-agent once (interactive pinentry), then (re)start the user services.
# Run this after a reboot, or whenever the agent is cold.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "Warming gpg-agent (you may be prompted for your key passphrase)…" >&2
gpg --decrypt "$ROOT/secrets.env.gpg" >/dev/null   # output discarded; never printed

systemctl --user daemon-reload
systemctl --user restart spotify-mcp.service spotify-tunnel.service
systemctl --user --no-pager status spotify-mcp.service spotify-tunnel.service || true
