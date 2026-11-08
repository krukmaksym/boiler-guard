"""Notification decisions and Ukrainian message texts. Pure: no I/O, unit-testable.

All dedup state lives in one small dict (stored as JSON in KV under one key), so a
run costs one KV read and writes only when something changes.
"""

import re
from urllib.parse import urlparse

import logic

TELEGRAM_API = "https://api.telegram.org"
_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")

BLIND_AFTER_SECONDS = 5 * 60
ERROR_REPEAT_SECONDS = 60 * 60  # the same error is re-sent at most hourly


class NotifyError(Exception):
    """Bad Telegram configuration. Never carries the token."""


# The only Telegram Bot API methods the Worker ever calls. answerCallbackQuery only
# works in webhook mode (polling sees presses too late to answer them); getWebhookInfo
# is read-only, for checking in webhook mode that Telegram still delivers here.
TELEGRAM_METHODS = frozenset(
    {"sendMessage", "getUpdates", "setMyCommands", "answerCallbackQuery", "getWebhookInfo"}
)
# Used only by scripts/telegram_webhook.py on your machine, never by the Worker.
SETUP_METHODS = frozenset({"setWebhook", "deleteWebhook", "getWebhookInfo"})


def telegram_api_url(token, method, allowed=TELEGRAM_METHODS):
    """URL for an allowlisted Bot API method. The token must sit in the path
    (Telegram's API design), so these URLs must never be logged."""
    if method not in allowed:
        raise NotifyError(f"Telegram method {method!r} is not allowed")
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        raise NotifyError("TELEGRAM_BOT_TOKEN has an unexpected format")
    return f"{TELEGRAM_API}/bot{token}/{method}"


def telegram_send_url(token):
    return telegram_api_url(token, "sendMessage")


def _v(voltage):
    return f"{voltage:.0f} В"


duration_text = logic.duration_text  # "45 хв" or "2 год 5 хв"


# --- messages (Ukrainian, written for the whole family) ----------------------


def _on_tail(switched, held, waited):
    if held:
        return "Бойлер лишається вимкненим: його вимкнули вручну."
    if not switched:
        return "Бойлер уже увімкнений."
    if waited:
        return f"Напруга стабільна вже {duration_text(waited)}, тож бойлер знову увімкнено."
    return "Бойлер знову увімкнено."


def grid_message(desired, voltage, switched, held=False, lasted=None, cooldown_minutes=0, waited=None):
    """lasted: how long the grid was gone, named when it returns (if known). waited:
    how long the voltage then stayed in range before the boiler switched back on."""
    if desired == "off":
        if switched:
            tail = (
                "Бойлер вимкнено, щоб не розряджати батарею; він увімкнеться, коли світло "
                f"повернеться{_cooldown_tail(cooldown_minutes)}."
            )
        else:
            tail = "Бойлер уже був вимкнений."
        return f"⚡️ Зникло світло (мережа {_v(voltage)}). {tail}"
    tail = _on_tail(switched, held, waited)
    gap = f" Світла не було {duration_text(lasted)}." if lasted is not None else ""
    return f"✅ Світло повернулося (мережа {_v(voltage)}).{gap} {tail}"


INVERTER_ISLAND_NOTE = "Інвертор відключається від мережі й живить дім від батареї."


def _cooldown_tail(cooldown_minutes):
    return f" і протримається {cooldown_minutes} хв" if cooldown_minutes else ""


def low_voltage_message(voltage, switched, off_below, on_at, cooldown_minutes=0):
    """Grid is present but below OFF_VOLTAGE. One decimal: 194.6 V must not read as 195 V.
    Says what each number means: a bare "(поріг 195 В)" next to "підніметься до 210 В"
    read as two contradicting thresholds."""
    head = f"📉 Низька напруга в мережі: {voltage:.1f} В, нижча за {off_below:.0f} В,"
    until = f"коли напруга підніметься до {on_at:.0f} В{_cooldown_tail(cooldown_minutes)}"
    if not switched:
        return f"{head} а бойлер уже був вимкнений. Він увімкнеться, {until}."
    return f"{head} тож бойлер вимкнено. Він увімкнеться, {until}."


def high_voltage_message(voltage, switched, high_off, high_on, cooldown_minutes=0, on_grid=False):
    """Grid is present but above HIGH_OFF_VOLTAGE. on_grid: Deye says the inverter's
    grid relay is still closed, so the note that it disconnects is left out."""
    head = f"📈 Висока напруга в мережі: {voltage:.1f} В, вища за {high_off:.0f} В,"
    head += " а бойлер уже був вимкнений." if not switched else " тож бойлер вимкнено."
    if not on_grid:
        head = f"{head} {INVERTER_ISLAND_NOTE}"
    until = f"коли напруга опуститься до {high_on:.0f} В{_cooldown_tail(cooldown_minutes)}"
    # "Бойлер", not "Він": after the inverter note "він" would read as the inverter.
    return f"{head} Бойлер увімкнеться, {until}."


def disconnected_message(voltage, switched, cooldown_minutes=0):
    """Grid present, but the inverter has opened its grid relay."""
    head = f"⚠️ Інвертор відключився від мережі (мережа {voltage:.1f} В) і живить дім від батареї."
    if not switched:
        return f"{head} Бойлер уже був вимкнений."
    return (
        f"{head} Бойлер вимкнено; він увімкнеться, коли інвертор знову підключиться до мережі"
        f"{_cooldown_tail(cooldown_minutes)}."
    )


def reconnected_message(voltage, switched, held=False, lasted=None, waited=None):
    gap = f" Відключення тривало {duration_text(lasted)}." if lasted is not None else ""
    return (
        f"✅ Інвертор знову підключився до мережі ({voltage:.1f} В).{gap} {_on_tail(switched, held, waited)}"
    )


def voltage_restored_message(voltage, switched, held=False, lasted=None, high=False, waited=None):
    tail = _on_tail(switched, held, waited)
    kind = "Висока" if high else "Низька"
    gap = f" {kind} напруга трималася {duration_text(lasted)}." if lasted is not None else ""
    return f"✅ Напруга в мережі відновилася ({voltage:.1f} В).{gap} {tail}"


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


# Why Deye gave no usable reading, by logic.reading_problem() code (plus "unreachable").
BLIND_REASONS = {
    "unreachable": "Deye Cloud не відповідає",
    "api": "Deye Cloud повертає помилку",
    "missing": "Deye не повертає даних про інвертор",
    "offline": "Deye показує інвертор офлайн (ймовірно, у його Wi-Fi-логера немає інтернету)",
    "stale": "дані від інвертора не оновлюються (ймовірно, у його Wi-Fi-логера немає інтернету)",
    "no_value": "Deye не передає напругу мережі",
}


def blind_message(reason=None):
    cause = BLIND_REASONS.get(reason)
    return (
        "⚠️ Вже 5 хвилин немає даних від інвертора Deye"  # BLIND_AFTER_SECONDS, fixed in code
        + (f": {cause}" if cause else "")
        + ". Автоматичне вимкнення бойлера тимчасово не працює. "
        "Якщо зникне світло — вимкніть бойлер вручну."
    )


def blind_recovered_message(voltage, lasted):
    return (
        f"✅ Дані від інвертора Deye знову надходять (мережа {_v(voltage)}). "
        f"Їх не було {duration_text(lasted)}. Захист бойлера працює."
    )


def error_message(detail):
    return (
        f"❌ Помилка в boiler-guard: {detail}\n"
        "Програма спробує ще раз за хвилину. Якщо зникло світло — перевірте бойлер вручну."
    )


RECOVERED_MESSAGE = "✅ boiler-guard знову працює нормально."

# Why Telegram updates aren't reaching the Worker, by webhook problem code.
WEBHOOK_PROBLEM_MESSAGES = {
    "conflict": (
        "⚠️ boiler-guard: Telegram не дає Worker отримувати оновлення: або в Telegram встановлено "
        "webhook, а Worker його не приймає (немає дійсного TELEGRAM_WEBHOOK_SECRET), або цього бота "
        "опитує ще інша програма. Бот не відповідає на команди і кнопки. Захист бойлера працює."
    ),
    "missing": (
        "⚠️ boiler-guard: Worker чекає оновлень через webhook, але в Telegram його не встановлено "
        "(або він веде деінде), тому бот не відповідає на команди і кнопки. Захист бойлера працює."
    ),
    "failing": (
        "⚠️ boiler-guard: Telegram не може доставити оновлення на webhook (Worker їх відхиляє або "
        "недоступний), тому бот не відповідає на команди і кнопки. Захист бойлера працює."
    ),
}


# --- dedup decisions -----------------------------------------------------------
# Each takes the state dict and returns a message (or None), updating state in place.


def on_voltage(state, voltage, now, reason=None):
    """Track how long Deye has been unavailable (voltage None). reason: why, as a
    BLIND_REASONS code, named in the alert; the recovery message says how long it lasted."""
    if voltage is None:
        since = state.setdefault("blind_since", now)
        if not state.get("blind_notified") and now - since >= BLIND_AFTER_SECONDS:
            state["blind_notified"] = True
            return blind_message(reason)
        return None
    notified = state.pop("blind_notified", False)
    since = state.pop("blind_since", now)
    return blind_recovered_message(voltage, now - since) if notified else None


RESUME_FAILED_MESSAGE = (
    "❌ Не вдалося увімкнути бойлер після ручного вимкнення: розетка Shelly не відповідає. "
    "Програма пробуватиме щохвилини; увімкніть бойлер вручну, якщо потрібно."
)


def on_switch_failed(state, desired, resume=False):
    """Shelly unreachable when a switch was needed: tell once per desired state.
    resume: the switch-on was due because the admin's hold ended, not the grid."""
    if state.get("switch_failed") == desired:
        return None
    state["switch_failed"] = desired
    return RESUME_FAILED_MESSAGE if resume else switch_failed_message(desired)


def on_resumed(state):
    """The switch-on after the admin's hold succeeded: a later failure is news again."""
    state.pop("switch_failed", None)


def on_switched(
    state,
    previous,
    desired,
    voltage,
    switched,
    held=False,
    off_below=205.0,
    on_at=210.0,
    now=None,
    high_off=265.0,
    high_on=255.0,
    cooldown_minutes=0,
    back_since=None,
    disconnected=None,
):
    """Desired state changed and was applied. Silent on the very first run (no previous).
    held: grid is back but the admin switched the boiler off by hand, so it stays off.
    A switch-off with grid still present (voltage too low or too high) is remembered,
    so the switch-on after it says the voltage recovered rather than that the grid
    came back. The switch-off time is remembered too, so the switch-on says how long
    it lasted. cooldown_minutes: how long the voltage must stay in range before the
    boiler switches back on, named in the switch-off message. back_since: when the
    voltage came back in range (the switch-on waited from then), if known.
    disconnected: the inverter's grid relay was open (True), closed (False) or not
    reported (None) at the switch-off."""
    state.pop("switch_failed", None)
    if desired == "off":
        if now is not None:
            state["off_since"] = now
        condition = logic.grid_condition(voltage, off_below, high_off)
        if disconnected and condition != "lost":
            condition = "relay"
        for flag in ("low_voltage", "high_voltage", "relay_voltage", "back_notified", "present_notified"):
            state.pop(flag, None)
        if condition in ("low", "high", "relay"):
            state[f"{condition}_voltage"] = True
        if previous is None:
            return None
        if condition == "relay":
            return disconnected_message(voltage, switched, cooldown_minutes)
        if condition == "low":
            return low_voltage_message(voltage, switched, off_below, on_at, cooldown_minutes)
        if condition == "high":
            return high_voltage_message(
                voltage, switched, high_off, high_on, cooldown_minutes, on_grid=disconnected is False
            )
        return grid_message(desired, voltage, switched, cooldown_minutes=cooldown_minutes)
    low = state.pop("low_voltage", False)
    high = state.pop("high_voltage", False)
    relay = state.pop("relay_voltage", False)
    since = state.pop("off_since", None)
    announced = state.pop("back_notified", False)
    state.pop("present_notified", None)
    if previous is None:
        return None
    if announced:  # on_back already said the grid is back and for how long
        return switched_on_after_wait_message(switched, held, now - back_since if back_since else None)
    back = back_since if back_since is not None else now
    lasted = back - since if back is not None and since is not None else None
    waited = now - back_since if now is not None and back_since is not None else None
    if relay:
        return reconnected_message(voltage, switched, held, lasted, waited)
    if low or high:
        return voltage_restored_message(voltage, switched, held, lasted, high=high, waited=waited)
    return grid_message(desired, voltage, switched, held, lasted, waited=waited)


def switched_on_after_wait_message(switched, held, waited):
    """The cooldown is over (the grid's return was announced when it started)."""
    if held:
        return None  # the "back" message already said it stays off
    stable = f"напруга стабільна вже {duration_text(waited)}" if waited else "напруга стабільна"
    if switched:
        return f"🔥 Бойлер знову увімкнено: {stable}."
    return f"🔥 Бойлер уже увімкнений: {stable}."


def back_message(voltage, lasted, cooldown_minutes, cause, held=False):
    """The grid (or the voltage, or the inverter's connection) is back; the boiler
    switches on only after the cooldown. cause: "low"/"high"/"relay" or None (outage)."""
    gap = duration_text(lasted) if lasted is not None else None
    if cause == "relay":
        head = f"✅ Інвертор знову підключився до мережі ({voltage:.1f} В)."
        gap = f" Відключення тривало {gap}." if gap else ""
    elif cause in ("low", "high"):
        head = f"✅ Напруга в мережі відновилася ({voltage:.1f} В)."
        kind = "Висока" if cause == "high" else "Низька"
        gap = f" {kind} напруга трималася {gap}." if gap else ""
    else:
        head = f"✅ Світло повернулося (мережа {_v(voltage)})."
        gap = f" Світла не було {gap}." if gap else ""
    if held:
        tail = "Бойлер лишається вимкненим: його вимкнули вручну."
    else:
        tail = f"Бойлер увімкнеться за {cooldown_minutes} хв, якщо напруга буде стабільною."
    return f"{head}{gap} {tail}"


def on_back(state, voltage, now, cooldown_minutes, held=False):
    """The cooldown after a switch-off has just started (the voltage is back in range):
    announce it now rather than only when the boiler switches on. Once per episode;
    on_lost_again re-arms it after a real outage during the wait."""
    if state.get("back_notified"):
        return None
    state["back_notified"] = True
    since = state.get("off_since")
    lasted = now - since if since is not None else None
    cause = next((c for c in ("relay", "low", "high") if state.get(f"{c}_voltage")), None)
    return back_message(voltage, lasted, cooldown_minutes, cause, held)


def on_back_but_low(state, voltage, now, on_at=210.0, high_on=255.0, cooldown_minutes=0):
    """After an outage the grid is back, but its voltage keeps the boiler off (below
    on_at, or above high_on). Say so at once instead of staying silent until the
    voltage reaches the window, and count the outage only up to now: from here the
    wait is a voltage problem, and on_back later says how long that lasted. Once per
    episode; nothing when the switch-off was for voltage or the relay already."""
    if state.get("back_notified"):  # a dip after "back" only restarts the wait, silently
        return None
    if any(state.get(flag) for flag in ("low_voltage", "high_voltage", "relay_voltage")):
        return None
    high = voltage > high_on
    since = state.get("off_since")
    lasted = now - since if since is not None else None
    state["off_since"] = now
    state["high_voltage" if high else "low_voltage"] = True
    state["present_notified"] = True
    wait = _cooldown_tail(cooldown_minutes)
    if high:
        head = f"💡 Світло повернулося (мережа {_v(voltage)}), але напруга зависока для бойлера."
        until = f"коли напруга опуститься до {high_on:.0f} В{wait}"
    else:
        head = f"💡 Світло повернулося (мережа {_v(voltage)}), але напруга низька для бойлера."
        until = f"коли напруга підніметься до {on_at:.0f} В{wait}"
    gap = f" Світла не було {duration_text(lasted)}." if lasted is not None else ""
    return f"{head}{gap} Бойлер увімкнеться, {until}."


def on_lost_again(state, voltage, now, cause=None, cooldown_minutes=0):
    """The grid is gone again during the cooldown (cause None), or the inverter has cut
    itself off it again (cause "relay"), after the family was told it was back: say so
    once and re-arm on_back, so the next return is announced with how long this lasted.
    A dip in voltage only restarts the wait, silently."""
    told_back = state.pop("back_notified", False)
    told_present = state.pop("present_notified", False)
    if not (told_back or told_present):
        return None
    state["off_since"] = now
    for flag in ("low_voltage", "high_voltage", "relay_voltage"):
        state.pop(flag, None)
    if cause == "relay":
        state["relay_voltage"] = True
        return (
            f"⚠️ Інвертор знову відключився від мережі (мережа {voltage:.1f} В) і живить дім від "
            "батареї. Бойлер лишається вимкненим; він увімкнеться, коли інвертор знову "
            f"підключиться до мережі{_cooldown_tail(cooldown_minutes)}."
        )
    return f"⚡️ Світло знову зникло (мережа {_v(voltage)}). Бойлер лишається вимкненим."


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


def is_polling_conflict(response):
    """True for Telegram's 409 refusal of getUpdates: a webhook is set, or another
    process polls the same bot. Either way this Worker gets no updates."""
    return isinstance(response, dict) and response.get("error_code") == 409


WEBHOOK_ERROR_WINDOW_SECONDS = 3600


def webhook_problem(info, expected_path, now):
    """From getWebhookInfo's result: "missing" (not set to this Worker's path),
    "failing" (deliveries erroring within the last hour with updates piling up), or
    None when Telegram delivers here fine."""
    info = info if isinstance(info, dict) else {}
    if urlparse(str(info.get("url") or "")).path != expected_path:
        return "missing"
    last_error = info.get("last_error_date")
    recent = isinstance(last_error, int) and now - last_error < WEBHOOK_ERROR_WINDOW_SECONDS
    pending = info.get("pending_update_count")
    if recent and isinstance(pending, int) and pending > 0:
        return "failing"
    return None


def on_webhook_problem(state, problem):
    """Tell once per problem while Telegram updates can't reach the Worker. problem: a
    WEBHOOK_PROBLEM_MESSAGES code, or None when updates were confirmed to arrive. (Pass
    nothing at all when that couldn't be checked, so a blip doesn't reset the dedup.)
    "failing" is told only when two checks in a row see it: one delivery that failed
    and is being retried (say, a request over the CPU limit) is not an outage."""
    if problem != "failing":
        state.pop("webhook_failing_seen", None)
    elif not state.get("webhook_failing_seen"):
        state["webhook_failing_seen"] = True
        return None
    if problem is None:
        state.pop("webhook_problem", None)
        return None
    if state.get("webhook_problem") == problem:
        return None
    state["webhook_problem"] = problem
    return WEBHOOK_PROBLEM_MESSAGES[problem]
