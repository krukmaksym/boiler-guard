#!/usr/bin/env bash
# Upload credentials from .prod.vars to Worker Secrets without printing them or
# putting them on a command line. Optionally seeds BEARER_TOKEN into KV.
#
# Usage: scripts/upload-secrets.sh [path/to/.prod.vars]
set -euo pipefail

ENV_FILE="${1:-.prod.vars}"
[[ -f "$ENV_FILE" ]] || { echo "No such file: $ENV_FILE" >&2; exit 1; }

umask 077
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

python3 -I "$(dirname "$0")/parse_env.py" "$ENV_FILE" bulk-json "$TMP_DIR"

# --no-install: only ever use the wrangler pinned in package-lock.json (run `npm ci` first).
npx --no-install wrangler secret bulk "$TMP_DIR/secrets.json" --env=""  # production (top-level)

if [[ -f "$TMP_DIR/token" ]]; then
  # Optional: saves one token request on first run. The Worker refreshes it itself.
  npx --no-install wrangler kv key put --env="" --binding=STATE --remote "deye:token" \
    --path "$TMP_DIR/token" --ttl 86400
fi
echo "Done."
