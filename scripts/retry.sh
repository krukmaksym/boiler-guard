#!/usr/bin/env bash
# Run a command and, if it fails, retry it up to 3 times (15 s, 30 s, 60 s apart).
# CI wraps its network steps in this, so a short outage of GitHub, PyPI, npm or
# Cloudflare (e.g. a 504 while downloading Pyodide) doesn't fail the run.
#
# Usage: scripts/retry.sh <command> [args...]
set -euo pipefail

retries=3
delay=15

for ((attempt = 1; ; attempt++)); do
  "$@" && exit 0
  status=$?
  if ((attempt > retries)); then
    echo "retry.sh: '$*' failed $attempt times, giving up (exit $status)" >&2
    exit "$status"
  fi
  echo "retry.sh: '$*' failed (exit $status), retry $attempt/$retries in ${delay}s" >&2
  sleep "$delay"
  delay=$((delay * 2))
done
