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

    class DurableObject:
        def __init__(self, ctx=None, env=None):
            self.ctx, self.env = ctx, env

    async def fetch(*args, **kwargs):
        raise AssertionError("tests must not reach the network")

    workers.WorkerEntrypoint = WorkerEntrypoint
    workers.DurableObject = DurableObject
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


class FakeStorage(FakeKV):
    """A Durable Object's ctx.storage: same get/put/delete, any value type."""


class FakeNamespace:
    """A Durable Object namespace binding running the real PlugState on FakeStorage."""

    def __init__(self, data=None, kv=None, migrated=True):
        self.data = dict(data or {})
        if migrated:
            self.data[entry.MIGRATED_KEY] = "1"
        ctx = types.SimpleNamespace(storage=FakeStorage())
        ctx.storage.data = self.data
        self.object = entry.PlugState(ctx, types.SimpleNamespace(STATE=kv))
        self.names = []

    def idFromName(self, name):  # noqa: N802 - the Workers API name
        self.names.append(name)
        return name

    def get(self, object_id):
        return self.object


def split_state(state):
    """(KV data, plug state data) from one dict of initial values."""
    state = dict(state or {})
    plug = {key: state.pop(key) for key in list(state) if key in entry.PLUG_KEYS}
    return state, plug


def shelly_status(output):
    return {"isok": True, "data": {"online": True, "device_status": {"switch:0": {"output": output}}}}


class EntryTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sleep = mock.patch.object(entry.asyncio, "sleep", mock.AsyncMock())
        sleep.start()
        self.addCleanup(sleep.stop)

    def worker(self, state=None, prod_state=None, plug=logic.OFF, voltage=230.0):
        kv_data, plug_data = split_state(state)
        env = types.SimpleNamespace(STATE=FakeKV(kv_data), SHELLY_CHANNEL="0", **SECRETS)
        env.PLUG = FakeNamespace(plug_data, kv=env.STATE)
        if prod_state is not None:
            env.PROD_PLUG = FakeNamespace(prod_state)
        worker = entry.Default(None, env)
        worker.plug_data = env.PLUG.data  # the plug state, for assertions
        self.switched = []
        self.plug = plug

        async def shelly(path, secrets, **params):
            if path == "/device/status":
                return shelly_status(self.plug == logic.ON)
            self.switched.append(params["turn"])
            self.plug = params["turn"]
            return {"isok": True}

        worker.shelly = shelly
        worker.deye_reading = mock.AsyncMock(return_value=logic.Reading(voltage, soc=80.0, load=0.0))
        return worker


class ManualOnTest(EntryTestCase):
    async def held_with_grid_then_on_while_blind(self):
        """⛔ with grid present (no grid transition), then ✅ while Deye has no fresh data."""
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON)
        await worker.apply_control(logic.OFF, {})
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], entry.HELD_ON)
        reply = await worker.apply_control(logic.ON, {"voltage": None})
        self.assertEqual(reply, bot.AUTO_WAIT_MESSAGE)
        self.assertNotIn(entry.MODE_KEY, worker.plug_data)
        self.assertEqual(self.switched, [logic.OFF])
        self.switched.clear()
        return worker

    async def test_on_without_fresh_data_lets_the_next_run_switch_on(self):
        worker = await self.held_with_grid_then_on_while_blind()
        outbox = []
        await worker.check({}, outbox, {})  # next cron run: fresh reading, grid present
        self.assertEqual(self.switched, [logic.ON])
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], logic.ON)
        self.assertEqual([m for m in outbox if m], [])  # grid didn't change: nothing to announce

    async def test_a_failed_switch_on_after_the_hold_is_reported_once(self):
        worker = await self.held_with_grid_then_on_while_blind()
        offline = mock.AsyncMock(return_value={"isok": True, "data": {"online": False}})
        worker.shelly, state, outbox = offline, {}, []
        await worker.check(state, outbox, {})
        await worker.check(state, outbox, {})
        self.assertEqual([m for m in outbox if m], [entry.notify.RESUME_FAILED_MESSAGE])
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], entry.HELD_ON)  # retried

        worker.shelly = self.worker(plug=logic.OFF).shelly  # Shelly is back
        await worker.check(state, outbox, {})
        self.assertNotIn("switch_failed", state)

    async def test_grid_lost_after_that_is_still_announced(self):
        worker = await self.held_with_grid_then_on_while_blind()
        worker.deye_reading = mock.AsyncMock(return_value=logic.Reading(0.0, soc=80.0, load=0.0))  # grid gone
        outbox = []
        await worker.check({}, outbox, {})
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], entry.COOLDOWN_OFF)
        self.assertEqual(len([m for m in outbox if m and "Зникло світло" in m]), 1)

    async def test_hold_keeps_the_plug_off_without_polling_shelly(self):
        worker = self.worker({entry.MODE_KEY: logic.OFF, entry.DESIRED_KEY: entry.HELD_ON})
        worker.shelly = mock.AsyncMock()
        await worker.check({}, [], {})
        worker.shelly.assert_not_awaited()

    async def test_on_on_battery_keeps_the_off_desired_state(self):
        worker = self.worker({entry.MODE_KEY: logic.OFF, entry.DESIRED_KEY: logic.OFF})
        await worker.apply_control(logic.ON, {"voltage": 150.0, "desired": logic.OFF})
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], logic.OFF)
        self.assertEqual(self.switched, [])

    async def test_on_with_grid_switches_on(self):
        worker = self.worker({entry.MODE_KEY: logic.OFF, entry.DESIRED_KEY: entry.HELD_ON})
        reply = await worker.apply_control(logic.ON, {"voltage": 230.0, "desired": logic.ON})
        self.assertEqual(self.switched, [logic.ON])
        self.assertTrue(reply.startswith("🔌 Бойлер увімкнено."))

    async def test_off_holds_and_the_cron_keeps_it_off(self):
        worker = self.worker(plug=logic.ON)
        await worker.apply_control(logic.OFF, {})
        self.assertEqual(self.switched, [logic.OFF])
        self.assertEqual(worker.plug_data[entry.MODE_KEY], logic.OFF)
        self.switched.clear()
        worker.plug_data[entry.DESIRED_KEY] = logic.OFF  # grid was gone, now it's back
        outbox = []
        await worker.check({}, outbox, {})
        self.assertEqual(self.switched, [])
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], entry.HELD_ON)
        self.assertTrue(any(m and "вимкнули вручну" in m for m in outbox))


class ProductionHoldTest(EntryTestCase):
    async def test_test_worker_never_switches_on_against_the_production_hold(self):
        worker = self.worker(prod_state={entry.MODE_KEY: logic.OFF})
        await worker.check({}, [], {})
        self.assertEqual(self.switched, [])
        reply = await worker.apply_control(logic.ON, {"voltage": 230.0, "desired": logic.ON})
        self.assertEqual(reply, bot.PROD_HOLD_MESSAGE)
        self.assertEqual(self.switched, [])

    async def test_a_switch_on_racing_the_production_hold_is_undone(self):
        # The test Worker switched on just before production's ⛔ landed.
        worker = self.worker(
            {entry.DESIRED_KEY: logic.ON}, prod_state={entry.MODE_KEY: logic.OFF}, plug=logic.ON
        )
        await worker.check({}, [], {})  # grid unchanged: only the hold enforcement runs
        self.assertEqual(self.switched, [logic.OFF])
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], entry.HELD_ON)

    async def test_the_production_hold_does_not_poll_shelly_every_minute(self):
        state = {entry.DESIRED_KEY: entry.HELD_ON}
        worker = self.worker(state, prod_state={entry.MODE_KEY: logic.OFF})
        worker.shelly = mock.AsyncMock()
        await worker.check({}, [], {})
        worker.shelly.assert_not_awaited()

    async def test_without_a_production_hold_it_switches_on(self):
        worker = self.worker(prod_state={})
        await worker.check({}, [], {})
        self.assertEqual(self.switched, [logic.ON])


class VoltageCooldownTest(EntryTestCase):
    def setUp(self):
        super().setUp()
        self.now = 1_800_000_000.0
        clock = mock.patch.object(entry.time, "time", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def at(self, worker, voltage, minutes):
        self.now = 1_800_000_000.0 + minutes * 60
        worker.deye_reading = mock.AsyncMock(return_value=logic.Reading(voltage, soc=80.0, load=0.0))

    async def low_voltage_off(self):
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=190.0)
        outbox = []
        await worker.check({}, outbox, {})
        self.assertEqual(self.switched, [logic.OFF])
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], entry.COOLDOWN_OFF)
        self.assertIn("протримається 10 хв", [m for m in outbox if m][0])
        return worker

    async def test_switches_on_only_after_the_voltage_stays_in_range(self):
        worker = await self.low_voltage_off()
        for minute in (1, 5, 10):
            self.at(worker, 230.0, minute)
            await worker.check({}, [], {})
            self.assertEqual(self.switched, [logic.OFF])  # still waiting
        self.at(worker, 230.0, 11)  # 10 min after it came back
        await worker.check({}, [], {})
        self.assertEqual(self.switched, [logic.OFF, logic.ON])
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], logic.ON)
        self.assertNotIn(entry.ON_PENDING_KEY, worker.plug_data)

    async def test_a_dip_during_the_wait_restarts_it(self):
        worker = await self.low_voltage_off()
        self.at(worker, 230.0, 1)
        await worker.check({}, [], {})
        self.at(worker, 207.0, 6)  # in the dead band: not back in range
        await worker.check({}, [], {})
        self.assertNotIn(entry.ON_PENDING_KEY, worker.plug_data)
        self.at(worker, 230.0, 7)
        await worker.check({}, [], {})
        self.at(worker, 230.0, 16)
        await worker.check({}, [], {})
        self.assertEqual(self.switched, [logic.OFF])
        self.at(worker, 230.0, 17)
        await worker.check({}, [], {})
        self.assertEqual(self.switched, [logic.OFF, logic.ON])

    async def test_high_voltage_switches_off_and_waits_too(self):
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=268.0)
        outbox = []
        await worker.check({}, outbox, {})
        self.assertEqual(self.switched, [logic.OFF])
        self.assertIn("Висока напруга", [m for m in outbox if m][0])
        self.at(worker, 245.0, 1)
        await worker.check({}, [], {})
        self.assertEqual(self.switched, [logic.OFF])

    async def test_grid_lost_waits_too(self):
        """A short outage and reconnect would flap the boiler just like low voltage."""
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=6.0)
        state, outbox = {}, []
        await worker.check(state, outbox, {})
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], entry.COOLDOWN_OFF)
        self.assertIn("протримається 10 хв", outbox[-1])
        self.at(worker, 221.0, 1)
        await worker.check(state, outbox, {})
        self.assertEqual(self.switched, [logic.OFF])
        self.at(worker, 221.0, 11)
        await worker.check(state, outbox, {})
        self.assertEqual(self.switched, [logic.OFF, logic.ON])
        self.assertIn("Світла не було 1 хв. Напруга стабільна вже 10 хв", outbox[-1])

    async def test_an_old_plain_off_gets_the_cooldown(self):
        worker = self.worker({entry.DESIRED_KEY: logic.OFF}, plug=logic.OFF, voltage=0.0)
        await worker.check({}, [], {})
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], entry.COOLDOWN_OFF)

    async def test_grid_back_but_too_low_after_an_outage_waits(self):
        worker = self.worker({entry.DESIRED_KEY: logic.OFF}, plug=logic.OFF, voltage=190.0)
        await worker.check({}, [], {})
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], entry.COOLDOWN_OFF)
        self.at(worker, 230.0, 1)
        await worker.check({}, [], {})
        self.assertEqual(self.switched, [])

    async def test_an_open_grid_relay_switches_off_and_waits(self):
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON)
        worker.deye_reading = mock.AsyncMock(return_value=logic.Reading(240.0, relay="Break"))
        outbox = []
        await worker.check({}, outbox, {})
        self.assertEqual(self.switched, [logic.OFF])
        self.assertIn("Інвертор відключився від мережі", outbox[-1])
        self.at(worker, 240.0, 1)  # relay closed again (the default reading has no relay)
        await worker.check({}, [], {})
        self.assertEqual(self.switched, [logic.OFF])  # cooldown
        self.at(worker, 240.0, 11)
        await worker.check({}, [], {})
        self.assertEqual(self.switched, [logic.OFF, logic.ON])

    async def test_admin_on_while_off_grid_says_so(self):
        worker = self.worker({entry.DESIRED_KEY: entry.COOLDOWN_OFF, entry.MODE_KEY: logic.OFF})
        worker.deye_reading = mock.AsyncMock(return_value=logic.Reading(240.0, relay="Break"))
        reply = await worker.apply_control(logic.ON, {})
        self.assertEqual(reply, bot.AUTO_WAIT_OFF_GRID_MESSAGE)
        self.assertEqual(self.switched, [])

    async def test_status_shows_the_battery_current(self):
        worker = self.worker()
        run = {"voltage": 230.0, "load": 1930.0, "battery_voltage": 53.0}
        snapshot = await worker.status_snapshot(run)
        self.assertAlmostEqual(snapshot["load_amps"], 1930 / 53 / 0.95)

    async def test_no_cooldown_when_set_to_zero(self):
        worker = await self.low_voltage_off()
        worker.env.VOLTAGE_COOLDOWN_SECONDS = "0"
        self.at(worker, 230.0, 1)
        await worker.check({}, [], {})
        self.assertEqual(self.switched, [logic.OFF, logic.ON])

    async def test_admin_on_ends_the_wait(self):
        worker = await self.low_voltage_off()
        self.at(worker, 230.0, 1)
        run = {}
        await worker.check({}, [], run)
        await worker.apply_control(logic.ON, run)
        self.assertEqual(self.switched, [logic.OFF, logic.ON])
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], logic.ON)
        self.assertNotIn(entry.ON_PENDING_KEY, worker.plug_data)

    async def test_status_shows_the_wait(self):
        worker = await self.low_voltage_off()
        self.at(worker, 230.0, 1)
        await worker.check({}, [], {})
        self.at(worker, 230.0, 4)
        snapshot = await worker.status_snapshot({"voltage": 230.0})
        self.assertEqual(snapshot["wait_seconds"], 7 * 60)


class PlugStateTest(EntryTestCase):
    async def test_plug_state_lives_in_the_durable_object_not_kv(self):
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=190.0)
        await worker.check({}, [], {})  # low voltage: switch off, cooldown state saved
        await worker.apply_control(logic.OFF, {})  # admin hold + verify window
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], entry.COOLDOWN_OFF)
        self.assertEqual(worker.plug_data[entry.MODE_KEY], logic.OFF)
        self.assertIn(entry.VERIFY_OFF_KEY, worker.plug_data)
        self.assertFalse(entry.PLUG_KEYS & set(worker.env.STATE.data))  # nothing in KV
        self.assertEqual(set(worker.env.PLUG.names), {entry.PLUG_OBJECT_NAME})

    async def test_first_call_copies_the_kv_values_once(self):
        kv = FakeKV(
            {
                entry.DESIRED_KEY: entry.HELD_ON,
                entry.MODE_KEY: logic.OFF,
                entry.VERIFY_OFF_KEY: "1",
                entry.TOKEN_KEY: "not-plug-state",
            }
        )
        namespace = FakeNamespace(kv=kv, migrated=False)
        store = namespace.get(namespace.idFromName("plug"))
        self.assertEqual(await store.get_value(entry.DESIRED_KEY), entry.HELD_ON)
        self.assertEqual(await store.get_value(entry.MODE_KEY), logic.OFF)
        self.assertEqual(await store.get_value(entry.VERIFY_OFF_KEY), "1")
        self.assertIn(entry.VERIFY_OFF_KEY + entry.EXPIRES_SUFFIX, namespace.data)  # window restarts
        self.assertNotIn(entry.TOKEN_KEY, namespace.data)
        self.assertEqual(kv.data[entry.DESIRED_KEY], entry.HELD_ON)  # KV kept for a rollback
        await store.put_value(entry.DESIRED_KEY, logic.ON)
        kv.data[entry.DESIRED_KEY] = logic.OFF
        self.assertEqual(await store.get_value(entry.DESIRED_KEY), logic.ON)  # copied only once

    async def test_values_expire_like_kv_ttl(self):
        namespace = FakeNamespace()
        store = namespace.get("plug")
        with mock.patch.object(entry.time, "time", return_value=1000.0):
            await store.put_value(entry.VERIFY_OFF_KEY, "1", 180)
            await store.put_value(entry.DESIRED_KEY, logic.ON)
        with mock.patch.object(entry.time, "time", return_value=1179.0):
            self.assertEqual(await store.get_value(entry.VERIFY_OFF_KEY), "1")
        with mock.patch.object(entry.time, "time", return_value=1180.0):
            self.assertEqual(await store.get_value(entry.VERIFY_OFF_KEY), "")
            self.assertEqual(await store.get_value(entry.DESIRED_KEY), logic.ON)  # no expiry
        self.assertNotIn(entry.VERIFY_OFF_KEY, namespace.data)
        await store.put_value(entry.VERIFY_OFF_KEY, "1")  # a later put without ttl keeps it
        self.assertNotIn(entry.VERIFY_OFF_KEY + entry.EXPIRES_SUFFIX, namespace.data)

    async def test_only_plug_keys_are_accepted(self):
        store = FakeNamespace().get("plug")
        for call in (store.get_value(entry.TOKEN_KEY), store.put_value("anything", "x")):
            with self.assertRaises(ValueError):
                await call

    async def test_missing_binding_is_a_config_error(self):
        worker = self.worker()
        del worker.env.PLUG
        with self.assertRaises(entry.ConfigError):
            await worker.check({}, [], {})


class ConfigCheckTest(EntryTestCase):
    async def test_on_voltage_below_off_voltage_is_a_config_error(self):
        worker = self.worker()
        worker.env.ON_VOLTAGE, worker.env.OFF_VOLTAGE = "200", "205"
        with self.assertRaises(entry.ConfigError):
            await worker.check({}, [], {})
        self.assertEqual(self.switched, [])

    async def test_high_thresholds_out_of_order_are_a_config_error(self):
        for high_on, high_off in (("252", "250"), ("209", "265")):
            worker = self.worker()
            worker.env.HIGH_ON_VOLTAGE, worker.env.HIGH_OFF_VOLTAGE = high_on, high_off
            with self.assertRaises(entry.ConfigError):
                await worker.check({}, [], {})

    async def test_negative_cooldown_is_a_config_error(self):
        worker = self.worker()
        worker.env.VOLTAGE_COOLDOWN_SECONDS = "-5"
        with self.assertRaises(entry.ConfigError):
            await worker.check({}, [], {})

    async def test_a_button_press_with_bad_config_still_gets_a_reply(self):
        worker = self.worker({entry.MODE_KEY: logic.OFF}, plug=logic.ON)
        worker.env.SHELLY_HOST = "evil.example.com"
        for action in (logic.ON, logic.OFF):
            self.assertEqual(await worker.apply_control(action, {}), bot.CONFIG_FAILED_MESSAGE)
        self.assertEqual(self.switched, [])
        self.assertEqual(worker.plug_data[entry.MODE_KEY], logic.OFF)  # nothing changed

    async def test_corrupted_offset_does_not_stop_polling(self):
        worker = self.worker({entry.OFFSET_KEY: "garbage"})
        worker.env.TELEGRAM_BOT_TOKEN = "123456789:" + "A" * 35
        worker.env.TELEGRAM_CHAT_ID = "-100"
        worker.telegram = mock.AsyncMock(return_value=(True, {"ok": True, "result": [{"update_id": 9}]}))
        self.assertIsNone(await worker.answer_commands({}))
        self.assertNotIn("offset", worker.telegram.await_args.args[1])
        self.assertEqual(worker.env.STATE.data[entry.OFFSET_KEY], "10")


class ShellyPacingTest(EntryTestCase):
    async def test_a_failed_shelly_call_still_counts_for_pacing(self):
        worker = self.worker({entry.VERIFY_OFF_KEY: "1", entry.MODE_KEY: logic.OFF})
        worker.shelly = mock.AsyncMock(side_effect=logic.ApiError("Shelly down"))
        run = {}
        await worker.enforce_hold(SECRETS, 0, run)
        self.assertIn("plug", run)  # so a status reply this run waits before calling again


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
    def test_weak_secret_means_polling(self):
        worker = self.worker()
        worker.env.TELEGRAM_WEBHOOK_SECRET = "short"  # noqa: S105 - deliberately invalid
        self.assertIsNone(worker.webhook_secret())
        worker.env.TELEGRAM_WEBHOOK_SECRET = "a" * 40
        self.assertEqual(worker.webhook_secret(), "a" * 40)

    def telegram_worker(self, secret=None):
        worker = self.worker()
        worker.env.TELEGRAM_BOT_TOKEN = "123456789:" + "A" * 35
        worker.env.TELEGRAM_CHAT_ID = "-100"
        if secret:
            worker.env.TELEGRAM_WEBHOOK_SECRET = secret
        return worker

    async def test_polling_reports_conflict_ok_or_unknown(self):
        worker = self.telegram_worker()
        worker.telegram = mock.AsyncMock(return_value=(False, REFUSED))
        self.assertEqual(await worker.answer_commands({}), "conflict")
        worker.telegram = mock.AsyncMock(return_value=(False, None))  # timeout, 5xx, ...
        self.assertIs(await worker.answer_commands({}), entry.UNKNOWN)
        worker.telegram = mock.AsyncMock(return_value=(True, {"ok": True, "result": []}))
        self.assertIsNone(await worker.answer_commands({}))

    async def test_webhook_mode_checks_the_webhook_hourly(self):
        secret = "s" * 40
        worker = self.telegram_worker(secret)
        info = {"ok": True, "result": {"url": "https://x.workers.dev" + entry.webhook.webhook_path(secret)}}
        worker.telegram = mock.AsyncMock(return_value=(True, info))
        on_the_hour = 1_800_000_000 - 1_800_000_000 % 3600 + 5
        self.assertIsNone(await worker.check_webhook(secret, on_the_hour))
        self.assertIs(await worker.check_webhook(secret, on_the_hour + 60), entry.UNKNOWN)
        self.assertEqual(worker.telegram.await_count, 1)  # other minutes: no call, no KV
        worker.telegram = mock.AsyncMock(return_value=(True, {"ok": True, "result": {"url": ""}}))
        self.assertEqual(await worker.check_webhook(secret, on_the_hour + 3600), "missing")


class ScheduledTimeTest(unittest.TestCase):
    def test_uses_the_cron_time_not_the_clock(self):
        controller = types.SimpleNamespace(scheduledTime=1_800_000_000_000)
        self.assertEqual(entry.scheduled_time(controller), 1_800_000_000)
        with mock.patch.object(entry.time, "time", return_value=42.0):
            self.assertEqual(entry.scheduled_time(None), 42.0)


REFUSED = {"ok": False, "error_code": 409, "description": "Conflict: webhook is active"}


class ScheduledTest(EntryTestCase):
    def scheduled_worker(self, telegram_result):
        worker = self.worker()
        worker.env.TELEGRAM_BOT_TOKEN = "123456789:" + "A" * 35
        worker.env.TELEGRAM_CHAT_ID = "-100"
        self.events = []

        async def check(state, outbox, run):
            outbox.append("grid alert")

        async def send_telegram(text, reply_to=None, markup=None):
            self.events.append(text)
            return True

        async def telegram(method, payload):
            self.events.append(method)
            return telegram_result

        worker.check, worker.send_telegram, worker.telegram = check, send_telegram, telegram
        return worker

    async def test_alerts_go_out_before_commands_are_handled(self):
        worker = self.scheduled_worker((True, {"ok": True, "result": []}))
        await worker.scheduled(None)
        self.assertEqual(self.events, ["grid alert", "getUpdates"])

    async def test_no_warning_is_saved_over_an_unsent_alert(self):
        worker = self.scheduled_worker((False, REFUSED))

        async def send_fails(text, reply_to=None, markup=None):
            self.events.append(text)
            return text != "grid alert"  # Telegram didn't take the grid alert; others go through

        worker.send_telegram = send_fails
        await worker.scheduled(None)
        self.assertEqual(self.events, ["grid alert", "getUpdates"])  # the warning waits
        self.assertNotIn(entry.NOTIFY_KEY, worker.env.STATE.data)  # the alert is retried next run

    async def test_a_blip_does_not_repeat_the_conflict_warning(self):
        worker = self.scheduled_worker((False, REFUSED))
        await worker.scheduled(None)
        warning = entry.notify.WEBHOOK_PROBLEM_MESSAGES["conflict"]
        self.assertEqual(self.events.count(warning), 1)
        worker.telegram = mock.AsyncMock(return_value=(False, None))  # a blip
        await worker.scheduled(None)
        worker.telegram = mock.AsyncMock(return_value=(False, REFUSED))
        await worker.scheduled(None)
        self.assertEqual(self.events.count(warning), 1)


if __name__ == "__main__":
    unittest.main()
