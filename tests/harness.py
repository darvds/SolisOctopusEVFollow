"""
Test harness: loads the pyscript app outside Home Assistant.

pyscript injects globals (pyscript, hass, state, task, service, time_trigger,
state_trigger) into each script, so the app can't be imported normally. The
harness compiles the file into a fresh namespace with fakes for all of those,
plus a fake asyncio (instant sleeps), a fake clock and a fake SolisCloud.
"""
import asyncio
import json
import logging
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

APP_PATH = Path(__file__).resolve().parents[1] / "pyscript" / "apps" / "solis_ev_follow.py"
TZ = ZoneInfo("Europe/London")

SWITCH = "switch.hypervolt_charging"
CURRENT = "sensor.hypervolt_charger_current"
DISPATCH = "binary_sensor.octopus_energy_test_intelligent_dispatching"

BASE_CONFIG = {
    "key_id": "test-key",
    "secret": "test-secret",
    "username": "user@example.com",
    "password": "hunter2",
    "plantId": "1234",
    "dispatch_sensor": DISPATCH,
    "inverter_timezone": "Europe/London",
    "inter_write_delay": 0,
}


# Keep the app's log output out of the test run (assertLogs still works).
_app_log = logging.getLogger("pyscript.solis_ev_follow")
_app_log.addHandler(logging.NullHandler())
_app_log.propagate = False


def _install_homeassistant_stub():
    if "homeassistant.helpers.aiohttp_client" in sys.modules:
        return
    ha = types.ModuleType("homeassistant")
    helpers = types.ModuleType("homeassistant.helpers")
    client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    client.async_get_clientsession = lambda hass: None
    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.aiohttp_client"] = client


class FakeState:
    def __init__(self, value):
        self.state = value


class FakeAsyncio:
    """Stands in for the asyncio module inside the app: sleeps are instant and recorded."""

    def __init__(self):
        self.sleeps = []
        self.on_sleep = None

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.on_sleep:
            self.on_sleep(seconds)


class _AppConfigMissing:
    """pyscript object for a file loaded outside apps/: app_config isn't set."""

    def __init__(self, config):
        self.config = config

    @property
    def app_config(self):
        raise AttributeError("app_config")


# -----------------------------
# Fake SolisCloud
# -----------------------------
OK_CONTROL = {"code": "0", "data": [{"code": "0"}]}

# Duplicated from the app on purpose, so a wrong CID in the app fails the tests.
CHARGE_TIME_CIDS = ["5946", "5949", "5952", "5955", "5958", "5961"]
CHARGE_SWITCH_CIDS = ["5916", "5917", "5918", "5919", "5920", "5921"]
DISCHARGE_SWITCH_CIDS = ["5922", "5923", "5924", "5925", "5926", "5927"]
SLOT_SWITCH_CIDS = CHARGE_SWITCH_CIDS + DISCHARGE_SWITCH_CIDS


def storage_inverter(inv_id="111", sn="SN111", name="Solis"):
    return {"id": inv_id, "sn": sn, "name": name, "productModel": "2"}


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status = status
        self._text = body if isinstance(body, str) else json.dumps(body if body is not None else {})

    async def text(self):
        return self._text


class FakeSolisCloud:
    """
    Records every POST and answers like SolisCloud. Override behaviour by
    assigning a list of FakeResponses to `queued[path]` (consumed in order,
    falling back to the defaults when empty).
    """

    def __init__(self, records=None):
        self.calls = []
        self.records = records if records is not None else [storage_inverter()]
        self.queued = {}
        # Inverter-side values: the 12 slot switches start off.
        self.values = {cid: "0" for cid in SLOT_SWITCH_CIDS}

    async def post(self, url, data=None, headers=None):
        path = url.split(":13333", 1)[1]
        body = json.loads(data)
        self.calls.append({"path": path, "body": body, "headers": headers or {}})
        if self.queued.get(path):
            return self.queued[path].pop(0)
        if path == "/v2/api/login":
            return FakeResponse(200, {"csrfToken": "tok-123"})
        if path == "/v1/api/inverterList":
            return FakeResponse(200, {"data": {"page": {"records": self.records}}})
        if path == "/v2/api/control":
            self.values[str(body["cid"])] = str(body["value"])
            return FakeResponse(200, OK_CONTROL)
        if path == "/v2/api/atRead":
            return FakeResponse(200, {"code": "0", "data": {"msg": self.values.get(str(body["cid"]), "")}})
        if path == "/v2/api/atReadBatch":
            items = [{"cid": int(cid), "msg": self.values.get(cid, "")} for cid in body["cids"].split(",")]
            return FakeResponse(200, {"code": "0", "data": [items]})
        return FakeResponse(404, {})

    def paths(self):
        return [c["path"] for c in self.calls]

    def control_writes(self):
        """(cid, value) for every control write in order, excluding the time sync."""
        out = []
        for c in self.calls:
            if c["path"] == "/v2/api/control" and c["body"].get("cid") != "56":
                out.append((c["body"]["cid"], c["body"]["value"]))
        return out

    def time_writes(self):
        """(cid, value) for charge slot time writes only."""
        return [w for w in self.control_writes() if w[0] in CHARGE_TIME_CIDS]

    def switch_writes(self):
        """(cid, value, yuanzhi) for slot switch writes."""
        out = []
        for c in self.calls:
            if c["path"] == "/v2/api/control" and c["body"]["cid"] in SLOT_SWITCH_CIDS:
                out.append((c["body"]["cid"], c["body"]["value"], c["body"].get("yuanzhi")))
        return out

    def time_syncs(self):
        return [c for c in self.calls if c["path"] == "/v2/api/control" and c["body"].get("cid") == "56"]


# -----------------------------
# The loaded app
# -----------------------------
class App:
    def __init__(self, config=None, as_app=True, time_zone="Europe/London"):
        _install_homeassistant_stub()
        cfg = dict(BASE_CONFIG)
        cfg.update(config or {})
        for key in [k for k, v in cfg.items() if v is None]:
            del cfg[key]

        self.states = {}
        self.state_sets = []
        self.triggers = []
        self.services = []
        self.unique_calls = []
        self.asyncio = FakeAsyncio()
        self.cloud = FakeSolisCloud()
        self.writes = None  # set by capture_writes()

        if as_app:
            pyscript_obj = types.SimpleNamespace(app_config=cfg, config={})
        else:
            pyscript_obj = _AppConfigMissing({"apps": {"solis_ev_follow": cfg}})

        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone=time_zone),
            states=types.SimpleNamespace(get=self._get_state),
        )
        g = {
            "__name__": "solis_ev_follow",
            "pyscript": pyscript_obj,
            "hass": hass,
            "state": types.SimpleNamespace(set=self._set_state),
            "task": types.SimpleNamespace(unique=self._unique),
            "service": self._service,
            "time_trigger": self._trigger("time_trigger"),
            "state_trigger": self._trigger("state_trigger"),
        }
        source = APP_PATH.read_text()
        exec(compile(source, str(APP_PATH), "exec"), g)

        g["asyncio"] = self.asyncio
        g["async_get_clientsession"] = lambda _hass: self.cloud
        self.g = g
        self.at("12:00")

    # --- pyscript / HA fakes ---
    def _get_state(self, entity_id):
        if entity_id in self.states:
            return FakeState(self.states[entity_id])
        return None

    def _set_state(self, entity_id, value, new_attributes=None):
        self.state_sets.append((entity_id, value, new_attributes or {}))

    def _unique(self, name, kill_me=False):
        self.unique_calls.append((name, kill_me))

    def _service(self, func):
        self.services.append(func.__name__)
        return func

    def _trigger(self, kind):
        def decorator_factory(*args, **kwargs):
            def decorator(func):
                self.triggers.append((kind, args, func.__name__))
                return func
            return decorator
        return decorator_factory

    # --- helpers for tests ---
    def __getattr__(self, name):
        g = self.__dict__.get("g")
        if g is not None and name in g:
            return g[name]
        raise AttributeError(name)

    def set_charger(self, switch="on", amps="16", dispatch="on"):
        for entity, value in ((SWITCH, switch), (CURRENT, amps), (DISPATCH, dispatch)):
            if value is None:
                self.states.pop(entity, None)
            else:
                self.states[entity] = value

    def at(self, hhmm, day=6, second=0):
        """Freeze the app's clock at 2026-10-<day> HH:MM (Europe/London)."""
        hour, minute = (int(x) for x in hhmm.split(":"))
        now = datetime(2026, 10, day, hour, minute, second, tzinfo=TZ)
        self.g["now_local"] = lambda: now
        return now

    def capture_writes(self, ok=True):
        """Replace the SolisCloud layer with a recorder. `ok` may be a bool or a list of bools."""
        self.writes = []
        results = list(ok) if isinstance(ok, (list, tuple)) else None

        async def fake_write(slot_values):
            self.writes.append(dict(slot_values))
            if results is not None:
                return results.pop(0) if results else True
            return ok

        self.g["solis_write_slots"] = fake_write
        return self.writes

    def mark_known(self):
        """Pretend the startup init already ran: the inverter's EV slot is known to be empty."""
        self.g["_slot"]["unknown"] = False

    def run(self, coro):
        return asyncio.run(coro)

    def reconcile(self, reason="test"):
        return self.run(self.g["reconcile"](reason))

    def slot(self):
        s = self.g["_slot"]
        return self.g["fmt_slot"](s["start"], s["end"])

    def last_status(self):
        sets = [s for s in self.state_sets if s[0] == "sensor.solis_ev_charge_slot"]
        return sets[-1] if sets else None


def local(hhmm, day=6):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def hours_ago(n):
    from datetime import timezone
    return datetime.now(timezone.utc) - timedelta(hours=n)
