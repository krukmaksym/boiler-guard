"""Pure decision logic: no I/O, no Workers runtime imports, so it is unit-testable."""

import re
from urllib.parse import urlencode, urlparse

ON = "on"
OFF = "off"

# The only Deye endpoints this Worker may ever call. Everything that changes
# inverter settings lives under /order/* and /strategy/*, and is refused here.
DEYE_READ_ONLY_PATHS = frozenset({"/v1.0/account/token", "/v1.0/device/latest"})

_SHELLY_HOST_RE = re.compile(r"^[a-z0-9-]+\.shelly\.cloud$")
_DEYE_HOST_RE = re.compile(r"^[a-z0-9-]+\.deyecloud\.com$")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})  # scripts/mock_deye.py


class ApiError(Exception):
    """An upstream API returned something we can't use. Never carries secrets."""


def deye_url(base, path, query=None):
    """Build a Deye URL, refusing any endpoint outside DEYE_READ_ONLY_PATHS
    and any host other than *.deyecloud.com (or a local mock over http)."""
    if path not in DEYE_READ_ONLY_PATHS:
        raise ApiError(f"refusing Deye endpoint {path!r}: not in the read-only allowlist")
    parsed = urlparse(base or "")
    host = (parsed.hostname or "").lower()
    if parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ApiError("DEYE_BASE_URL must be a bare origin like 'https://eu1-developer.deyecloud.com'")
    https_deye = parsed.scheme == "https" and _DEYE_HOST_RE.match(host)
    local_mock = parsed.scheme == "http" and host in _LOCAL_HOSTS
    if not (https_deye or local_mock):
        raise ApiError("DEYE_BASE_URL must be https://*.deyecloud.com")
    url = f"{parsed.scheme}://{parsed.netloc}{path}"
    return f"{url}?{urlencode(query)}" if query else url


def validate_shelly_host(host):
    """Only ever send the Shelly auth key to a *.shelly.cloud host."""
    host = (host or "").strip().lower()
    host = host.removeprefix("https://").rstrip("/")
    if not _SHELLY_HOST_RE.match(host):
        raise ApiError("SHELLY_HOST must look like 'shelly-<n>-<region>.shelly.cloud'")
    return host


def parse_deye_token(status, data):
    """Return (access_token, ttl_seconds) from a Deye /account/token response."""
    if status != 200 or not isinstance(data, dict) or not data.get("success"):
        msg = data.get("msg") if isinstance(data, dict) else None
        raise ApiError(f"Deye token request failed (HTTP {status}, msg={msg!r})")
    token = data.get("accessToken")
    if not isinstance(token, str) or not token:
        raise ApiError("Deye token response has no accessToken")
    try:
        expires_in = int(data.get("expiresIn") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    # Refresh a day early; fall back to 1 day if Deye doesn't say.
    ttl = max(expires_in - 86400, 3600) if expires_in else 86400
    return token, ttl


def deye_login_backoff(status):
    """True when a failed Deye login should pause further logins. Deye answering and
    refusing (bad credentials, rate limit, odd response) risks an account lockout if
    retried every minute; a 5xx is Deye's own outage and must not blind the guard for
    the whole backoff once Deye is back."""
    return not (isinstance(status, int) and 500 <= status <= 599)


DEYE_NO_PERMISSION_CODE = "2101042"  # "auth no operation permission"


def is_deye_auth_error(status, data):
    """True when a fresh token might help. A permission denial is not one of those:
    re-logging in every minute would never fix it and just hammers Deye's login."""
    if status in (401, 403):
        return True
    if isinstance(data, dict) and data.get("success") is False:
        msg = str(data.get("msg") or "").lower()
        if str(data.get("code")) == DEYE_NO_PERMISSION_CODE or "permission" in msg:
            return False
        return "token" in msg or "auth" in msg
    return False


def grid_voltage(data, sn, now, key="GridVoltageL1L2", max_age_seconds=180):
    """Grid voltage from a Deye /device/latest response, or None if Deye can't be trusted.

    Matches `key` exactly: LoadVoltage*/ACVoltage* are the inverter's own output
    and stay ~230V on battery, so they must never be mistaken for grid.
    None covers: failed response, SN missing, inverter offline, stale or missing reading.
    """
    return _reading(_fresh_device(data, sn, now, max_age_seconds)[0], key)


def battery_soc(data, sn, now, key="SOC", max_age_seconds=180):
    """Battery state of charge (%) from the same response, or None."""
    return _reading(_fresh_device(data, sn, now, max_age_seconds)[0], key)


def backup_load(data, sn, now, key="UPSLoadPower", max_age_seconds=180):
    """Power (W) the inverter is feeding to its backup (UPS) output, or None."""
    return _reading(_fresh_device(data, sn, now, max_age_seconds)[0], key)


def reading_problem(data, sn, now, key="GridVoltageL1L2", max_age_seconds=180):
    """Why grid_voltage() gives None, as (code, log detail); None when it doesn't.

    Codes: "api" (Deye said no), "missing" (inverter not in the response),
    "offline" (Deye marks the inverter offline), "stale" (no fresh upload),
    "no_value" (the voltage key is missing or not a number).
    """
    device, problem = _fresh_device(data, sn, now, max_age_seconds)
    if problem:
        return problem
    if _reading(device, key) is None:
        return "no_value", f"no {key} value"
    return None


def _fresh_device(data, sn, now, max_age_seconds):
    """(the inverter's entry, None) if the response is successful, online and fresh,
    else (None, (code, detail)) saying why not."""
    if not isinstance(data, dict) or not data.get("success"):
        msg = data.get("msg") if isinstance(data, dict) else None
        return None, ("api", f"Deye returned no data (msg={str(msg)[:80]!r})")
    device = next(
        (d for d in data.get("deviceDataList") or [] if str(d.get("deviceSn")) == str(sn)),
        None,
    )
    if device is None:
        return None, ("missing", "inverter not in Deye response")
    if device.get("deviceState") != 1:
        return None, ("offline", f"inverter offline (deviceState={device.get('deviceState')!r})")

    collected = device.get("collectionTime")
    if not isinstance(collected, (int, float)) or isinstance(collected, bool):
        return None, ("stale", "no collectionTime")
    if collected > 1e12:  # milliseconds
        collected /= 1000
    if now - collected > max_age_seconds:
        return None, ("stale", f"last upload {(now - collected) / 60:.0f} min ago")
    return device, None


def _reading(device, key):
    if device is None:
        return None
    for item in device.get("dataList") or []:
        if item.get("key") == key:
            try:
                return float(item.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def desired_state(voltage, previous, on_at=210.0, off_below=205.0):
    """Hysteresis: ON at >= on_at, OFF below off_below, otherwise keep previous (may be None)."""
    if voltage is None:
        return None
    if voltage >= on_at:
        return ON
    if voltage < off_below:
        return OFF
    return previous


def plug_state(data, channel=0):
    """ON/OFF from a Shelly cloud /device/status response; None if offline/unknown."""
    if not isinstance(data, dict) or not data.get("isok"):
        return None
    body = data.get("data") or {}
    if body.get("online") is not True:
        return None
    status = body.get("device_status") or {}
    output = None
    switch = status.get(f"switch:{channel}")  # Gen2/Gen3
    if isinstance(switch, dict):
        output = switch.get("output")
    relays = status.get("relays")  # Gen1
    if output is None and isinstance(relays, list) and len(relays) > channel:
        output = relays[channel].get("ison")
    if not isinstance(output, bool):
        return None
    return ON if output else OFF


def plug_power(data, channel=0):
    """Live power draw in watts from a Shelly cloud /device/status response, or None."""
    if not isinstance(data, dict) or not data.get("isok"):
        return None
    body = data.get("data") or {}
    if body.get("online") is not True:
        return None
    status = body.get("device_status") or {}
    power = None
    switch = status.get(f"switch:{channel}")  # Gen2/Gen3
    if isinstance(switch, dict):
        power = switch.get("apower")
    meters = status.get("meters")  # Gen1
    if power is None and isinstance(meters, list) and len(meters) > channel:
        power = meters[channel].get("power")
    if not isinstance(power, (int, float)) or isinstance(power, bool):
        return None
    return float(power)


def control_target(action, desired_now):
    """Plug state an admin's button press should produce, or None to leave it.

    'off' always switches off. 'on' switches on only while grid is present right now
    (this run's desired state is ON); on battery or without fresh data the boiler is
    never turned on from the bot, it just goes back to automatic control.
    """
    if action == OFF:
        return OFF
    if action == ON and desired_now == ON:
        return ON
    return None
