#!/usr/bin/env bash
# Deploy the Worker code and its secrets from .prod.vars as ONE new version
# (wrangler deploy --secrets-file), so the Worker never runs half-configured.
# Secret values are never printed or put on a command line.
#
# Usage: scripts/deploy.sh [path/to/.prod.vars]
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE="${1:-.prod.vars}"
[[ -f "$ENV_FILE" ]] || { echo "No such file: $ENV_FILE" >&2; exit 1; }

python3 -m unittest discover -s tests -q

umask 077
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

python3 -I scripts/parse_env.py "$ENV_FILE" bulk-json "$TMP_DIR"

# pywrangler syncs Python deps, then runs the wrangler pinned in package-lock.json.
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pywrangler deploy --env="" --secrets-file "$TMP_DIR/secrets.json"
