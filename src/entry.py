"""Boiler guard: switch the Shelly plug off when the Deye inverter loses grid.

Runs on a cron trigger. Credentials come from Worker Secrets. The plug state (the
last desired state, the admin's manual-off flags, the switch-on cooldown) lives in
the PlugState Durable Object, which is strongly consistent; the STATE KV namespace
only holds the auto-refreshed Deye token, notification dedup state and the Telegram update
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
from workers import DurableObject, Response, WorkerEntrypoint, fetch

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
# This isolate's memory of the Deye token and the login backoff, used when KV can't be
# read or written, so a KV outage neither blinds the guard nor lets it log in every
# minute. Best effort: an isolate may be replaced at any time.
MEMO = {}
DESIRED_KEY = "state:desired"
# Saved instead of "on" when grid is back but the admin holds the plug off, so once the
# hold ends (by any path) the next run with grid sees work to do and switches on.
HELD_ON = "on:held"
# Saved instead of "off" by every switch-off. Switching back on then waits until the
# voltage has stayed in range for VOLTAGE_COOLDOWN_SECONDS, so a voltage hovering
# around a threshold can't flip the boiler on and off every minute. This covers
# outages too: a short outage and reconnect flaps the same way. (A plain "off" is
# from before this existed.)
COOLDOWN_OFF = "off:cooldown"
ON_PENDING_KEY = "state:on_pending"  # when the voltage came back in range (COOLDOWN_OFF only)
MODE_KEY = "state:mode"  # "off" while the admin holds the boiler off; absent = automatic
# Set for a few minutes after the admin presses ⛔: the cron then checks the plug really
# stayed off (a switch-on racing the press, or reading a not-yet-visible mode from KV,
# is undone). Outside this window the cron never touches Shelly unless grid changes.
VERIFY_OFF_KEY = "state:verify_off"
VERIFY_OFF_SECONDS = 180
NOTIFY_KEY = "state:notify"
OFFSET_KEY = "telegram:offset"
LAST_UPDATE_KEY = "telegram:last_update"  # webhook: highest update_id handled (replay guard)
# After a week without updates Telegram picks the next update_id at random, so a saved
# one must not outlive that: a lower new id would be dropped as a replay for good
# (webhook), or sit below the saved offset (polling). Telegram retries a failed
# delivery for far less than a day.
LAST_UPDATE_TTL_SECONDS = 24 * 3600
OFFSET_TTL_SECONDS = 6 * 24 * 3600
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


# The plug state is kept in one Durable Object instead of KV: KV is eventually
# consistent (a read elsewhere can return a value up to ~60 s old), which could end
# the cooldown early or miss an admin's hold. Everything else stays in KV.
PLUG_KEYS = frozenset({DESIRED_KEY, MODE_KEY, VERIFY_OFF_KEY, ON_PENDING_KEY})
PLUG_OBJECT_NAME = "plug"  # one object per Worker: there is one plug
MIGRATED_KEY = "migrated_from_kv"  # set once the KV values were copied over
EXPIRES_SUFFIX = "|expires"  # epoch seconds after which a value counts as gone
# Read back after every write: a Durable Object read right after a write must see it,
# so a mismatch means the store is broken. Without this a store that loses values
# would fail silently: no cooldown, no outage alerts, and the admin's ⛔ ignored.
READ_BACK_KEYS = frozenset({DESIRED_KEY, MODE_KEY})
# For /status durations: since when the grid, the battery and the boiler have been in
# their current state (logic.track). Display only, so it's not mirrored to KV.
TIMELINE_KEY = "state:timeline"
BOILER_WATTS = "1670"  # heating power at 230 V, when Shelly isn't reporting it right now


class PlugState(DurableObject):
    """The plug state as a tiny store: get_value, put_value (with an optional expiry,
    like KV's TTL) and delete_value, for the PLUG_KEYS only. Its first call copies the
    values that KV held before this existed (KV keeps them, so a rollback still works)."""

    def __init__(self, ctx, env):
        self.ctx = ctx
        self.env = env
        self.migrated = False  # cached once storage says so

    @staticmethod
    def _check(key):
        if key not in PLUG_KEYS:
            raise ValueError(f"not a plug state key: {key!r}")

    async def _ready(self):
        if self.migrated:
            return
        storage = self.ctx.storage
        if await storage.get(MIGRATED_KEY):
            self.migrated = True
            return
        kv = getattr(self.env, "STATE", None)
        for key in sorted(PLUG_KEYS):
            value = await kv.get(key) if kv is not None else None
            if isinstance(value, str) and value and not await storage.get(key):
                await storage.put(key, value)
                if key == VERIFY_OFF_KEY:  # KV's TTL doesn't copy over: restart the window
                    await storage.put(key + EXPIRES_SUFFIX, time.time() + VERIFY_OFF_SECONDS)
        await storage.put(MIGRATED_KEY, "1")
        self.migrated = True

    async def get_value(self, key):
        """The value, or "" when it is missing or expired."""
        self._check(key)
        await self._ready()
        storage = self.ctx.storage
        expires = await storage.get(key + EXPIRES_SUFFIX)
        if isinstance(expires, (int, float)) and time.time() >= expires:
            await self.delete_value(key)
            return ""
        value = await storage.get(key)
        return value if isinstance(value, str) else ""

    async def put_value(self, key, value, ttl=0):
        self._check(key)
        await self._ready()
        storage = self.ctx.storage
        await storage.put(key, str(value))
        if ttl:
            await storage.put(key + EXPIRES_SUFFIX, time.time() + float(ttl))
        else:
            await storage.delete(key + EXPIRES_SUFFIX)

    async def delete_value(self, key):
        """Deletes the value; "1" when there was one, else "" (so callers can skip work)."""
        self._check(key)
        await self._ready()
        storage = self.ctx.storage
        existed = bool(await storage.get(key))
        if existed:  # nothing to delete costs no storage write (called every minute in outages)
            await storage.delete(key)
        if await storage.get(key + EXPIRES_SUFFIX) is not None:
            await storage.delete(key + EXPIRES_SUFFIX)
        return "1" if existed else ""

    async def track(self, updates, now):
        """Apply observations (JSON, see logic.track) to the timeline and return it as
        JSON. Runs in one call, with no other I/O between the read and the write, so a
        cron run and a /status can't overwrite each other's update."""
        storage = self.ctx.storage
        try:
            timeline = json.loads(await storage.get(TIMELINE_KEY) or "{}")
        except ValueError:
            timeline = {}
        if not isinstance(timeline, dict):
            timeline = {}
        if logic.track(timeline, json.loads(updates), float(now)):
            await storage.put(TIMELINE_KEY, json.dumps(timeline))
        return json.dumps(timeline)


def plug_stub(namespace):
    """The PlugState object behind a Durable Object namespace binding."""
    return namespace.get(namespace.idFromName(PLUG_OBJECT_NAME))


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
            await self.kv_put(LAST_UPDATE_KEY, str(update_id), ttl=LAST_UPDATE_TTL_SECONDS)
        await self.handle_updates([update], {}, live=True)

    async def scheduled(self, controller, env=None, ctx=None):
        if self.is_expired():
            log("deployment expired (EXPIRES_AT); doing nothing")
            return
        outbox = []
        run = {}  # what this run learned, for /status replies
        # Without the saved notify state (KV unreadable) the run goes on with an empty
        # one and saves nothing over the real one: alerts must never stop the plug logic.
        loaded = await self.load_notify_state()
        state = loaded if loaded is not None else {}
        before = json.dumps(state, sort_keys=True) if loaded is not None else None
        try:
            await self.check(state, outbox, run)
        except Exception as exc:
            signature, detail = error_summary(exc)
            outbox.append(notify.on_error(state, signature, detail, time.time()))
            raise
        else:
            outbox.append(notify.on_success(state))
        finally:
            saved = False
            try:
                saved = await self.flush(state, before, [m for m in outbox if m])
            except Exception as exc:  # notifications must never mask the real outcome
                log(f"notification flush failed: {type(exc).__name__}")
            await self.poll_and_check_telegram(state, run, saved, scheduled_time(controller))

    async def poll_and_check_telegram(self, state, run, saved, now):
        """After the alerts: poll and answer commands, or in webhook mode check that
        Telegram still delivers, then warn once if updates can't reach the Worker.
        Never raises. saved: whether the alerts' flush saved the notify state; if it
        didn't, the warning waits, since saving now would overwrite a state this run
        couldn't read or write. now: the cron's scheduled time, so a slow run still
        hits the hourly check."""
        problem = UNKNOWN
        try:
            secret = self.webhook_secret()
            if secret is None:  # polling mode; with a secret, Telegram pushes updates to fetch()
                problem = await self.answer_commands(run)
            else:
                problem = await self.check_webhook(secret, now)
        except Exception as exc:
            log(f"command handling failed: {type(exc).__name__}")
        if not saved or problem is UNKNOWN:
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

    def thresholds(self):
        """{on_at, off_below, high_on, high_off} for logic.desired_state. They must be in
        order OFF <= ON <= HIGH_ON <= HIGH_OFF, or the hysteresis breaks (a voltage
        between a swapped pair would count as in range), so anything else is a config error."""
        values = {
            "off_below": self.volts("OFF_VOLTAGE", "205"),
            "on_at": self.volts("ON_VOLTAGE", "210"),
            "high_on": self.volts("HIGH_ON_VOLTAGE", "255"),
            "high_off": self.volts("HIGH_OFF_VOLTAGE", "265"),
        }
        if not values["on_at"] >= values["off_below"]:  # also refuses NaN
            raise ConfigError("ON_VOLTAGE must not be below OFF_VOLTAGE")
        if not values["high_off"] >= values["high_on"] >= values["on_at"]:
            raise ConfigError("need ON_VOLTAGE <= HIGH_ON_VOLTAGE <= HIGH_OFF_VOLTAGE")
        return values

    def volts(self, name, default):
        """A voltage variable as a float. Names the variable when it isn't a number: a
        bare ValueError would reach the chat as just "ValueError"."""
        try:
            return float(self.var(name, default))
        except ValueError:
            raise ConfigError(f"{name} must be a number") from None

    def whole(self, name, default, unit=""):
        """A variable that must be a whole number, 0 or more."""
        try:
            value = int(self.var(name, default))
        except ValueError:
            value = -1
        if value < 0:
            raise ConfigError(f"{name} must be a whole number{unit}, 0 or more")
        return value

    def cooldown_seconds(self):
        """VOLTAGE_COOLDOWN_SECONDS: how long the voltage must stay in range before the
        boiler switches back on after a voltage switch-off (0 turns the wait off)."""
        return self.whole("VOLTAGE_COOLDOWN_SECONDS", "600", " of seconds")

    def blind_off_seconds(self):
        """BLIND_OFF_SECONDS: after this long without a trustworthy Deye reading the
        boiler is switched off, in case the grid went with the data (0: never)."""
        return self.whole("BLIND_OFF_SECONDS", "900", " of seconds")

    def channel(self):
        return self.whole("SHELLY_CHANNEL", "0")

    # --- KV ---------------------------------------------------------------

    async def kv_get(self, key):
        value = await self.env.STATE.get(key)
        return value if isinstance(value, str) and value else None

    # --- plug state (Durable Object) -------------------------------------

    def plug(self):
        namespace = getattr(self.env, "PLUG", None)
        if namespace is None:
            raise ConfigError("missing the PLUG Durable Object binding (see wrangler.toml)")
        return plug_stub(namespace)

    async def state_get(self, key):
        value = await self.plug().get_value(key)
        return value if isinstance(value, str) and value else None

    async def state_put(self, key, value, ttl=None):
        """Raises logic.ApiError when a READ_BACK_KEYS value doesn't read back."""
        await self.plug().put_value(key, value, int(ttl or 0))
        await self.mirror_to_kv(key, value, ttl)
        if key in READ_BACK_KEYS:
            saved = await self.state_get(key)
            if saved != str(value):
                log(f"plug state read-back failed for {key}: got {saved!r}")
                raise logic.ApiError(f"plug state not saved: {key} did not read back (Durable Object)")

    async def state_delete(self, key):
        """Deletes a plug key; True when it was set."""
        existed = bool(await self.plug().delete_value(key))
        if existed:
            await self.mirror_to_kv(key, None)
        return existed

    async def mirror_to_kv(self, key, value, ttl=None):
        """Also write the change to KV, which is never read for these keys any more, so
        that rolling back to a version that reads KV starts from the current state.
        Best effort: the Durable Object is what counts. Changes are rare (no KV write
        on an ordinary run).
        TODO(after 2026-11-20): remove this, and the old plug keys from KV, once the
        Durable Object version has run two weeks without a rollback."""
        try:
            if value is None:
                await self.env.STATE.delete(key)
            else:
                await self.kv_put(key, value, ttl)
        except Exception as exc:
            log(f"KV mirror of {key} failed: {type(exc).__name__}")

    async def track(self, **updates):
        """Record observations for the /status durations and return the timeline;
        {} if that fails (display only: it must never break the boiler logic)."""
        try:
            return json.loads(await self.plug().track(json.dumps(updates), time.time()))
        except Exception as exc:
            log(f"timeline update failed: {type(exc).__name__}")
            return {}

    async def track_boiler(self, state, data, channel, switched):
        """The plug is now in `state`: switched just now by the bot (switched) or found
        so. data: the Shelly status read before switching, for its energy counter."""
        energy = logic.plug_energy(data, channel) if state == logic.ON else None
        await self.track(boiler={"state": state, "certain": switched, "wh": energy})

    def boiler_watts(self, voltage=None):
        """The boiler's heating power at this grid voltage: BOILER_WATTS is its power at
        230 V, and a heater's power goes with the voltage squared (1366-1656 W were seen
        at 207-229 V). The nominal value when the voltage is unknown or not grid."""
        try:
            value = float(self.var("BOILER_WATTS", BOILER_WATTS))
        except ValueError:
            value = float(BOILER_WATTS)
        if not value > 0:
            value = float(BOILER_WATTS)
        if logic.grid_presence(voltage) == "present":
            value *= (voltage / 230.0) ** 2
        return value

    async def prod_held_off(self):
        """Test Worker only: True while production's admin holds the boiler off. Both
        Workers switch the same real plug, so the test one must honour that hold too.
        Read from production's PlugState through the PROD_PLUG binding (env.test only);
        never written."""
        prod = getattr(self.env, "PROD_PLUG", None)
        if prod is None:
            return False
        value = await plug_stub(prod).get_value(MODE_KEY)
        return isinstance(value, str) and value == logic.OFF

    async def held_off(self):
        """True while an admin holds the boiler off (here or, for the test Worker, in production)."""
        return await self.state_get(MODE_KEY) == logic.OFF or await self.prod_held_off()

    async def saved_desired(self):
        """(raw saved value, desired state it stands for): HELD_ON counts as "on",
        COOLDOWN_OFF as "off"."""
        saved = await self.state_get(DESIRED_KEY)
        return saved, {HELD_ON: logic.ON, COOLDOWN_OFF: logic.OFF}.get(saved, saved)

    async def clear_on_pending(self):
        """The voltage left the range again (or the boiler is back on): the wait restarts."""
        await self.state_delete(ON_PENDING_KEY)

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
            try:
                cached = await self.kv_get(TOKEN_KEY)
            except Exception as exc:
                log(f"Deye token unreadable in KV: {type(exc).__name__}")
                cached = MEMO.get("token")
            if cached:
                return cached
        if await self.login_backed_off():
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
                await self.back_off_login()
            raise
        MEMO["token"] = token
        try:
            await self.kv_put(TOKEN_KEY, token, ttl)
        except Exception as exc:  # the token still serves this run, and this isolate's next ones
            log(f"Deye token not saved to KV: {type(exc).__name__}")
        log("refreshed Deye token")
        return token

    async def login_backed_off(self):
        if time.time() < MEMO.get("backoff_until", 0):
            return True
        try:
            return bool(await self.kv_get(TOKEN_BACKOFF_KEY))
        except Exception as exc:
            log(f"Deye login backoff unreadable in KV: {type(exc).__name__}")
            return False

    async def back_off_login(self):
        """No Deye login for TOKEN_BACKOFF_SECONDS (remembered in memory too, for when KV is down)."""
        MEMO["backoff_until"] = time.time() + TOKEN_BACKOFF_SECONDS
        try:
            await self.kv_put(TOKEN_BACKOFF_KEY, "1", TOKEN_BACKOFF_SECONDS)
        except Exception as exc:
            log(f"Deye login backoff not saved to KV: {type(exc).__name__}")

    async def deye_latest(self, secrets):
        payload = {"deviceList": [secrets["INVERTER_SN"]]}
        for refresh in (False, True):
            token = await self.deye_token(secrets, refresh=refresh)
            status, data = await self.deye_post("/v1.0/device/latest", payload, token=token)
            if not logic.is_deye_auth_error(status, data):
                return data
        # Even a brand-new token was refused: another login won't help, and one every
        # minute risks an account lockout. Wait out the backoff like a failed login.
        await self.back_off_login()
        return data

    async def deye_reading(self, secrets):
        """A logic.Reading from one Deye call. Its voltage is None when Deye is
        unavailable for any reason, and its problem is then (code, log detail)."""
        try:
            data = await self.deye_latest(secrets)
        except Exception as exc:  # network errors, bad token response, ...
            # Only our own messages: a runtime error's text may hold a URL (see error_summary).
            log(f"Deye unavailable: {error_summary(exc)[1]}")
            return logic.Reading(problem=("unreachable", type(exc).__name__))
        now = time.time()
        max_age = self.whole("MAX_DATA_AGE_SECONDS", "180", " of seconds")
        sn = secrets["INVERTER_SN"]
        key = self.var("GRID_VOLTAGE_KEY", "GridVoltageL1L2")
        load_key = self.var("BACKUP_LOAD_KEY", "UPSLoadPower")
        return logic.device_readings(
            data,
            sn,
            now=now,
            key=key,
            load_key=load_key,
            battery_key=self.var("BATTERY_POWER_KEY", "BatteryPower"),
            relay_key=self.var("GRID_RELAY_KEY", "GridRelayStatus"),
            battery_voltage_key=self.var("BATTERY_VOLTAGE_KEY", "BatteryVoltage"),
            max_age_seconds=max_age,
        )

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
        """The saved notify state; {} when there is none or it is garbage; None when KV
        can't be read (then nothing may be saved over it, see flush)."""
        try:
            raw = await self.kv_get(NOTIFY_KEY)
        except Exception as exc:
            log(f"notify state unreadable: {type(exc).__name__}")
            return None
        try:
            state = json.loads(raw or "{}")
        except ValueError:
            state = {}
        return state if isinstance(state, dict) else {}

    async def flush(self, state, before, messages):
        """Send what earlier runs couldn't deliver, then this run's messages, in order.
        From the first failed send on, everything is kept in the state (notify.keep_unsent)
        and retried by the next run: most alerts are produced only on the run that saw the
        change, so they can't simply be produced again. Then save the state if it changed.
        before: the state as loaded (JSON), or None when it couldn't be read: then
        nothing is saved. True when the saved state is up to date."""
        now = time.time()
        queue = notify.take_unsent(state, now) + [{"text": text, "at": now} for text in messages]
        for index, item in enumerate(queue):
            if not await self.send_telegram(notify.late_text(item, now)):
                notify.keep_unsent(state, queue[index:])
                break
        if before is None:
            return False
        after = json.dumps(state, sort_keys=True)
        if after != before:
            await self.kv_put(NOTIFY_KEY, after)
        return True

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
        saved = await self.kv_get(OFFSET_KEY)
        # A corrupted value must not stop polling for good. Starting over is safe: Telegram
        # already dropped every update the last saved offset confirmed.
        offset = int(saved) if saved and saved.isdigit() else 0
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
            await self.kv_put(OFFSET_KEY, str(new_offset), ttl=OFFSET_TTL_SECONDS)
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
        secrets = self.secrets()
        thresholds = self.thresholds()
        reading = await self.deye_reading(secrets)
        run.update(reading._asdict())
        if reading.voltage is not None:
            previous = (await self.saved_desired())[1]
            disconnected = logic.relay_open(reading.relay) is True
            run["desired"] = logic.desired_state(
                reading.voltage, previous, **thresholds, disconnected=disconnected
            )

    async def status_snapshot(self, run):
        """Everything a status reply shows. Shelly is polled fresh (state and live
        power) since this only runs when someone asks."""
        await self.ensure_reading(run)
        if "plug" in run:
            await asyncio.sleep(1.1)  # Shelly was already called this run (~1 request/second)
        plug, power, energy = await self.poll_plug()
        return {
            "voltage": run.get("voltage"),
            "plug": plug,
            "soc": run.get("soc"),
            "battery": run.get("battery"),
            "relay": run.get("relay"),
            "load": run.get("load"),
            "load_amps": logic.battery_amps(run.get("load"), run.get("battery_voltage")),
            "manual_off": await self.held_off(),
            "power": power,
            "wait_seconds": await self.cooldown_remaining(),
            **self.reply_limits(with_off=True),
            "durations": await self.durations(run, plug, power, energy),
        }

    async def cooldown_remaining(self):
        """Seconds until a voltage switch-off may end (the voltage is back in range and
        has to stay there), or None when the boiler isn't waiting for that."""
        if (await self.saved_desired())[0] != COOLDOWN_OFF:
            return None
        since = logic.parse_time(await self.state_get(ON_PENDING_KEY))
        if since is None:
            return None
        try:
            return logic.cooldown_left(since, time.time(), self.cooldown_seconds())
        except ConfigError:
            return None

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
        if action == logic.ON and await self.prod_held_off():
            return bot.PROD_HOLD_MESSAGE
        try:
            secrets = self.secrets()
            channel = self.channel()
            if action == logic.ON:
                await self.ensure_reading(run)  # "on" needs to know whether grid is present
        except (ConfigError, logic.ApiError, ValueError) as exc:
            log(f"admin control: bad config: {type(exc).__name__}")
            return bot.CONFIG_FAILED_MESSAGE
        target = logic.control_target(action, run.get("desired"))
        switched = False
        if target is not None:
            try:
                if "plug" in run:
                    await asyncio.sleep(1.1)  # Shelly was already called this run (~1 request/second)
                run["plug"] = None  # marks Shelly as called, even if this call fails
                plug_data = await self.shelly("/device/status", secrets)
                actual = logic.plug_state(plug_data, channel)
                if actual is None:
                    return bot.CONTROL_FAILED_MESSAGE
                if actual != target:
                    await asyncio.sleep(1.1)
                    await self.shelly("/device/relay/control", secrets, channel=channel, turn=target)
                    log(f"admin control: switched plug {actual} -> {target}")
                    switched = True
                run["plug"] = target
                await self.track_boiler(target, plug_data, channel, switched=switched)
            except Exception as exc:
                log(f"admin control: Shelly failed: {type(exc).__name__}")
                return bot.CONTROL_FAILED_MESSAGE
        try:
            if action == logic.OFF:
                await self.state_put(MODE_KEY, logic.OFF)
                await self.state_put(VERIFY_OFF_KEY, "1", ttl=VERIFY_OFF_SECONDS)
                if (await self.saved_desired())[0] == logic.ON:
                    await self.state_put(DESIRED_KEY, HELD_ON)  # grid is on, but now held off
            else:
                await self.state_delete(MODE_KEY)
                if target == logic.ON and (await self.saved_desired())[0] == COOLDOWN_OFF:
                    # The admin switched on during the voltage cooldown: that ends it.
                    await self.state_put(DESIRED_KEY, logic.ON)
                    await self.clear_on_pending()
        except logic.ApiError:
            return bot.STATE_FAILED_MESSAGE
        off_grid = logic.relay_open(run.get("relay")) is True
        voltage = run.get("voltage")
        return bot.control_message(
            action, target, switched, voltage=voltage, off_grid=off_grid, limits=self.reply_limits()
        )

    def reply_limits(self, with_off=False):
        """The thresholds messages name, read from the Worker's variables on every call,
        so a config change shows up in the very next message. {} (the code's defaults)
        if misconfigured. with_off: also off_below and high_off (for /status)."""
        try:
            thresholds = self.thresholds()
            limits = {
                "on_at": thresholds["on_at"],
                "high_on": thresholds["high_on"],
                "cooldown_minutes": round(self.cooldown_seconds() / 60),
            }
            if with_off:
                limits.update(off_below=thresholds["off_below"], high_off=thresholds["high_off"])
            return limits
        except (ConfigError, ValueError):
            return {}

    async def poll_plug(self):
        """(on/off, watts, energy Wh) from Shelly; Nones when it doesn't answer."""
        try:
            data = await self.shelly("/device/status", self.secrets())
        except Exception as exc:
            log(f"status: Shelly poll failed: {type(exc).__name__}")
            return None, None, None
        channel = self.channel()
        return (
            logic.plug_state(data, channel),
            logic.plug_power(data, channel),
            logic.plug_energy(data, channel),
        )

    async def durations(self, run, plug, power, energy):
        """For /status: {"grid"|"battery"|"boiler": (seconds, True), "heated": seconds}
        for what the bot saw start. Records this observation too (a boiler switched by
        hand is first seen here). A state that already held when tracking began, or a
        hand switch noticed only later, shows no duration: "понад 3 хв" right after a
        deploy, while the grid had been on for hours, was true but misleading. The
        heating time is the energy used since the switch-on divided by the heating
        power: the thermostat's on/off cycles add up correctly."""
        voltage = run.get("voltage")
        timeline = await self.track(
            grid={"state": logic.grid_presence(voltage), "exact": True},
            battery={"state": logic.battery_direction(run.get("soc"), run.get("battery")), "exact": True},
            boiler={"state": plug, "wh": energy if plug == logic.ON else None},
        )
        now = time.time()  # after track(): a state first seen just now must not be in the future
        found = {}
        current = {
            "grid": logic.grid_presence(voltage),
            "battery": logic.battery_direction(run.get("soc"), run.get("battery")),
            "boiler": plug,
        }
        for name, state in current.items():
            entry = timeline.get(name)
            if not isinstance(entry, dict) or entry.get("state") != state or state is None:
                continue
            if name == "boiler" and state != logic.ON:
                continue
            since = entry.get("since")
            if isinstance(since, (int, float)) and entry.get("exact"):
                found[name] = (max(now - since, 0.0), True)
                if name == "boiler":
                    base = entry.get("wh")
                    if isinstance(base, (int, float)) and energy is not None and energy >= base:
                        heating_now = power is not None and power >= bot.HEATING_WATTS
                        watts = power if heating_now else self.boiler_watts(voltage)
                        found["heated"] = (energy - base) / watts * 3600
        return found

    # --- main loop ----------------------------------------------------------

    async def enforce_hold(self, secrets, channel, run):
        """Within VERIFY_OFF_SECONDS of a manual ⛔, make sure the plug really is off.
        Costs one KV read on ordinary runs; Shelly is polled only inside the window.
        The test Worker also enforces production's hold once after saving "on" (it may
        have switched on just before production's ⛔), then saves HELD_ON so it doesn't
        poll the shared Shelly account every minute for the rest of the hold."""
        verifying = await self.state_get(VERIFY_OFF_KEY) and await self.state_get(MODE_KEY) == logic.OFF
        prod_race = (
            not verifying and await self.prod_held_off() and await self.state_get(DESIRED_KEY) == logic.ON
        )
        if not (verifying or prod_race):
            return
        try:
            run["plug"] = None  # marks Shelly as called, even if this call fails
            actual = logic.plug_state(await self.shelly("/device/status", secrets), channel)
            run["plug"] = actual
            if actual == logic.ON:
                await asyncio.sleep(1.1)  # Shelly cloud allows ~1 request/second
                await self.shelly("/device/relay/control", secrets, channel=channel, turn=logic.OFF)
                run["plug"] = logic.OFF
                log("re-applied the admin's manual off: the plug was on while held off")
                await self.track(boiler={"state": logic.OFF, "certain": True})
            if prod_race and actual is not None:
                await self.state_put(DESIRED_KEY, HELD_ON)
        except Exception as exc:  # retried on the next run while the window lasts
            log(f"manual-off check failed: {type(exc).__name__}")

    async def check(self, state, outbox, run):
        secrets = self.secrets()
        channel = self.channel()
        thresholds = self.thresholds()
        cooldown = self.cooldown_seconds()
        blind_off = self.blind_off_seconds()

        reading = await self.deye_reading(secrets)
        run.update(reading._asdict())
        voltage, relay, problem = reading.voltage, reading.relay, reading.problem
        relay_state = logic.relay_open(relay)  # True: off grid, False: on grid, None: unknown
        disconnected = relay_state is True
        after_gap = "blind_since" in state  # the previous run(s) had no trustworthy reading
        now = time.time()
        outbox.append(notify.on_voltage(state, voltage, now, problem and problem[0], blind_off))
        if voltage is None:
            blind_for = now - state.get("blind_since", now)
            due = bool(blind_off) and blind_for >= blind_off
            log(
                f"no trustworthy grid reading ({problem[1]}); "
                + ("fail-safe off" if due else "leaving plug as is")
            )
            if not (due and await self.blind_switch_off(secrets, channel, state, outbox, run, blind_for)):
                await self.enforce_hold(secrets, channel, run)  # switching off is always safe
            return
        # A change first seen after a gap in Deye's data may have happened any time
        # during the gap, so it gets no exact start (and so no duration).
        await self.track(
            grid={"state": logic.grid_presence(voltage), "exact": not after_gap},
            battery={"state": logic.battery_direction(reading.soc, reading.battery), "exact": not after_gap},
        )

        saved, previous = await self.saved_desired()
        desired = logic.desired_state(voltage, previous, **thresholds, disconnected=disconnected)
        run["desired"] = desired
        back_since = None  # when the voltage came back in range after a switch-off
        if desired == logic.ON and saved == COOLDOWN_OFF:
            now = time.time()
            back_since = logic.parse_time(await self.state_get(ON_PENDING_KEY))
            if logic.cooldown_left(back_since, now, cooldown) > 0:
                if back_since is None:
                    await self.state_put(ON_PENDING_KEY, str(now))
                    # Tell the family now; the switch-on message follows after the wait.
                    held_now = await self.held_off()
                    outbox.append(notify.on_back(state, voltage, now, round(cooldown / 60), held_now))
                log(f"grid {voltage:.1f}V back in range; switching on after {cooldown}s of stable voltage")
                await self.enforce_hold(secrets, channel, run)
                return
        if desired == logic.OFF and saved == COOLDOWN_OFF:
            await self.clear_on_pending()
            condition = logic.grid_condition(voltage, thresholds["off_below"], thresholds["high_off"])
            if state.get("blind_off") and (disconnected or condition != "ok"):
                # The first reading after a fail-safe switch-off shows a real reason to be
                # off (outage, voltage, relay): announce that, as the switch-off it would
                # have been, so the return is later told with the right words and duration.
                outbox.append(
                    self.switch_alert(
                        state,
                        logic.ON,
                        logic.OFF,
                        voltage,
                        thresholds,
                        cooldown,
                        False,
                        disconnected=relay_state,
                    )
                )
            elif logic.grid_condition(voltage) == "lost":
                outbox.append(notify.on_lost_again(state, voltage, time.time()))
            elif disconnected:
                cooldown_minutes = round(cooldown / 60)
                outbox.append(notify.on_lost_again(state, voltage, time.time(), "relay", cooldown_minutes))
            else:  # grid present, but outside the switch-on window (e.g. 196 V after an outage)
                outbox.append(
                    notify.on_back_but_low(
                        state,
                        voltage,
                        time.time(),
                        on_at=thresholds["on_at"],
                        high_on=thresholds["high_on"],
                        cooldown_minutes=round(cooldown / 60),
                    )
                )
        elif desired == logic.OFF and saved == logic.OFF:
            await self.state_put(DESIRED_KEY, COOLDOWN_OFF)  # switched off before the cooldown existed
        # Grid unchanged, but the hold that kept the plug off has ended: apply "on" now.
        resume = saved == HELD_ON and desired == logic.ON and not await self.held_off()
        if desired is None or (desired == previous and not resume):
            log(f"grid {voltage:.1f}V, relay={relay!r}, desired={desired}; no change")
            await self.enforce_hold(secrets, channel, run)
            return

        # Desired state changed. The admin's manual off wins over grid returning.
        held = not resume and desired == logic.ON and await self.held_off()
        target = logic.OFF if held else desired

        # Poll Shelly first, command only if it differs.
        run["plug"] = None  # marks Shelly as called, even if this call fails
        plug_data = await self.shelly("/device/status", secrets)
        actual = logic.plug_state(plug_data, channel)
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
            log(f"grid {voltage:.1f}V, relay={relay!r}: switched plug {actual} -> {target}")
        else:
            log(f"grid {voltage:.1f}V: plug already {actual}" + (" (held off by admin)" if held else ""))
        await self.track_boiler(target, plug_data, channel, switched=actual != target)

        if held:
            await self.state_put(DESIRED_KEY, HELD_ON)
        else:
            await self.state_put(DESIRED_KEY, COOLDOWN_OFF if desired == logic.OFF else desired)
        if saved == COOLDOWN_OFF and desired == logic.ON:
            await self.clear_on_pending()
        if resume:
            notify.on_resumed(state)
            return  # no grid change to announce; the admin's ✅ reply already said so
        outbox.append(
            self.switch_alert(
                state,
                previous,
                desired,
                voltage,
                thresholds,
                cooldown,
                actual != target,
                held=held,
                back_since=back_since,
                disconnected=relay_state,
            )
        )

    def switch_alert(self, state, previous, desired, voltage, thresholds, cooldown, switched, **more):
        """notify.on_switched with this deployment's thresholds and cooldown."""
        return notify.on_switched(
            state,
            previous,
            desired,
            voltage,
            switched=switched,
            off_below=thresholds["off_below"],
            on_at=thresholds["on_at"],
            now=time.time(),
            high_off=thresholds["high_off"],
            high_on=thresholds["high_on"],
            cooldown_minutes=round(cooldown / 60),
            **more,
        )

    async def blind_switch_off(self, secrets, channel, state, outbox, run, blind_for):
        """Fail-safe: Deye has given no trustworthy reading for BLIND_OFF_SECONDS, so the
        grid may be gone without the guard seeing it. Switch the boiler off and save it
        as an ordinary switch-off, so it comes back by the usual path: a reading in
        range, then the cooldown. Does nothing when the boiler is already off for the
        guard (switched off, held off by the admin, or never decided). True when Shelly
        was called (so the caller makes no second call this run). A Shelly failure is
        only logged and retried next run: the blind alert already asked the family to
        switch the boiler off by hand."""
        if (await self.saved_desired())[0] != logic.ON:
            return False
        try:
            run["plug"] = None  # marks Shelly as called, even if this call fails
            plug_data = await self.shelly("/device/status", secrets)
            actual = logic.plug_state(plug_data, channel)
            run["plug"] = actual
            if actual is None:
                log("fail-safe off: Shelly plug offline/unknown; will retry next run")
                return True
            if actual == logic.ON:
                await asyncio.sleep(1.1)  # Shelly cloud allows ~1 request/second
                await self.shelly("/device/relay/control", secrets, channel=channel, turn=logic.OFF)
                run["plug"] = logic.OFF
        except Exception as exc:
            log(f"fail-safe off failed: {type(exc).__name__}; will retry next run")
            return True
        switched = actual == logic.ON
        log(
            f"no trustworthy reading for {blind_for:.0f}s: "
            + ("switched plug on -> off" if switched else "plug already off")
        )
        await self.track_boiler(logic.OFF, plug_data, channel, switched=switched)
        await self.state_put(DESIRED_KEY, COOLDOWN_OFF)
        await self.clear_on_pending()
        minutes = round(self.cooldown_seconds() / 60)
        outbox.append(notify.on_blind_off(state, time.time(), blind_for, switched, minutes))
        return True


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
