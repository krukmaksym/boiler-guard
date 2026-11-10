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


def shelly_status(output, power=None, energy=None):
    switch = {"output": output}
    if power is not None:
        switch["apower"] = power
    if energy is not None:
        switch["aenergy"] = {"total": energy}
    return {"isok": True, "data": {"online": True, "device_status": {"switch:0": switch}}}


class EntryTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sleep = mock.patch.object(entry.asyncio, "sleep", mock.AsyncMock())
        sleep.start()
        self.addCleanup(sleep.stop)
        entry.MEMO.clear()

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
        self.power, self.energy = None, None  # what the fake Shelly reports (W, Wh)

        async def shelly(path, secrets, **params):
            if path == "/device/status":
                return shelly_status(self.plug == logic.ON, self.power, self.energy)
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
        self.assertEqual(  # announced as soon as the grid is back, not after the wait
            outbox[-1],
            "✅ Світло повернулося (мережа 221 В). Світла не було 1 хв. "
            "Бойлер увімкнеться за 10 хв, якщо напруга буде стабільною.",
        )
        sent = len([m for m in outbox if m])
        self.at(worker, 221.0, 5)
        await worker.check(state, outbox, {})
        self.assertEqual(len([m for m in outbox if m]), sent)  # nothing during the wait
        self.at(worker, 221.0, 11)
        await worker.check(state, outbox, {})
        self.assertEqual(self.switched, [logic.OFF, logic.ON])
        self.assertEqual(outbox[-1], "🔥 Бойлер знову увімкнено: напруга стабільна вже 10 хв.")

    async def test_grid_lost_again_during_the_wait_is_announced_once(self):
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=0.0)
        state, outbox = {}, []
        await worker.check(state, outbox, {})
        self.at(worker, 225.0, 1)
        await worker.check(state, outbox, {})  # "back"
        self.at(worker, 0.0, 3)
        await worker.check(state, outbox, {})
        self.assertEqual(outbox[-1], "⚡️ Світло знову зникло (мережа 0 В). Бойлер лишається вимкненим.")
        self.at(worker, 0.0, 4)
        await worker.check(state, outbox, {})
        self.assertEqual(len([m for m in outbox if m and "знову зникло" in m]), 1)  # once
        self.at(worker, 225.0, 30)
        await worker.check(state, outbox, {})
        self.assertIn("Світла не було 27 хв", outbox[-1])  # since it went away again

    async def test_inverter_off_grid_again_during_the_wait_is_announced_once(self):
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=0.0)
        state, outbox = {}, []
        await worker.check(state, outbox, {})
        self.at(worker, 230.0, 1)
        await worker.check(state, outbox, {})  # "back"
        for minute in (3, 4):
            self.now = 1_800_000_000.0 + minute * 60
            worker.deye_reading = mock.AsyncMock(return_value=logic.Reading(240.0, relay="Break"))
            await worker.check(state, outbox, {})
        relay = [m for m in outbox if m and "знову відключився" in m]
        self.assertEqual(len(relay), 1)
        self.assertIn("протримається 10 хв", relay[0])
        self.at(worker, 230.0, 20)  # relay back ("Pull-in" implied by no relay key)
        await worker.check(state, outbox, {})
        self.assertIn(
            "✅ Інвертор знову підключився до мережі (230.0 В). Відключення тривало 17 хв.", outbox[-1]
        )
        self.assertEqual(self.switched, [logic.OFF])  # still waiting for the cooldown

    async def test_grid_back_at_low_voltage_after_an_outage(self):
        """What happened on 2026-11-05: an outage at 19:02, the grid back at low voltage
        for hours, 210 V only at 23:50. The outage must not be counted as 4 h 48 min."""
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=0.0)
        state, outbox = {}, []
        await worker.check(state, outbox, {})  # 19:02 outage
        self.at(worker, 196.0, 130)  # back at 196 V after 2 h 10 min
        await worker.check(state, outbox, {})
        self.assertEqual(
            outbox[-1],
            "💡 Світло повернулося (мережа 196 В), але напруга низька для бойлера. "
            "Світла не було 2 год 10 хв. Бойлер увімкнеться, коли напруга підніметься до 210 В "
            "і протримається 10 хв.",
        )
        sent = len([m for m in outbox if m])
        for minute, volts in ((140, 201.0), (200, 207.0)):  # still low or in the dead band
            self.at(worker, volts, minute)
            await worker.check(state, outbox, {})
        self.assertEqual(len([m for m in outbox if m]), sent)  # said once
        self.at(worker, 212.0, 288)  # 210 V+ at last
        await worker.check(state, outbox, {})
        self.assertIn(
            "✅ Напруга в мережі відновилася (212.0 В). Низька напруга трималася 2 год 38 хв.", outbox[-1]
        )
        self.at(worker, 216.0, 299)
        await worker.check(state, outbox, {})
        self.assertEqual(outbox[-1], "🔥 Бойлер знову увімкнено: напруга стабільна вже 11 хв.")

    async def test_grid_lost_again_after_back_at_low_voltage_is_announced(self):
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=0.0)
        state, outbox = {}, []
        await worker.check(state, outbox, {})
        self.at(worker, 196.0, 10)
        await worker.check(state, outbox, {})
        self.at(worker, 0.0, 20)
        await worker.check(state, outbox, {})
        self.assertIn("Світло знову зникло", outbox[-1])
        self.at(worker, 225.0, 30)
        await worker.check(state, outbox, {})
        self.assertIn("✅ Світло повернулося (мережа 225 В). Світла не було 10 хв.", outbox[-1])

    async def test_a_dip_below_on_after_back_stays_silent(self):
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=0.0)
        state, outbox = {}, []
        await worker.check(state, outbox, {})
        self.at(worker, 230.0, 5)
        await worker.check(state, outbox, {})  # "back"
        sent = len([m for m in outbox if m])
        self.at(worker, 207.0, 7)  # dead band: the wait restarts
        await worker.check(state, outbox, {})
        self.assertEqual(len([m for m in outbox if m]), sent)

    async def test_the_status_says_when_the_wait_is_over(self):
        worker = await self.low_voltage_off()
        self.at(worker, 230.0, 1)
        await worker.check({}, [], {})
        self.at(worker, 230.0, 11.5)  # the wait is over, the next run switches on
        snapshot = await worker.status_snapshot({"voltage": 230.0})
        self.assertEqual(snapshot["wait_seconds"], 0)

    async def test_a_voltage_dip_during_the_wait_is_silent(self):
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=190.0)
        state, outbox = {}, []
        await worker.check(state, outbox, {})
        self.at(worker, 230.0, 1)
        await worker.check(state, outbox, {})
        self.assertIn("✅ Напруга в мережі відновилася (230.0 В)", outbox[-1])
        sent = len([m for m in outbox if m])
        self.at(worker, 200.0, 4)  # dips again: the wait restarts, no message
        await worker.check(state, outbox, {})
        self.at(worker, 230.0, 5)
        await worker.check(state, outbox, {})
        self.assertEqual(len([m for m in outbox if m]), sent)

    async def test_back_while_held_says_it_stays_off(self):
        worker = self.worker({entry.DESIRED_KEY: logic.ON, entry.MODE_KEY: logic.OFF}, voltage=0.0)
        state, outbox = {}, []
        await worker.check(state, outbox, {})
        self.at(worker, 230.0, 1)
        await worker.check(state, outbox, {})
        self.assertIn("Бойлер лишається вимкненим: його вимкнули вручну.", outbox[-1])
        self.at(worker, 230.0, 11)
        outbox.clear()
        await worker.check(state, outbox, {})
        self.assertEqual([m for m in outbox if m], [])  # nothing new to say
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], entry.HELD_ON)

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

    async def test_refused_on_in_the_dead_band_names_the_threshold(self):
        worker = self.worker({entry.DESIRED_KEY: entry.COOLDOWN_OFF})
        worker.deye_reading = mock.AsyncMock(return_value=logic.Reading(209.0, relay="Pull-in"))
        reply = await worker.apply_control(logic.ON, {})
        self.assertIn("нижча за поріг увімкнення бойлера (210 В)", reply)
        self.assertIn("підніметься до 210 В і протримається 10 хв.", reply)
        worker.env.ON_VOLTAGE = "212"
        reply = await worker.apply_control(logic.ON, {})
        self.assertIn("(212 В)", reply)  # follows the configured threshold

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
        for key in (entry.DESIRED_KEY, entry.MODE_KEY, entry.VERIFY_OFF_KEY):  # mirrored for a rollback
            self.assertEqual(worker.env.STATE.data[key], worker.plug_data[key])
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

    async def test_kv_mirror_follows_deletes_without_extra_writes(self):
        worker = self.worker({entry.ON_PENDING_KEY: "123"})
        worker.env.STATE.data[entry.ON_PENDING_KEY] = "123"
        await worker.clear_on_pending()
        self.assertNotIn(entry.ON_PENDING_KEY, worker.plug_data)
        self.assertNotIn(entry.ON_PENDING_KEY, worker.env.STATE.data)
        worker.env.STATE.delete = mock.AsyncMock()
        await worker.clear_on_pending()  # nothing to clear: no KV write
        worker.env.STATE.delete.assert_not_awaited()

    async def test_a_failed_kv_mirror_does_not_break_the_run(self):
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=0.0)
        worker.env.STATE.put = mock.AsyncMock(side_effect=OSError("kv down"))
        await worker.check({}, [], {})
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], entry.COOLDOWN_OFF)

    async def test_deleting_a_missing_key_writes_nothing(self):
        namespace = FakeNamespace()
        store = namespace.get("plug")
        storage = store.ctx.storage
        storage.delete = mock.AsyncMock()
        self.assertEqual(await store.delete_value(entry.ON_PENDING_KEY), "")
        storage.delete.assert_not_awaited()
        namespace.data[entry.ON_PENDING_KEY] = "123"
        self.assertEqual(await store.delete_value(entry.ON_PENDING_KEY), "1")
        storage.delete.assert_awaited_once_with(entry.ON_PENDING_KEY)

    async def test_the_migrated_flag_is_read_once(self):
        namespace = FakeNamespace()
        store = namespace.get("plug")
        await store.get_value(entry.DESIRED_KEY)
        del namespace.data[entry.MIGRATED_KEY]  # would re-copy from KV if read again
        await store.get_value(entry.DESIRED_KEY)
        self.assertNotIn(entry.MIGRATED_KEY, namespace.data)

    def losing_reads(self, worker):
        """A store whose writes succeed but whose reads come back empty."""
        worker.env.PLUG.object.get_value = mock.AsyncMock(return_value="")

    async def test_a_store_that_loses_values_fails_loudly(self):
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=0.0)
        self.losing_reads(worker)
        with self.assertRaises(logic.ApiError) as raised:
            await worker.check({}, [], {})
        self.assertIn("plug state not saved", str(raised.exception))
        self.assertEqual(self.switched, [logic.OFF])  # the plug was still switched first
        detail = entry.error_summary(raised.exception)[1]
        self.assertIn("plug state not saved", entry.notify.error_message(detail))

    async def test_a_hold_that_is_not_saved_says_so(self):
        worker = self.worker(plug=logic.ON)
        self.losing_reads(worker)
        self.assertEqual(await worker.apply_control(logic.OFF, {}), bot.STATE_FAILED_MESSAGE)
        self.assertEqual(self.switched, [logic.OFF])

    async def test_a_working_store_reads_back_without_errors(self):
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=0.0)
        await worker.check({}, [], {})
        self.assertEqual(await worker.apply_control(logic.OFF, {}), bot.control_message("off", "off", False))

    async def test_missing_binding_is_a_config_error(self):
        worker = self.worker()
        del worker.env.PLUG
        with self.assertRaises(entry.ConfigError):
            await worker.check({}, [], {})


class MessagesFollowConfigTest(EntryTestCase):
    """Every number a message names comes from the Worker's variables, read on each
    call, so changing the config changes the next message (no hard-coded values)."""

    CONFIG = {
        "OFF_VOLTAGE": "197",
        "ON_VOLTAGE": "214",
        "HIGH_ON_VOLTAGE": "250",
        "HIGH_OFF_VOLTAGE": "260",
        "VOLTAGE_COOLDOWN_SECONDS": "300",
    }

    def configured(self, *args, **kwargs):
        worker = self.worker(*args, **kwargs)
        for name, value in self.CONFIG.items():
            setattr(worker.env, name, value)
        return worker

    async def test_switch_off_alerts(self):
        for volts, expected in (
            (
                196.0,
                "196.0 В, нижча за 197 В, тож бойлер вимкнено. Він увімкнеться, коли напруга "
                "підніметься до 214 В і протримається 5 хв.",
            ),
            (262.0, "262.0 В, вища за 260 В, тож бойлер вимкнено."),
        ):
            worker = self.configured({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=volts)
            outbox = []
            await worker.check({}, outbox, {})
            self.assertIn(expected, outbox[-1])
        self.assertIn("опуститься до 250 В і протримається 5 хв", outbox[-1])

    async def test_status_and_refused_on(self):
        worker = self.configured({entry.DESIRED_KEY: entry.COOLDOWN_OFF}, voltage=205.0)
        snapshot = await worker.status_snapshot({"voltage": 205.0})
        for key, value in (("off_below", 197.0), ("on_at", 214.0), ("high_on", 250.0), ("high_off", 260.0)):
            self.assertEqual(snapshot[key], value)
        status = bot.status_message(**snapshot)
        self.assertIn(
            "⏳ Бойлер увімкнеться сам, коли напруга підніметься до 214 В і протримається 5 хв", status
        )
        reply = await worker.apply_control(logic.ON, {})
        self.assertIn("нижча за поріг увімкнення бойлера (214 В)", reply)
        self.assertIn("протримається 5 хв.", reply)

    async def test_a_change_shows_up_in_the_next_message(self):
        worker = self.configured({entry.DESIRED_KEY: entry.COOLDOWN_OFF}, voltage=205.0)
        first = bot.status_message(**await worker.status_snapshot({"voltage": 205.0}))
        worker.env.ON_VOLTAGE = "208"  # e.g. edited in the Cloudflare dashboard
        second = bot.status_message(**await worker.status_snapshot({"voltage": 205.0}))
        self.assertIn("до 214 В", first)
        self.assertIn("до 208 В", second)


class DurationsTest(EntryTestCase):
    def setUp(self):
        super().setUp()
        self.now = 1_800_000_000.0
        clock = mock.patch.object(entry.time, "time", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def at(self, worker, voltage, minutes, battery=None, soc=60.0):
        self.now = 1_800_000_000.0 + minutes * 60
        worker.deye_reading = mock.AsyncMock(
            return_value=logic.Reading(voltage, soc=soc, load=100.0, battery=battery)
        )

    async def status(self, worker):
        return bot.status_message(**await worker.status_snapshot({}))

    async def test_grid_and_battery_durations(self):
        worker = self.worker({entry.DESIRED_KEY: logic.OFF})
        self.at(worker, 0.0, 0, battery=640.0)  # first seen: started at some unknown time
        await worker.check({}, [], {})
        self.at(worker, 0.0, 30, battery=640.0)
        text = await self.status(worker)
        # Already so when tracking began (e.g. right after a deploy): no misleading
        # "понад 30 хв" while it may have lasted for hours.
        self.assertIn("⚡️ Світла немає (мережа 0 В)", text)
        self.assertIn("розряджається: 640 Вт", text)
        self.at(worker, 225.0, 40, battery=-1000.0)  # grid back: an exact change
        await worker.check({}, [], {})
        self.at(worker, 225.0, 85, battery=-1000.0)
        text = await self.status(worker)
        self.assertIn("💡 Світло є 45 хв (мережа 225 В)", text)
        self.assertIn("заряджається 45 хв: 1000 Вт", text)

    async def test_boiler_on_time_and_heating_time(self):
        worker = self.worker({entry.DESIRED_KEY: logic.OFF}, plug=logic.OFF)
        self.energy = 5000.0
        self.at(worker, 230.0, 0)
        await worker.check({}, [], {})  # the cron switches it on
        self.assertEqual(self.switched, [logic.ON])
        # 2 h later: it heated for 1 h 30 min in total (2505 Wh at 1670 W, 230 V), idle now
        self.energy, self.power = 5000.0 + 2505.0, 0.0
        self.at(worker, 230.0, 120)
        text = await self.status(worker)
        self.assertIn("🔥 Бойлер увімкнений 2 год, з них нагрівав воду 1 год 30 хв", text)
        self.assertIn("(вода нагріта)", text)

    async def test_idle_heating_power_follows_the_grid_voltage(self):
        worker = self.worker({entry.DESIRED_KEY: logic.OFF}, plug=logic.OFF)
        self.energy = 0.0
        self.at(worker, 230.0, 0)
        await worker.check({}, [], {})
        watts_at_207 = 1670.0 * (207 / 230) ** 2  # ≈ 1353 W, as seen on 2026-11-08 (1366 W)
        self.energy, self.power = watts_at_207, 0.0  # one hour's worth at 207 V
        self.at(worker, 207.0, 90)
        self.assertIn("з них нагрівав воду 1 год\n", await self.status(worker))

    async def test_charging_time_survives_the_taper_near_full(self):
        worker = self.worker({entry.DESIRED_KEY: logic.OFF})
        self.at(worker, 230.0, 0, battery=640.0)
        await worker.check({}, [], {})  # discharging known
        self.at(worker, 230.0, 10, battery=-1000.0, soc=80.0)
        await worker.check({}, [], {})  # charging starts: an exact change
        for minute, watts in ((60, -15.0), (61, -25.0), (62, -12.0)):  # taper wobbles around 20 W
            self.at(worker, 230.0, minute, battery=watts, soc=99.0)
            await worker.check({}, [], {})
        self.at(worker, 230.0, 70, battery=-30.0, soc=99.0)
        self.assertIn("заряджається 1 год: 30 Вт", await self.status(worker))

    async def test_a_change_seen_after_a_data_gap_has_no_duration(self):
        worker = self.worker({entry.DESIRED_KEY: logic.OFF})
        state = {}
        self.at(worker, 230.0, 0)
        await worker.check(state, [], {})
        self.at(worker, None, 1)  # Deye silent: the outage starts somewhere in here
        worker.deye_reading = mock.AsyncMock(return_value=logic.Reading(problem=("stale", "no upload")))
        await worker.check(state, [], {})
        self.at(worker, 0.0, 30)
        await worker.check(state, [], {})  # first seen after the gap
        self.at(worker, 0.0, 40)
        self.assertIn("⚡️ Світла немає (мережа 0 В)", await self.status(worker))
        self.at(worker, 225.0, 50)
        await worker.check(state, [], {})  # an ordinary change again: exact
        self.at(worker, 225.0, 60)
        self.assertIn("💡 Світло є 10 хв", await self.status(worker))

    async def test_heating_time_uses_the_measured_power_while_heating(self):
        worker = self.worker({entry.DESIRED_KEY: logic.OFF}, plug=logic.OFF)
        self.energy = 100.0
        self.at(worker, 230.0, 0)
        await worker.check({}, [], {})
        self.energy, self.power = 100.0 + 700.0, 1400.0  # 30 min at 1400 W
        self.at(worker, 230.0, 30)
        self.assertIn("з них нагрівав воду 30 хв", await self.status(worker))

    async def test_a_boiler_already_on_shows_no_duration_until_switched(self):
        """The 2026-11-08 screenshot: on since before the deploy, first seen by /status."""
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON)
        self.energy, self.power = 50.0, 1534.0
        self.at(worker, 220.0, 0)
        await self.status(worker)  # first seen on here
        self.energy = 50.0 + 825.0
        self.at(worker, 220.0, 60)
        text = await self.status(worker)
        self.assertIn("🔥 Бойлер увімкнений\n", text)
        self.assertNotIn("нагрівав", text)

    async def test_a_change_first_seen_by_status_itself_is_shown(self):
        """The clock moves on between calls: the change's own timestamp must not count
        as "in the future" and be dropped (the bug in the first version)."""
        worker = self.worker({entry.DESIRED_KEY: logic.OFF})
        self.at(worker, 0.0, 0)
        await worker.check({}, [], {})  # outage known
        self.at(worker, 225.0, 5)
        ticks = iter(self.now + n * 0.001 for n in range(100))
        with mock.patch.object(entry.time, "time", lambda: next(ticks)):
            text = await self.status(worker)  # the grid's return is first seen here
        self.assertIn("💡 Світло є 1 хв (мережа 225 В)", text)

    async def test_no_energy_counter_means_no_heating_time(self):
        worker = self.worker({entry.DESIRED_KEY: logic.OFF}, plug=logic.OFF)
        self.at(worker, 230.0, 0)
        await worker.check({}, [], {})
        self.at(worker, 230.0, 20)
        text = await self.status(worker)
        self.assertIn("🔥 Бойлер увімкнений 20 хв\n", text)

    async def test_a_broken_timeline_never_breaks_the_run(self):
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=0.0)
        worker.env.PLUG.object.track = mock.AsyncMock(side_effect=RuntimeError("storage"))
        await worker.check({}, [], {})
        self.assertEqual(self.switched, [logic.OFF])  # switched off anyway
        self.assertNotIn("понад", bot.status_message(**await worker.status_snapshot({})))


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

    async def test_a_bad_number_names_its_variable(self):
        for name in ("OFF_VOLTAGE", "SHELLY_CHANNEL", "BLIND_OFF_SECONDS", "MAX_DATA_AGE_SECONDS"):
            worker = self.worker()
            setattr(worker.env, name, "abc")
            worker.deye_latest = mock.AsyncMock(return_value={})
            del worker.deye_reading  # the real one, which reads MAX_DATA_AGE_SECONDS
            with self.assertRaisesRegex(entry.ConfigError, name):
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


class BlindFailSafeTest(EntryTestCase):
    """No trustworthy Deye reading for BLIND_OFF_SECONDS: the boiler is switched off."""

    BLIND = logic.Reading(problem=("stale", "last upload 20 min ago"))

    def blind_worker(self, state=None, plug=logic.ON):
        worker = self.worker({entry.DESIRED_KEY: logic.ON} if state is None else state, plug=plug)
        worker.deye_reading = mock.AsyncMock(return_value=self.BLIND)
        return worker

    async def run_at(self, worker, state, at):
        outbox = []
        with mock.patch.object(entry.time, "time", return_value=at):
            await worker.check(state, outbox, {})
        return [m for m in outbox if m]

    async def test_switches_off_once_after_the_limit(self):
        worker, state = self.blind_worker(), {}
        self.assertEqual(await self.run_at(worker, state, 1000), [])
        alert = await self.run_at(worker, state, 1000 + 5 * 60)
        self.assertIn("ще 10 хв", alert[0])  # the blind alert says the switch-off follows
        self.assertEqual(await self.run_at(worker, state, 1000 + 14 * 60), [])
        self.assertEqual(self.switched, [])
        told = await self.run_at(worker, state, 1000 + 15 * 60)
        self.assertEqual(self.switched, [logic.OFF])
        self.assertIn("немає вже 15 хв, тож бойлер вимкнено", told[0])
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], entry.COOLDOWN_OFF)
        worker.shelly = mock.AsyncMock()
        self.assertEqual(await self.run_at(worker, state, 1000 + 16 * 60), [])
        worker.shelly.assert_not_awaited()  # done: no Shelly call for the rest of the gap

    async def test_zero_turns_it_off(self):
        worker, state = self.blind_worker(), {}
        worker.env.BLIND_OFF_SECONDS = "0"
        await self.run_at(worker, state, 1000)
        alert = await self.run_at(worker, state, 1000 + 3600)
        self.assertNotIn("про всяк випадок", alert[0])
        self.assertEqual(self.switched, [])

    async def test_a_boiler_the_guard_already_keeps_off_is_left_alone(self):
        for saved in (entry.COOLDOWN_OFF, entry.HELD_ON, None):
            worker = self.blind_worker({entry.DESIRED_KEY: saved} if saved else {}, plug=logic.OFF)
            worker.shelly = mock.AsyncMock()
            state = {"blind_since": 0, "blind_notified": True}
            self.assertEqual(await self.run_at(worker, state, 3600), [])
            worker.shelly.assert_not_awaited()

    async def test_shelly_offline_or_failing_is_retried_quietly(self):
        worker, state = self.blind_worker(), {"blind_since": 0, "blind_notified": True}
        good = worker.shelly
        worker.shelly = mock.AsyncMock(return_value={"isok": True, "data": {"online": False}})
        self.assertEqual(await self.run_at(worker, state, 3600), [])
        worker.shelly = mock.AsyncMock(side_effect=logic.ApiError("Shelly down"))
        self.assertEqual(await self.run_at(worker, state, 3660), [])
        self.assertEqual(worker.plug_data[entry.DESIRED_KEY], logic.ON)  # not saved: retried
        worker.shelly = good
        self.assertEqual(len(await self.run_at(worker, state, 3720)), 1)
        self.assertEqual(self.switched, [logic.OFF])

    async def switched_off_blind(self):
        worker, state = self.blind_worker(), {"blind_since": 0, "blind_notified": True}
        await self.run_at(worker, state, 3600)
        self.assertEqual(self.switched, [logic.OFF])
        self.switched.clear()
        return worker, state

    async def test_data_back_in_range_switches_on_after_the_cooldown(self):
        worker, state = await self.switched_off_blind()
        worker.deye_reading = mock.AsyncMock(return_value=logic.Reading(230.0))
        back = await self.run_at(worker, state, 4000)
        self.assertIn("знову надходять", back[0])
        self.assertIn("вимкнений через відсутність даних, увімкнеться за 10 хв", back[1])
        self.assertNotIn("Світло повернулося", "".join(back))  # no outage was seen
        self.assertEqual(self.switched, [])
        on = await self.run_at(worker, state, 4000 + 600)
        self.assertEqual(self.switched, [logic.ON])
        self.assertIn("Бойлер знову увімкнено", on[0])
        self.assertNotIn("blind_off", state)

    async def test_data_back_without_grid_is_told_as_the_outage_it_is(self):
        worker, state = await self.switched_off_blind()
        worker.deye_reading = mock.AsyncMock(return_value=logic.Reading(0.0))
        back = await self.run_at(worker, state, 4000)
        self.assertIn("Зникло світло", back[1])
        self.assertIn("уже був вимкнений", back[1])
        self.assertEqual(await self.run_at(worker, state, 4060), [])  # once
        worker.deye_reading = mock.AsyncMock(return_value=logic.Reading(230.0))
        returned = await self.run_at(worker, state, 4600)
        self.assertIn("Світло повернулося", returned[0])
        self.assertIn("Світла не було 10 хв", returned[0])

    async def test_data_back_between_the_thresholds_says_what_the_boiler_waits_for(self):
        worker, state = await self.switched_off_blind()
        worker.deye_reading = mock.AsyncMock(return_value=logic.Reading(207.0))  # 205..210: stays off
        back = await self.run_at(worker, state, 4000)
        self.assertIn("Світло є (мережа 207 В), але напруга низька", back[1])
        self.assertNotIn("Світла не було", back[1])
        self.assertEqual(self.switched, [])


class DeyeReloginTest(EntryTestCase):
    async def test_a_fresh_token_refused_too_backs_off_instead_of_logging_in_every_minute(self):
        worker = self.worker({entry.TOKEN_KEY: "cached"})
        logins = []

        async def deye_post(path, payload, token=None, query=None):
            if path == "/v1.0/account/token":
                logins.append(1)
                return 200, {"success": True, "accessToken": "fresh", "expiresIn": "5184000"}
            return 200, {"success": False, "msg": "auth invalid token"}

        worker.deye_post = deye_post
        await worker.deye_latest(SECRETS)
        self.assertEqual(len(logins), 1)
        for _ in range(3):
            with self.assertRaises(logic.ApiError):
                await worker.deye_latest(SECRETS)
        self.assertEqual(len(logins), 1)


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


class DeyeTokenWithoutKvTest(EntryTestCase):
    """A KV outage must not blind the guard (no token) or make it log in every minute."""

    def kv_down(self, worker):
        worker.env.STATE.get = mock.AsyncMock(side_effect=RuntimeError("KV down"))
        worker.env.STATE.put = mock.AsyncMock(side_effect=RuntimeError("KV down"))

    async def test_logs_in_once_and_reuses_the_token_from_memory(self):
        worker = self.worker()
        self.kv_down(worker)
        login = {"success": True, "accessToken": "fresh", "expiresIn": "5184000"}
        worker.deye_post = mock.AsyncMock(return_value=(200, login))
        self.assertEqual(await worker.deye_token(SECRETS), "fresh")
        self.assertEqual(await worker.deye_token(SECRETS), "fresh")
        self.assertEqual(worker.deye_post.await_count, 1)

    async def test_a_refused_login_backs_off_in_memory(self):
        worker = self.worker()
        self.kv_down(worker)
        worker.deye_post = mock.AsyncMock(return_value=(200, {"success": False, "msg": "bad password"}))
        for _ in range(3):
            with self.assertRaises(logic.ApiError):
                await worker.deye_token(SECRETS)
        self.assertEqual(worker.deye_post.await_count, 1)


class WebhookReplayTest(EntryTestCase):
    async def test_duplicate_update_is_handled_once(self):
        worker = self.worker()
        worker.handle_updates = mock.AsyncMock()
        await worker.handle_webhook_update({"update_id": 5})
        await worker.handle_webhook_update({"update_id": 5})
        await worker.handle_webhook_update({"update_id": 4})
        self.assertEqual(worker.handle_updates.await_count, 1)

    async def test_the_guard_expires_before_telegram_restarts_its_ids(self):
        """After a week without updates Telegram picks the next update_id at random; a
        guard without an expiry would drop a lower one as a replay for good."""
        worker = self.worker()
        worker.handle_updates = mock.AsyncMock()
        worker.kv_put = mock.AsyncMock()
        await worker.handle_webhook_update({"update_id": 900_000_000})
        ttl = worker.kv_put.await_args.kwargs["ttl"]
        self.assertTrue(60 <= ttl < 7 * 24 * 3600)


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

    def saved_state(self, worker):
        return entry.json.loads(worker.env.STATE.data[entry.NOTIFY_KEY])

    async def test_an_unsent_alert_is_kept_and_sent_first_by_a_later_run(self):
        worker = self.scheduled_worker((False, REFUSED))
        telegram_up = False

        async def send(text, reply_to=None, markup=None):
            self.events.append(text)
            return telegram_up

        worker.send_telegram = send
        await worker.scheduled(None)
        warning = entry.notify.WEBHOOK_PROBLEM_MESSAGES["conflict"]
        # The warning queues up behind the alert instead of overtaking or replacing it.
        self.assertEqual(self.events, ["grid alert", "getUpdates", "grid alert"])
        self.assertEqual([m["text"] for m in self.saved_state(worker)["unsent"]], ["grid alert", warning])

        async def quiet_check(state, outbox, run):
            pass

        worker.check, telegram_up = quiet_check, True
        self.events.clear()
        await worker.scheduled(None)
        self.assertEqual(self.events, ["grid alert", warning, "getUpdates"])
        self.assertNotIn("unsent", self.saved_state(worker))
        self.events.clear()
        await worker.scheduled(None)
        self.assertEqual(self.events, ["getUpdates"])  # sent once, not again

    async def test_an_alert_that_waited_says_how_late_it_is(self):
        worker = self.scheduled_worker((True, {"ok": True, "result": []}))
        old = {"unsent": [{"text": "grid alert", "at": entry.time.time() - 15 * 60}]}
        worker.env.STATE.data[entry.NOTIFY_KEY] = entry.json.dumps(old)

        async def quiet_check(state, outbox, run):
            pass

        worker.check = quiet_check
        await worker.scheduled(None)
        self.assertIn("затрималося на 15 хв", self.events[0])
        self.assertTrue(self.events[0].endswith("grid alert"))

    async def test_a_kv_outage_does_not_stop_the_plug_logic(self):
        """Grid lost while KV can't be read: the boiler is still switched off, the
        alert still goes out, and nothing is saved over the unread notify state."""
        worker = self.worker({entry.DESIRED_KEY: logic.ON}, plug=logic.ON, voltage=0.0)
        worker.env.TELEGRAM_BOT_TOKEN = "123456789:" + "A" * 35
        worker.env.TELEGRAM_CHAT_ID = "-100"
        sent = []

        async def telegram(method, payload):
            if method == "sendMessage":
                sent.append(payload["text"])
            return True, {"ok": True, "result": []}

        worker.telegram = telegram
        worker.env.STATE.get = mock.AsyncMock(side_effect=RuntimeError("KV down"))
        worker.env.STATE.put = mock.AsyncMock(side_effect=RuntimeError("KV down"))
        await worker.scheduled(None)
        self.assertEqual(self.switched, [logic.OFF])
        self.assertEqual(len([m for m in sent if "Зникло світло" in m]), 1)
        self.assertNotIn(entry.NOTIFY_KEY, worker.env.STATE.data)

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
