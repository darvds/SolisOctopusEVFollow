import base64
import hashlib
import hmac
import json
import unittest

from tests.harness import App, FakeResponse, hours_ago, storage_inverter


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
            "/v2/api/login", "/v1/api/inverterList", "/v2/api/control", "/v2/api/control",
        ])
        login = self.cloud.calls[0]["body"]
        self.assertEqual(login, {"userInfo": "user@example.com", "password": self.app.passwordEncode("hunter2")})
        self.assertEqual(self.cloud.calls[1]["body"], {"stationId": "1234"})
        self.assertEqual(len(self.cloud.time_syncs()), 1)
        self.assertEqual(self.cloud.control_writes(), [("5949", "14:00-14:30")])
        control = self.cloud.calls[-1]
        self.assertEqual(control["body"]["inverterSn"], "SN111")
        self.assertEqual(control["headers"]["token"], "tok-123")

    def test_slot_cids(self):
        self.write({1: "23:30-05:30", 2: "a", 3: "b", 4: "c", 5: "d", 6: "e"})
        self.assertEqual(self.cloud.control_writes(), [
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
        self.assertEqual(self.cloud.control_writes(), [("5949", "x")])

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
        self.assertEqual(app.cloud.control_writes(), [("5946", "a"), ("5949", "b")])


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
