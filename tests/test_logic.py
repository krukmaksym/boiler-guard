import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import logic  # noqa: E402

SN = "1234567890"
NOW = 1_800_000_000


def deye(voltage="223.20", collected=NOW - 60, state=1, sn=SN):
    """Synthetic /device/latest response shaped like the real one."""
    return {
        "code": "1000000",
        "msg": "success",
        "success": True,
        "deviceDataList": [
            {
                "deviceSn": sn,
                "deviceType": "INVERTER",
                "deviceState": state,
                "collectionTime": collected,
                "dataList": [
                    {"key": "ACVoltageRUA", "value": "230.00", "unit": "V"},
                    {"key": "GridVoltageL1L2", "value": voltage, "unit": "V"},
                    {"key": "LoadVoltageL1L2", "value": "230.00", "unit": "V"},
                    {"key": "GenVoltage", "value": "105.20", "unit": "V"},
                ],
            }
        ],
    }


def shelly(output=True, online=True):
    return {
        "isok": True,
        "data": {"online": online, "device_status": {"switch:0": {"id": 0, "output": output}}},
    }


class GridVoltageTest(unittest.TestCase):
    def test_reads_grid_key_only(self):
        self.assertEqual(logic.grid_voltage(deye("0.00"), SN, NOW), 0.0)
        self.assertEqual(logic.grid_voltage(deye("223.20"), SN, NOW), 223.2)

    def test_unavailable_cases_return_none(self):
        self.assertIsNone(logic.grid_voltage(None, SN, NOW))
        self.assertIsNone(logic.grid_voltage({"success": False, "msg": "x"}, SN, NOW))
        self.assertIsNone(logic.grid_voltage(deye(sn="other"), SN, NOW))
        self.assertIsNone(logic.grid_voltage(deye(state=2), SN, NOW))
        self.assertIsNone(logic.grid_voltage(deye(collected=NOW - 3600), SN, NOW))
        self.assertIsNone(logic.grid_voltage(deye(collected=None), SN, NOW))
        self.assertIsNone(logic.grid_voltage(deye(voltage="n/a"), SN, NOW))

    def test_missing_grid_key_is_none_not_load_voltage(self):
        data = deye()
        items = data["deviceDataList"][0]["dataList"]
        data["deviceDataList"][0]["dataList"] = [i for i in items if i["key"] != "GridVoltageL1L2"]
        self.assertIsNone(logic.grid_voltage(data, SN, NOW))

    def test_millisecond_timestamp(self):
        self.assertEqual(logic.grid_voltage(deye(collected=(NOW - 60) * 1000), SN, NOW), 223.2)


class HysteresisTest(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(logic.desired_state(210.0, None), logic.ON)
        self.assertEqual(logic.desired_state(204.9, None), logic.OFF)
        self.assertEqual(logic.desired_state(0.0, logic.ON), logic.OFF)

    def test_dead_band_keeps_previous(self):
        for previous in (logic.ON, logic.OFF, None):
            self.assertEqual(logic.desired_state(205.0, previous), previous)
            self.assertEqual(logic.desired_state(209.9, previous), previous)

    def test_unknown_voltage(self):
        self.assertIsNone(logic.desired_state(None, logic.ON))


class PlugStateTest(unittest.TestCase):
    def test_gen3_switch(self):
        self.assertEqual(logic.plug_state(shelly(True)), logic.ON)
        self.assertEqual(logic.plug_state(shelly(False)), logic.OFF)

    def test_gen1_relays(self):
        data = {"isok": True, "data": {"online": True, "device_status": {"relays": [{"ison": False}]}}}
        self.assertEqual(logic.plug_state(data), logic.OFF)

    def test_unknown(self):
        self.assertIsNone(logic.plug_state(shelly(online=False)))
        self.assertIsNone(logic.plug_state({"isok": False}))
        broken = copy.deepcopy(shelly())
        del broken["data"]["device_status"]["switch:0"]["output"]
        self.assertIsNone(logic.plug_state(broken))


class ShellyHostTest(unittest.TestCase):
    def test_accepts_cloud_hosts(self):
        self.assertEqual(logic.validate_shelly_host("shelly-1-eu.shelly.cloud"), "shelly-1-eu.shelly.cloud")
        self.assertEqual(logic.validate_shelly_host("https://shelly-1-eu.shelly.cloud/"), "shelly-1-eu.shelly.cloud")

    def test_rejects_others(self):
        for host in ("evil.com", "shelly.cloud.evil.com", "a.shelly.cloud/x", "a.shelly.cloud@evil.com", ""):
            with self.assertRaises(logic.ApiError):
                logic.validate_shelly_host(host)


class DeyeUrlTest(unittest.TestCase):
    BASE = "https://eu1-developer.deyecloud.com"

    def test_allows_read_only_paths(self):
        self.assertEqual(
            logic.deye_url(self.BASE, "/v1.0/device/latest"),
            "https://eu1-developer.deyecloud.com/v1.0/device/latest",
        )
        self.assertEqual(
            logic.deye_url(self.BASE, "/v1.0/account/token", {"appId": "1"}),
            "https://eu1-developer.deyecloud.com/v1.0/account/token?appId=1",
        )
        self.assertTrue(logic.deye_url("http://localhost:8790", "/v1.0/device/latest"))

    # Every endpoint in https://eu1-developer.deyecloud.com/v2/api-docs (2026-10-18)
    # that can change inverter settings, the station or the account.
    DEYE_WRITE_ENDPOINTS = (
        "/v1.0/order/battery/modeControl",
        "/v1.0/order/battery/parameter/update",
        "/v1.0/order/battery/type/update",
        "/v1.0/order/customControl",  # raw Modbus: can write any register
        "/v1.0/order/gridPeakShaving/control",
        "/v1.0/order/smartload/update",
        "/v1.0/order/sys/energyPattern/update",
        "/v1.0/order/sys/limitControl",
        "/v1.0/order/sys/power/update",
        "/v1.0/order/sys/solarSell/control",
        "/v1.0/order/sys/tou/switch",
        "/v1.0/order/sys/tou/update",
        "/v1.0/order/sys/workMode/update",
        "/v1.0/strategy/dynamicControl",
        "/v1.0/device/addLogger",
        "/v1.0/device/deleteLogger",
        "/v1.0/device/register",
        "/v1.0/station/create",
    )

    def test_refuses_every_write_endpoint(self):
        for path in self.DEYE_WRITE_ENDPOINTS:
            with self.assertRaises(logic.ApiError, msg=path):
                logic.deye_url(self.BASE, path)

    def test_refuses_path_tricks(self):
        for path in (
            "/v1.0/device/latest/../../order/sys/workMode/update",
            "/v1.0/device/latest?x=1",
            "/v1.0/Device/latest",
        ):
            with self.assertRaises(logic.ApiError):
                logic.deye_url(self.BASE, path)

    def test_refuses_foreign_or_odd_bases(self):
        for base in (
            "http://eu1-developer.deyecloud.com",
            "https://evil.com",
            "https://deyecloud.com.evil.com",
            "https://user:pw@eu1-developer.deyecloud.com",
            "https://eu1-developer.deyecloud.com/prefix",
            "http://evil.com",
            "",
        ):
            with self.assertRaises(logic.ApiError):
                logic.deye_url(base, "/v1.0/device/latest")


class DeyeTokenTest(unittest.TestCase):
    def test_parse(self):
        token, ttl = logic.parse_deye_token(200, {"success": True, "accessToken": "t", "expiresIn": "5183999"})
        self.assertEqual(token, "t")
        self.assertEqual(ttl, 5183999 - 86400)

    def test_failure(self):
        with self.assertRaises(logic.ApiError):
            logic.parse_deye_token(200, {"success": False, "msg": "bad password"})

    def test_auth_error_detection(self):
        self.assertTrue(logic.is_deye_auth_error(401, None))
        self.assertTrue(logic.is_deye_auth_error(200, {"success": False, "msg": "token expired"}))
        self.assertFalse(logic.is_deye_auth_error(200, {"success": False, "msg": "device not found"}))
        # Real response for a read-only user hitting a settings endpoint.
        self.assertFalse(logic.is_deye_auth_error(
            200, {"success": False, "code": "2101042", "msg": "auth no operation permission"}))
        self.assertFalse(logic.is_deye_auth_error(200, deye()))


if __name__ == "__main__":
    unittest.main()
