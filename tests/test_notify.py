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
        # answerCallbackQuery included: a cron run sees presses too late to answer them.
        for method in ("deleteMessage", "setWebhook", "answerCallbackQuery", "sendMessage/../x"):
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
        msg = notify.on_voltage(state, None, T0 + 15 * 60, "stale")
        self.assertIn("15 хвилин", msg)
        self.assertIn("не оновлюються", msg)
        self.assertIsNone(notify.on_voltage(state, None, T0 + 30 * 60))  # no repeat
        recovered = notify.on_voltage(state, 226.4, T0 + 32 * 60)
        self.assertIn("знову надходять", recovered)
        self.assertIn("226 В", recovered)
        self.assertIn("не було 32 хв", recovered)
        self.assertEqual(state, {})

    def test_unknown_reason_is_left_out(self):
        self.assertIn("Deye. Автоматичне", notify.blind_message(None))
        self.assertIn("Deye. Автоматичне", notify.blind_message("whatever"))
        self.assertIn("офлайн", notify.blind_message("offline"))

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

    def test_outage_length_on_return(self):
        state = {}
        notify.on_switched(state, "on", "off", 0.0, switched=True, now=T0)
        on = notify.on_switched(state, "off", "on", 226.0, switched=True, now=T0 + 2 * 3600 + 5 * 60)
        self.assertIn("Світла не було 2 год 5 хв.", on)
        self.assertEqual(state, {})

    def test_low_voltage_length_on_return(self):
        state = {}
        notify.on_switched(state, "on", "off", 204.2, switched=True, now=T0)
        on = notify.on_switched(state, "off", "on", 212.0, switched=True, now=T0 + 40 * 60)
        self.assertIn("Низька напруга трималася 40 хв.", on)

    def test_no_length_when_switch_off_time_unknown(self):
        # e.g. the switch-off happened before this was deployed
        on = notify.on_switched({}, "off", "on", 226.0, switched=True, now=T0)
        self.assertEqual(on, "✅ Світло повернулося (мережа 226 В). Бойлер знову увімкнено.")

    def test_duration_text(self):
        self.assertEqual(notify.duration_text(10), "1 хв")
        self.assertEqual(notify.duration_text(59 * 60), "59 хв")
        self.assertEqual(notify.duration_text(3600), "1 год")
        self.assertEqual(notify.duration_text(3600 + 29 * 60 + 40), "1 год 30 хв")

    def test_grid_back_while_held_off(self):
        msg = notify.on_switched({}, "off", "on", 226.0, switched=False, held=True)
        self.assertIn("Світло повернулося", msg)
        self.assertIn("лишається вимкненим", msg)

    def test_already_in_state_wording(self):
        self.assertIn("уже був вимкнений", notify.on_switched({}, "on", "off", 0.0, switched=False))

    def test_first_run_is_silent(self):
        self.assertIsNone(notify.on_switched({}, None, "on", 230.0, switched=False))

    def test_low_voltage_off_and_restored(self):
        state = {}
        off = notify.on_switched(state, "on", "off", 204.2, switched=True, off_below=205.0, on_at=210.0)
        self.assertIn("Низька напруга", off)
        self.assertIn("204.2 В", off)
        self.assertIn("поріг 205 В", off)
        self.assertIn("до 210 В", off)
        self.assertNotIn("Зникло світло", off)
        on = notify.on_switched(state, "off", "on", 213.4, switched=True)
        self.assertIn("Напруга в мережі відновилася", on)
        self.assertIn("увімкнено", on)
        self.assertEqual(state, {})

    def test_low_voltage_already_off_and_held(self):
        state = {}
        self.assertIn("уже був вимкнений", notify.on_switched(state, "on", "off", 204.6, switched=False))
        held = notify.on_switched(state, "off", "on", 211.0, switched=False, held=True)
        self.assertIn("відновилася", held)
        self.assertIn("лишається вимкненим", held)

    def test_outage_clears_low_voltage_flag(self):
        state = {}
        notify.on_switched(state, None, "off", 204.0, switched=True)  # first run: silent, flag kept
        self.assertTrue(state["low_voltage"])
        notify.on_switched(state, "on", "off", 0.0, switched=True)
        self.assertIn("Світло повернулося", notify.on_switched(state, "off", "on", 226.0, switched=True))

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
