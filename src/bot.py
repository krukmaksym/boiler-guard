"""Telegram commands and boiler control buttons, handled on the next cron run. Pure: no I/O.

The Worker polls getUpdates once per run instead of exposing a webhook, so it
still has no public URL. Only messages from the configured chat are handled.
/status and /help are read-only for everyone in the chat; the /boiler buttons
work only for Telegram user IDs listed in TELEGRAM_ADMIN_IDS.
"""

STATUS = "/status"
BOILER = "/boiler"
HELP_COMMANDS = frozenset({"/start", "/help"})

# Buttons are refused once their panel is older than this, so a tap that sat in
# Telegram's queue (or an old panel scrolled up in the chat) can't act later.
PANEL_TTL_SECONDS = 10 * 60
ACTIONS = {"boiler:on": "on", "boiler:off": "off"}  # callback_data -> action

HELP_MESSAGE = (
    "👋 Я стежу за світлом і бойлером: вимикаю бойлер, коли зникає світло, "
    "і вмикаю, коли воно повертається.\n"
    "/status — поточний стан (відповідь протягом хвилини).\n"
    "/boiler — керування бойлером (лише для адміністратора)."
)

# Shown in Telegram's command menu (setMyCommands).
COMMANDS = [
    {"command": "status", "description": "Поточний стан світла і бойлера"},
    {"command": "boiler", "description": "Керування бойлером (адміністратор)"},
]

PANEL_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "🔌 Увімкнути", "callback_data": "boiler:on"},
            {"text": "⛔ Вимкнути", "callback_data": "boiler:off"},
        ]
    ]
}

HOLD_NOTE = "Він не увімкнеться сам, навіть коли є світло, доки адміністратор не натисне «Увімкнути»."
AUTO_NOTE = "Ним знову керує автоматичний захист."
AUTO_WAIT_MESSAGE = (
    "🔌 Автоматичний режим відновлено. Зараз світла немає (або немає свіжих даних від "
    "інвертора), тому бойлер не вмикається від батареї. Він увімкнеться сам, коли буде світло."
)
CONTROL_FAILED_MESSAGE = (
    "❌ Не вдалося змінити стан бойлера: розетка Shelly не відповідає. Спробуйте ще раз пізніше."
)
STALE_PANEL_MESSAGE = "⌛️ Ця панель застаріла. Надішліть /boiler, щоб отримати нову."


def command_of(text):
    """'/status@some_bot extra' -> '/status'; None for non-commands."""
    if not isinstance(text, str) or not text.startswith("/"):
        return None
    return text.split()[0].partition("@")[0].lower()


def admin_ids(value):
    """'123, 456' -> {'123', '456'}. Anything that isn't a numeric ID is dropped."""
    if not isinstance(value, str):
        return frozenset()
    return frozenset(part.strip() for part in value.split(",") if part.strip().isdigit())


def _in_chat(message, chat_id):
    return isinstance(message, dict) and str((message.get("chat") or {}).get("id")) == str(chat_id)


def requests(updates, chat_id):
    """(command, message_id, user_id) for supported commands from the allowed chat only."""
    found = []
    for update in updates or []:
        message = update.get("message") if isinstance(update, dict) else None
        if not _in_chat(message, chat_id):
            continue  # strangers messaging the bot are ignored
        command = command_of(message.get("text"))
        if command in (STATUS, BOILER) or command in HELP_COMMANDS:
            found.append((command, message.get("message_id"), (message.get("from") or {}).get("id")))
    return found


def taps(updates, chat_id):
    """Button presses on panels in the allowed chat, oldest first."""
    found = []
    for update in updates or []:
        query = update.get("callback_query") if isinstance(update, dict) else None
        if not isinstance(query, dict) or not _in_chat(query.get("message"), chat_id):
            continue
        action = ACTIONS.get(query.get("data"))
        if action is None:
            continue
        found.append(
            {
                "id": query.get("id"),
                "user": (query.get("from") or {}).get("id"),
                "action": action,
                "panel": query["message"].get("message_id"),
                "panel_date": query["message"].get("date"),
            }
        )
    return found


def tap_verdict(tap, admins, now):
    """'ok', 'not_admin' or 'stale'. Admin is checked by Telegram user ID, which
    Telegram authenticates, not by chat membership."""
    if str(tap.get("user")) not in admins:
        return "not_admin"
    date = tap.get("panel_date")
    if not isinstance(date, int) or isinstance(date, bool) or now - date > PANEL_TTL_SECONDS:
        return "stale"
    return "ok"


def next_offset(updates, current):
    """getUpdates offset that confirms everything received (Telegram drops them)."""
    ids = [
        u["update_id"] for u in updates or [] if isinstance(u, dict) and isinstance(u.get("update_id"), int)
    ]
    return max(ids) + 1 if ids else current


def replies(found):
    """At most one reply per kind per run, each answering the latest ask:
    {kind: (message_id, user_id)}."""
    latest = {}
    for command, message_id, user_id in found:
        kind = {STATUS: "status", BOILER: "boiler"}.get(command, "help")
        latest[kind] = (message_id, user_id)
    return latest


def control_message(action, target, switched):
    """Reply to a button press. Says whether the plug was actually switched or was
    already in that state, so the admin isn't told "switched on" for a no-op."""
    if action == "off":
        head = "⛔ Бойлер вимкнено вручну." if switched else "⛔ Бойлер вже вимкнено."
        return f"{head} {HOLD_NOTE}"
    if target != "on":
        return AUTO_WAIT_MESSAGE
    head = "🔌 Бойлер увімкнено." if switched else "🔌 Бойлер вже увімкнено."
    return f"{head} {AUTO_NOTE}"


def not_admin_message(user_id, configured):
    if not configured:
        return f"🔒 Керування бойлером не налаштоване. Ваш Telegram ID: {user_id}"
    return f"🔒 Керувати бойлером може лише адміністратор. Ваш Telegram ID: {user_id}"


def status_message(voltage, plug, soc, off_below=205.0, manual_off=False):
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
    if manual_off:
        lines.append("✋ Ручний режим: бойлер вимкнено адміністратором")
    if soc is not None:
        lines.append(f"🔋 Батарея {soc:.0f}%")
    if voltage is None:
        lines.append("⚠️ Автоматичний захист тимчасово не працює")
    else:
        lines.append("🛡 Автоматичний захист працює")
    return "\n".join(lines)


def panel_message(status_text):
    return f"{status_text}\n\nКнопки діють 10 хвилин."
