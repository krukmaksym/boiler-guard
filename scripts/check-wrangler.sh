#!/usr/bin/env bash
# pywrangler runs `npx --yes wrangler`: without the wrangler pinned in package.json
# installed in node_modules, npx would download the latest wrangler and run it with
# your Cloudflare login and secrets. Refuse unless `npm ci` has installed the pin.
set -euo pipefail

cd "$(dirname "$0")/.."
want="$(node -p 'require("./package.json").devDependencies.wrangler')"
have="$(node -p 'require("./node_modules/wrangler/package.json").version' 2>/dev/null || true)"
if [[ "$have" != "$want" ]]; then
  echo "wrangler $want isn't installed (found: ${have:-none}); run 'npm ci' first" >&2
  exit 1
fi
