#!/usr/bin/env bash
# Provision the Cloudflare Tunnel for the MCP via the API, idempotently. Auto-discovers your account
# and zone from the API token (needs Account>Cloudflare Tunnel:Edit, Zone>DNS:Edit, Zone>Zone:Read),
# creates (or reuses) a named tunnel, stores its run-token in secrets.env.gpg, points the public
# hostname at http://localhost:8080, and creates the proxied DNS record.
#
# Sources scripts/secrets.sh (so CF_API_TOKEN must already be set there). Neither the API token nor
# the tunnel token ever appears in argv (passed to curl via a --config FIFO built with the printf
# builtin) or on stdout. No `set -x`.
#
# NOTE: there is intentionally NO /user/tokens/verify call — that endpoint rejects ACCOUNT-OWNED
# tokens even when they are valid. The zone lookup below is the real validity + scope check.
#
# Usage: scripts/cf-provision.sh [hostname]   (default: derived from BASE_URL in config.env)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/scripts/secrets.sh"
: "${CF_API_TOKEN:?CF_API_TOKEN not set — add it with scripts/set-secret.sh CF_API_TOKEN}"

if [ -f "$ROOT/config.env" ]; then . "$ROOT/config.env"; fi
HOST="${1:-}"
if [ -z "$HOST" ] && [ -n "${BASE_URL:-}" ]; then HOST="${BASE_URL#*://}"; HOST="${HOST%%/*}"; fi
: "${HOST:?provide a hostname arg, or set BASE_URL in config.env}"
[[ "$HOST" =~ ^[a-zA-Z0-9.-]+$ ]] || { echo "cf-provision: invalid hostname '$HOST'." >&2; exit 1; }
ZONE_NAME="${HOST#*.}"          # host -> zone (strip the first label)
TUNNEL_NAME="spotify-mcp"
SERVICE="http://localhost:8080"
API="https://api.cloudflare.com/client/v4"

cf() {
  curl -fsS --config <(printf 'header = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\n' "$CF_API_TOKEN") "$@"
}

# Safe JSON extractor: navigates result by a dotted path passed as ARGV (never eval'd as code).
# Path segments: dict keys, or numeric list indices; missing/short -> empty string.
jget() {
  python3 -c '
import sys, json
cur = json.load(sys.stdin)
for part in sys.argv[1].split("."):
    if part == "":
        continue
    if isinstance(cur, list):
        cur = cur[int(part)] if part.isdigit() and len(cur) > int(part) else (cur[0] if cur else {})
    elif isinstance(cur, dict):
        cur = cur.get(part, "")
    else:
        cur = ""
print(cur if cur not in (None, {}, []) else "")
' "$1"
}

echo "[1/4] discovering account + zone for $ZONE_NAME (also the token validity check)…"
ZJSON="$(cf "$API/zones?name=$ZONE_NAME")"
ZONE_ID="$(printf '%s' "$ZJSON" | jget "result.0.id")"
ACCOUNT_ID="$(printf '%s' "$ZJSON" | jget "result.0.account.id")"
[ -n "$ZONE_ID" ] && [ -n "$ACCOUNT_ID" ] || { echo "  ERROR: could not read zone $ZONE_NAME — token is invalid or lacks Zone:Read on it." >&2; exit 1; }
echo "  zone=$ZONE_ID account=$ACCOUNT_ID"

echo "[2/4] finding or creating tunnel '$TUNNEL_NAME'…"
TUNNEL_ID="$(cf "$API/accounts/$ACCOUNT_ID/cfd_tunnel?name=$TUNNEL_NAME&is_deleted=false" | jget "result.0.id")"
if [ -z "$TUNNEL_ID" ]; then
  TUNNEL_ID="$(cf -X POST "$API/accounts/$ACCOUNT_ID/cfd_tunnel" --data "{\"name\":\"$TUNNEL_NAME\",\"config_src\":\"cloudflare\"}" | jget "result.id")"
  echo "  created tunnel $TUNNEL_ID"
else
  echo "  reusing tunnel $TUNNEL_ID"
fi

echo "[3/4] storing tunnel run-token in secrets.env.gpg…"
TUNNEL_TOKEN="$(cf "$API/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/token" | jget "result")"
printf '%s\n' "$TUNNEL_TOKEN" | "$ROOT/scripts/set-secret.sh" CF_TUNNEL_TOKEN
unset TUNNEL_TOKEN

echo "[4/4] setting ingress + DNS…"
cf -X PUT "$API/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" \
  --data "{\"config\":{\"ingress\":[{\"hostname\":\"$HOST\",\"service\":\"$SERVICE\"},{\"service\":\"http_status:404\"}]}}" >/dev/null
echo "  ingress: $HOST -> $SERVICE"
REC_ID="$(cf "$API/zones/$ZONE_ID/dns_records?type=CNAME&name=$HOST" | jget "result.0.id")"
DNS="{\"type\":\"CNAME\",\"name\":\"$HOST\",\"content\":\"$TUNNEL_ID.cfargotunnel.com\",\"proxied\":true}"
if [ -z "$REC_ID" ]; then
  cf -X POST "$API/zones/$ZONE_ID/dns_records" --data "$DNS" >/dev/null
  echo "  DNS: created CNAME $HOST"
else
  cf -X PUT "$API/zones/$ZONE_ID/dns_records/$REC_ID" --data "$DNS" >/dev/null
  echo "  DNS: updated CNAME $HOST"
fi

echo "cf-provision: done — tunnel $TUNNEL_ID, $HOST -> $SERVICE (proxied)."
