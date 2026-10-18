"""List the Telegram chats your bot has seen, to find TELEGRAM_CHAT_ID.

  1. Add the bot to your chat (for a group: add it, then send /start@<botname> there).
  2. python3 -I scripts/telegram_chat_id.py [.prod.vars]

Reads TELEGRAM_BOT_TOKEN from the vars file and never prints it.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import notify  # noqa: E402


def read_token(path):
    for line in Path(path).read_text().splitlines():
        m = re.match(r"^\s*TELEGRAM_BOT_TOKEN\s*=\s*(.*?)\s*$", line)
        if m:
            return m.group(1).strip("\"'")
    sys.exit(f"TELEGRAM_BOT_TOKEN not found in {path}")


def main(path=".prod.vars"):
    send_url = notify.telegram_send_url(read_token(path))  # validates the token format
    url = send_url.removesuffix("/sendMessage") + "/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310 - fixed https host
            data = json.load(resp)
    except Exception as exc:
        sys.exit(f"Telegram request failed: {type(exc).__name__}")  # no text: URL holds the token
    chats = {}
    for update in data.get("result", []):
        msg = update.get("message") or update.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            chats[chat["id"]] = (chat.get("type"), chat.get("title") or chat.get("first_name"))
    if not chats:
        print("No chats yet: send a message to the bot (or /start@<botname> in the group) and retry.")
    for chat_id, (kind, name) in chats.items():
        print(f'TELEGRAM_CHAT_ID = "{chat_id}"   # {kind}: {name}')


if __name__ == "__main__":
    main(*sys.argv[1:])
