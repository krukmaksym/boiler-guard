#!/usr/bin/env bash
# The boiler-guard-test Worker exists only while you verify a change with the test
# bot and test group.
#
#   scripts/test-env.sh up [hours]   # deploy this checkout for 1-12 h (default 2) and point the test bot at it
#   scripts/test-env.sh status       # where the test bot points, and the Worker's deployments
#   scripts/test-env.sh down         # remove the test bot's webhook and delete the Worker
#
# Reads .test.vars: the test bot, test group and their own TELEGRAM_WEBHOOK_SECRET,
# plus the same Deye and Shelly credentials as production (the real plug switches).
# After EXPIRES_AT the Worker does nothing even if `down` is forgotten. Secret values
# are never printed or put on a command line.
set -euo pipefail

cd "$(dirname "$0")/.."
VARS=.test.vars
[[ -f "$VARS" ]] || { echo "No $VARS: copy .test.vars.example and fill it in" >&2; exit 1; }

# The test environment must never take over the production bot or chat.
python3 -I - "$VARS" <<'EOF'
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
import parse_env

test = parse_env.parse(sys.argv[1])
prod = parse_env.parse(".prod.vars") if Path(".prod.vars").exists() else {}
for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_WEBHOOK_SECRET"):
    if not test.get(key):
        sys.exit(f"{sys.argv[1]}: {key} is required for the test environment")
    if test.get(key) == prod.get(key):
        sys.exit(f"{sys.argv[1]}: {key} is the same as in .prod.vars; use the test bot and test group")
EOF

case "${1:-}" in
  up)
    HOURS="${2:-2}"
    if ! [[ "$HOURS" =~ ^[0-9]+$ ]] || (( HOURS < 1 || HOURS > 12 )); then
      echo "hours must be 1-12" >&2
      exit 1
    fi
    python3 -m unittest discover -s tests -q
    EXPIRES_AT=$(( $(date +%s) + HOURS * 3600 ))

    umask 077
    TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "$TMP_DIR"' EXIT
    python3 -I scripts/parse_env.py "$VARS" bulk-json "$TMP_DIR"
    PATH="$PWD/.venv/bin:$PATH" .venv/bin/pywrangler deploy --env test \
      --secrets-file "$TMP_DIR/secrets.json" --var "EXPIRES_AT:$EXPIRES_AT" | tee "$TMP_DIR/deploy.log"

    URL="$(grep -oE 'https://boiler-guard-test\.[a-z0-9-]+\.workers\.dev' "$TMP_DIR/deploy.log" | head -1 || true)"
    [[ -n "$URL" ]] || { echo "Couldn't find the workers.dev URL in the deploy output" >&2; exit 1; }
    python3 -I scripts/telegram_webhook.py set "$URL" "$VARS"
    echo "boiler-guard-test is up until $(date -r "$EXPIRES_AT" '+%H:%M %Z'). Run 'scripts/test-env.sh down' when done."
    ;;
  status)
    python3 -I scripts/telegram_webhook.py info "$VARS"
    npx --no-install wrangler deployments list --env test 2>&1 | tail -n 8 || echo "boiler-guard-test is not deployed"
    ;;
  down)
    python3 -I scripts/telegram_webhook.py delete "$VARS" || echo "Couldn't remove the webhook; continuing"
    npx --no-install wrangler delete --env test < /dev/null
    echo "boiler-guard-test deleted: no cron runs, no URL. Its KV namespace stays for next time."
    ;;
  *)
    sed -n '2,12p' "$0"
    exit 1
    ;;
esac
