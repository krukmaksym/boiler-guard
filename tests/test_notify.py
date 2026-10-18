import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import notify

T0 = 1_800_000_000
TOKEN = "123456789:" + "A" * 35


class TelegramUrlTest(unittest.TestCase):
    def test_valid_token(self):
        self.assertEqual(notify.telegram_send_url(TOKEN), f"https://api.telegram.org/bot{TOKEN}/sendMessage")

    def test_method_allowlist(self):
        self.assertTrue(notify.telegram_api_url(TOKEN, "getUpdates").endswith("/getUpdates"))
        for method in ("deleteMessage", "setWebhook", "sendMessage/../x"):
            with self.assertRaises(notify.NotifyError):
                notify.telegram_api_url(TOKEN, method)

    def test_rejects_odd_tokens(self):
        for bad in (None, "", "abc", "123:short", f"{TOKEN}/../../x", "evil.com/bot1:" + "A" * 35):
            with self.assertRaises(notify.NotifyError):
                notify.telegram_send_url(bad)


class BlindTest(unittest.TestCase):
    def test_notifies_once_after_15_minutes_then_recovers(self):
        state = {}
        self.assertIsNone(notify.on_voltage(state, None, T0))
        self.assertIsNone(notify.on_voltage(state, None, T0 + 14 * 60))
        msg = notify.on_voltage(state, None, T0 + 15 * 60)
        self.assertIn("15 хвилин", msg)
        self.assertIsNone(notify.on_voltage(state, None, T0 + 30 * 60))  # no repeat
        recovered = notify.on_voltage(state, 226.4, T0 + 31 * 60)
        self.assertIn("знову надходять", recovered)
        self.assertIn("226 В", recovered)
        self.assertEqual(state, {})

    def test_short_glitch_is_silent(self):
        state = {}
        notify.on_voltage(state, None, T0)
        self.assertIsNone(notify.on_voltage(state, 230.0, T0 + 120))
        self.assertEqual(state, {})


class SwitchTest(unittest.TestCase):
    def test_grid_lost_and_back(self):
        state = {}
        off = notify.on_switched(state, "on", "off", 0.0, switched=True)
        self.assertIn("Зникло світло", off)
        self.assertIn("вимкнено", off)
        on = notify.on_switched(state, "off", "on", 226.0, switched=True)
        self.assertIn("Світло повернулося", on)
        self.assertIn("увімкнено", on)

    def test_already_in_state_wording(self):
        self.assertIn("уже був вимкнений", notify.on_switched({}, "on", "off", 0.0, switched=False))

    def test_first_run_is_silent(self):
        self.assertIsNone(notify.on_switched({}, None, "on", 230.0, switched=False))

    def test_switch_failed_once_per_state_and_cleared_by_success(self):
        state = {}
        self.assertIn("вимкніть бойлер вручну", notify.on_switch_failed(state, "off"))
        self.assertIsNone(notify.on_switch_failed(state, "off"))
        notify.on_switched(state, "on", "off", 0.0, switched=True)
        self.assertNotIn("switch_failed", state)
        self.assertIsNotNone(notify.on_switch_failed(state, "off"))


class ErrorTest(unittest.TestCase):
    def test_dedup_hourly_and_recovery(self):
        state = {}
        self.assertIn("Помилка", notify.on_error(state, "ApiError:x", "x", T0))
        self.assertIsNone(notify.on_error(state, "ApiError:x", "x", T0 + 60))
        self.assertIsNotNone(notify.on_error(state, "ApiError:y", "y", T0 + 120))  # new error
        self.assertIsNotNone(notify.on_error(state, "ApiError:y", "y", T0 + 120 + 3600))
        self.assertEqual(notify.on_success(state), notify.RECOVERED_MESSAGE)
        self.assertIsNone(notify.on_success(state))


if __name__ == "__main__":
    unittest.main()
