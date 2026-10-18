"""Notification decisions and Ukrainian message texts. Pure: no I/O, unit-testable.

All dedup state lives in one small dict (stored as JSON in KV under one key), so a
run costs one KV read and writes only when something changes.
"""

import re

TELEGRAM_API = "https://api.telegram.org"
_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")

BLIND_AFTER_SECONDS = 15 * 60
ERROR_REPEAT_SECONDS = 60 * 60  # the same error is re-sent at most hourly


class NotifyError(Exception):
    """Bad Telegram configuration. Never carries the token."""


# The only Telegram Bot API methods this project ever calls.
TELEGRAM_METHODS = frozenset({"sendMessage", "getUpdates", "setMyCommands", "answerCallbackQuery"})


def telegram_api_url(token, method):
    """URL for an allowlisted Bot API method. The token must sit in the path
    (Telegram's API design), so these URLs must never be logged."""
    if method not in TELEGRAM_METHODS:
        raise NotifyError(f"Telegram method {method!r} is not allowed")
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        raise NotifyError("TELEGRAM_BOT_TOKEN has an unexpected format")
    return f"{TELEGRAM_API}/bot{token}/{method}"


def telegram_send_url(token):
    return telegram_api_url(token, "sendMessage")


def _v(voltage):
    return f"{voltage:.0f} В"


# --- messages (Ukrainian, written for the whole family) ----------------------


def grid_message(desired, voltage, switched, held=False):
    if desired == "off":
        tail = "Бойлер вимкнено, щоб не розряджати батарею." if switched else "Бойлер уже був вимкнений."
        return f"⚡️ Зникло світло (мережа {_v(voltage)}). {tail}"
    if held:
        tail = "Бойлер лишається вимкненим: його вимкнули вручну."
    else:
        tail = "Бойлер знову увімкнено." if switched else "Бойлер уже увімкнений."
    return f"✅ Світло повернулося (мережа {_v(voltage)}). {tail}"


def switch_failed_message(desired):
    if desired == "off":
        return (
            "❌ Зникло світло, але не вдалося вимкнути бойлер: розетка Shelly не відповідає. "
            "Будь ласка, вимкніть бойлер вручну."
        )
    return (
        "❌ Світло повернулося, але не вдалося увімкнути бойлер: розетка Shelly не відповідає. "
        "Увімкніть бойлер вручну, коли буде зручно."
    )


BLIND_MESSAGE = (
    "⚠️ Вже 15 хвилин немає даних від інвертора Deye. "
    "Автоматичне вимкнення бойлера тимчасово не працює. "
    "Якщо зникне світло — вимкніть бойлер вручну."
)


def blind_recovered_message(voltage):
    return f"✅ Дані від інвертора Deye знову надходять (мережа {_v(voltage)}). Захист бойлера працює."


def error_message(detail):
    return (
        f"❌ Помилка в boiler-guard: {detail}\n"
        "Програма спробує ще раз за хвилину. Якщо зникло світло — перевірте бойлер вручну."
    )


RECOVERED_MESSAGE = "✅ boiler-guard знову працює нормально."


# --- dedup decisions -----------------------------------------------------------
# Each takes the state dict and returns a message (or None), updating state in place.


def on_voltage(state, voltage, now):
    """Track how long Deye has been unavailable (voltage None)."""
    if voltage is None:
        since = state.setdefault("blind_since", now)
        if not state.get("blind_notified") and now - since >= BLIND_AFTER_SECONDS:
            state["blind_notified"] = True
            return BLIND_MESSAGE
        return None
    notified = state.pop("blind_notified", False)
    state.pop("blind_since", None)
    return blind_recovered_message(voltage) if notified else None


def on_switch_failed(state, desired):
    """Shelly unreachable when a switch was needed: tell once per desired state."""
    if state.get("switch_failed") == desired:
        return None
    state["switch_failed"] = desired
    return switch_failed_message(desired)


def on_switched(state, previous, desired, voltage, switched, held=False):
    """Desired state changed and was applied. Silent on the very first run (no previous).
    held: grid is back but the admin switched the boiler off by hand, so it stays off."""
    state.pop("switch_failed", None)
    if previous is None:
        return None
    return grid_message(desired, voltage, switched, held)


def on_error(state, signature, detail, now):
    """Same error at most once per ERROR_REPEAT_SECONDS; a new error is sent at once."""
    last = state.get("error") or {}
    if last.get("sig") == signature and now - last.get("at", 0) < ERROR_REPEAT_SECONDS:
        return None
    state["error"] = {"sig": signature, "at": now}
    return error_message(detail)


def on_success(state):
    """A run finished without an exception: announce recovery after an error."""
    if state.pop("error", None):
        return RECOVERED_MESSAGE
    return None
