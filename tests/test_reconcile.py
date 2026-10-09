import unittest

from tests.harness import App, local


def charging_app(config=None):
    app = App(config)
    app.capture_writes()
    app.mark_known()
    return app


class RollingSlotScenarioTests(unittest.TestCase):
    """The behaviour the whole app exists for, step by step through an afternoon."""

    def test_afternoon_dispatch(self):
        app = charging_app()
        steps = [
            # time,   charging, expected write (None = no write), programmed slot after
            ("14:00", False, None, "00:00-00:00"),
            ("14:12", True, "14:00-14:30", "14:00-14:30"),
            ("14:20", True, None, "14:00-14:30"),
            ("14:27", True, "14:00-15:00", "14:00-15:00"),
            ("14:45", True, None, "14:00-15:00"),
            ("14:57", True, "14:00-15:30", "14:00-15:30"),
            ("15:10", False, None, "14:00-15:30"),  # car stopped: finish the half-hour
            ("15:25", False, None, "14:00-15:30"),
            ("15:30", False, "00:00-00:00", "00:00-00:00"),  # expired: clear it
            ("15:35", False, None, "00:00-00:00"),
        ]
        for hhmm, charging, expected_write, expected_slot in steps:
            with self.subTest(time=hhmm):
                app.writes.clear()
                app.at(hhmm)
                app.set_charger(dispatch="on" if charging else "off")
                app.reconcile()
                expected = [{2: expected_write}] if expected_write else []
                self.assertEqual(app.writes, expected)
                self.assertEqual(app.slot(), expected_slot)

    def test_charging_resumes_after_slot_expired(self):
        app = charging_app()
        app.set_charger()
        app.at("14:12")
        app.reconcile()
        app.set_charger(dispatch="off")
        app.at("14:30")
        app.reconcile()
        self.assertEqual(app.slot(), "00:00-00:00")
        app.set_charger()
        app.at("14:41")
        app.reconcile()
        self.assertEqual(app.slot(), "14:30-15:00")

    def test_charging_resumes_before_slot_expired_keeps_start(self):
        app = charging_app()
        app.set_charger()
        app.at("14:05")
        app.reconcile()
        app.set_charger(dispatch="off")
        app.at("14:15")
        app.reconcile()
        app.set_charger()
        app.at("14:27")
        app.reconcile()
        self.assertEqual(app.slot(), "14:00-15:00")


class ExtensionTimingTests(unittest.TestCase):
    def test_extends_exactly_at_lead_time(self):
        app = charging_app()
        app.set_charger()
        app.at("14:05")
        app.reconcile()
        app.g["now_local"] = lambda: local("14:26").replace(second=59)
        app.reconcile()
        self.assertEqual(app.slot(), "14:00-14:30")
        app.at("14:27")
        app.reconcile()
        self.assertEqual(app.slot(), "14:00-15:00")

    def test_custom_lead_time(self):
        app = charging_app({"extend_lead_minutes": 5})
        app.set_charger()
        app.at("14:05")
        app.reconcile()
        app.at("14:25")
        app.reconcile()
        self.assertEqual(app.slot(), "14:00-15:00")

    def test_slot_not_shortened_by_next_check(self):
        app = charging_app()
        app.set_charger()
        app.at("14:57")
        app.reconcile()
        self.assertEqual(app.slot(), "14:30-15:30")
        app.at("15:01")
        app.reconcile()
        self.assertEqual(app.slot(), "14:30-15:30")

    def test_slot_never_shrinks_if_clock_goes_backwards(self):
        # e.g. HA's clock is corrected by NTP after the slot was extended
        app = charging_app()
        app.set_charger()
        app.at("14:57")
        app.reconcile()
        app.writes.clear()
        app.at("14:50")
        app.reconcile()
        self.assertEqual(app.slot(), "14:30-15:30")
        self.assertEqual(app.writes, [])

    def test_desired_slot_directly(self):
        app = App()
        self.assertEqual(app.desired_slot(local("14:12"), False), (None, None))
        self.assertEqual(app.desired_slot(local("14:12"), True), (local("14:00"), local("14:30")))


class CoreWindowInteractionTests(unittest.TestCase):
    def test_not_extended_into_core_window(self):
        app = charging_app()
        app.set_charger()
        app.at("23:12")
        app.reconcile()
        self.assertEqual(app.slot(), "23:00-23:30")
        app.at("23:27")
        app.reconcile()
        self.assertEqual(app.slot(), "23:00-23:30")
        app.at("23:35")
        app.reconcile()
        self.assertEqual(app.writes[-1], {2: "00:00-00:00"})

    def test_no_slot_inside_core_window(self):
        app = charging_app()
        app.set_charger()
        app.at("02:00", day=7)
        app.reconcile()
        self.assertEqual(app.writes, [])

    def test_slot_starts_at_end_of_core_window(self):
        app = charging_app()
        app.set_charger()
        app.at("05:27", day=7)
        app.reconcile()
        self.assertEqual(app.writes, [{2: "05:00-06:00"}])

    def test_without_core_management_charges_overnight(self):
        app = charging_app({"manage_core_slot": False})
        app.set_charger()
        app.at("02:10", day=7)
        app.reconcile()
        self.assertEqual(app.writes, [{2: "02:00-02:30"}])


class WriteBehaviourTests(unittest.TestCase):
    def test_no_write_when_idle_and_known(self):
        app = charging_app()
        app.set_charger(dispatch="off")
        app.reconcile()
        app.reconcile()
        self.assertEqual(app.writes, [])

    def test_unknown_inverter_state_is_cleared(self):
        app = App()
        app.capture_writes()
        app.set_charger(dispatch="off")
        app.reconcile()
        self.assertEqual(app.writes, [{2: "00:00-00:00"}])
        self.assertFalse(app._slot["unknown"])
        app.reconcile()
        self.assertEqual(len(app.writes), 1)

    def test_failed_write_is_retried(self):
        app = App()
        app.capture_writes(ok=[False, True])
        app.mark_known()
        app.set_charger()
        app.at("14:12")
        app.reconcile()
        self.assertEqual(app.slot(), "00:00-00:00")
        self.assertFalse(app._flags["last_write_ok"])
        app.at("14:15")
        app.reconcile()
        self.assertEqual(app.writes, [{2: "14:00-14:30"}, {2: "14:00-14:30"}])
        self.assertEqual(app.slot(), "14:00-14:30")
        self.assertTrue(app._flags["last_write_ok"])

    def test_custom_ev_slot(self):
        app = charging_app({"ev_slot": 5})
        app.set_charger()
        app.at("14:12")
        app.reconcile()
        self.assertEqual(app.writes, [{5: "14:00-14:30"}])

    def test_busy_flag_released_after_error(self):
        app = App()
        app.mark_known()

        async def broken(_):
            raise RuntimeError("boom")

        app.g["solis_write_slots"] = broken
        app.set_charger()
        with self.assertRaises(RuntimeError):
            app.reconcile()
        self.assertFalse(app._flags["busy"])

    def test_missing_dispatch_sensor_config_does_nothing(self):
        app = App({"dispatch_sensor": None})
        app.capture_writes()
        app.set_charger()
        with self.assertLogs("pyscript.solis_ev_follow", "ERROR"):
            app.reconcile()
        self.assertEqual(app.writes, [])


class StatusSensorTests(unittest.TestCase):
    def test_status_while_charging(self):
        app = charging_app()
        app.set_charger(amps="16")
        app.at("14:12")
        app.reconcile("charging started")
        entity, value, attrs = app.last_status()
        self.assertEqual(entity, "sensor.solis_ev_charge_slot")
        self.assertEqual(value, "charging")
        self.assertEqual(attrs["programmed_slot"], "14:00-14:30")
        self.assertTrue(attrs["ev_charging"])
        self.assertEqual(attrs["charger_amps"], 16.0)
        self.assertEqual(attrs["dispatch_state"], "on")
        self.assertEqual(attrs["last_reason"], "charging started")
        self.assertTrue(attrs["last_write_ok"])
        self.assertFalse(attrs["diagnostics_only"])
        self.assertEqual(attrs["ev_slot"], 2)

    def test_status_when_idle(self):
        app = charging_app()
        app.set_charger(dispatch="off")
        app.reconcile()
        _, value, attrs = app.last_status()
        self.assertEqual(value, "idle")
        self.assertEqual(attrs["programmed_slot"], "00:00-00:00")
        self.assertFalse(attrs["ev_charging"])


if __name__ == "__main__":
    unittest.main()
