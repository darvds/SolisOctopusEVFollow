# Solis EV-Follow Charging

A [pyscript](https://github.com/custom-components/pyscript) app for Home Assistant that charges a **Solis** home battery from the grid **only while an EV is actually being smart-charged** on **Octopus Intelligent**.

## Why

The usual approach is to program the battery's charge slots from Octopus' *planned* dispatches. But if the car doesn't actually start charging in a planned slot, Octopus doesn't apply the cheap rate, and the battery charges at peak price.

This app waits for the car to draw power during a dispatch that has really **started**, then charges the battery for exactly those half-hours.

## How it works

- **Slot 1** holds the fixed Octopus core window (default `23:30-05:30`).
- **Slot 2** (configurable) follows the car as a rolling half-hour window:
  - When the charger is drawing current **and** the Octopus dispatch has started (held for `debounce_seconds`), the slot is set to `[start of this half-hour, next :00/:30]`.
  - A few minutes before each `:00`/`:30`, if the car is still charging, the end moves out by another 30 minutes.
  - When the car stops, the slot isn't extended. The battery finishes the current half-hour, which Octopus bills at the cheap rate as a whole, and then stops.
  - Once expired, the slot is reset to `00:00-00:00`.
- A check every 5 minutes clears expired slots and catches any missed state changes.
- **Fail-safe:** if Home Assistant, pyscript or SolisCloud goes down, the battery charges for at most the remainder of the current half-hour.

| Time  | Car      | Slot 2                                  |
|-------|----------|-----------------------------------------|
| 14:12 | charging | `14:00-14:30`                           |
| 14:27 | charging | `14:00-15:00`                           |
| 14:57 | charging | `14:00-15:30`                           |
| 15:10 | stopped  | unchanged, finishes the half-hour       |
| 15:30 | stopped  | `00:00-00:00`                           |

## Requirements

- Home Assistant with [pyscript](https://github.com/custom-components/pyscript) (HACS)
- [Octopus Energy integration](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy), with the intelligent dispatching sensor set to **"started dispatches only"**
- A charger integration exposing an on/off switch and a charging-current sensor. Tested with [Hypervolt](https://github.com/gndean/home-assistant-hypervolt-charger).
- A Solis hybrid inverter on **6-slot firmware** (HMI ≥ `4B00`) with SolisCloud API access (key ID and secret)

## Installation

1. Copy `pyscript/apps/solis_ev_follow.py` to `<config>/pyscript/apps/solis_ev_follow.py`.
   It **must** be in the `apps/` folder, and the app name in the config must match the filename.
2. Merge the block from [`configuration.example.yaml`](configuration.example.yaml) into `configuration.yaml`. Keep a single `pyscript:` block.
3. Add your SolisCloud credentials to `secrets.yaml`:
   ```yaml
   solis_key_id: "..."
   solis_secret: "..."
   solis_username: "..."
   solis_password: "..."
   solis_plant_id: "..."
   ```
4. Disable any other automation that writes Solis charge slots.
5. Restart Home Assistant.

Start with `diagnostics_only: true`. The app then logs what it *would* write without touching the inverter. Once it looks right during a real dispatch, set it to `false` and restart.

## Configuration

| Option | Default | Description |
|---|---|---|
| `key_id`, `secret`, `username`, `password`, `plantId` | **required** | SolisCloud API credentials |
| `inverter_sn` / `inverter_id` | auto | Only needed if the plant has more than one storage inverter |
| `dispatch_sensor` | **required** | `binary_sensor.octopus_energy_<id>_intelligent_dispatching` |
| `charger_switch` | `switch.hypervolt_charging` | Charger on/off switch |
| `charger_current` | `sensor.hypervolt_charger_current` | Charger current sensor (A) |
| `min_current_amps` | `3` | Minimum current to count as charging |
| `debounce_seconds` | `60` | How long charging must persist before the battery starts |
| `extend_lead_minutes` | `3` | How early before `:00`/`:30` to extend the slot |
| `ev_slot` | `2` | Solis charge slot driven by this app (2–6) |
| `manage_core_slot` | `true` | Write `core_window` into slot 1 |
| `core_window` | `23:30-05:30` | Octopus core off-peak window |
| `clear_other_slots` | `true` | Clear unused slots on startup |
| `inverter_timezone` | HA time zone | Time zone the inverter clock and slots use |
| `sync_inverter_time` | `true` | Sync the inverter clock (at most once per 24h) |
| `diagnostics_only` | `false` | Log instead of writing to the inverter |
| `control_retries` | `3` | Retries per SolisCloud write |
| `verify_readback` | `false` | Read each value back after writing |
| `startup_delay_seconds` | `60` | Wait for other integrations after startup |

## Monitoring

- **`sensor.solis_ev_charge_slot`** (Developer Tools → States) shows the programmed slot, whether the car counts as charging, charger amps, dispatch state, the last reason and whether the last write worked.
- **Actions** (Developer Tools → Actions):
  - `pyscript.solis_ev_follow_reconcile`: re-evaluate now.
  - `pyscript.solis_ev_follow_init`: rewrite the whole schedule (core window and cleared slots).
- **Debug logging:**
  ```yaml
  logger:
    logs:
      custom_components.pyscript: debug
      pyscript.solis_ev_follow: debug
  ```

## Troubleshooting

- **No sensor and nothing in the logs.** The file isn't loaded as an app. Check it's in `pyscript/apps/` and that `pyscript: apps: solis_ev_follow:` exists in `configuration.yaml`, then restart fully.
- **Slots are at the wrong times.** Check the inverter clock and `inverter_timezone`.
- **Before sharing logs**, remove inverter serial numbers and your Octopus account ID.

## Development

The tests use only the Python standard library (3.9+), with no Home Assistant or pyscript install needed:

```bash
python3 -m unittest discover -s tests -t .
```

`tests/harness.py` loads the app with fake versions of pyscript's built-in globals (`hass`, `state`, `task`, the trigger decorators), a frozen clock, instant sleeps and a fake SolisCloud API. That lets the tests drive the app through a full charging session and check exactly which charge slots it would write. The tests cover:

- **Slot logic:** rolling half-hour windows, extending at `:27`/`:57`, finishing the half-hour, clearing, core-window hand-over, midnight
- **Charge detection:** dispatch plus current threshold, ignoring preheating and blips, unavailable sensors
- **Triggers:** debounce, startup initialisation, cron timing, services
- **SolisCloud:** request signing, CIDs per slot, retries, inverter selection, daily time sync, diagnostics mode

CI runs the suite on every push (`.github/workflows/tests.yml`).

## Credits

- [Moondevil-ha/solis-smart-charging](https://github.com/Moondevil-ha/solis-smart-charging) (MIT): the SolisCloud API signing, login, inverter selection, time sync and control-write code is adapted from this project, which schedules battery charging from Octopus *planned* dispatches.
- [stevegal/solis_control](https://github.com/stevegal/solis_control) (MIT): the original SolisCloud API logic used by the project above.
- [hultenvp/solis-sensor](https://github.com/hultenvp/solis-sensor): the 6-slot charge time CID mapping.

Not affiliated with Solis, Octopus Energy, Hypervolt or Home Assistant.

## License

[MIT](LICENSE)
