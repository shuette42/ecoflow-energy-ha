# STREAM AC 5000 - Entity Reference

Full list of all entities created for the STREAM AC 5000.

**Models:** STREAM AC 5000 (`ES22`).

**This is not the Stream entity set.** Despite the shared product name, an `ES22` speaks a different protocol from the BK-series Stream devices: it sends none of their telemetry messages and describes power as a flow matrix rather than as individual readings. It therefore has its own device type, parser and entity list. See [Stream](stream.md) for the BK series.

**Totals:** 51 sensors, 2 binary sensors

> Entities marked with *disabled* are available but hidden by default. Enable them in **Settings > Devices > EcoFlow STREAM AC 5000 > Entities** (click the filter icon and show disabled entities).

> **Enhanced Mode only.** This device is not reachable through the IoT Developer API, so Standard Mode reports error 1006 and no entities fill. Set the integration up with an EcoFlow account e-mail and password.

> **Entities marked *accessory* are created only once the device actually reports the reading**, and they appear on their own the moment it does, without a restart. Whether a unit has solar wired to the EcoFlow itself, and which smart meter is linked in the app, are installation choices rather than model differences, so listing them for everyone would leave most owners with entities that can never fill.

---

## Sensors - Battery

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Battery SOC | % | - | enabled | State of charge, as shown in the app |
| Battery SOC (Precise) | % | diagnostic | disabled | High-resolution system SoC |
| Precise SoC | % | diagnostic | disabled | Pack-level SoC, straight from the BMS. Runs about two points above Battery SOC (Precise) directly above it, which is the system figure and the one the app shows. The two names are easy to confuse; prefer the system one unless you specifically want the pack reading |
| Battery SoH | % | - | enabled | State of health |
| Battery Power | W | - | enabled | Signed battery power (positive = charging, negative = discharging) |
| Battery Charge Power | W | - | enabled | Charging power (always >= 0) |
| Battery Discharge Power | W | - | enabled | Discharging power (always >= 0) |
| Battery Charge/Discharge State | - | diagnostic | disabled | `standby`, `charging` or `discharging` |

## Sensors - Power Flow

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Home Power | W | - | enabled | Total house consumption |
| Grid Power | W | - | enabled | Signed grid power from the linked smart meter (positive = drawing, negative = feeding in). Absent while no meter is linked in the EcoFlow app |
| Grid Import Power | W | - | enabled | Power drawn from the grid, derived from the flow matrix |
| Grid Export Power | W | - | enabled | Power fed into the grid, derived from the flow matrix |
| Home From Battery | W | diagnostic | disabled | House load covered by the battery |
| Home From Grid | W | diagnostic | disabled | House load covered by the grid |
| Solar Power | W | - | *accessory* | The solar figure the device derives for itself, which on an installation with separate PV is its inference rather than a measurement of that system. See the note below |
| Home From Solar | W | diagnostic | *accessory*, disabled | House load covered by solar |

## Sensors - Smart Meter

Only an EcoFlow P1 meter reports per-phase values. A meter that reports a single total, such as a Tibber Pulse, feeds Grid Power above and creates none of these.

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Grid Phase A/B/C Power | W | diagnostic | *accessory*, disabled | Active power per phase |
| Grid Phase A/B/C Voltage | V | diagnostic | *accessory*, disabled | Voltage per phase |
| Grid Phase A/B/C Current | A | diagnostic | *accessory*, disabled | Current per phase |
| AC Frequency | Hz | diagnostic | *accessory*, disabled | Grid frequency |

## Sensors - Configuration

The settings that also have a control read back here, so an automation can see what the device reports rather than what was last requested.

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Work Mode | - | diagnostic | enabled | `self_powered`, `intelligent_plus` or `custom` |
| Max Charge SoC | % | diagnostic | enabled | Upper SoC limit set in the app |
| Min Discharge SoC | % | diagnostic | enabled | Lower SoC limit set in the app |
| Backup Reserve | % | diagnostic | enabled | Reserve level held for a power cut |
| Max Grid-tied Output Power | W | diagnostic | enabled | Account-level output limit. Raised by asking EcoFlow. A task power above it is clamped, not refused |
| Max Grid Input Power | W | diagnostic | enabled | Account-level input limit, settable in the app |
| Scheduled Discharge Power | W | diagnostic | enabled | Power setpoint of the discharge task, mirrors the Max Discharging Power number |
| Scheduled Charge Power | W | diagnostic | enabled | Power setpoint of the charge task, mirrors the Max Grid Charging Power number |
| Scheduled Charge Target SoC | % | diagnostic | enabled | The charge task's own SoC target, shown in the app as "Charge limit". Preserved on every power write |

## Sensors - Battery Diagnostics

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Battery Voltage | V | - | enabled | Pack voltage |
| Battery Current | A | diagnostic | disabled | Pack current (positive = charging) |
| Battery Temp | C | - | enabled | Pack temperature |
| Cell Temp (Max) | C | diagnostic | disabled | Highest cell temperature |
| Cell Temp (Min) | C | diagnostic | disabled | Lowest cell temperature |
| MOSFET Temp (Max) | C | diagnostic | disabled | Highest MOSFET temperature |
| Cell Voltage (Max) | mV | diagnostic | disabled | Highest cell voltage |
| Cell Voltage (Min) | mV | diagnostic | disabled | Lowest cell voltage |
| Design Capacity | mAh | diagnostic | disabled | Nameplate capacity |
| Full Capacity | mAh | diagnostic | disabled | Present full-charge capacity |
| Remaining Capacity | mAh | diagnostic | disabled | Charge left in the pack |

## Sensors - Energy Dashboard

All five are integrated from the matching power reading, so they only ever count up.

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Battery Charge Energy | kWh | - | enabled | Lifetime energy into the battery |
| Battery Discharge Energy | kWh | - | enabled | Lifetime energy out of the battery |
| Grid Import Energy | kWh | - | enabled | Lifetime energy drawn from the grid |
| Grid Export Energy | kWh | - | enabled | Lifetime energy fed into the grid |
| Home Energy | kWh | diagnostic | disabled | Lifetime house consumption |

There is deliberately no Solar Energy counter, see the note below.

## Binary Sensors

| Entity | Category | Default | Description |
|:---|:---:|:---:|:---|
| Backup Reserve | diagnostic | enabled | Whether a reserve is held for a power cut |
| Backup Socket | diagnostic | enabled | Whether the backup socket is switched on |

## Notes

- **Grid import and export come from the flow matrix, not from the meter.** The device reports the grid split as separate paths, so both counters are non-negative by construction, which is what the Energy Dashboard needs. Grid Power carries the signed meter reading alongside them.
- **Solar can appear on a unit with no PV wired to the EcoFlow.** The device derives a solar figure of its own from the house flows, and the app shows it too, as "Solar generation" on the home screen. It is reported here as the device reports it. On an installation whose PV is a separate system this figure is the EcoFlow's inference, not a measurement of that system. That is also why there is no lifetime Solar Energy counter: a counter of that kind only ever goes up and cannot be corrected later, so an inferred figure would permanently credit the Energy Dashboard with production that never happened. Solar Power carries the same information without that risk.
- **Cycle count is not reported.** One field looks like one but was observed at 497, 499 and 1311 within minutes, so it is left out rather than exposed as a counter that would read as a meter reset.
- **Deleting one task of several leaves its last values behind, deleting the last one clears them.** The device reports one task per message and simply stops mentioning a task that no longer exists, which is indistinguishable from not having mentioned it yet, so the Scheduled Charge and Scheduled Discharge Power sensors keep their last reading until a task of that kind is reported again. When the last task goes, the device sends an empty task block instead of no block at all, and that is unambiguous: all nine scheduled-task readings go to unknown.
- **Battery Power is derived from the flow matrix**, not taken from the device's own live battery field. That field stops being sent the moment the unit goes idle and holds its last active value, so it reports a charge or discharge that has already stopped. The flow edges do not have that problem.
