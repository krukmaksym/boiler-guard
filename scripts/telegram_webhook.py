"""Point the bot's Telegram webhook at a Worker, check it, or remove it.

  python3 -I scripts/telegram_webhook.py set <https://worker-origin> [vars-file]
  python3 -I scripts/telegram_webhook.py info [vars-file]
  python3 -I scripts/telegram_webhook.py delete [vars-file]

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET from the vars file (default
.prod.vars). The webhook path is derived from the secret (src/webhook.py), so the
Worker and Telegram agree without the path being stored anywhere. Never prints the
token, the secret or the path.

Order matters. To switch to webhook mode: upload TELEGRAM_WEBHOOK_SECRET to the
Worker first (it then stops polling), then `set`. To go back: `delete`, then remove
the secret from the Worker (it resumes polling).
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script dir
from telegram_chat_id import read_token, read_var  # noqa: E402

import notify  # noqa: E402
import webhook  # noqa: E402

ALLOWED_UPDATES = ["message", "callback_query"]


def call(token, method, payload=None):
    url = notify.telegram_api_url(token, method, allowed=notify.SETUP_METHODS)
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed https host
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        data = json.load(exc)  # Telegram's error description never contains the token
    except Exception as exc:
        sys.exit(f"Telegram request failed: {type(exc).__name__}")  # no text: URL holds the token
    if not data.get("ok"):
        sys.exit(f"Telegram refused {method}: {data.get('description')}")
    return data.get("result")


def secret_of(path):
    secret = read_var(path, "TELEGRAM_WEBHOOK_SECRET")
    if not webhook.valid_secret(secret):
        sys.exit("TELEGRAM_WEBHOOK_SECRET must be 32-256 characters of A-Z a-z 0-9 _ -")
    return secret


def origin_of(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/") or parsed.query:
        sys.exit("Give the Worker's origin, like https://boiler-guard.<subdomain>.workers.dev")
    return f"https://{parsed.netloc}"


def cmd_set(origin, path=".prod.vars"):
    secret = secret_of(path)
    origin = origin_of(origin)
    call(
        read_token(path),
        "setWebhook",
        {
            "url": origin + webhook.webhook_path(secret),
            "secret_token": secret,
            "allowed_updates": ALLOWED_UPDATES,
            "max_connections": 1,  # one update at a time: presses can't race each other
            "drop_pending_updates": True,  # never replay presses queued while polling
        },
    )
    print(f"Webhook set: {origin}/telegram/<derived from the secret>")


def cmd_info(path=".prod.vars"):
    info = call(read_token(path), "getWebhookInfo")
    url = info.get("url") or ""
    if not url:
        print("No webhook: the bot is in polling mode.")
        return
    parsed = urlparse(url)
    try:
        expected = webhook.webhook_path(read_var(path, "TELEGRAM_WEBHOOK_SECRET"))
        matches = parsed.path == expected
    except SystemExit:
        matches = "unknown (no TELEGRAM_WEBHOOK_SECRET in the vars file)"
    print(f"Webhook host: {parsed.hostname}")
    print(f"Path matches TELEGRAM_WEBHOOK_SECRET: {matches}")
    print(f"Pending updates: {info.get('pending_update_count')}")
    print(f"Max connections: {info.get('max_connections')} | updates: {info.get('allowed_updates')}")
    if info.get("last_error_date"):
        when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(info["last_error_date"]))
        print(f"Last delivery error: {when}: {info.get('last_error_message')}")


def cmd_delete(path=".prod.vars"):
    call(read_token(path), "deleteWebhook", {"drop_pending_updates": True})
    print("Webhook removed: the bot is back in polling mode once the Worker has no TELEGRAM_WEBHOOK_SECRET.")


def main(argv):
    commands = {"set": (cmd_set, 2, 3), "info": (cmd_info, 1, 2), "delete": (cmd_delete, 1, 2)}
    if not argv or argv[0] not in commands or not commands[argv[0]][1] <= len(argv) <= commands[argv[0]][2]:
        sys.exit(__doc__)
    commands[argv[0]][0](*argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
