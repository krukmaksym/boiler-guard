import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import webhook  # noqa: E402

SECRET = "s" * 40
TELEGRAM_IP = "149.154.167.220"


def request(**overrides):
    args = {
        "method": "POST",
        "path": webhook.webhook_path(SECRET),
        "header_secret": SECRET,
        "content_type": "application/json",
        "content_length": "350",
        "client_ip": TELEGRAM_IP,
        "secret": SECRET,
    }
    args.update(overrides)
    return webhook.authorized(**args)


class SecretTest(unittest.TestCase):
    def test_valid_secret(self):
        self.assertTrue(webhook.valid_secret("aZ09_-" * 6))
        for bad in (None, "", "short", "x" * 31, "has space" * 5, "x" * 257, "ü" * 40):
            self.assertFalse(webhook.valid_secret(bad), bad)

    def test_path_is_derived_and_unguessable(self):
        path = webhook.webhook_path(SECRET)
        self.assertRegex(path, r"^/telegram/[0-9a-f]{32}$")
        self.assertEqual(path, webhook.webhook_path(SECRET))  # stable for setWebhook
        self.assertNotEqual(path, webhook.webhook_path("t" * 40))
        self.assertNotIn(SECRET, path)


class AuthorizedTest(unittest.TestCase):
    def test_genuine_delivery(self):
        self.assertTrue(request())
        self.assertTrue(request(content_type="application/json; charset=utf-8"))
        self.assertTrue(request(client_ip="91.108.6.10"))

    def test_rejections(self):
        cases = {
            "wrong method": {"method": "GET"},
            "wrong path": {"path": "/telegram/" + "0" * 32},
            "root path": {"path": "/"},
            "missing header": {"header_secret": None},
            "wrong header": {"header_secret": "x" * 40},
            "header prefix": {"header_secret": SECRET[:-1]},
            "not json": {"content_type": "text/plain"},
            "no length": {"content_length": None},
            "empty body": {"content_length": "0"},
            "too big": {"content_length": str(webhook.MAX_BODY_BYTES + 1)},
            "not telegram ip": {"client_ip": "203.0.113.5"},
            "no ip": {"client_ip": None},
            "bad ip": {"client_ip": "not-an-ip"},
            "weak secret": {"secret": "short", "header_secret": "short"},
        }
        for name, overrides in cases.items():
            self.assertFalse(request(**overrides), name)

    def test_ip_check_can_be_disabled_for_local_tests(self):
        self.assertTrue(request(client_ip="127.0.0.1", check_ip=False))
        self.assertFalse(request(client_ip="127.0.0.1", check_ip=False, header_secret="x" * 40))


class ExpiryAndCooldownTest(unittest.TestCase):
    def test_expired(self):
        self.assertFalse(webhook.expired(None, 100))  # production: no expiry
        self.assertTrue(webhook.expired(None, 100, required=True))  # test env must have one
        self.assertTrue(webhook.expired("", 100, required=True))
        self.assertFalse(webhook.expired("200", 100, required=True))
        self.assertTrue(webhook.expired("100", 100))
        self.assertTrue(webhook.expired("soon", 100))

    def test_cooled_down(self):
        self.assertTrue(webhook.cooled_down(None, 100, 10))
        self.assertFalse(webhook.cooled_down("95", 100, 10))
        self.assertTrue(webhook.cooled_down("90", 100, 10))
        self.assertTrue(webhook.cooled_down("garbage", 100, 10))


if __name__ == "__main__":
    unittest.main()
