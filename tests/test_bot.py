import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import bot

CHAT = "-1001234567890"


def update(update_id, text, chat_id=CHAT, message_id=None):
    return {
        "update_id": update_id,
        "message": {"message_id": message_id or update_id, "chat": {"id": int(chat_id)}, "text": text},
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
        ]
        self.assertEqual(bot.requests(updates, CHAT), [("/status", 1), ("/help", 5)])

    def test_one_reply_per_kind_to_the_latest(self):
        found = [("/status", 10), ("/start", 11), ("/status", 12), ("/help", 13)]
        self.assertEqual(bot.replies(found), {"status": 12, "help": 13})

    def test_next_offset(self):
        self.assertEqual(bot.next_offset([update(7, "x"), update(9, "y")], 0), 10)
        self.assertEqual(bot.next_offset([], 42), 42)


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


if __name__ == "__main__":
    unittest.main()
