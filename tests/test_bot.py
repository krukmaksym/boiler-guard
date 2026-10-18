import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import bot

CHAT = "-1001234567890"


ADMIN = 111
NOW = 1_800_000_000


def update(update_id, text, chat_id=CHAT, message_id=None, user_id=ADMIN):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id or update_id,
            "from": {"id": user_id},
            "chat": {"id": int(chat_id)},
            "text": text,
        },
    }


def press(update_id, data="boiler:off", user_id=ADMIN, chat_id=CHAT, panel_date=NOW - 60):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"q{update_id}",
            "from": {"id": user_id},
            "data": data,
            "message": {"message_id": 50, "date": panel_date, "chat": {"id": int(chat_id)}},
        },
    }


class CommandParsingTest(unittest.TestCase):
    def test_command_of(self):
        self.assertEqual(bot.command_of("/status"), "/status")
        self.assertEqual(bot.command_of("/status@elecricity_guard_bot please"), "/status")
        self.assertEqual(bot.command_of("/STATUS"), "/status")
        self.assertIsNone(bot.command_of("status"))
        self.assertIsNone(bot.command_of(None))

    def test_only_allowed_chat_and_known_commands(self):
        updates = [
            update(1, "/status"),
            update(2, "/status", chat_id="555"),  # stranger in a private chat
            update(3, "hello"),
            update(4, "/turn_off"),  # unknown / not supported: ignored
            update(5, "/help@elecricity_guard_bot"),
            {"update_id": 6, "edited_message": {"text": "/status"}},
            update(7, "/boiler", user_id=222),
        ]
        self.assertEqual(
            bot.requests(updates, CHAT), [("/status", 1, ADMIN), ("/help", 5, ADMIN), ("/boiler", 7, 222)]
        )

    def test_one_reply_per_kind_to_the_latest(self):
        found = [
            ("/status", 10, 1),
            ("/start", 11, 1),
            ("/status", 12, 2),
            ("/help", 13, 1),
            ("/boiler", 14, 1),
        ]
        self.assertEqual(bot.replies(found), {"status": (12, 2), "help": (13, 1), "boiler": (14, 1)})

    def test_next_offset(self):
        self.assertEqual(bot.next_offset([update(7, "x"), update(9, "y")], 0), 10)
        self.assertEqual(bot.next_offset([], 42), 42)


class AdminButtonsTest(unittest.TestCase):
    ADMINS = frozenset({str(ADMIN)})

    def test_admin_ids(self):
        self.assertEqual(bot.admin_ids(" 111, 222 ,x,, "), {"111", "222"})
        self.assertEqual(bot.admin_ids(None), frozenset())
        self.assertEqual(bot.admin_ids(""), frozenset())

    def test_taps_only_from_allowed_chat_and_known_buttons(self):
        updates = [
            press(1),
            press(2, chat_id="555"),
            press(3, data="boiler:explode"),
            press(4, data="boiler:on"),
        ]
        found = bot.taps(updates, CHAT)
        self.assertEqual([t["action"] for t in found], ["off", "on"])
        self.assertEqual(
            found[0], {"id": "q1", "user": ADMIN, "action": "off", "panel": 50, "panel_date": NOW - 60}
        )

    def test_verdicts(self):
        self.assertEqual(bot.tap_verdict(bot.taps([press(1)], CHAT)[0], self.ADMINS, NOW), "ok")
        stranger = bot.taps([press(1, user_id=222)], CHAT)[0]
        self.assertEqual(bot.tap_verdict(stranger, self.ADMINS, NOW), "not_admin")
        self.assertEqual(bot.tap_verdict(stranger, frozenset(), NOW), "not_admin")  # no admins configured
        old = bot.taps([press(1, panel_date=NOW - bot.PANEL_TTL_SECONDS - 1)], CHAT)[0]
        self.assertEqual(bot.tap_verdict(old, self.ADMINS, NOW), "stale")
        undated = bot.taps([press(1, panel_date=None)], CHAT)[0]
        self.assertEqual(bot.tap_verdict(undated, self.ADMINS, NOW), "stale")

    def test_control_message_says_switched_or_already(self):
        self.assertTrue(bot.control_message("on", "on", switched=True).startswith("🔌 Бойлер увімкнено."))
        already_on = bot.control_message("on", "on", switched=False)
        self.assertEqual(already_on, "🔌 Бойлер вже увімкнено. Ним знову керує автоматичний захист.")
        self.assertTrue(
            bot.control_message("off", "off", switched=True).startswith("⛔ Бойлер вимкнено вручну.")
        )
        already_off = bot.control_message("off", "off", switched=False)
        self.assertTrue(already_off.startswith("⛔ Бойлер вже вимкнено."))
        self.assertIn("не увімкнеться сам", already_off)
        self.assertEqual(bot.control_message("on", None, switched=False), bot.AUTO_WAIT_MESSAGE)  # on battery

    def test_not_admin_message_shows_id(self):
        self.assertIn("222", bot.not_admin_message(222, configured=True))
        self.assertIn("не налаштоване", bot.not_admin_message(222, configured=False))


class StatusMessageTest(unittest.TestCase):
    def test_grid_ok(self):
        text = bot.status_message(226.4, "on", 87.0)
        self.assertIn("Світло є (мережа 226 В)", text)
        self.assertIn("Бойлер увімкнений", text)
        self.assertIn("Батарея 87%", text)
        self.assertIn("захист працює", text)

    def test_outage(self):
        text = bot.status_message(0.0, "off", 64.0)
        self.assertIn("Світла немає (мережа 0 В)", text)
        self.assertIn("Бойлер вимкнений", text)

    def test_unknowns(self):
        text = bot.status_message(None, None, None)
        self.assertIn("Немає свіжих даних", text)
        self.assertIn("Стан бойлера невідомий", text)
        self.assertNotIn("Батарея", text)
        self.assertIn("тимчасово не працює", text)

    def test_manual_off_line(self):
        self.assertIn("Ручний режим", bot.status_message(226.0, "off", 80.0, manual_off=True))
        self.assertNotIn("Ручний режим", bot.status_message(226.0, "on", 80.0))


if __name__ == "__main__":
    unittest.main()
