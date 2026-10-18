"""Read-only Telegram commands, answered on the next cron run. Pure: no I/O.

The Worker polls getUpdates once per run instead of exposing a webhook, so it
still has no public URL. Only messages from the configured chat are answered,
and no command can change anything.
"""

STATUS = "/status"
HELP_COMMANDS = frozenset({"/start", "/help"})

HELP_MESSAGE = (
    "👋 Я стежу за світлом і бойлером: вимикаю бойлер, коли зникає світло, "
    "і вмикаю, коли воно повертається.\n"
    "/status — поточний стан (відповідь протягом хвилини)."
)

# Shown in Telegram's command menu (setMyCommands).
COMMANDS = [{"command": "status", "description": "Поточний стан світла і бойлера"}]


def command_of(text):
    """'/status@some_bot extra' -> '/status'; None for non-commands."""
    if not isinstance(text, str) or not text.startswith("/"):
        return None
    return text.split()[0].partition("@")[0].lower()


def requests(updates, chat_id):
    """(command, message_id) for supported commands from the allowed chat only."""
    found = []
    for update in updates or []:
        message = update.get("message") if isinstance(update, dict) else None
        if not isinstance(message, dict):
            continue
        if str((message.get("chat") or {}).get("id")) != str(chat_id):
            continue  # strangers messaging the bot are ignored
        command = command_of(message.get("text"))
        if command == STATUS or command in HELP_COMMANDS:
            found.append((command, message.get("message_id")))
    return found


def next_offset(updates, current):
    """getUpdates offset that confirms everything received (Telegram drops them)."""
    ids = [
        u["update_id"] for u in updates or [] if isinstance(u, dict) and isinstance(u.get("update_id"), int)
    ]
    return max(ids) + 1 if ids else current


def replies(found):
    """At most one help and one status reply per run, each answering the latest ask."""
    latest = {}
    for command, message_id in found:
        kind = "status" if command == STATUS else "help"
        latest[kind] = message_id
    return latest


def status_message(voltage, plug, soc, off_below=205.0):
    lines = ["📊 Поточний стан"]
    if voltage is None:
        lines.append("❔ Немає свіжих даних від інвертора Deye")
    elif voltage >= off_below:
        lines.append(f"💡 Світло є (мережа {voltage:.0f} В)")
    else:
        lines.append(f"⚡️ Світла немає (мережа {voltage:.0f} В)")
    if plug == "on":
        lines.append("🔥 Бойлер увімкнений")
    elif plug == "off":
        lines.append("💤 Бойлер вимкнений")
    else:
        lines.append("❔ Стан бойлера невідомий (розетка Shelly не відповідає)")
    if soc is not None:
        lines.append(f"🔋 Батарея {soc:.0f}%")
    if voltage is None:
        lines.append("⚠️ Автоматичний захист тимчасово не працює")
    else:
        lines.append("🛡 Автоматичний захист працює")
    return "\n".join(lines)
