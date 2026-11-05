"""List every reading Deye Cloud returns for the inverter (key = value unit), and its
battery settings (config/battery), to find key names such as the grid relay status.

  python3 -I scripts/deye_keys.py [.prod.vars] [filter]

filter: only keys containing this text, case-insensitive (e.g. "relay").
Read-only: logs in and calls /v1.0/device/latest and /v1.0/config/battery through
logic.deye_url, like the Worker. Never prints credentials or the token.
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from telegram_chat_id import read_var  # noqa: E402

import logic  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects, like the Worker: urllib would carry the bearer token along."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError for the 3xx


OPENER = urllib.request.build_opener(NoRedirect)


def post(base, path, payload, token=None, query=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"bearer {token}"
    request = urllib.request.Request(  # noqa: S310 - logic.deye_url allows only https *.deyecloud.com
        logic.deye_url(base, path, query), data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with OPENER.open(request, timeout=15) as resp:
            return resp.status, json.load(resp)
    except Exception as exc:
        sys.exit(f"Deye request failed: {type(exc).__name__}")  # no text: may echo the request


def main(path=".prod.vars", wanted=""):
    base = "https://eu1-developer.deyecloud.com"
    try:
        base = read_var(path, "DEYE_BASE_URL")
    except SystemExit:
        pass  # not in the vars file: the Worker's default
    status, data = post(
        base,
        "/v1.0/account/token",
        {
            "appSecret": read_var(path, "APP_SECRET"),
            "email": read_var(path, "USER_EMAIL"),
            "password": read_var(path, "SHA_256_PASSWORD"),
        },
        query={"appId": read_var(path, "DEYE_APP_ID")},
    )
    token, _ = logic.parse_deye_token(status, data)
    sn = read_var(path, "INVERTER_SN")
    _, data = post(base, "/v1.0/device/latest", {"deviceList": [sn]}, token=token)
    device = next((d for d in data.get("deviceDataList") or [] if str(d.get("deviceSn")) == sn), None)
    if device is None:
        sys.exit(f"inverter not in Deye response (msg={data.get('msg')!r})")
    collected = device.get("collectionTime")
    if isinstance(collected, (int, float)) and not isinstance(collected, bool):
        collected = collected / 1000 if collected > 1e12 else collected
        age = f"uploaded {(time.time() - collected) / 60:.0f} min ago"
    else:
        age = f"collectionTime={collected!r}"
    print(f"deviceState={device.get('deviceState')!r}, {age}")
    for item in device.get("dataList") or []:
        key = str(item.get("key"))
        if wanted.lower() in key.lower():
            print(f"{key} = {item.get('value')} {item.get('unit') or ''}".rstrip())
    # Battery settings (read-only), e.g. maxDischargeCurrent = the "Max A Discharge" setting.
    _, config = post(base, "/v1.0/config/battery", {"deviceSn": sn}, token=token)
    if not isinstance(config, dict) or not config.get("success"):
        print(f"config/battery: not available (msg={(config or {}).get('msg')!r})")
        return
    for key, value in config.items():
        if key not in ("code", "msg", "success", "requestId") and wanted.lower() in key.lower():
            print(f"config/battery: {key} = {value}")


if __name__ == "__main__":
    main(*sys.argv[1:])
