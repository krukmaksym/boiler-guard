"""Read the Worker's secrets out of a vars file (.prod.vars) and write them in a
deploy- or dev-friendly form.

Never prints values. Usage:
  parse_env.py <vars-file> bulk-json <out-dir>   -> <out-dir>/secrets.json (+ <out-dir>/token)
  parse_env.py <vars-file> dev-vars  <out-file>  -> .dev.vars for local `wrangler dev`

For local runs, generate .dev.vars from .test.vars (test bot and group), never from
.prod.vars. dev-vars leaves out TELEGRAM_WEBHOOK_SECRET: Telegram can't reach a
local Worker, so it must keep polling.
"""

import json
import os
import re
import sys
from pathlib import Path

SECRETS = (
    "DEYE_APP_ID",
    "APP_SECRET",
    "USER_EMAIL",
    "SHA_256_PASSWORD",
    "INVERTER_SN",
    "SHELLY_HOST",
    "DEVICE_ID",
    "SHELLY_AUTH_KEY",
)
# Uploaded when present; the Worker runs without them (no notifications).
OPTIONAL_SECRETS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ADMIN_IDS", "TELEGRAM_WEBHOOK_SECRET")
LINE_RE = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$")


def parse(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue  # comments, blank lines, '---' separators
        key, value = m.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    missing = [k for k in SECRETS if not values.get(k)]
    if missing:
        sys.exit(f"Missing in {path}: {', '.join(missing)}")
    return values


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def main(env_file, mode, target):
    values = parse(env_file)
    secrets = {k: values[k] for k in SECRETS + OPTIONAL_SECRETS if values.get(k)}
    if mode == "bulk-json":
        write_private(Path(target) / "secrets.json", json.dumps(secrets))
        if values.get("BEARER_TOKEN"):
            write_private(Path(target) / "token", values["BEARER_TOKEN"])
    elif mode == "dev-vars":
        secrets.pop("TELEGRAM_WEBHOOK_SECRET", None)  # local runs poll; Telegram can't reach them
        write_private(target, "".join(f"{k}={json.dumps(v)}\n" for k, v in secrets.items()))
    else:
        sys.exit(f"Unknown mode: {mode}")
    print(f"Wrote {len(secrets)} secrets ({mode})")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
