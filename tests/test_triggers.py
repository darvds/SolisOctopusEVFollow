import unittest

from tests.harness import CURRENT, DISPATCH, SWITCH, App


class TriggerRegistrationTests(unittest.TestCase):
    def test_triggers_registered(self):
        app = App()
        triggers = {name: (kind, args) for kind, args, name in app.triggers}
        self.assertEqual(triggers["solis_ev_follow_startup"], ("time_trigger", ("startup",)))
        self.assertEqual(triggers["solis_ev_follow_state_change"], ("state_trigger", (SWITCH, CURRENT, DISPATCH)))
        self.assertEqual(
            triggers["solis_ev_follow_periodic"],
            ("time_trigger", ("cron(*/5 * * * *)", "cron(27,57 * * * *)")),
        )

    def test_extension_cron_follows_lead_time(self):
        app = App({"extend_lead_minutes": 5})
        periodic = [args for _, args, name in app.triggers if name == "solis_ev_follow_periodic"][0]
        self.assertIn("cron(25,55 * * * *)", periodic)

    def test_services_registered(self):
        self.assertEqual(App().services, ["solis_ev_follow_reconcile", "solis_ev_follow_init"])


class StateChangeTests(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.app.capture_writes()
        self.app.mark_known()
        self.app.at("14:12")

    def fire(self):
        self.app.run(self.app.solis_ev_follow_state_change(var_name=CURRENT, value="16"))

    def test_sustained_charging_starts_battery_after_debounce(self):
        self.app.set_charger()
        self.fire()
        self.assertEqual(self.app.asyncio.sleeps, [60])
        self.assertEqual(self.app.unique_calls, [("solis_ev_follow_debounce", True)])
        self.assertEqual(self.app.writes, [{2: "14:00-14:30"}])

    def test_brief_blip_is_ignored(self):
        # e.g. the car is full and the charger only checks it for a few seconds
        self.app.set_charger()
        self.app.asyncio.on_sleep = lambda _: self.app.set_charger(amps="0")
        with self.assertLogs("pyscript.solis_ev_follow", "INFO") as logs:
            self.fire()
        self.assertEqual(self.app.writes, [])
        self.assertTrue(any("did not persist" in line for line in logs.output))

    def test_custom_debounce(self):
        app = App({"debounce_seconds": 15})
        app.capture_writes()
        app.mark_known()
        app.set_charger()
        app.run(app.solis_ev_follow_state_change())
        self.assertEqual(app.asyncio.sleeps, [15])

    def test_only_acts_when_charging_state_flips(self):
        self.app.set_charger()
        self.fire()
        self.fire()  # current fluctuates, still charging: nothing new
        self.assertEqual(self.app.asyncio.sleeps, [60])
        self.assertEqual(len(self.app.writes), 1)

    def test_stopping_does_not_write(self):
        self.app.set_charger()
        self.fire()
        self.app.set_charger(amps="0")
        self.fire()
        self.assertEqual(self.app.writes, [{2: "14:00-14:30"}])
        self.assertEqual(self.app.slot(), "14:00-14:30")

    def test_not_charging_does_nothing(self):
        self.app.set_charger(dispatch="off")
        self.fire()
        self.assertEqual(self.app.asyncio.sleeps, [])
        self.assertEqual(self.app.writes, [])


class StartupTests(unittest.TestCase):
    def test_startup_initialises_then_reconciles(self):
        app = App()
        app.capture_writes()
        app.set_charger()
        app.at("14:12")
        app.run(app.solis_ev_follow_startup())
        self.assertEqual(app.asyncio.sleeps, [60])
        self.assertEqual(app.writes, [
            {1: "23:30-05:30", 2: "00:00-00:00", 3: "00:00-00:00",
             4: "00:00-00:00", 5: "00:00-00:00", 6: "00:00-00:00"},
            {2: "14:00-14:30"},
        ])
        self.assertTrue(app._flags["last_charging"])

    def test_startup_when_idle_only_initialises(self):
        app = App()
        app.capture_writes()
        app.set_charger(dispatch="off")
        app.run(app.solis_ev_follow_startup())
        self.assertEqual(len(app.writes), 1)
        self.assertFalse(app._flags["last_charging"])

    def test_startup_refuses_ev_slot_1_with_core_slot(self):
        app = App({"ev_slot": 1})
        app.capture_writes()
        with self.assertLogs("pyscript.solis_ev_follow", "ERROR"):
            app.run(app.solis_ev_follow_startup())
        self.assertEqual(app.writes, [])


class InitScheduleTests(unittest.TestCase):
    def init(self, config=None, ok=True):
        app = App(config)
        app.capture_writes(ok=ok)
        app.run(app.init_schedule())
        return app

    def test_default_schedule(self):
        app = self.init()
        self.assertEqual(app.writes, [{
            1: "23:30-05:30", 2: "00:00-00:00", 3: "00:00-00:00",
            4: "00:00-00:00", 5: "00:00-00:00", 6: "00:00-00:00",
        }])
        self.assertFalse(app._slot["unknown"])

    def test_spaces_stripped_from_core_window(self):
        app = self.init({"core_window": "23:30 - 05:30"})
        self.assertEqual(app.writes[0][1], "23:30-05:30")

    def test_keep_other_slots(self):
        app = self.init({"clear_other_slots": False})
        self.assertEqual(app.writes, [{1: "23:30-05:30", 2: "00:00-00:00"}])

    def test_without_core_management(self):
        app = self.init({"manage_core_slot": False, "clear_other_slots": False})
        self.assertEqual(app.writes, [{2: "00:00-00:00"}])

    def test_failure_leaves_state_unknown(self):
        app = self.init(ok=False)
        self.assertTrue(app._slot["unknown"])
        self.assertFalse(app._flags["busy"])

    def test_init_service_resets_running_slot(self):
        app = App()
        app.capture_writes()
        app.mark_known()
        app.set_charger(dispatch="off")
        app._slot.update({"start": app.at("14:00"), "end": app.at("14:30")})
        app.at("14:10")
        app.run(app.solis_ev_follow_init())
        self.assertEqual(app.slot(), "00:00-00:00")
        self.assertEqual(len(app.writes), 1)


if __name__ == "__main__":
    unittest.main()
