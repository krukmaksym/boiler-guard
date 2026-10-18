"""Local stand-in for the Deye API, for simulating grid loss in `pywrangler dev`.

  python3 -I scripts/mock_deye.py [port]          # default 8790
  curl "http://localhost:8790/set?v=0"            # grid lost
  curl "http://localhost:8790/set?v=230"          # grid back

Run the Worker with: pywrangler dev --var DEYE_BASE_URL:http://localhost:8790
Never logs request headers or bodies (they carry real credentials).
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

state = {"voltage": "230.00"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/set":
            state["voltage"] = f"{float(parse_qs(url.query)['v'][0]):.2f}"
            print(f"mock: GridVoltageL1L2 = {state['voltage']}V", flush=True)
            return self.reply({"voltage": state["voltage"]})
        self.reply({"success": False, "msg": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        path = urlparse(self.path).path
        if path == "/v1.0/account/token":
            return self.reply(
                {"code": "1000000", "success": True, "accessToken": "mock-token", "expiresIn": "5183999"}
            )
        if path == "/v1.0/device/latest":
            print(f"mock: served device/latest ({state['voltage']}V)", flush=True)
            devices = [
                {
                    "deviceSn": sn,
                    "deviceType": "INVERTER",
                    "deviceState": 1,
                    "collectionTime": int(time.time()),
                    "dataList": [
                        {"key": "GridVoltageL1L2", "value": state["voltage"], "unit": "V"},
                        {"key": "LoadVoltageL1L2", "value": "230.00", "unit": "V"},
                    ],
                }
                for sn in body.get("deviceList", [])
            ]
            return self.reply(
                {"code": "1000000", "msg": "success", "success": True, "deviceDataList": devices}
            )
        self.reply({"success": False, "msg": "not found"}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
    print(f"mock: Deye on http://localhost:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
