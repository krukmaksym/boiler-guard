"""Boiler guard: switch the Shelly plug off when the Deye inverter loses grid.

Runs on a cron trigger. Credentials come from Worker Secrets; the STATE KV
namespace only holds the auto-refreshed Deye token and the last desired plug state.
"""

import asyncio
import json
import time
from urllib.parse import urlencode

from js import AbortSignal, Object
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint, fetch

import logic

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
REQUEST_TIMEOUT_MS = 15000


class ConfigError(Exception):
    pass


async def http_json(url, *, method="GET", headers=None, body=None):
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
        signal=AbortSignal.timeout(REQUEST_TIMEOUT_MS),
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
        await self.check()

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

    async def grid_voltage(self, secrets):
        """Grid voltage, or None when Deye is unavailable for any reason."""
        try:
            data = await self.deye_latest(secrets)
        except Exception as exc:  # network errors, bad token response, ...
            log(f"Deye unavailable: {type(exc).__name__}: {exc}")
            return None
        return logic.grid_voltage(
            data,
            secrets["INVERTER_SN"],
            now=time.time(),
            key=self.var("GRID_VOLTAGE_KEY", "GridVoltageL1L2"),
            max_age_seconds=int(self.var("MAX_DATA_AGE_SECONDS", "900")),
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

    # --- main loop ----------------------------------------------------------

    async def check(self):
        secrets = self.secrets()
        channel = int(self.var("SHELLY_CHANNEL", "0"))

        voltage = await self.grid_voltage(secrets)
        if voltage is None:
            log("no trustworthy grid reading; leaving plug as is")
            return

        previous = await self.kv_get(DESIRED_KEY)
        desired = logic.desired_state(
            voltage,
            previous,
            on_at=float(self.var("ON_VOLTAGE", "210")),
            off_below=float(self.var("OFF_VOLTAGE", "205")),
        )
        if desired is None or desired == previous:
            log(f"grid {voltage:.1f}V, desired={desired}; no change")
            return

        # Desired state changed: poll Shelly first, command only if it differs.
        actual = logic.plug_state(await self.shelly("/device/status", secrets), channel)
        if actual is None:
            log("Shelly plug offline/unknown; will retry next run")
            return  # don't persist, so the next run tries again

        if actual != desired:
            await asyncio.sleep(1.1)  # Shelly cloud allows ~1 request/second
            await self.shelly("/device/relay/control", secrets, channel=channel, turn=desired)
            log(f"grid {voltage:.1f}V: switched plug {actual} -> {desired}")
        else:
            log(f"grid {voltage:.1f}V: plug already {actual}")

        await self.kv_put(DESIRED_KEY, desired)
