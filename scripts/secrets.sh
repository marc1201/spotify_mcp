# shellcheck shell=bash
# Source me (do not execute). Decrypts secrets.env.gpg and exports the secrets into the current
# shell. Mirrors the PawOS .cf-env gpg idiom: decrypt into a variable, then eval the `export …`
# lines. Nothing secret is ever printed to stdout/stderr. No `set -x` here — it would echo secrets.

_secrets_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null && pwd)"
_secrets_gpg="${_secrets_dir}/secrets.env.gpg"

if [ -r "$_secrets_gpg" ]; then
  _secrets_tty="$(tty 2>/dev/null || true)"
  if [ -n "$_secrets_tty" ] && [ "$_secrets_tty" != "not a tty" ]; then
    export GPG_TTY="$_secrets_tty"
    _secrets_plain="$(gpg --quiet --decrypt "$_secrets_gpg" 2>/dev/null || true)"
  else
    _secrets_plain="$(GPG_TTY=/dev/null gpg --quiet --no-tty --pinentry-mode error --decrypt "$_secrets_gpg" 2>/dev/null || true)"
  fi
  if [ -n "$_secrets_plain" ]; then
    eval "$_secrets_plain"
  else
    echo "secrets.sh: gpg --decrypt failed for $_secrets_gpg." >&2
    echo "secrets.sh: if non-interactive, warm the agent once from a TTY: 'gpg --decrypt $_secrets_gpg >/dev/null'" >&2
  fi
  unset _secrets_plain _secrets_tty
else
  echo "secrets.sh: $_secrets_gpg not found — create it with scripts/set-secret.sh." >&2
fi
unset _secrets_dir _secrets_gpg
