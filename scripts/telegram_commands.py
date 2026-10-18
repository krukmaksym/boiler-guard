"""Register the bot's command menu (/status) with Telegram. Run once after setup.

  python3 -I scripts/telegram_commands.py [.prod.vars]

Reads TELEGRAM_BOT_TOKEN from the vars file and never prints it.
"""

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script dir
from telegram_chat_id import read_token  # noqa: E402

import bot  # noqa: E402
import notify  # noqa: E402


def main(path=".prod.vars"):
    url = notify.telegram_api_url(read_token(path), "setMyCommands")
    body = json.dumps({"commands": bot.COMMANDS}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed https host
            ok = json.load(resp).get("ok")
    except Exception as exc:
        sys.exit(f"Telegram request failed: {type(exc).__name__}")  # no text: URL holds the token
    names = ", ".join("/" + c["command"] for c in bot.COMMANDS)
    print(f"Registered commands: {names}" if ok else "Telegram rejected the commands")


if __name__ == "__main__":
    main(*sys.argv[1:])
