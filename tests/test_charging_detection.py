import unittest

from tests.harness import App


class CarChargingTests(unittest.TestCase):
    def setUp(self):
        self.app = App()

    def charging(self):
        return self.app.car_charging()[0]

    def test_charging_during_started_dispatch(self):
        self.app.set_charger(switch="on", amps="16", dispatch="on")
        charging, info = self.app.car_charging()
        self.assertTrue(charging)
        self.assertEqual(info, {"charger_switch": "on", "charger_amps": 16.0, "dispatch": "on"})

    def test_drawing_power_without_dispatch_is_ignored(self):
        # e.g. preconditioning the car, or a bump charge: never cheap
        self.app.set_charger(amps="16", dispatch="off")
        self.assertFalse(self.charging())

    def test_dispatch_without_current_is_ignored(self):
        # dispatch sensor "could be incorrectly on" when the car isn't charging
        self.app.set_charger(amps="0", dispatch="on")
        self.assertFalse(self.charging())

    def test_charger_switch_off(self):
        self.app.set_charger(switch="off", amps="16", dispatch="on")
        self.assertFalse(self.charging())

    def test_current_threshold_is_inclusive(self):
        self.app.set_charger(amps="2.9")
        self.assertFalse(self.charging())
        self.app.set_charger(amps="3")
        self.assertTrue(self.charging())

    def test_custom_threshold(self):
        app = App({"min_current_amps": 10})
        app.set_charger(amps="9.5")
        self.assertFalse(app.car_charging()[0])
        app.set_charger(amps="10")
        self.assertTrue(app.car_charging()[0])

    def test_unavailable_or_missing_current_counts_as_zero(self):
        for value in ("unavailable", "unknown", "", None):
            with self.subTest(value=value):
                self.app.set_charger(amps=value)
                charging, info = self.app.car_charging()
                self.assertFalse(charging)
                self.assertEqual(info["charger_amps"], 0.0)

    def test_unavailable_dispatch_sensor(self):
        self.app.set_charger(dispatch="unavailable")
        self.assertFalse(self.charging())
        self.app.set_charger(dispatch=None)
        self.assertFalse(self.charging())

    def test_custom_entity_ids(self):
        app = App({
            "charger_switch": "switch.my_charger",
            "charger_current": "sensor.my_amps",
            "dispatch_sensor": "binary_sensor.my_dispatch",
        })
        app.states.update({"switch.my_charger": "on", "sensor.my_amps": "32", "binary_sensor.my_dispatch": "on"})
        self.assertTrue(app.car_charging()[0])


if __name__ == "__main__":
    unittest.main()
