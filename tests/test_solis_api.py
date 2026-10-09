import base64
import hashlib
import hmac
import json
import unittest

from tests.harness import (
    DISCHARGE_SWITCH_CIDS as DISCHARGE,
    OK_CONTROL,
    SLOT_SWITCH_CIDS,
    App,
    FakeResponse,
    hours_ago,
    storage_inverter,
)


class AuthHelperTests(unittest.TestCase):
    def setUp(self):
        self.app = App()

    def test_password_encode_is_md5_hex(self):
        self.assertEqual(self.app.passwordEncode("password"), "5f4dcc3b5aa765d61d8327deb882cf99")

    def test_digest_is_base64_md5(self):
        expected = base64.b64encode(hashlib.md5(b"{}").digest()).decode()
        self.assertEqual(self.app.digest("{}"), expected)

    def test_prepare_header_signature(self):
        body = '{"a":1}'
        headers = self.app.prepare_header({"key_id": "kid", "secret": "sec"}, body, "/v2/api/control")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Content-MD5"], self.app.digest(body))
        self.assertTrue(headers["Date"].endswith(" GMT"))
        to_sign = "\n".join(["POST", headers["Content-MD5"], "application/json", headers["Date"], "/v2/api/control"])
        sig = base64.b64encode(hmac.new(b"sec", to_sign.encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(headers["Authorization"], "API kid:" + sig)

    def test_clean_json_removes_trailing_commas(self):
        cleaned = self.app._clean_json_text('{"a": [1, 2,], "b": {"c": "x,}",},}')
        self.assertEqual(json.loads(cleaned), {"a": [1, 2], "b": {"c": "x,}"}})


class WriteSlotsTests(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.cloud = self.app.cloud

    def write(self, slots):
        return self.app.run(self.app.solis_write_slots(slots))

    def test_happy_path(self):
        self.assertTrue(self.write({2: "14:00-14:30"}))
        self.assertEqual(self.cloud.paths(), [
            "/v2/api/login", "/v1/api/inverterList", "/v2/api/control",  # time sync
            "/v2/api/atReadBatch", "/v2/api/control", "/v2/api/control",  # switches, time, enable
        ])
        login = self.cloud.calls[0]["body"]
        self.assertEqual(login, {"userInfo": "user@example.com", "password": self.app.passwordEncode("hunter2")})
        self.assertEqual(self.cloud.calls[1]["body"], {"stationId": "1234"})
        self.assertEqual(len(self.cloud.time_syncs()), 1)
        self.assertEqual(self.cloud.control_writes(), [("5949", "14:00-14:30"), ("5917", "1")])
        control = self.cloud.calls[-2]
        self.assertEqual(control["body"]["inverterSn"], "SN111")
        self.assertEqual(control["headers"]["token"], "tok-123")

    def test_slot_cids(self):
        self.write({1: "23:30-05:30", 2: "a", 3: "b", 4: "c", 5: "d", 6: "e"})
        self.assertEqual(self.cloud.time_writes(), [
            ("5946", "23:30-05:30"), ("5949", "a"), ("5952", "b"),
            ("5955", "c"), ("5958", "d"), ("5961", "e"),
        ])

    def test_time_sync_payload(self):
        self.write({2: "x"})
        body = self.cloud.time_syncs()[0]["body"]
        self.assertEqual(body["inverterId"], "111")
        self.assertRegex(body["value"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_inverter_lookup_is_cached(self):
        self.write({2: "x"})
        self.write({2: "y"})
        self.assertEqual(self.cloud.paths().count("/v1/api/inverterList"), 1)
        self.assertEqual(self.cloud.paths().count("/v2/api/login"), 2)

    def test_time_sync_at_most_daily(self):
        self.write({2: "x"})
        self.write({2: "y"})
        self.assertEqual(len(self.cloud.time_syncs()), 1)
        self.app._cache["last_time_sync"] = hours_ago(25)
        self.write({2: "z"})
        self.assertEqual(len(self.cloud.time_syncs()), 2)

    def test_time_sync_disabled(self):
        app = App({"sync_inverter_time": False})
        app.run(app.solis_write_slots({2: "x"}))
        self.assertEqual(app.cloud.time_syncs(), [])

    def test_failed_time_sync_does_not_block_write(self):
        self.cloud.queued["/v2/api/control"] = [FakeResponse(500, {})]
        self.assertTrue(self.write({2: "x"}))
        self.assertIsNone(self.app._cache["last_time_sync"])
        self.assertEqual(self.cloud.time_writes(), [("5949", "x")])

    def test_diagnostics_mode_never_calls_solis(self):
        app = App({"diagnostics_only": True})
        self.assertTrue(app.run(app.solis_write_slots({2: "14:00-14:30"})))
        self.assertEqual(app.cloud.calls, [])

    def test_login_failure(self):
        self.cloud.queued["/v2/api/login"] = [FakeResponse(401, {})]
        self.assertFalse(self.write({2: "x"}))
        self.assertEqual(self.cloud.paths(), ["/v2/api/login"])

    def test_login_without_token(self):
        self.cloud.queued["/v2/api/login"] = [FakeResponse(200, {"msg": "bad"})]
        self.assertFalse(self.write({2: "x"}))

    def test_one_failed_slot_reports_failure_but_writes_the_rest(self):
        app = App({"sync_inverter_time": False, "control_retries": 1})
        app.cloud.queued["/v2/api/control"] = [FakeResponse(500, {})]
        self.assertFalse(app.run(app.solis_write_slots({1: "a", 2: "b"})))
        self.assertEqual(app.cloud.time_writes(), [("5946", "a"), ("5949", "b")])
        # slot 1's time failed, so it must not be switched on with a stale time
        self.assertEqual(app.cloud.switch_writes(), [("5917", "1", "0")])


class SlotSwitchTests(unittest.TestCase):
    def setUp(self):
        self.app = App({"sync_inverter_time": False})
        self.cloud = self.app.cloud

    def write(self, slots):
        return self.app.run(self.app.solis_write_slots(slots))

    def test_window_enables_slot_after_time_is_written(self):
        self.assertTrue(self.write({2: "14:00-14:30"}))
        self.assertEqual(self.cloud.control_writes(), [("5949", "14:00-14:30"), ("5917", "1")])
        self.assertEqual(self.cloud.values["5917"], "1")

    def test_clearing_disables_slot_before_time_is_cleared(self):
        self.cloud.values["5917"] = "1"
        self.assertTrue(self.write({2: "00:00-00:00"}))
        self.assertEqual(self.cloud.control_writes(), [("5917", "0"), ("5949", "00:00-00:00")])

    def test_no_switch_write_when_already_in_state(self):
        self.cloud.values["5917"] = "1"
        self.write({2: "14:00-15:00"})
        self.assertEqual(self.cloud.switch_writes(), [])
        self.cloud.values["5917"] = "0"
        self.cloud.calls.clear()
        self.write({2: "00:00-00:00"})
        self.assertEqual(self.cloud.switch_writes(), [])

    def test_yuanzhi_is_bitmask_of_all_switches_before_change(self):
        # charge slot 1 on (bit 0), discharge slot 1 on (bit 6), discharge slot 6 on (bit 11)
        self.cloud.values.update({"5916": "1", "5922": "1", "5927": "1"})
        self.write({2: "14:00-14:30"})
        self.assertEqual(self.cloud.switch_writes(), [("5917", "1", str(1 + 64 + 2048))])

    def test_yuanzhi_tracks_earlier_switch_writes_in_same_batch(self):
        self.write({1: "23:30-05:30", 2: "00:00-00:00", 3: "10:00-10:30"})
        self.assertEqual(self.cloud.switch_writes(), [
            ("5916", "1", "0"),  # slot 1 on, nothing on before
            ("5918", "1", "1"),  # slot 3 on, slot 1 (bit 0) already on
        ])

    def test_switches_read_once_per_write_session(self):
        self.write({1: "23:30-05:30", 2: "00:00-00:00", 3: "00:00-00:00"})
        self.assertEqual(self.cloud.paths().count("/v2/api/atReadBatch"), 1)
        batch = [c for c in self.cloud.calls if c["path"] == "/v2/api/atReadBatch"][0]
        self.assertEqual(batch["body"]["cids"], ",".join([str(c) for c in range(5916, 5928)]))

    def test_init_schedule_switch_states(self):
        self.cloud.values.update({"5917": "1", "5920": "1", "5922": "1"})
        app = self.app
        app.run(app.init_schedule())
        self.assertEqual(self.cloud.values["5916"], "1")  # core window on
        for cid in ("5917", "5918", "5919", "5920", "5921"):
            self.assertEqual(self.cloud.values[cid], "0", cid)  # cleared slots off
        self.assertEqual(self.cloud.values["5922"], "1")  # discharge slots untouched
        self.assertFalse(any(w[0] in DISCHARGE for w in self.cloud.switch_writes()))

    def test_falls_back_to_single_reads_when_batch_fails(self):
        self.cloud.queued["/v2/api/atReadBatch"] = [FakeResponse(500, {})]
        self.cloud.values["5916"] = "1"
        self.assertTrue(self.write({2: "14:00-14:30"}))
        self.assertEqual(self.cloud.paths().count("/v2/api/atRead"), 12)
        self.assertEqual(self.cloud.switch_writes(), [("5917", "1", "1")])

    def test_unknown_states_still_enable_without_yuanzhi(self):
        self.cloud.queued["/v2/api/atReadBatch"] = [FakeResponse(500, {})]
        self.cloud.queued["/v2/api/atRead"] = [FakeResponse(500, {})] * 36
        with self.assertLogs("pyscript.solis_ev_follow", "WARNING"):
            self.assertTrue(self.write({2: "14:00-14:30"}))
        self.assertEqual(self.cloud.switch_writes(), [("5917", "1", None)])

    def test_failed_enable_reports_failure(self):
        app = App({"sync_inverter_time": False, "control_retries": 1})
        app.cloud.queued["/v2/api/control"] = [FakeResponse(200, OK_CONTROL), FakeResponse(500, {})]
        self.assertFalse(app.run(app.solis_write_slots({2: "14:00-14:30"})))

    def test_switch_management_can_be_disabled(self):
        app = App({"sync_inverter_time": False, "manage_slot_switches": False})
        app.run(app.solis_write_slots({2: "14:00-14:30"}))
        self.assertEqual(app.cloud.paths(), ["/v2/api/login", "/v1/api/inverterList", "/v2/api/control"])

    def test_slot_switch_mask(self):
        mask = self.app.slot_switch_mask
        states = {cid: "0" for cid in SLOT_SWITCH_CIDS}
        self.assertEqual(mask(states), "0")
        states.update({"5921": "1", "5922": "1"})
        self.assertEqual(mask(states), str((1 << 5) + (1 << 6)))
        del states["5916"]
        self.assertIsNone(mask(states))


class WriteControlRetryTests(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.cloud = self.app.cloud

    def write_control(self):
        return self.app.run(self.app.write_control(self.cloud, "tok", "SN1", "5949", "14:00-14:30"))

    def test_retries_http_errors(self):
        self.cloud.queued["/v2/api/control"] = [FakeResponse(500, {}), FakeResponse(502, {})]
        self.assertTrue(self.write_control())
        self.assertEqual(len(self.cloud.calls), 3)

    def test_retries_api_error_codes(self):
        self.cloud.queued["/v2/api/control"] = [
            FakeResponse(200, {"code": "1", "msg": "busy"}),
            FakeResponse(200, {"code": "0", "data": [{"code": "-1"}]}),
        ]
        self.assertTrue(self.write_control())
        self.assertEqual(len(self.cloud.calls), 3)

    def test_gives_up_after_retries(self):
        self.cloud.queued["/v2/api/control"] = [FakeResponse(500, {})] * 3
        with self.assertLogs("pyscript.solis_ev_follow", "ERROR"):
            self.assertFalse(self.write_control())
        self.assertEqual(len(self.cloud.calls), 3)

    def test_garbage_response_is_retried(self):
        self.cloud.queued["/v2/api/control"] = [FakeResponse(200, "<html>oops</html>")]
        self.assertTrue(self.write_control())

    def test_trailing_comma_response_accepted(self):
        self.cloud.queued["/v2/api/control"] = [FakeResponse(200, '{"code":"0","data":[{"code":"0",},],}')]
        self.assertTrue(self.write_control())
        self.assertEqual(len(self.cloud.calls), 1)

    def test_verify_readback(self):
        app = App({"verify_readback": True})
        self.assertTrue(app.run(app.write_control(app.cloud, "tok", "SN1", "5949", "x")))
        self.assertEqual(app.cloud.paths(), ["/v2/api/control", "/v2/api/atRead"])
        self.assertEqual(app.cloud.calls[1]["body"], {"inverterSn": "SN1", "cid": "5949"})

    def test_verify_readback_mismatch_is_logged(self):
        app = App({"verify_readback": True})
        app.cloud.queued["/v2/api/atRead"] = [FakeResponse(200, {"code": "0", "data": {"msg": "00:00-00:00"}})]
        with self.assertLogs("pyscript.solis_ev_follow", "WARNING") as logs:
            app.run(app.write_control(app.cloud, "tok", "SN1", "5949", "14:00-14:30"))
        self.assertTrue(any("read '00:00-00:00'" in line for line in logs.output))

    def test_yuanzhi_sent_only_when_given(self):
        app = App()
        app.run(app.write_control(app.cloud, "tok", "SN1", "5949", "x"))
        app.run(app.write_control(app.cloud, "tok", "SN1", "5917", "1", old_value="65"))
        self.assertNotIn("yuanzhi", app.cloud.calls[0]["body"])
        self.assertEqual(app.cloud.calls[1]["body"]["yuanzhi"], "65")


class ReadValueTests(unittest.TestCase):
    def test_reads_msg_from_object_response(self):
        app = App()
        app.cloud.values["5946"] = "23:30-05:30"
        self.assertEqual(app.run(app.get_control_value(app.cloud, "tok", "SN1", "5946")), "23:30-05:30")

    def test_reads_msg_from_list_response(self):
        app = App()
        app.cloud.queued["/v2/api/atRead"] = [FakeResponse(200, {"code": "0", "data": [{"msg": "1"}]})]
        self.assertEqual(app.run(app.get_control_value(app.cloud, "tok", "SN1", "5916")), "1")

    def test_returns_none_after_retries(self):
        app = App()
        app.cloud.queued["/v2/api/atRead"] = [FakeResponse(500, {})] * 3
        self.assertIsNone(app.run(app.get_control_value(app.cloud, "tok", "SN1", "5916")))


class InverterSelectionTests(unittest.TestCase):
    def select(self, records, config=None):
        app = App(config)
        app.cloud.records = records
        return app.run(app.get_inverter(app.cloud))

    def test_single_storage_inverter(self):
        records = [{"id": "9", "sn": "PV9", "productModel": "1"}, storage_inverter("2", "SN2")]
        self.assertEqual(self.select(records), ("2", "SN2"))

    def test_single_non_storage_inverter(self):
        self.assertEqual(self.select([{"id": "9", "sn": "PV9", "productModel": "1"}]), ("9", "PV9"))

    def test_multiple_storage_inverters_need_config(self):
        records = [storage_inverter("1", "SN1"), storage_inverter("2", "SN2")]
        with self.assertLogs("pyscript.solis_ev_follow", "ERROR"):
            self.assertEqual(self.select(records), (None, None))

    def test_pick_by_serial(self):
        records = [storage_inverter("1", "SN1"), storage_inverter("2", "SN2")]
        self.assertEqual(self.select(records, {"inverter_sn": "SN2"}), ("2", "SN2"))

    def test_pick_by_id(self):
        records = [storage_inverter("1", "SN1"), storage_inverter("2", "SN2")]
        self.assertEqual(self.select(records, {"inverter_id": "1"}), ("1", "SN1"))

    def test_configured_inverter_not_found(self):
        with self.assertLogs("pyscript.solis_ev_follow", "ERROR"):
            self.assertEqual(self.select([storage_inverter("1", "SN1")], {"inverter_sn": "NOPE"}), (None, None))

    def test_undefined_secret_treated_as_unset(self):
        self.assertEqual(self.select([storage_inverter("1", "SN1")], {"inverter_sn": "unknown"}), ("1", "SN1"))

    def test_no_inverters(self):
        with self.assertLogs("pyscript.solis_ev_follow", "ERROR"):
            self.assertEqual(self.select([]), (None, None))

    def test_inverter_list_http_error(self):
        app = App()
        app.cloud.queued["/v1/api/inverterList"] = [FakeResponse(500, {})]
        self.assertEqual(app.run(app.get_inverter(app.cloud)), (None, None))
        self.assertIsNone(app._cache["inverter_id"])


if __name__ == "__main__":
    unittest.main()
