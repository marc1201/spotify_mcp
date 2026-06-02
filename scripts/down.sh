#!/usr/bin/env bash
set -euo pipefail
systemctl --user stop spotify-tunnel.service spotify-mcp.service
echo "stopped spotify-tunnel + spotify-mcp" >&2
