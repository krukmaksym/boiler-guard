"""entry.py's state transitions, with the Workers runtime (js, pyodide, workers)
replaced by small stubs and KV by a dict. Deye and Shelly are faked per test."""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _stub_runtime():
    js = types.ModuleType("js")
    js.AbortSignal = types.SimpleNamespace(timeout=lambda ms: None)
    js.Object = types.SimpleNamespace(fromEntries=dict)
    pyodide = types.ModuleType("pyodide")
    ffi = types.ModuleType("pyodide.ffi")
    ffi.to_js = lambda value, dict_converter=None: value
    pyodide.ffi = ffi
    workers = types.ModuleType("workers")

    class WorkerEntrypoint:
        def __init__(self, ctx=None, env=None):
            self.ctx, self.env = ctx, env

    async def fetch(*args, **kwargs):
        raise AssertionError("tests must not reach the network")

    workers.WorkerEntrypoint = WorkerEntrypoint
    workers.Response = lambda body, status=200: (status, body)
    workers.fetch = fetch
    for name, module in {"js": js, "pyodide": pyodide, "pyodide.ffi": ffi, "workers": workers}.items():
        sys.modules.setdefault(name, module)


_stub_runtime()

import bot  # noqa: E402
import entry  # noqa: E402
import logic  # noqa: E402

SECRETS = {name: f"test-{name.lower()}" for name in entry.REQUIRED_SECRETS}
SECRETS["SHELLY_HOST"] = "shelly-1-eu.shelly.cloud"


class FakeKV:
    def __init__(self, data=None):
        self.data = dict(data or {})

    async def get(self, key):
        return self.data.get(key)

    async def put(self, key, value, options=None):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)


def shelly_status(output):
    return {"isok": True, "data": {"online": True, "device_status": {"switch:0": {"output": output}}}}


class EntryTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sleep = mock.patch.object(entry.asyncio, "sleep", mock.AsyncMock())
        sleep.start()
        self.addCleanup(sleep.stop)

    def worker(self, state=None, prod_state=None, plug=logic.OFF, voltage=230.0):
        env = types.SimpleNamespace(STATE=FakeKV(state), SHELLY_CHANNEL="0", **SECRETS)
        if prod_state is not None:
            env.PROD_STATE = FakeKV(prod_state)
        worker = entry.Default(None, env)
        self.switched = []

        async def shelly(path, secrets, **params):
            if path == "/device/status":
                return shelly_status(plug == logic.ON)
            self.switched.append(params["turn"])
            return {"isok": True}

        worker.shelly = shelly
        worker.deye_reading = mock.AsyncMock(return_value=(voltage, 80.0, 0.0, None))
        return worker


class ManualOnTest(EntryTestCase):
    async def test_on_without_fresh_data_lets_the_next_run_switch_on(self):
        # ⛔ held the boiler off while grid was present, so "on" was saved as desired.
        worker = self.worker({entry.MODE_KEY: logic.OFF, entry.DESIRED_KEY: logic.ON})
        reply = await worker.apply_control(logic.ON, {"voltage": None})  # Deye blind right now
        self.assertEqual(reply, bot.AUTO_WAIT_MESSAGE)
        self.assertNotIn(entry.MODE_KEY, worker.env.STATE.data)
        self.assertEqual(self.switched, [])

        await worker.check({}, [], {})  # next cron run: fresh reading, grid present
        self.assertEqual(self.switched, [logic.ON])
        self.assertEqual(worker.env.STATE.data[entry.DESIRED_KEY], logic.ON)

    async def test_on_on_battery_keeps_the_off_desired_state(self):
        worker = self.worker({entry.MODE_KEY: logic.OFF, entry.DESIRED_KEY: logic.OFF})
        await worker.apply_control(logic.ON, {"voltage": 150.0, "desired": logic.OFF})
        self.assertEqual(worker.env.STATE.data[entry.DESIRED_KEY], logic.OFF)
        self.assertEqual(self.switched, [])

    async def test_on_with_grid_switches_on(self):
        worker = self.worker({entry.MODE_KEY: logic.OFF, entry.DESIRED_KEY: logic.ON})
        reply = await worker.apply_control(logic.ON, {"voltage": 230.0, "desired": logic.ON})
        self.assertEqual(self.switched, [logic.ON])
        self.assertTrue(reply.startswith("🔌 Бойлер увімкнено."))

    async def test_off_holds_and_the_cron_keeps_it_off(self):
        worker = self.worker(plug=logic.ON)
        await worker.apply_control(logic.OFF, {})
        self.assertEqual(self.switched, [logic.OFF])
        self.assertEqual(worker.env.STATE.data[entry.MODE_KEY], logic.OFF)
        self.switched.clear()
        worker.env.STATE.data[entry.DESIRED_KEY] = logic.OFF  # grid was gone, now it's back
        worker.shelly = self.worker(plug=logic.OFF).shelly
        await worker.check({}, [], {})
        self.assertEqual(self.switched, [])


class ProductionHoldTest(EntryTestCase):
    async def test_test_worker_never_switches_on_against_the_production_hold(self):
        worker = self.worker(prod_state={entry.MODE_KEY: logic.OFF})
        await worker.check({}, [], {})
        self.assertEqual(self.switched, [])
        reply = await worker.apply_control(logic.ON, {"voltage": 230.0, "desired": logic.ON})
        self.assertEqual(reply, bot.PROD_HOLD_MESSAGE)
        self.assertEqual(self.switched, [])

    async def test_without_a_production_hold_it_switches_on(self):
        worker = self.worker(prod_state={})
        await worker.check({}, [], {})
        self.assertEqual(self.switched, [logic.ON])


class DeyeLoginBackoffTest(EntryTestCase):
    async def login(self, status, data):
        worker = self.worker()
        worker.deye_post = mock.AsyncMock(return_value=(status, data))
        with self.assertRaises(logic.ApiError):
            await worker.deye_token(SECRETS)
        return worker.env.STATE.data

    async def test_server_error_does_not_back_off(self):
        self.assertNotIn(entry.TOKEN_BACKOFF_KEY, await self.login(502, None))

    async def test_refused_login_backs_off(self):
        data = await self.login(200, {"success": False, "msg": "bad password"})
        self.assertIn(entry.TOKEN_BACKOFF_KEY, data)


class WebhookReplayTest(EntryTestCase):
    async def test_duplicate_update_is_handled_once(self):
        worker = self.worker()
        worker.handle_updates = mock.AsyncMock()
        await worker.handle_webhook_update({"update_id": 5})
        await worker.handle_webhook_update({"update_id": 5})
        await worker.handle_webhook_update({"update_id": 4})
        self.assertEqual(worker.handle_updates.await_count, 1)


class WebhookSecretTest(EntryTestCase):
    def test_weak_secret_is_detected(self):
        worker = self.worker()
        self.assertFalse(worker.webhook_secret_weak())  # not set: polling, nothing to report
        worker.env.TELEGRAM_WEBHOOK_SECRET = "short"  # noqa: S105 - deliberately invalid
        self.assertTrue(worker.webhook_secret_weak())
        self.assertIsNone(worker.webhook_secret())
        worker.env.TELEGRAM_WEBHOOK_SECRET = "a" * 40
        self.assertFalse(worker.webhook_secret_weak())


if __name__ == "__main__":
    unittest.main()
