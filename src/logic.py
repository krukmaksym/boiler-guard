"""Pure decision logic: no I/O, no Workers runtime imports, so it is unit-testable."""

import math
import re
from typing import NamedTuple
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
    return device_readings(data, sn, now, key=key, max_age_seconds=max_age_seconds).voltage


def battery_soc(data, sn, now, key="SOC", max_age_seconds=180):
    """Battery state of charge (%) from the same response, or None."""
    return device_readings(data, sn, now, soc_key=key, max_age_seconds=max_age_seconds).soc


def backup_load(data, sn, now, key="UPSLoadPower", max_age_seconds=180):
    """Power (W) the inverter is feeding to its backup (UPS) output, or None."""
    return device_readings(data, sn, now, load_key=key, max_age_seconds=max_age_seconds).load


def battery_power(data, sn, now, key="BatteryPower", max_age_seconds=180):
    """Battery power (W) from the same response, or None. Deye's sign: negative while
    charging, positive while discharging."""
    return device_readings(data, sn, now, battery_key=key, max_age_seconds=max_age_seconds).battery


def reading_problem(data, sn, now, key="GridVoltageL1L2", max_age_seconds=180):
    """Why grid_voltage() gives None, as (code, log detail); None when it doesn't.

    Codes: "api" (Deye said no), "missing" (inverter not in the response),
    "offline" (Deye marks the inverter offline), "stale" (no fresh upload),
    "no_value" (the voltage key is missing or not a number).
    """
    return device_readings(data, sn, now, key=key, max_age_seconds=max_age_seconds).problem


def grid_relay(data, sn, now, key="GridRelayStatus", max_age_seconds=180):
    """The inverter's grid relay status text (e.g. "Pull-in"), or None."""
    return device_readings(data, sn, now, relay_key=key, max_age_seconds=max_age_seconds).relay


# What Deye reports for the inverter's grid relay: "Pull-in" while connected to the
# grid, "Break" once it has disconnected itself.
RELAY_STATES = {"pull-in": False, "break": True}


def relay_open(status):
    """True when the inverter has disconnected itself from the grid ("Break"), False
    while connected ("Pull-in"), None when not reported or an unknown value (then the
    voltage limits decide, so an odd value can't keep the boiler off)."""
    if not isinstance(status, str):
        return None
    return RELAY_STATES.get(status.strip().lower())


class Reading(NamedTuple):
    """What one Deye /device/latest lookup gives. Only voltage drives the plug; the
    rest is shown in /status. problem: (code, log detail) when voltage is None."""

    voltage: float | None = None
    soc: float | None = None  # battery %
    load: float | None = None  # backup output W
    battery: float | None = None  # battery W, negative while charging
    relay: str | None = None  # grid relay text: "Pull-in" or "Break"
    problem: tuple | None = None
    battery_voltage: float | None = None  # V


def device_readings(
    data,
    sn,
    now,
    key="GridVoltageL1L2",
    soc_key="SOC",
    load_key="UPSLoadPower",
    battery_key="BatteryPower",
    relay_key="GridRelayStatus",
    battery_voltage_key="BatteryVoltage",
    max_age_seconds=180,
):
    """A Reading from one device lookup; the functions above each return one field."""
    device, problem = _fresh_device(data, sn, now, max_age_seconds)
    voltage = _reading(device, key)
    if problem is None and voltage is None:
        problem = ("no_value", f"no {key} value")
    return Reading(
        voltage=voltage,
        soc=_reading(device, soc_key),
        load=_reading(device, load_key),
        battery=_reading(device, battery_key),
        relay=_text(device, relay_key),
        problem=problem,
        battery_voltage=_reading(device, battery_voltage_key),
    )


NOMINAL_BATTERY_VOLTS = 51.2  # a 16-cell LiFePO4 pack, when Deye doesn't report the voltage
INVERTER_EFFICIENCY = 0.95  # battery W per backup-output W, roughly, at these loads


def battery_amps(load_watts, battery_volts=None):
    """Current the battery would supply for this backup load if the inverter ran on
    battery alone (relay "Break"), or None when the load is unknown. Rounded up a
    little by the inverter's losses, so it errs on the safe side."""
    if load_watts is None:
        return None
    volts = battery_volts if battery_volts and battery_volts > 0 else NOMINAL_BATTERY_VOLTS
    return max(load_watts, 0.0) / volts / INVERTER_EFFICIENCY


def _fresh_device(data, sn, now, max_age_seconds):
    """(the inverter's entry, None) if the response is successful, online and fresh,
    else (None, (code, detail)) saying why not."""
    if not isinstance(data, dict) or not data.get("success"):
        msg = data.get("msg") if isinstance(data, dict) else None
        return None, ("api", f"Deye returned no data (msg={str(msg)[:80]!r})")
    device = next(
        (d for d in _dicts(data.get("deviceDataList")) if str(d.get("deviceSn")) == str(sn)),
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
    """The key's value as a finite float, else None ("NaN" would slip past every threshold)."""
    if device is None:
        return None
    for item in _dicts(device.get("dataList")):
        if item.get("key") == key:
            try:
                value = float(item.get("value"))
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) else None
    return None


def _text(device, key):
    """The key's value as a short string, else None."""
    if device is None:
        return None
    for item in _dicts(device.get("dataList")):
        if item.get("key") == key:
            value = item.get("value")
            if not isinstance(value, str):
                return None
            return value.strip()[:40] or None
    return None


def _dicts(value):
    """The dict items of a list from an upstream response; anything else is skipped,
    so an odd response reads as "no data" instead of crashing the run."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dict(value):
    return value if isinstance(value, dict) else {}


def _channel_status(data, channel):
    """(Gen2/3 switch:<channel> dict, Gen1 entries) from a Shelly cloud /device/status
    response, or None when the plug is offline or the response failed."""
    if not isinstance(data, dict) or not data.get("isok"):
        return None
    body = _dict(data.get("data"))
    if body.get("online") is not True:
        return None
    status = _dict(body.get("device_status"))

    def gen1(name):
        items = status.get(name)
        return _dict(items[channel]) if isinstance(items, list) and len(items) > channel else {}

    return _dict(status.get(f"switch:{channel}")), gen1("relays"), gen1("meters")


# Below this the grid is gone; at or above it the grid is present (maybe out of range).
GRID_LOST_BELOW = 100.0


def grid_condition(voltage, off_below=205.0, high_off=265.0):
    """None (no reading), "lost", "low" (present, below OFF_VOLTAGE), "high" (present,
    above HIGH_OFF_VOLTAGE, the inverter's own limit) or "ok"."""
    if voltage is None:
        return None
    if voltage < GRID_LOST_BELOW:
        return "lost"
    if voltage < off_below:
        return "low"
    if voltage > high_off:
        return "high"
    return "ok"


def desired_state(
    voltage, previous, on_at=210.0, off_below=205.0, high_on=255.0, high_off=265.0, disconnected=False
):
    """Hysteresis on both sides: OFF below off_below or above high_off, ON within
    [on_at, high_on], otherwise keep previous (may be None). disconnected: the
    inverter's grid relay is open, so the house runs on battery whatever the voltage."""
    if voltage is None:
        return None
    if disconnected or voltage < off_below or voltage > high_off:
        return OFF
    if on_at <= voltage <= high_on:
        return ON
    return previous


def parse_time(text):
    """Epoch seconds saved in KV as text, or None when missing or garbage."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def cooldown_left(since, now, cooldown_seconds):
    """Seconds the voltage must still stay in range before the boiler may switch back
    on after a voltage switch-off (0: it may now). since: when the voltage came back
    in range, or None when that wasn't recorded yet (the wait starts now)."""
    if since is None:
        return float(cooldown_seconds)
    return min(max(cooldown_seconds - (now - since), 0.0), float(cooldown_seconds))


def plug_state(data, channel=0):
    """ON/OFF from a Shelly cloud /device/status response; None if offline/unknown."""
    found = _channel_status(data, channel)
    if found is None:
        return None
    switch, relay, _ = found
    output = switch.get("output")  # Gen2/Gen3
    if output is None:
        output = relay.get("ison")  # Gen1
    if not isinstance(output, bool):
        return None
    return ON if output else OFF


def plug_power(data, channel=0):
    """Live power draw in watts from a Shelly cloud /device/status response, or None."""
    found = _channel_status(data, channel)
    if found is None:
        return None
    switch, _, meter = found
    power = switch.get("apower")  # Gen2/Gen3
    if power is None:
        power = meter.get("power")  # Gen1
    if not isinstance(power, (int, float)) or isinstance(power, bool) or not math.isfinite(power):
        return None
    return float(power)


def plug_energy(data, channel=0):
    """The plug's energy counter in Wh from a Shelly cloud /device/status response, or
    None. Only differences matter (the heating time since a switch-on)."""
    found = _channel_status(data, channel)
    if found is None:
        return None
    switch, _, meter = found
    total = _dict(switch.get("aenergy")).get("total")  # Gen2/Gen3: Wh
    if total is None:
        total = meter.get("total")  # Gen1: watt-minutes
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            total = total / 60
    if not isinstance(total, (int, float)) or isinstance(total, bool) or not math.isfinite(total):
        return None
    return float(total)


def duration_text(seconds):
    """ "45 хв" or "2 год 5 хв"; at least 1 хв."""
    minutes = max(round(seconds / 60), 1)
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes} хв"
    return f"{hours} год {minutes} хв" if minutes else f"{hours} год"


BATTERY_IDLE_WATTS = 20  # battery power below this (either way) counts as neither charging nor discharging
BATTERY_FULL_PERCENT = 100  # at 100% a trickle charge (or idle) reads as "fully charged"


def battery_state(soc, watts):
    """ "full", "charging", "discharging", "idle", or None when the power is unknown.
    Deye's sign: negative while charging. A full battery's trickle isn't "charging"."""
    if watts is None:
        return None
    if soc is not None and soc >= BATTERY_FULL_PERCENT and watts < BATTERY_IDLE_WATTS:
        return "full"
    if watts <= -BATTERY_IDLE_WATTS:
        return "charging"
    if watts >= BATTERY_IDLE_WATTS:
        return "discharging"
    return "idle"


def battery_direction(soc, watts):
    """ "charging" or "discharging" for the battery's duration, else None. Idle and full
    don't count as a change: while the charge tapers off near full, the power wobbles
    around BATTERY_IDLE_WATTS, and every "idle" reading would restart "заряджається"."""
    state = battery_state(soc, watts)
    return state if state in ("charging", "discharging") else None


def grid_presence(voltage):
    """ "present" (any voltage from GRID_LOST_BELOW up), "absent", or None (no reading)."""
    condition = grid_condition(voltage)
    if condition is None:
        return None
    return "absent" if condition == "lost" else "present"


def track(timeline, updates, now):
    """Apply observations to a timeline {name: {"state", "since", "exact", "wh"}} in
    place; True when anything changed. updates: {name: {"state", "exact", "certain",
    "wh"}}. A state seen for the first time gets exact False (it began at some unknown
    time before) unless "certain" (the bot itself just switched it); a change from a
    known state is exact when the caller says so (the cron sees grid and battery
    changes within a minute; a boiler switched by hand is only noticed later). "wh":
    the plug's energy counter when the boiler was seen switching on."""
    changed = False
    for name, update in updates.items():
        state = update.get("state")
        if state is None:
            continue
        stored = timeline.get(name)
        if isinstance(stored, dict) and stored.get("state") == state:
            continue
        known = isinstance(stored, dict) and stored.get("state") is not None
        timeline[name] = {
            "state": state,
            "since": now,
            "exact": bool(update.get("certain") or (known and update.get("exact"))),
            "wh": update.get("wh"),
        }
        changed = True
    return changed


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
