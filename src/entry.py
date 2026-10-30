"""Boiler guard: switch the Shelly plug off when the Deye inverter loses grid.

Runs on a cron trigger. Credentials come from Worker Secrets; the STATE KV
namespace only holds the auto-refreshed Deye token, the last desired plug state,
the admin's manual-off flag, notification dedup state and the Telegram update
offset. Optional Telegram notifications, /status replies and /boiler buttons run
after the plug logic and can never block or break it.

Telegram updates arrive one of two ways:
- polling (default): each cron run calls getUpdates, so replies take up to a minute
  and the Worker needs no public URL;
- webhook (when TELEGRAM_WEBHOOK_SECRET is set): Telegram POSTs each update to
  fetch(), which answers within seconds. Requests must pass every check in
  webhook.py first; anything else gets a 404. The cron then stops polling.

A deployment with EXPIRES_AT (the boiler-guard-test environment) does nothing at
all once that time has passed: no cron work, 404 to every request.
"""

import asyncio
import json
import time
from urllib.parse import urlencode, urlparse

from js import AbortSignal, Object
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint, fetch

import bot
import logic
import notify
import webhook

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
# Saved instead of "on" when grid is back but the admin holds the plug off, so once the
# hold ends (by any path) the next run with grid sees work to do and switches on.
HELD_ON = "on:held"
MODE_KEY = "state:mode"  # "off" while the admin holds the boiler off; absent = automatic
# Set for a few minutes after the admin presses ⛔: the cron then checks the plug really
# stayed off (a switch-on racing the press, or reading a not-yet-visible mode from KV,
# is undone). Outside this window the cron never touches Shelly unless grid changes.
VERIFY_OFF_KEY = "state:verify_off"
VERIFY_OFF_SECONDS = 180
NOTIFY_KEY = "state:notify"
OFFSET_KEY = "telegram:offset"
LAST_UPDATE_KEY = "telegram:last_update"  # webhook: highest update_id handled (replay guard)
WEBHOOK_CHECK_SECONDS = 3600  # webhook mode: getWebhookInfo once an hour (on the hour's first run)
COOLDOWN_KEY = "telegram:cooldown:{kind}"  # webhook: when status / the panel was last sent
STATUS_COOLDOWN_SECONDS = 10  # each of them at most this often (on-demand Deye/Shelly reads)
REQUEST_TIMEOUT_MS = 15000
NOTIFY_TIMEOUT_MS = 10000


UNKNOWN = object()  # a webhook problem check that couldn't tell this run


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
        """Telegram webhook, only when TELEGRAM_WEBHOOK_SECRET is set. Every other
        request (wrong path, method, header, size or source) gets the same 404."""
        secret = self.webhook_secret()
        if secret is None or self.is_expired():
            return Response("Not found", status=404)
        headers = request.headers
        if not webhook.authorized(
            method=request.method,
            path=urlparse(str(request.url)).path,
            header_secret=headers.get(webhook.SECRET_HEADER),
            content_type=headers.get("Content-Type"),
            content_length=headers.get("Content-Length"),
            client_ip=headers.get("CF-Connecting-IP"),
            secret=secret,
            check_ip=self.var("TELEGRAM_IP_CHECK", "on") != "off",
        ):
            return Response("Not found", status=404)
        body = await request.text()
        if len(body.encode()) > webhook.MAX_BODY_BYTES:
            return Response("Not found", status=404)
        try:
            update = json.loads(body)
        except ValueError:
            update = None
        if isinstance(update, dict):
            try:
                await self.handle_webhook_update(update)
            except Exception as exc:
                log(f"webhook handling failed: {type(exc).__name__}")
        # Always 200 once authenticated: Telegram retries anything else, which could
        # repeat an action. Failures are logged and reported in the chat instead.
        return Response("ok", status=200)

    async def handle_webhook_update(self, update):
        update_id = update.get("update_id")
        if isinstance(update_id, int) and not isinstance(update_id, bool):
            last = await self.kv_get(LAST_UPDATE_KEY)
            if last and last.isdigit() and update_id <= int(last):
                log("webhook: duplicate update ignored")
                return
            # Saved before handling, so a retry after a crash can't repeat an action.
            # Best effort: KV is eventually consistent, max_connections=1 does the rest.
            await self.kv_put(LAST_UPDATE_KEY, str(update_id))
        await self.handle_updates([update], {}, live=True)

    async def scheduled(self, controller, env=None, ctx=None):
        if self.is_expired():
            log("deployment expired (EXPIRES_AT); doing nothing")
            return
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
            delivered = False
            try:
                delivered = await self.flush(state, before, [m for m in outbox if m])
            except Exception as exc:  # notifications must never mask the real outcome
                log(f"notification flush failed: {type(exc).__name__}")
            await self.poll_and_check_telegram(state, run, delivered, scheduled_time(controller))

    async def poll_and_check_telegram(self, state, run, delivered, now):
        """After the alerts: poll and answer commands, or in webhook mode check that
        Telegram still delivers, then warn once if updates can't reach the Worker.
        Never raises. delivered: whether the alerts' flush saved the notify state; if
        it didn't, the warning waits, since saving now would drop the unsent alerts.
        now: the cron's scheduled time, so a slow run still hits the hourly check."""
        problem = UNKNOWN
        try:
            secret = self.webhook_secret()
            if secret is None:  # webhook mode: Telegram pushes updates to fetch()
                problem = await self.answer_commands(run)
            else:
                problem = await self.check_webhook(secret, now)
        except Exception as exc:
            log(f"command handling failed: {type(exc).__name__}")
        if not delivered or problem is UNKNOWN:
            return
        try:
            before = json.dumps(state, sort_keys=True)
            message = notify.on_webhook_problem(state, problem)
            await self.flush(state, before, [message] if message else [])
        except Exception as exc:
            log(f"notification flush failed: {type(exc).__name__}")

    # --- config -----------------------------------------------------------

    def var(self, name, default):
        value = getattr(self.env, name, None)
        return value if isinstance(value, str) and value else default

    def webhook_secret(self):
        """TELEGRAM_WEBHOOK_SECRET when set and strong enough, else None (polling mode)."""
        secret = self.var("TELEGRAM_WEBHOOK_SECRET", None)
        if secret is not None and not webhook.valid_secret(secret):
            log("TELEGRAM_WEBHOOK_SECRET is too weak (need 32+ of A-Z a-z 0-9 _ -); webhook disabled")
            return None
        return secret

    def is_expired(self):
        """The test environment must carry EXPIRES_AT; production normally has none."""
        required = self.var("ENVIRONMENT", "production") == "test"
        return webhook.expired(self.var("EXPIRES_AT", None), time.time(), required=required)

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

    async def prod_held_off(self):
        """Test Worker only: True while production's admin holds the boiler off. Both
        Workers switch the same real plug, so the test one must honour that hold too.
        Read from the PROD_STATE binding (env.test only); never written."""
        prod = getattr(self.env, "PROD_STATE", None)
        if prod is None:
            return False
        value = await prod.get(MODE_KEY)
        return isinstance(value, str) and value == logic.OFF

    async def held_off(self):
        """True while an admin holds the boiler off (here or, for the test Worker, in production)."""
        return await self.kv_get(MODE_KEY) == logic.OFF or await self.prod_held_off()

    async def saved_desired(self):
        """(raw KV value, desired state it stands for): HELD_ON counts as "on"."""
        saved = await self.kv_get(DESIRED_KEY)
        return saved, logic.ON if saved == HELD_ON else saved

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
            if logic.deye_login_backoff(status):
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
        """(grid voltage, battery %, backup load W, problem) from one Deye call. Voltage
        is None when Deye is unavailable for any reason, and problem is then
        (code, log detail)."""
        try:
            data = await self.deye_latest(secrets)
        except Exception as exc:  # network errors, bad token response, ...
            log(f"Deye unavailable: {type(exc).__name__}: {exc}")
            return None, None, None, ("unreachable", type(exc).__name__)
        now = time.time()
        max_age = int(self.var("MAX_DATA_AGE_SECONDS", "180"))
        sn = secrets["INVERTER_SN"]
        key = self.var("GRID_VOLTAGE_KEY", "GridVoltageL1L2")
        load_key = self.var("BACKUP_LOAD_KEY", "UPSLoadPower")
        return logic.device_readings(data, sn, now=now, key=key, load_key=load_key, max_age_seconds=max_age)

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
        return delivered

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
        """Polling mode: fetch new updates with getUpdates and handle them. Returns the
        webhook problem: "conflict" when Telegram refuses polling (a webhook is set, or
        something else polls this bot), None when polling worked, UNKNOWN when it
        couldn't tell."""
        token, _ = self.telegram_config()
        if not token:
            return None
        offset = int(await self.kv_get(OFFSET_KEY) or 0)
        payload = {"timeout": 0, "allowed_updates": ["message", "callback_query"]}
        if offset:
            payload["offset"] = offset
        ok, data = await self.telegram("getUpdates", payload)
        if not ok:
            return "conflict" if notify.is_polling_conflict(data) else UNKNOWN
        updates = data.get("result") or []
        # Confirm first: a command that crashes this run must not be replayed forever.
        new_offset = bot.next_offset(updates, offset)
        if new_offset != offset:  # KV write only when updates arrived
            await self.kv_put(OFFSET_KEY, str(new_offset))
        await self.handle_updates(updates, run, live=False)
        return None

    async def check_webhook(self, secret, now):
        """Webhook mode, once an hour: the webhook problem code from getWebhookInfo
        (see notify.webhook_problem), None when deliveries arrive, UNKNOWN when not
        checked this run. Gated on the clock, so the other runs cost no I/O."""
        token, _ = self.telegram_config()
        if not token or int(now // 60) % (WEBHOOK_CHECK_SECONDS // 60):
            return UNKNOWN
        ok, data = await self.telegram("getWebhookInfo", {})
        if not ok:
            return UNKNOWN
        return notify.webhook_problem(data.get("result"), webhook.webhook_path(secret), now)

    async def handle_updates(self, updates, run, live):
        """Apply the admin's latest button press, then answer /status, /boiler, /help
        and keyboard presses from the configured chat. live: webhook mode (answers
        right away, reads Deye on demand, acknowledges button presses)."""
        token, chat_id = self.telegram_config()
        if not token:
            return
        admins = bot.admin_ids(self.var("TELEGRAM_ADMIN_IDS", None))
        await self.handle_taps(bot.taps(updates, chat_id), admins, run, acknowledge=live)

        wanted = bot.replies(bot.requests(updates, chat_id))
        if "help" in wanted:
            await self.send_telegram(bot.HELP_MESSAGE, reply_to=wanted["help"]["reply_to"])
        boiler = wanted.get("boiler")
        if boiler and str(boiler["user"]) not in admins:
            text = bot.not_admin_message(boiler["user"], bool(admins))
            await self.send_telegram(text, reply_to=boiler["reply_to"])
            boiler = None
        status = wanted.get("status")
        if live:
            # On-demand reads hit Deye and Shelly, so a stuck button or a spammer in
            # the chat can't hammer them. Status and the panel cool down separately,
            # so opening the panel right after a status still works.
            if status and not await self.cooled_down("status"):
                status = None
            if boiler and not await self.cooled_down("boiler"):
                boiler = None
        if not (status or boiler):
            return
        snapshot = await self.status_snapshot(run)
        if status:
            text = bot.status_message(**snapshot)
            await self.send_telegram(text, reply_to=status["reply_to"])
            log("answered status")
        if boiler:
            # Polling judges a press when the next cron sees it, so promise only the time
            # that is sure to be accepted.
            ttl = bot.PANEL_TTL_LIVE_SECONDS if live else bot.PANEL_TTL_SECONDS - bot.POLL_DELAY_SECONDS
            text = bot.panel_message(bot.status_message(**snapshot), ttl)
            await self.send_telegram(text, reply_to=boiler["reply_to"], markup=bot.PANEL_KEYBOARD)
            log("sent control panel")

    async def cooled_down(self, kind):
        """True (and starts a new cooldown) when `kind` may be answered on demand now."""
        key = COOLDOWN_KEY.format(kind=kind)
        now = time.time()
        if not webhook.cooled_down(await self.kv_get(key), now, STATUS_COOLDOWN_SECONDS):
            log(f"{kind} request ignored: cooldown")
            return False
        await self.kv_put(key, str(now), ttl=60)  # 60 s is KV's minimum TTL
        return True

    async def ensure_reading(self, run):
        """Read Deye if this invocation hasn't yet (webhook requests, or a cron run
        that failed before reading). Read-only, like every Deye call."""
        if "voltage" in run:
            return
        voltage, soc, load, _ = await self.deye_reading(self.secrets())
        run.update(voltage=voltage, soc=soc, load=load)
        if voltage is not None:
            run["desired"] = logic.desired_state(
                voltage,
                (await self.saved_desired())[1],
                on_at=float(self.var("ON_VOLTAGE", "210")),
                off_below=float(self.var("OFF_VOLTAGE", "205")),
            )

    async def status_snapshot(self, run):
        """Everything a status reply shows. Shelly is polled fresh (state and live
        power) since this only runs when someone asks."""
        await self.ensure_reading(run)
        if "plug" in run:
            await asyncio.sleep(1.1)  # Shelly was already called this run (~1 request/second)
        plug, power = await self.poll_plug()
        return {
            "voltage": run.get("voltage"),
            "plug": plug,
            "soc": run.get("soc"),
            "load": run.get("load"),
            "off_below": float(self.var("OFF_VOLTAGE", "205")),
            "manual_off": await self.held_off(),
            "power": power,
        }

    async def handle_taps(self, presses, admins, run, acknowledge=False):
        """Apply only the latest valid button press from an admin; tell non-admins and
        stale-panel presses why nothing happened.

        acknowledge (webhook mode): answer each press with answerCallbackQuery, which
        stops the button's spinner and shows refusals as a toast. Polling sees presses
        up to a minute later, past Telegram's few-second window, so there refusals go
        to the chat instead and the spinner stops by itself."""
        latest = None
        ttl = bot.PANEL_TTL_LIVE_SECONDS if acknowledge else bot.PANEL_TTL_SECONDS
        stale_panels = set()
        refused = {}  # user -> panel, one reply per non-admin per run
        for tap in presses:
            verdict = bot.tap_verdict(tap, admins, time.time(), ttl)
            if acknowledge:
                answer = {"callback_query_id": tap["id"]}
                if verdict != "ok":
                    answer["text"] = bot.NOT_ADMIN_TOAST if verdict == "not_admin" else bot.STALE_TOAST
                await self.telegram("answerCallbackQuery", answer)
            if verdict == "ok":
                latest = tap
            elif verdict == "not_admin":
                log("ignored a button press from a non-admin")
                if not acknowledge:
                    refused[tap["user"]] = tap["panel"]
            elif not acknowledge:
                stale_panels.add(tap["panel"])
        for user, panel in refused.items():
            await self.send_telegram(bot.not_admin_message(user, bool(admins)), reply_to=panel)
        for panel in stale_panels:
            await self.send_telegram(bot.STALE_PANEL_MESSAGE, reply_to=panel)
        if latest:
            text = await self.apply_control(latest["action"], run)
            await self.send_telegram(text, reply_to=latest["panel"])
            log(f"admin control: {latest['action']}")

    async def apply_control(self, action, run):
        """Carry out an admin's button press and return the reply. The mode is saved
        only after the plug is in the right state, so a failure changes nothing."""
        if action == logic.ON:
            if await self.prod_held_off():
                return bot.PROD_HOLD_MESSAGE
            await self.ensure_reading(run)  # "on" needs to know whether grid is present
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
            await self.kv_put(VERIFY_OFF_KEY, "1", ttl=VERIFY_OFF_SECONDS)
            if (await self.saved_desired())[0] == logic.ON:
                await self.kv_put(DESIRED_KEY, HELD_ON)  # grid is on, but now held off
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

    async def enforce_hold(self, secrets, channel, run):
        """Within VERIFY_OFF_SECONDS of a manual ⛔, make sure the plug really is off.
        Costs one KV read on ordinary runs; Shelly is polled only inside the window.
        The test Worker also enforces production's hold once after saving "on" (it may
        have switched on just before production's ⛔), then saves HELD_ON so it doesn't
        poll the shared Shelly account every minute for the rest of the hold."""
        verifying = await self.kv_get(VERIFY_OFF_KEY) and await self.kv_get(MODE_KEY) == logic.OFF
        prod_race = (
            not verifying and await self.prod_held_off() and await self.kv_get(DESIRED_KEY) == logic.ON
        )
        if not (verifying or prod_race):
            return
        try:
            actual = logic.plug_state(await self.shelly("/device/status", secrets), channel)
            run["plug"] = actual
            if actual == logic.ON:
                await asyncio.sleep(1.1)  # Shelly cloud allows ~1 request/second
                await self.shelly("/device/relay/control", secrets, channel=channel, turn=logic.OFF)
                run["plug"] = logic.OFF
                log("re-applied the admin's manual off: the plug was on while held off")
            if prod_race and actual is not None:
                await self.kv_put(DESIRED_KEY, HELD_ON)
        except Exception as exc:  # retried on the next run while the window lasts
            log(f"manual-off check failed: {type(exc).__name__}")

    async def check(self, state, outbox, run):
        secrets = self.secrets()
        channel = int(self.var("SHELLY_CHANNEL", "0"))

        voltage, soc, load, problem = await self.deye_reading(secrets)
        run.update(voltage=voltage, soc=soc, load=load)
        outbox.append(notify.on_voltage(state, voltage, time.time(), problem and problem[0]))
        if voltage is None:
            log(f"no trustworthy grid reading ({problem[1]}); leaving plug as is")
            await self.enforce_hold(secrets, channel, run)  # switching off is always safe
            return

        saved, previous = await self.saved_desired()
        on_at = float(self.var("ON_VOLTAGE", "210"))
        off_below = float(self.var("OFF_VOLTAGE", "205"))
        desired = logic.desired_state(voltage, previous, on_at=on_at, off_below=off_below)
        run["desired"] = desired
        # Grid unchanged, but the hold that kept the plug off has ended: apply "on" now.
        resume = saved == HELD_ON and desired == logic.ON and not await self.held_off()
        if desired is None or (desired == previous and not resume):
            log(f"grid {voltage:.1f}V, desired={desired}; no change")
            await self.enforce_hold(secrets, channel, run)
            return

        # Desired state changed. The admin's manual off wins over grid returning.
        held = not resume and desired == logic.ON and await self.held_off()
        target = logic.OFF if held else desired

        # Poll Shelly first, command only if it differs.
        actual = logic.plug_state(await self.shelly("/device/status", secrets), channel)
        run["plug"] = actual
        if actual is None:
            log("Shelly plug offline/unknown; will retry next run")
            if not held:
                outbox.append(notify.on_switch_failed(state, desired, resume=resume))
            return  # don't persist, so the next run tries again

        if actual != target:
            await asyncio.sleep(1.1)  # Shelly cloud allows ~1 request/second
            if target == logic.ON and await self.held_off():
                # Last check before switching on: a ⛔ pressed while this run was deciding wins.
                held, target = True, logic.OFF
                log("admin held the boiler off while this run was deciding")
        if actual != target:
            await self.shelly("/device/relay/control", secrets, channel=channel, turn=target)
            run["plug"] = target
            log(f"grid {voltage:.1f}V: switched plug {actual} -> {target}")
        else:
            log(f"grid {voltage:.1f}V: plug already {actual}" + (" (held off by admin)" if held else ""))

        await self.kv_put(DESIRED_KEY, HELD_ON if held else desired)
        if resume:
            notify.on_resumed(state)
            return  # no grid change to announce; the admin's ✅ reply already said so
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


def scheduled_time(controller):
    """The cron trigger's scheduled time in epoch seconds (exact, however long the run
    took so far); the current time if the controller doesn't say."""
    scheduled_ms = getattr(controller, "scheduledTime", None)
    if isinstance(scheduled_ms, (int, float)) and not isinstance(scheduled_ms, bool):
        return scheduled_ms / 1000
    return time.time()


def error_summary(exc):
    """(dedup signature, human detail). Our own exceptions carry safe messages;
    anything else is reported by type only, in case its text holds a URL or secret."""
    if isinstance(exc, (logic.ApiError, ConfigError)):
        detail = str(exc)
    else:
        detail = type(exc).__name__
    return f"{type(exc).__name__}:{detail}", detail
