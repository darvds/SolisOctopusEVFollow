import unittest
from datetime import datetime

from tests.harness import TZ, App, local


class HalfHourRoundingTests(unittest.TestCase):
    def setUp(self):
        self.app = App()

    def test_floor_half_hour(self):
        floor = self.app.floor_half_hour
        self.assertEqual(floor(local("10:13")), local("10:00"))
        self.assertEqual(floor(local("10:30")), local("10:30"))
        self.assertEqual(floor(datetime(2026, 10, 6, 10, 59, 59, 999, tzinfo=TZ)), local("10:30"))

    def test_next_half_hour_is_strictly_after(self):
        nxt = self.app.next_half_hour
        self.assertEqual(nxt(local("10:13")), local("10:30"))
        self.assertEqual(nxt(local("10:30")), local("11:00"))
        self.assertEqual(nxt(local("10:00")), local("10:30"))

    def test_next_half_hour_crosses_midnight(self):
        self.assertEqual(self.app.next_half_hour(local("23:45")), local("00:00", day=7))


class CoreWindowTests(unittest.TestCase):
    def test_default_window_wraps_midnight(self):
        in_core = App().in_core_window
        self.assertFalse(in_core(local("23:29")))
        self.assertTrue(in_core(local("23:30")))
        self.assertTrue(in_core(local("00:00")))
        self.assertTrue(in_core(local("03:00")))
        self.assertTrue(in_core(local("05:29")))
        self.assertFalse(in_core(local("05:30")))
        self.assertFalse(in_core(local("14:00")))

    def test_non_wrapping_window(self):
        in_core = App({"core_window": "01:00-04:00"}).in_core_window
        self.assertFalse(in_core(local("00:59")))
        self.assertTrue(in_core(local("01:00")))
        self.assertTrue(in_core(local("03:59")))
        self.assertFalse(in_core(local("04:00")))

    def test_window_with_spaces_is_parsed(self):
        app = App({"core_window": "23:30 - 05:30"})
        self.assertTrue(app.in_core_window(local("02:00")))

    def test_disabled_core_slot_never_in_core(self):
        app = App({"manage_core_slot": False})
        self.assertFalse(app.in_core_window(local("02:00")))


class FormatSlotTests(unittest.TestCase):
    def setUp(self):
        self.app = App()

    def test_empty_slot(self):
        self.assertEqual(self.app.fmt_slot(None, None), "00:00-00:00")
        self.assertEqual(self.app.fmt_slot(local("10:00"), None), "00:00-00:00")

    def test_slot_formatting(self):
        self.assertEqual(self.app.fmt_slot(local("14:00"), local("15:30")), "14:00-15:30")

    def test_slot_ending_at_midnight(self):
        self.assertEqual(self.app.fmt_slot(local("23:00"), local("00:00", day=7)), "23:00-00:00")


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        app = App({"inter_write_delay": None, "inverter_timezone": None})
        self.assertEqual(app.CHARGER_SWITCH, "switch.hypervolt_charging")
        self.assertEqual(app.CHARGER_CURRENT, "sensor.hypervolt_charger_current")
        self.assertEqual(app.MIN_CURRENT_AMPS, 3.0)
        self.assertEqual(app.DEBOUNCE_SECONDS, 60)
        self.assertEqual(app.EXTEND_LEAD_MINUTES, 3)
        self.assertEqual(app.EV_SLOT, 2)
        self.assertTrue(app.MANAGE_CORE_SLOT)
        self.assertEqual(app.CORE_WINDOW, "23:30-05:30")
        self.assertTrue(app.CLEAR_OTHER_SLOTS)
        self.assertTrue(app.SYNC_INVERTER_TIME)
        self.assertFalse(app.DIAGNOSTICS_ONLY)
        self.assertFalse(app.VERIFY_READBACK)
        self.assertEqual(app.INTER_WRITE_DELAY, 0.25)

    def test_timezone_defaults_to_home_assistant(self):
        app = App({"inverter_timezone": None}, time_zone="Europe/Paris")
        self.assertEqual(app.INVERTER_TIMEZONE, "Europe/Paris")

    def test_invalid_timezone_falls_back_to_utc(self):
        from datetime import timezone
        app = App({"inverter_timezone": "Not/AZone"})
        self.assertIs(app.INV_TZ, timezone.utc)

    def test_boolean_strings(self):
        for value, expected in (("true", True), ("yes", True), ("on", True), ("1", True),
                                (True, True), ("false", False), ("no", False), (False, False)):
            with self.subTest(value=value):
                self.assertEqual(App({"diagnostics_only": value}).DIAGNOSTICS_ONLY, expected)

    def test_config_read_from_yaml_when_not_loaded_as_app(self):
        app = App({"ev_slot": 4}, as_app=False)
        self.assertEqual(app.EV_SLOT, 4)
        self.assertEqual(app.DISPATCH_SENSOR, "binary_sensor.octopus_energy_test_intelligent_dispatching")


if __name__ == "__main__":
    unittest.main()
