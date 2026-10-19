"""Post the bot's menu message (📊 Статус / 🎛 Керування buttons) to the chat. Pin it
there by hand afterwards; run again only if the menu changes.

  python3 -I scripts/telegram_menu.py [.prod.vars]

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the vars file and never prints the token.
"""

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script dir
from telegram_chat_id import read_token, read_var  # noqa: E402

import bot  # noqa: E402
import notify  # noqa: E402


def main(path=".prod.vars"):
    url = notify.telegram_api_url(read_token(path), "sendMessage")
    payload = {
        "chat_id": read_var(path, "TELEGRAM_CHAT_ID"),
        "text": bot.MENU_MESSAGE,
        "reply_markup": bot.MENU_KEYBOARD,
    }
    req = urllib.request.Request(  # noqa: S310
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed https host
            ok = json.load(resp).get("ok")
    except Exception as exc:
        sys.exit(f"Telegram request failed: {type(exc).__name__}")  # no text: URL holds the token
    print(
        "Menu posted: open the chat, long-press the message and choose Pin."
        if ok
        else "Telegram rejected the menu"
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
