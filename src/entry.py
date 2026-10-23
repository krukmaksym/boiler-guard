"""Boiler guard: switch the Shelly plug off when the Deye inverter loses grid.

Runs on a cron trigger. Credentials come from Worker Secrets; the STATE KV
namespace only holds the auto-refreshed Deye token, the last desired plug state,
the admin's manual-off flag, notification dedup state and the Telegram update
offset. Optional Telegram notifications, /status replies and /boiler buttons run
after the plug logic and can never block or break it. Updates are fetched by
polling (getUpdates) each run: no webhook, so the Worker still has no public URL.
"""

import asyncio
import json
import time
from urllib.parse import urlencode

from js import AbortSignal, Object
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint, fetch

import bot
import logic
import notify

REQUIRED_SECRETS = (
    "DEYE_APP_ID",
    "APP_SECRET",
    "USER_EMAIL",
    "SHA_256_PASSWORD",
    "INVERTER_SN",
    "SHELLY_HOST",
    "DEVICE_ID",
    "SHELLY_AUTH_KEY",
)
# KV key names, not credentials.
TOKEN_KEY = "deye:token"  # noqa: S105
TOKEN_BACKOFF_KEY = "deye:token_backoff"  # noqa: S105
TOKEN_BACKOFF_SECONDS = 900  # don't hammer Deye's login (account lockout) on bad credentials
DESIRED_KEY = "state:desired"
MODE_KEY = "state:mode"  # "off" while the admin holds the boiler off; absent = automatic
NOTIFY_KEY = "state:notify"
OFFSET_KEY = "telegram:offset"
REQUEST_TIMEOUT_MS = 15000
NOTIFY_TIMEOUT_MS = 10000


class ConfigError(Exception):
    pass


async def http_json(url, *, method="GET", headers=None, body=None, timeout_ms=REQUEST_TIMEOUT_MS):
    """Return (status, parsed JSON or None). Never logs the request.

    Redirects are not followed, so credentials in a body can't be replayed to
    another host; a 3xx simply counts as a failure.
    """
    resp = await fetch(
        url,
        method=method,
        headers=headers or {},
        body=body,
        redirect="manual",
        signal=AbortSignal.timeout(timeout_ms),
    )
    text = await resp.text()
    try:
        return resp.status, json.loads(text)
    except ValueError:
        return resp.status, None


def log(message):
    print(f"boiler-guard: {message}")


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        # No HTTP surface: the Worker is cron-only.
        return Response("Not found", status=404)

    async def scheduled(self, controller, env=None, ctx=None):
        outbox = []
        run = {}  # what this run learned, for /status replies
        state = await self.load_notify_state()
        before = json.dumps(state, sort_keys=True)
        try:
            await self.check(state, outbox, run)
        except Exception as exc:
            signature, detail = error_summary(exc)
            outbox.append(notify.on_error(state, signature, detail, time.time()))
            raise
        else:
            outbox.append(notify.on_success(state))
        finally:
            try:
                await self.flush(state, before, [m for m in outbox if m])
            except Exception as exc:  # notifications must never mask the real outcome
                log(f"notification flush failed: {type(exc).__name__}")
            try:
                await self.answer_commands(run)
            except Exception as exc:
                log(f"command handling failed: {type(exc).__name__}")

    # --- config -----------------------------------------------------------

    def var(self, name, default):
        value = getattr(self.env, name, None)
        return value if isinstance(value, str) and value else default

    def secrets(self):
        values = {name: self.var(name, None) for name in REQUIRED_SECRETS}
        missing = [name for name, value in values.items() if value is None]
        if missing:
            raise ConfigError(f"missing Worker Secrets: {', '.join(missing)}")
        values["SHELLY_HOST"] = logic.validate_shelly_host(values["SHELLY_HOST"])
        return values

    # --- KV ---------------------------------------------------------------

    async def kv_get(self, key):
        value = await self.env.STATE.get(key)
        return value if isinstance(value, str) and value else None

    async def kv_put(self, key, value, ttl=None):
        if ttl:
            options = to_js({"expirationTtl": int(ttl)}, dict_converter=Object.fromEntries)
            await self.env.STATE.put(key, value, options)
        else:
            await self.env.STATE.put(key, value)

    # --- Deye -------------------------------------------------------------

    async def deye_post(self, path, payload, token=None, query=None):
        """The only way this Worker talks to Deye: read-only allowlisted paths."""
        url = logic.deye_url(self.var("DEYE_BASE_URL", "https://eu1-developer.deyecloud.com"), path, query)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"bearer {token}"
        return await http_json(url, method="POST", headers=headers, body=json.dumps(payload))

    async def deye_token(self, secrets, refresh=False):
        if not refresh:
            cached = await self.kv_get(TOKEN_KEY)
            if cached:
                return cached
        if await self.kv_get(TOKEN_BACKOFF_KEY):
            raise logic.ApiError("Deye login failed recently; backing off")
        status, data = await self.deye_post(
            "/v1.0/account/token",
            {
                "appSecret": secrets["APP_SECRET"],
                "email": secrets["USER_EMAIL"],
                "password": secrets["SHA_256_PASSWORD"],
            },
            query={"appId": secrets["DEYE_APP_ID"]},
        )
        try:
            token, ttl = logic.parse_deye_token(status, data)
        except logic.ApiError:
            await self.kv_put(TOKEN_BACKOFF_KEY, "1", TOKEN_BACKOFF_SECONDS)
            raise
        await self.kv_put(TOKEN_KEY, token, ttl)
        log("refreshed Deye token")
        return token

    async def deye_latest(self, secrets):
        payload = {"deviceList": [secrets["INVERTER_SN"]]}
        for refresh in (False, True):
            token = await self.deye_token(secrets, refresh=refresh)
            status, data = await self.deye_post("/v1.0/device/latest", payload, token=token)
            if not logic.is_deye_auth_error(status, data):
                return data
        return data

    async def deye_reading(self, secrets):
        """(grid voltage, battery %, problem) from one Deye call. Voltage is None when
        Deye is unavailable for any reason, and problem is then (code, log detail)."""
        try:
            data = await self.deye_latest(secrets)
        except Exception as exc:  # network errors, bad token response, ...
            log(f"Deye unavailable: {type(exc).__name__}: {exc}")
            return None, None, ("unreachable", type(exc).__name__)
        now = time.time()
        max_age = int(self.var("MAX_DATA_AGE_SECONDS", "900"))
        sn = secrets["INVERTER_SN"]
        key = self.var("GRID_VOLTAGE_KEY", "GridVoltageL1L2")
        voltage = logic.grid_voltage(data, sn, now=now, key=key, max_age_seconds=max_age)
        soc = logic.battery_soc(data, sn, now=now, max_age_seconds=max_age)
        problem = logic.reading_problem(data, sn, now=now, key=key, max_age_seconds=max_age)
        return voltage, soc, problem

    # --- Shelly -----------------------------------------------------------

    async def shelly(self, path, secrets, **params):
        # POST form body keeps auth_key out of URLs and request logs.
        form = {"id": secrets["DEVICE_ID"], "auth_key": secrets["SHELLY_AUTH_KEY"], **params}
        status, data = await http_json(
            f"https://{secrets['SHELLY_HOST']}{path}",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urlencode(form),
        )
        if status != 200 or not isinstance(data, dict) or not data.get("isok"):
            raise logic.ApiError(f"Shelly {path} failed (HTTP {status})")
        return data

    # --- notifications ------------------------------------------------------

    async def load_notify_state(self):
        try:
            state = json.loads(await self.kv_get(NOTIFY_KEY) or "{}")
        except ValueError:
            state = {}
        return state if isinstance(state, dict) else {}

    async def flush(self, state, before, messages):
        """Send queued messages, then persist dedup state. If a send fails the state is
        not saved, so the next run retries that notification."""
        delivered = True
        for text in messages:
            delivered = await self.send_telegram(text) and delivered
        after = json.dumps(state, sort_keys=True)
        if delivered and after != before:
            await self.kv_put(NOTIFY_KEY, after)

    def telegram_config(self):
        token = self.var("TELEGRAM_BOT_TOKEN", None)
        chat_id = self.var("TELEGRAM_CHAT_ID", None)
        return (token, chat_id) if token and chat_id else (None, None)

    async def telegram(self, method, payload):
        """(ok, response) for an allowlisted Bot API method. Never raises."""
        token, _ = self.telegram_config()
        try:
            url = notify.telegram_api_url(token, method)
            status, data = await http_json(
                url,
                method="POST",
                headers={"Content-Type": "application/json"},
                body=json.dumps(payload),
                timeout_ms=NOTIFY_TIMEOUT_MS,
            )
        except notify.NotifyError as exc:
            log(str(exc))
            return False, None
        except Exception as exc:
            # Only the type: the request URL contains the bot token.
            log(f"Telegram {method} failed: {type(exc).__name__}")
            return False, None
        if status != 200 or not (isinstance(data, dict) and data.get("ok")):
            description = data.get("description") if isinstance(data, dict) else None
            log(f"Telegram {method} failed (HTTP {status}, {description!r})")
            return False, data
        return True, data

    async def send_telegram(self, text, reply_to=None, markup=None):
        """True when delivered or when Telegram isn't configured."""
        token, chat_id = self.telegram_config()
        if not token:
            return True
        payload = {
            "chat_id": chat_id,
            "text": self.var("NOTIFY_PREFIX", "") + text,
            "disable_web_page_preview": True,
        }
        if reply_to is not None:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if markup is not None:
            payload["reply_markup"] = markup
        ok, _ = await self.telegram("sendMessage", payload)
        if ok:
            log("Telegram message sent")
        return ok

    # --- commands, keyboard and admin buttons --------------------------------

    async def answer_commands(self, run):
        """Poll for new updates: apply the admin's latest button press, then answer
        /status, /boiler, /help and keyboard presses from the configured chat."""
        token, chat_id = self.telegram_config()
        if not token:
            return
        offset = int(await self.kv_get(OFFSET_KEY) or 0)
        payload = {"timeout": 0, "allowed_updates": ["message", "callback_query"]}
        if offset:
            payload["offset"] = offset
        ok, data = await self.telegram("getUpdates", payload)
        if not ok:
            return
        updates = data.get("result") or []
        # Confirm first: a command that crashes this run must not be replayed forever.
        new_offset = bot.next_offset(updates, offset)
        if new_offset != offset:  # KV write only when updates arrived
            await self.kv_put(OFFSET_KEY, str(new_offset))

        admins = bot.admin_ids(self.var("TELEGRAM_ADMIN_IDS", None))
        await self.handle_taps(bot.taps(updates, chat_id), admins, run)

        wanted = bot.replies(bot.requests(updates, chat_id))
        if "help" in wanted:
            await self.send_telegram(bot.HELP_MESSAGE, reply_to=wanted["help"]["reply_to"])
        boiler = wanted.get("boiler")
        if boiler and str(boiler["user"]) not in admins:
            text = bot.not_admin_message(boiler["user"], bool(admins))
            await self.send_telegram(text, reply_to=boiler["reply_to"])
            boiler = None
        status = wanted.get("status")
        if status or boiler:
            snapshot = await self.status_snapshot(run)
            if status:
                text = bot.status_message(**snapshot)
                await self.send_telegram(text, reply_to=status["reply_to"])
                log("answered status")
            if boiler:
                text = bot.panel_message(bot.status_message(**snapshot))
                await self.send_telegram(text, reply_to=boiler["reply_to"], markup=bot.PANEL_KEYBOARD)
                log("sent control panel")

    async def status_snapshot(self, run):
        """Everything a status reply shows. Shelly is polled fresh (state and live
        power) since this only runs when someone asks."""
        if "plug" in run:
            await asyncio.sleep(1.1)  # Shelly was already called this run (~1 request/second)
        plug, power = await self.poll_plug()
        return {
            "voltage": run.get("voltage"),
            "plug": plug,
            "soc": run.get("soc"),
            "off_below": float(self.var("OFF_VOLTAGE", "205")),
            "manual_off": await self.kv_get(MODE_KEY) == logic.OFF,
            "power": power,
        }

    async def handle_taps(self, presses, admins, run):
        """Answer every button press; apply only the latest valid one from an admin."""
        latest = None
        stale_panels = set()
        for tap in presses:
            verdict = bot.tap_verdict(tap, admins, time.time())
            answer = {"callback_query_id": tap["id"]}
            if verdict == "ok":
                latest = tap
            elif verdict == "not_admin":
                answer["text"] = "🔒 Лише для адміністратора"
                log("ignored a button press from a non-admin")
            else:
                stale_panels.add(tap["panel"])
            # Stops the button's spinner; may fail if the press is too old, which is harmless.
            await self.telegram("answerCallbackQuery", answer)
        for panel in stale_panels:
            await self.send_telegram(bot.STALE_PANEL_MESSAGE, reply_to=panel)
        if latest:
            text = await self.apply_control(latest["action"], run)
            await self.send_telegram(text, reply_to=latest["panel"])
            log(f"admin control: {latest['action']}")

    async def apply_control(self, action, run):
        """Carry out an admin's button press and return the reply. The mode is saved
        only after the plug is in the right state, so a failure changes nothing."""
        target = logic.control_target(action, run.get("desired"))
        switched = False
        if target is not None:
            try:
                secrets = self.secrets()
                channel = int(self.var("SHELLY_CHANNEL", "0"))
                if "plug" in run:
                    await asyncio.sleep(1.1)  # Shelly was already called this run (~1 request/second)
                actual = logic.plug_state(await self.shelly("/device/status", secrets), channel)
                if actual is None:
                    return bot.CONTROL_FAILED_MESSAGE
                if actual != target:
                    await asyncio.sleep(1.1)
                    await self.shelly("/device/relay/control", secrets, channel=channel, turn=target)
                    log(f"admin control: switched plug {actual} -> {target}")
                    switched = True
                run["plug"] = target
            except Exception as exc:
                log(f"admin control: Shelly failed: {type(exc).__name__}")
                return bot.CONTROL_FAILED_MESSAGE
        if action == logic.OFF:
            await self.kv_put(MODE_KEY, logic.OFF)
        else:
            await self.env.STATE.delete(MODE_KEY)
        return bot.control_message(action, target, switched)

    async def poll_plug(self):
        """(on/off, watts) from Shelly; (None, None) when it doesn't answer."""
        try:
            data = await self.shelly("/device/status", self.secrets())
        except Exception as exc:
            log(f"status: Shelly poll failed: {type(exc).__name__}")
            return None, None
        channel = int(self.var("SHELLY_CHANNEL", "0"))
        return logic.plug_state(data, channel), logic.plug_power(data, channel)

    # --- main loop ----------------------------------------------------------

    async def check(self, state, outbox, run):
        secrets = self.secrets()
        channel = int(self.var("SHELLY_CHANNEL", "0"))

        voltage, soc, problem = await self.deye_reading(secrets)
        run.update(voltage=voltage, soc=soc)
        outbox.append(notify.on_voltage(state, voltage, time.time(), problem and problem[0]))
        if voltage is None:
            log(f"no trustworthy grid reading ({problem[1]}); leaving plug as is")
            return

        previous = await self.kv_get(DESIRED_KEY)
        on_at = float(self.var("ON_VOLTAGE", "210"))
        off_below = float(self.var("OFF_VOLTAGE", "205"))
        desired = logic.desired_state(voltage, previous, on_at=on_at, off_below=off_below)
        run["desired"] = desired
        if desired is None or desired == previous:
            log(f"grid {voltage:.1f}V, desired={desired}; no change")
            return

        # Desired state changed. The admin's manual off wins over grid returning.
        held = desired == logic.ON and await self.kv_get(MODE_KEY) == logic.OFF
        target = logic.OFF if held else desired

        # Poll Shelly first, command only if it differs.
        actual = logic.plug_state(await self.shelly("/device/status", secrets), channel)
        run["plug"] = actual
        if actual is None:
            log("Shelly plug offline/unknown; will retry next run")
            if not held:
                outbox.append(notify.on_switch_failed(state, desired))
            return  # don't persist, so the next run tries again

        if actual != target:
            await asyncio.sleep(1.1)  # Shelly cloud allows ~1 request/second
            await self.shelly("/device/relay/control", secrets, channel=channel, turn=target)
            run["plug"] = target
            log(f"grid {voltage:.1f}V: switched plug {actual} -> {target}")
        else:
            log(f"grid {voltage:.1f}V: plug already {actual}" + (" (held off by admin)" if held else ""))

        await self.kv_put(DESIRED_KEY, desired)
        outbox.append(
            notify.on_switched(
                state,
                previous,
                desired,
                voltage,
                switched=actual != target,
                held=held,
                off_below=off_below,
                on_at=on_at,
                now=time.time(),
            )
        )


def error_summary(exc):
    """(dedup signature, human detail). Our own exceptions carry safe messages;
    anything else is reported by type only, in case its text holds a URL or secret."""
    if isinstance(exc, (logic.ApiError, ConfigError)):
        detail = str(exc)
    else:
        detail = type(exc).__name__
    return f"{type(exc).__name__}:{detail}", detail
