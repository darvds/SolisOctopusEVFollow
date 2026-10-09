"""
Solis EV-Follow Charging (pyscript app)

Charges the Solis battery from the grid only while the car is actually being
smart-charged by Octopus Intelligent, instead of pre-programming the planned
dispatch windows (which don't go cheap if the car never starts charging).

How it works
------------
- Slot 1 holds the fixed Octopus core window (23:30-05:30 by default).
- One "EV slot" (slot 2 by default) is driven as a rolling half-hour window:
    * When the Hypervolt is drawing current AND the Octopus dispatch has
      *started*, the EV slot is set to [start of this half-hour, next :00/:30].
    * A few minutes before each :00/:30, if the car is still charging, the end
      is pushed out by another 30 minutes.
    * When the car stops, nothing is extended, so the battery keeps charging
      until the end of the current half-hour (Octopus bills the whole
      half-hour at the cheap rate) and then stops on its own.
    * Once the slot has expired, it is reset to 00:00-00:00.
- Slot enable switches are kept in step: a slot is switched on whenever a
  real window is written to it, and switched off when it is cleared.
- Fail-safe: if HA / pyscript / SolisCloud dies, the battery charges for at
  most the remainder of the current half-hour.

Requires the Octopus Energy integration's intelligent dispatching sensor to be
set to "started dispatches only", and 6-slot Solis firmware (HMI >= 4B00).
"""
import json
import re
import asyncio
import logging
import hashlib
import hmac
import base64
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from zoneinfo import ZoneInfo
from homeassistant.helpers.aiohttp_client import async_get_clientsession

log = logging.getLogger("pyscript.solis_ev_follow")
log.setLevel(logging.DEBUG)

VERSION = "1.1.0"

# -----------------------------
# SolisCloud endpoints / constants
# -----------------------------
BASE_URL = "https://www.soliscloud.com:13333"
VERB = "POST"
LOGIN_URL = "/v2/api/login"
CONTROL_URL = "/v2/api/control"
AT_READ_URL = "/v2/api/atRead"
AT_READ_BATCH_URL = "/v2/api/atReadBatch"
INVERTER_LIST_URL = "/v1/api/inverterList"

# 6-slot firmware (HMI >= 4B00) charge time CIDs, slot 1..6
CHARGE_TIME_CIDS = ["5946", "5949", "5952", "5955", "5958", "5961"]

# Per-slot on/off switches ("1"/"0"). Writes must include "yuanzhi": a bitmask
# of all 12 switch states before the change, bit = index in SLOT_SWITCH_CIDS
# (charge slots 1-6 -> bits 0-5, discharge slots 1-6 -> bits 6-11).
CHARGE_SWITCH_CIDS = ["5916", "5917", "5918", "5919", "5920", "5921"]
DISCHARGE_SWITCH_CIDS = ["5922", "5923", "5924", "5925", "5926", "5927"]
SLOT_SWITCH_CIDS = CHARGE_SWITCH_CIDS + DISCHARGE_SWITCH_CIDS
EMPTY_SLOT = "00:00-00:00"

STATUS_SENSOR = "sensor.solis_ev_charge_slot"

# -----------------------------
# Configuration (from configuration.yaml -> pyscript: apps: solis_ev_follow:)
# -----------------------------
try:
    # Set when this file lives in <config>/pyscript/apps/
    CFG = pyscript.app_config or {}
except Exception:
    # Fallback if the file was placed directly in <config>/pyscript/
    CFG = (pyscript.config.get("apps") or {}).get("solis_ev_follow") or {}
    log.warning("Not loaded as a pyscript app; move this file to <config>/pyscript/apps/")


def _cfg_bool(key, default):
    return str(CFG.get(key, default)).lower() in ("true", "1", "yes", "on")


CHARGER_SWITCH = str(CFG.get("charger_switch", "switch.hypervolt_charging"))
CHARGER_CURRENT = str(CFG.get("charger_current", "sensor.hypervolt_charger_current"))
DISPATCH_SENSOR = str(CFG.get("dispatch_sensor", ""))
MIN_CURRENT_AMPS = float(CFG.get("min_current_amps", 3))
DEBOUNCE_SECONDS = int(CFG.get("debounce_seconds", 60))
EXTEND_LEAD_MINUTES = int(CFG.get("extend_lead_minutes", 3))
STARTUP_DELAY_SECONDS = int(CFG.get("startup_delay_seconds", 60))

EV_SLOT = int(CFG.get("ev_slot", 2))
MANAGE_CORE_SLOT = _cfg_bool("manage_core_slot", True)
CORE_WINDOW = str(CFG.get("core_window", "23:30-05:30"))
CLEAR_OTHER_SLOTS = _cfg_bool("clear_other_slots", True)
MANAGE_SLOT_SWITCHES = _cfg_bool("manage_slot_switches", True)

INVERTER_TIMEZONE = str(CFG.get("inverter_timezone", hass.config.time_zone or "UTC"))
SYNC_INVERTER_TIME = _cfg_bool("sync_inverter_time", True)

DIAGNOSTICS_ONLY = _cfg_bool("diagnostics_only", False)
CONTROL_RETRIES = int(CFG.get("control_retries", 3))
CONTROL_DELAY = float(CFG.get("control_delay", 0.5))
INTER_WRITE_DELAY = float(CFG.get("inter_write_delay", 0.25))
VERIFY_READBACK = _cfg_bool("verify_readback", False)

try:
    INV_TZ = ZoneInfo(INVERTER_TIMEZONE)
except Exception as e:
    log.warning("Invalid inverter_timezone '%s' (%s), falling back to UTC", INVERTER_TIMEZONE, e)
    INV_TZ = timezone.utc

# -----------------------------
# Runtime state (dicts so functions can mutate without `global`)
# -----------------------------
# Programmed EV slot as inverter-local datetimes. "unknown" means we don't
# know what's on the inverter (e.g. after a restart) and must write it.
_slot = {"start": None, "end": None, "unknown": True}
_cache = {"inverter_id": None, "inverter_sn": None, "last_time_sync": None}
_flags = {"busy": False, "last_charging": None, "last_reason": None, "last_write_ok": None}


# -----------------------------
# SolisCloud auth helpers (unchanged from solis_smart_charging)
# -----------------------------
def digest(body: str) -> str:
    return base64.b64encode(hashlib.md5(body.encode("utf-8")).digest()).decode("utf-8")


def passwordEncode(password: str) -> str:
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def prepare_header(config, body: str, canonicalized_resource: str):
    content_md5 = digest(body)
    content_type = "application/json"
    now = datetime.now(timezone.utc)
    date = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
    encrypt_str = (
        VERB + "\n" + content_md5 + "\n" + content_type + "\n" + date + "\n" + canonicalized_resource
    )
    hmac_obj = hmac.new(
        config["secret"].encode("utf-8"),
        msg=encrypt_str.encode("utf-8"),
        digestmod=hashlib.sha1,
    )
    sign = base64.b64encode(hmac_obj.digest())
    authorization = "API " + str(config["key_id"]) + ":" + sign.decode("utf-8")
    return {
        "Content-MD5": content_md5,
        "Content-Type": content_type,
        "Date": date,
        "Authorization": authorization,
    }


def _clean_json_text(text: str) -> str:
    # SolisCloud sometimes returns JSON with trailing commas.
    return re.sub(r'("(?:\\?.)*?")|,\s*([}\]])', r"\1\2", text)


# -----------------------------
# SolisCloud I/O helpers
# -----------------------------
async def solis_post(session, url_path, body_dict, token=None):
    body = json.dumps(body_dict, separators=(",", ":"))
    headers = prepare_header(CFG, body, url_path)
    if token:
        headers["token"] = token
    return await session.post(BASE_URL + url_path, data=body, headers=headers)


async def resp_json(resp):
    text = await resp.text()
    return json.loads(_clean_json_text(text))


async def solis_login(session):
    body = {"userInfo": str(CFG["username"]), "password": passwordEncode(str(CFG["password"]))}
    resp = await solis_post(session, LOGIN_URL, body)
    if resp.status != HTTPStatus.OK:
        log.error("Login failed with status %s", resp.status)
        return None
    data = await resp_json(resp)
    token = data.get("csrfToken")
    if not token:
        log.error("Login succeeded but csrfToken missing: %s", data)
    return token


async def get_inverter(session):
    """Return (inverter_id, inverter_sn), cached after the first lookup."""
    if _cache["inverter_id"] and _cache["inverter_sn"]:
        return _cache["inverter_id"], _cache["inverter_sn"]

    resp = await solis_post(session, INVERTER_LIST_URL, {"stationId": str(CFG["plantId"])})
    if resp.status != HTTPStatus.OK:
        log.error("inverterList failed status %s", resp.status)
        return None, None
    data = await resp_json(resp)
    records = (data.get("data") or {}).get("page", {}).get("records", []) or []
    if not records:
        log.error("No inverters returned from inverterList")
        return None, None

    cfg_sn = str(CFG.get("inverter_sn", "")).strip()
    cfg_id = str(CFG.get("inverter_id", "")).strip()
    if cfg_sn.lower() in ("unknown", "unavailable", "none"):
        cfg_sn = ""
    if cfg_id.lower() in ("unknown", "unavailable", "none"):
        cfg_id = ""

    chosen = None
    if cfg_sn or cfg_id:
        for r in records:
            if (cfg_sn and str(r.get("sn")) == cfg_sn) or (cfg_id and str(r.get("id")) == cfg_id):
                chosen = r
                break
    else:
        storage_records = []
        for r in records:
            if str(r.get("productModel")) == "2":
                storage_records.append(r)
        if len(storage_records) == 1:
            chosen = storage_records[0]
        elif len(storage_records) == 0 and len(records) == 1:
            chosen = records[0]

    if not chosen:
        log.error("Could not pick an inverter; set inverter_sn or inverter_id. Available:")
        for r in records:
            log.error("  - ID: %s, SN: %s, Name: %s, ProductModel: %s",
                      r.get("id"), r.get("sn"), r.get("name"), r.get("productModel"))
        return None, None

    _cache["inverter_id"] = chosen.get("id")
    _cache["inverter_sn"] = chosen.get("sn")
    log.info("Using inverter ID=%s SN=%s Name=%s",
             _cache["inverter_id"], _cache["inverter_sn"], chosen.get("name"))
    return _cache["inverter_id"], _cache["inverter_sn"]


async def get_control_value(session, token, inverter_sn, cid):
    """Read one CID. Returns its value as a string, or None."""
    for attempt in range(1, CONTROL_RETRIES + 1):
        r = await solis_post(session, AT_READ_URL,
                             {"inverterSn": str(inverter_sn), "cid": str(cid)}, token=token)
        if r.status != HTTPStatus.OK:
            await asyncio.sleep(0.2)
            continue
        try:
            data = await resp_json(r)
            payload = data.get("data")
            if isinstance(payload, list) and payload:
                payload = payload[0]
            if str(data.get("code")) == "0" and isinstance(payload, dict) and "msg" in payload:
                return str(payload["msg"])
        except Exception as e:
            log.warning("AT_READ cid=%s attempt %s parse error: %s", cid, attempt, e)
        await asyncio.sleep(0.2)
    return None


async def write_control(session, token, inverter_sn, cid, value, old_value=None):
    body = {"inverterSn": str(inverter_sn), "cid": str(cid), "value": str(value)}
    if old_value is not None:
        body["yuanzhi"] = str(old_value)
    last_text = None
    for attempt in range(1, CONTROL_RETRIES + 1):
        r = await solis_post(session, CONTROL_URL, body, token=token)
        try:
            last_text = await r.text()
        except Exception:
            last_text = None

        if r.status != HTTPStatus.OK:
            log.warning("CONTROL cid=%s attempt %s/%s http=%s", cid, attempt, CONTROL_RETRIES, r.status)
            await asyncio.sleep(0.3)
            continue
        try:
            data = json.loads(_clean_json_text(last_text or ""))
            payload = data.get("data") or []
            if str(data.get("code")) == "0" and payload and str(payload[0].get("code")) == "0":
                if VERIFY_READBACK:
                    await asyncio.sleep(CONTROL_DELAY)
                    rb = await get_control_value(session, token, inverter_sn, cid)
                    if rb is not None and rb != str(value):
                        log.warning("Readback cid=%s: wrote '%s' but read '%s'", cid, value, rb)
                    else:
                        log.debug("Readback cid=%s: %s", cid, rb)
                return True
        except Exception as e:
            log.warning("CONTROL cid=%s attempt %s/%s parse error: %s", cid, attempt, CONTROL_RETRIES, e)
        await asyncio.sleep(0.3)

    log.error("CONTROL cid=%s failed after %s attempts. Last response: %s", cid, CONTROL_RETRIES, last_text)
    return False


async def read_slot_switches(session, token, inverter_sn):
    """Return {cid: "0"/"1"} for the 12 slot switches; unreadable ones are left out."""
    states = {}
    try:
        r = await solis_post(session, AT_READ_BATCH_URL,
                             {"inverterSn": str(inverter_sn), "cids": ",".join(SLOT_SWITCH_CIDS)},
                             token=token)
        if r.status == HTTPStatus.OK:
            data = await resp_json(r)
            if str(data.get("code")) == "0":
                for outer in data.get("data") or []:
                    items = outer if isinstance(outer, list) else [outer]
                    for item in items:
                        if isinstance(item, dict) and "cid" in item and "msg" in item:
                            states[str(item["cid"])] = str(item["msg"])
        else:
            log.warning("atReadBatch for slot switches failed http=%s", r.status)
    except Exception as e:
        log.warning("atReadBatch for slot switches failed: %s", e)

    for cid in SLOT_SWITCH_CIDS:
        if cid not in states:
            value = await get_control_value(session, token, inverter_sn, cid)
            if value is not None:
                states[cid] = value
    log.debug("Slot switch states: %s", states)
    return states


def slot_switch_mask(states):
    """The "yuanzhi" bitmask for the current switch states, or None if any is unknown."""
    mask = 0
    for bit, cid in enumerate(SLOT_SWITCH_CIDS):
        if cid not in states:
            return None
        if states[cid] == "1":
            mask |= 1 << bit
    return str(mask)


async def set_charge_slot_switch(session, token, inverter_sn, states, slot_num, enabled):
    """Switch a charge slot on/off if it isn't already. Updates `states` on success."""
    cid = CHARGE_SWITCH_CIDS[slot_num - 1]
    want = "1" if enabled else "0"
    if states.get(cid) == want:
        return True
    old_value = slot_switch_mask(states)
    if old_value is None:
        log.warning("Slot switch states unknown; writing CID %s without yuanzhi", cid)
    log.info("%s charge slot %s (CID %s)", "Enabling" if enabled else "Disabling", slot_num, cid)
    ok = await write_control(session, token, inverter_sn, cid, want, old_value=old_value)
    if ok:
        states[cid] = want
    return ok


async def sync_inverter_time(session, token, inverter_id):
    value = datetime.now(INV_TZ).strftime("%Y-%m-%d %H:%M:%S")
    body = f'{{"inverterId":"{inverter_id}","cid":"56","value":"{value}"}}'
    headers = prepare_header(CFG, body, CONTROL_URL)
    headers["token"] = token
    try:
        resp = await session.post(BASE_URL + CONTROL_URL, data=body, headers=headers)
        if resp.status == HTTPStatus.OK:
            data = await resp_json(resp)
            if str(data.get("code")) == "0":
                log.info("Synced inverter time to %s (%s)", value, INVERTER_TIMEZONE)
                _cache["last_time_sync"] = datetime.now(timezone.utc)
                return
            log.warning("Time sync returned code=%s msg=%s", data.get("code"), data.get("msg"))
        else:
            log.warning("Time sync HTTP status: %s", resp.status)
    except Exception as e:
        log.error("Time sync failed: %s", e)


async def solis_write_slots(slot_values):
    """Write {slot_number: "HH:MM-HH:MM"} to the inverter. Returns True if all succeeded."""
    if DIAGNOSTICS_ONLY:
        log.warning("DIAGNOSTICS MODE: would write %s", slot_values)
        return True

    session = async_get_clientsession(hass)
    token = await solis_login(session)
    if not token:
        return False
    inverter_id, inverter_sn = await get_inverter(session)
    if not inverter_sn:
        return False

    if SYNC_INVERTER_TIME:
        last = _cache["last_time_sync"]
        if last is None or datetime.now(timezone.utc) - last > timedelta(hours=24):
            await sync_inverter_time(session, token, inverter_id)

    switches = None
    if MANAGE_SLOT_SWITCHES:
        switches = await read_slot_switches(session, token, inverter_sn)

    ok = True
    for slot_num in sorted(slot_values.keys()):
        value = slot_values[slot_num]
        enable = value != EMPTY_SLOT

        # Clearing: switch off first so the slot is never enabled as 00:00-00:00.
        if switches is not None and not enable:
            if not await set_charge_slot_switch(session, token, inverter_sn, switches, slot_num, False):
                ok = False

        cid = CHARGE_TIME_CIDS[slot_num - 1]
        log.info("Writing charge slot %s (CID %s) = %s", slot_num, cid, value)
        time_ok = await write_control(session, token, inverter_sn, cid, value)
        if not time_ok:
            ok = False

        # Real window: switch on only once the new time is in place.
        if switches is not None and enable and time_ok:
            if not await set_charge_slot_switch(session, token, inverter_sn, switches, slot_num, True):
                ok = False
        await asyncio.sleep(INTER_WRITE_DELAY)
    return ok


# -----------------------------
# Time / decision logic
# -----------------------------
def _parse_window(text):
    start_s, end_s = text.split("-")
    return (datetime.strptime(start_s.strip(), "%H:%M").time(),
            datetime.strptime(end_s.strip(), "%H:%M").time())


CORE_START, CORE_END = _parse_window(CORE_WINDOW)


def now_local():
    return datetime.now(INV_TZ)


def floor_half_hour(dt):
    return dt.replace(minute=(dt.minute // 30) * 30, second=0, microsecond=0)


def next_half_hour(dt):
    """The next :00/:30 strictly after dt."""
    return floor_half_hour(dt) + timedelta(minutes=30)


def in_core_window(dt):
    if not MANAGE_CORE_SLOT:
        return False
    t = dt.time()
    if CORE_START <= CORE_END:
        return CORE_START <= t < CORE_END
    return t >= CORE_START or t < CORE_END


def fmt_slot(start, end):
    if start is None or end is None:
        return EMPTY_SLOT
    return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"


def _state(entity_id):
    s = hass.states.get(entity_id)
    return s.state if s else None


def car_charging():
    """True when the Hypervolt is drawing current during a *started* Octopus dispatch."""
    switch_state = _state(CHARGER_SWITCH)
    dispatch_state = _state(DISPATCH_SENSOR)
    current_raw = _state(CHARGER_CURRENT)
    try:
        amps = float(current_raw)
    except (TypeError, ValueError):
        amps = 0.0
    charging = switch_state == "on" and dispatch_state == "on" and amps >= MIN_CURRENT_AMPS
    info = {"charger_switch": switch_state, "charger_amps": amps, "dispatch": dispatch_state}
    return charging, info


def desired_slot(now, charging):
    """Return the (start, end) the EV slot should hold right now, or (None, None)."""
    active = _slot["end"] is not None and now < _slot["end"]
    lookahead = now + timedelta(minutes=EXTEND_LEAD_MINUTES)

    if charging and not in_core_window(lookahead):
        # End at the next boundary after now+lead, so a few minutes before
        # :00/:30 this rolls on by another half-hour.
        end = next_half_hour(lookahead)
        start = _slot["start"] if active else floor_half_hour(now)
        if active and _slot["end"] > end:
            end = _slot["end"]
        return start, end

    if active:
        # Car stopped (or core window reached): let the current half-hour run out.
        return _slot["start"], _slot["end"]
    return None, None


# -----------------------------
# Reconcile: make the inverter's EV slot match what it should be
# -----------------------------
async def reconcile(reason):
    if not DISPATCH_SENSOR:
        log.error("dispatch_sensor not configured - doing nothing")
        return

    # Serialise: pyscript runs on the event loop, so check-and-set is atomic.
    while _flags["busy"]:
        await asyncio.sleep(1)
    _flags["busy"] = True
    try:
        now = now_local()
        charging, info = car_charging()
        start, end = desired_slot(now, charging)

        unchanged = (start == _slot["start"] and end == _slot["end"])
        if unchanged and not _slot["unknown"]:
            log.debug("[%s] no change (charging=%s, slot=%s)", reason, charging, fmt_slot(start, end))
        else:
            value = fmt_slot(start, end)
            log.info("[%s] charging=%s %s -> EV slot %s = %s", reason, charging, info, EV_SLOT, value)
            ok = await solis_write_slots({EV_SLOT: value})
            _flags["last_write_ok"] = ok
            if ok:
                _slot["start"] = start
                _slot["end"] = end
                _slot["unknown"] = False
            else:
                log.error("Failed to write EV slot; will retry on next check")

        _flags["last_reason"] = reason
        publish_status(charging, info)
    finally:
        _flags["busy"] = False


def publish_status(charging, info):
    programmed = fmt_slot(_slot["start"], _slot["end"])
    state.set(
        STATUS_SENSOR,
        "charging" if _slot["end"] is not None else "idle",
        new_attributes={
            "ev_slot": EV_SLOT,
            "programmed_slot": programmed,
            "slot_state_unknown": _slot["unknown"],
            "ev_charging": charging,
            "charger_switch": info.get("charger_switch"),
            "charger_amps": info.get("charger_amps"),
            "dispatch_state": info.get("dispatch"),
            "core_window": CORE_WINDOW if MANAGE_CORE_SLOT else None,
            "last_reason": _flags["last_reason"],
            "last_write_ok": _flags["last_write_ok"],
            "diagnostics_only": DIAGNOSTICS_ONLY,
            "inverter_timezone": INVERTER_TIMEZONE,
            "version": VERSION,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "friendly_name": "Solis EV charge slot",
            "icon": "mdi:home-battery",
        },
    )


async def init_schedule():
    """Put the inverter into a known state: core window in slot 1, everything else empty."""
    slots = {EV_SLOT: EMPTY_SLOT}
    if MANAGE_CORE_SLOT:
        slots[1] = CORE_WINDOW.replace(" ", "")
    if CLEAR_OTHER_SLOTS:
        for n in range(1, 7):
            if n not in slots:
                slots[n] = EMPTY_SLOT

    while _flags["busy"]:
        await asyncio.sleep(1)
    _flags["busy"] = True
    try:
        log.info("Initialising charge schedule: %s", slots)
        ok = await solis_write_slots(slots)
        _flags["last_write_ok"] = ok
        if ok:
            _slot["start"] = None
            _slot["end"] = None
            _slot["unknown"] = False
        else:
            log.error("Schedule initialisation had failures; EV slot will be rewritten on next check")
    finally:
        _flags["busy"] = False


# -----------------------------
# Triggers
# -----------------------------
@time_trigger("startup")
async def solis_ev_follow_startup():
    log.info("=== Solis EV-Follow v%s starting (EV slot %s, tz %s, diagnostics_only=%s) ===",
             VERSION, EV_SLOT, INVERTER_TIMEZONE, DIAGNOSTICS_ONLY)
    if MANAGE_CORE_SLOT and EV_SLOT == 1:
        log.error("ev_slot cannot be 1 while manage_core_slot is enabled (slot 1 holds the core window)")
        return
    if not DISPATCH_SENSOR or hass.states.get(DISPATCH_SENSOR) is None:
        log.warning("Dispatch sensor '%s' not found yet - check dispatch_sensor in config", DISPATCH_SENSOR)
    # Let the Octopus / Hypervolt integrations finish loading.
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    await init_schedule()
    charging, _ = car_charging()
    _flags["last_charging"] = charging
    await reconcile("startup")


@state_trigger(CHARGER_SWITCH, CHARGER_CURRENT, DISPATCH_SENSOR)
async def solis_ev_follow_state_change(var_name=None, value=None, old_value=None):
    charging, _ = car_charging()
    if charging == _flags["last_charging"]:
        return
    _flags["last_charging"] = charging

    if not charging:
        log.info("Car stopped charging (%s=%s); battery will finish the current half-hour", var_name, value)
        return

    # Debounce: only one pending start at a time; re-check after the wait.
    task.unique("solis_ev_follow_debounce", kill_me=True)
    log.info("Car charging detected (%s=%s); confirming in %ss", var_name, value, DEBOUNCE_SECONDS)
    await asyncio.sleep(DEBOUNCE_SECONDS)
    still_charging, _ = car_charging()
    if still_charging:
        await reconcile("charging started")
    else:
        log.info("Charging did not persist for %ss - ignoring", DEBOUNCE_SECONDS)


@time_trigger(
    "cron(*/5 * * * *)",
    f"cron({30 - EXTEND_LEAD_MINUTES},{60 - EXTEND_LEAD_MINUTES} * * * *)",
)
async def solis_ev_follow_periodic():
    # Extends the slot just before each :00/:30, clears expired slots, and
    # recovers from missed state changes.
    await reconcile("periodic")


# -----------------------------
# Services (for testing from Developer Tools -> Actions)
# -----------------------------
@service
async def solis_ev_follow_reconcile():
    """Re-evaluate and update the Solis EV charge slot now."""
    await reconcile("manual")


@service
async def solis_ev_follow_init():
    """Rewrite the full schedule (core window in slot 1, other slots cleared)."""
    await init_schedule()
    await reconcile("manual init")
