#!/usr/bin/env bash
# Add or update ONE secret in secrets.env.gpg without it ever touching the terminal/transcript.
# Usage:  scripts/set-secret.sh SPOTIFY_CLIENT_SECRET
# The value is read with `read -s` (hidden), merged into the decrypted set in memory, and the file
# is re-encrypted asymmetrically to your GPG key. Nothing secret is printed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GPG_FILE="$ROOT/secrets.env.gpg"
# Recipient (gpg key id or email) comes from config.env (SECRETS_RECIPIENT) or the env var.
if [ -f "$ROOT/config.env" ]; then . "$ROOT/config.env"; fi
RECIPIENT="${SECRETS_RECIPIENT:-you@example.com}"
KEY="${1:?usage: set-secret.sh KEY_NAME}"
if ! [[ "$KEY" =~ ^[A-Z_][A-Z0-9_]*$ ]]; then
  echo "set-secret: invalid KEY name '$KEY' (use UPPER_SNAKE_CASE)." >&2
  exit 1
fi

# Decrypt current contents into memory (empty only if the file doesn't exist yet). If the file
# exists but can't be decrypted (e.g. cold gpg-agent), ABORT — overwriting would lose other secrets.
CURRENT=""
if [ -r "$GPG_FILE" ]; then
  if ! CURRENT="$(gpg --quiet --decrypt "$GPG_FILE" 2>/dev/null)"; then
    echo "set-secret: cannot decrypt existing $GPG_FILE (cold gpg-agent?)." >&2
    echo "set-secret: warm it first, then retry:  gpg --decrypt $GPG_FILE >/dev/null" >&2
    exit 1
  fi
fi

if [ -t 0 ]; then printf 'Value for %s (input hidden, will not echo): ' "$KEY" >&2; fi
IFS= read -rs VALUE
[ -t 0 ] && echo >&2 || true
if [ -z "$VALUE" ]; then
  echo "set-secret: empty value — aborting, nothing changed." >&2
  exit 1
fi

# Drop any existing export line for KEY, then append the new one (%q keeps it eval-safe).
UPDATED="$(printf '%s\n' "$CURRENT" | grep -vE "^export ${KEY}=" || true)"
UPDATED="$(printf '%s\nexport %s=%q\n' "$UPDATED" "$KEY" "$VALUE")"

printf '%s\n' "$UPDATED" | gpg --quiet --yes --encrypt --recipient "$RECIPIENT" --output "$GPG_FILE"
chmod 600 "$GPG_FILE"
unset VALUE CURRENT UPDATED
echo "set-secret: updated $KEY in secrets.env.gpg (encrypted to $RECIPIENT)." >&2
