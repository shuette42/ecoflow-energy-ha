# STREAM AC 5000 - Entity Reference

Full list of all entities created for the STREAM AC 5000.

**Models:** STREAM AC 5000 (`ES22`), STREAM 5000 (`ES21`).

**The STREAM 5000 gets the sensors and binary sensors, not the controls.** It is the same product on a different model number and sends the same telemetry, so every reading below applies to it. The switches, numbers and select do not: each of those rebuilds a frame captured from an AC 5000, and no recording from a STREAM 5000 has confirmed it accepts them. A power setpoint writes a scheduled task into the battery, so the difference is not cosmetic. A recording from one of these units is what turns them on.

**This is not the Stream entity set.** Despite the shared product name, an `ES22` speaks a different protocol from the BK-series Stream devices: it sends none of their telemetry messages and describes power as a flow matrix rather than as individual readings. It therefore has its own device type, parser and entity list. See [Stream](stream.md) for the BK series.

**Totals:** 56 sensors, 2 binary sensors, 2 switches, 5 numbers, 1 select

> Entities marked with *disabled* are available but hidden by default. Enable them in **Settings > Devices > EcoFlow STREAM AC 5000 > Entities** (click the filter icon and show disabled entities).

> **Enhanced Mode only.** This device is not reachable through the IoT Developer API, so Standard Mode reports error 1006 and no entities fill. Set the integration up with an EcoFlow account e-mail and password.

> **Entities marked *accessory* are created only once the device actually reports the reading**, and they appear on their own the moment it does, without a restart. Whether a unit has solar wired to the EcoFlow itself, and which smart meter is linked in the app, are installation choices rather than model differences, so listing them for everyone would leave most owners with entities that can never fill.

---

## Sensors - Battery

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Battery SOC | % | - | enabled | State of charge, as shown in the app. With units linked this is the system figure, the mean across them |
| Battery SOC (Precise) | % | diagnostic | disabled | This unit's own state of charge, at higher resolution. On a single unit it tracks Battery SOC above; with units linked the two differ, and the app shows this one under the unit's name |
| Precise SoC | % | diagnostic | disabled | Pack-level SoC, straight from the BMS, and also this unit's own rather than the system figure. It runs slightly above Battery SOC (Precise) directly above it. The two names are easy to confuse |
| Battery SoH | % | - | enabled | State of health |
| Battery Power | W | - | enabled | Signed battery power (positive = charging, negative = discharging), derived from the flows the device reports, including the charge from its own PV strings where it has them. With units linked this is the system figure, the sum across them |
| Unit Battery Power | W | diagnostic | disabled | This unit's own share of the battery power above. On a single unit it repeats that reading, which is why it is off by default |
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
| Solar Power | W | - | *accessory* | The solar the app reports separately from this unit's own strings, which it shows as a third-party figure and adds to the PV strings for its solar total. On a unit with no PV wired to the EcoFlow this is the whole solar reading. See the note below |

## Sensors - PV Strings

The MPPT reading, and a different quantity from Solar Power above. The EcoFlow app shows the unit's own strings and a separate third-party figure side by side, and adds them for the solar total it displays. Each of these five appears once that string has actually produced something, so a unit with no PV on the EcoFlow gains none of them and a string that is never wired does not become an entity reading zero forever. Once a string has its entity it keeps it, and reads 0 W overnight rather than holding the last daylight value.

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| PV Total Power | W | - | *accessory* | Total across the strings; equals the sum of the four below |
| PV 1 Power | W | - | *accessory* | String 1 |
| PV 2 Power | W | - | *accessory* | String 2 |
| PV 3 Power | W | - | *accessory* | String 3 |
| PV 4 Power | W | - | *accessory* | String 4 |

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
| Max Grid-tied Output Power | W | diagnostic | enabled | The app's "Max grid-tied output power". Its ceiling is raised by asking EcoFlow. A task power above the setting is clamped, not refused |
| Max Grid Input Power | W | diagnostic | enabled | The app's "Max grid input power", the charge-side limit |
| Scheduled Discharge Power | W | diagnostic | enabled | Power setpoint of the discharge task, mirrors the number of the same name |
| Scheduled Charge Power | W | diagnostic | enabled | Power setpoint of the charge task, mirrors the number of the same name |
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

## Numbers

> **The two power numbers are setpoints, not the device's limits.** The EcoFlow app calls them "Max discharging power" and "Max grid charging power", but a maximum is what they are only while a smart meter is linked; with none linked the device delivers the figure asked for. They are named after the sensors that report them back instead, and they sit close enough to the Max Grid-tied Output Power and Max Grid Input Power *sensors* to be mistaken for those. Those two sensors are the limits, they are configured in the app, and neither is writable from Home Assistant: this device offers no way to write a limit at all. Asking a setpoint for more than a limit allows is accepted, acknowledged and then clamped by the device, so no value set here can drive the unit past the limit its owner configured.

| Entity | Unit | Range | Description |
|:---|:---:|:---:|:---|
| Scheduled Discharge Power | W | 0-2500 | Discharge power setpoint, not a limit. This is the entity an external optimiser writes. The app calls it "Max discharging power" |
| Scheduled Charge Power | W | 0-2500 | Charge power setpoint, not a limit. Grid charging, so it is how a cheap-tariff charge is driven. The app calls it "Max grid charging power" |
| Max Charge SoC | % | 50-100 | Upper SoC limit. Both limits are one setting on the wire, so changing either sends both |
| Min Discharge SoC | % | 0-50 | Lower SoC limit |
| Backup Reserve | % | 0-100 | Level held back for a power cut. Field 30 holds this and the on/off flag together, so changing either sends both |

## Selects

| Entity | Options | Description |
|:---|:---|:---|
| Work Mode | Self-powered, Intelligent Mode+, Custom | The device's operating mode |

## Switches

| Entity | Description |
|:---|:---|
| Backup Reserve | Whether a reserve is held back for a power cut. Writes the on/off flag together with the level, since the device holds both in one field |
| Backup Socket | The app's backup socket control |

## Driving this battery from an optimiser

### Whether a smart meter is linked changes what the controls mean

This is the single most important thing about this device, and it is not obvious.

**With a smart meter linked in the EcoFlow app**, the device runs closed loop against that meter. It will not discharge into an export, so the power setpoint acts only as a ceiling on covering house load. Request 1400 W into a house that needs 200 W and you get 200 W. This was measured, including with feed-in explicitly enabled, which does not change it.

**With no meter linked**, the device runs open loop. The app's own help text says it plainly: *"When no meter is linked, power from the system's grid-tied ports goes to the home, and any unused power will flow to the grid."* The setpoint then becomes an absolute power command. Confirmed on hardware: unlinking a Tibber Pulse turned a 1400 W request that had been delivering nothing extra into a measured 1400 W discharge.

So for an optimiser that wants to command power rather than cap it, **unlink the meter from the EcoFlow app** and let the optimiser do the metering. The device then no longer self-consumes on its own, which is the point.

Two readings go with the meter, and it is worth knowing which before unlinking. Measured on a unit four days after its meter was removed:

- **Solar Power disappears entirely**, and the sensor goes unavailable. On this device solar is a figure the unit infers from the house flows rather than measures, so with no meter there is nothing to infer it from. A unit with no meter cannot report solar even if panels are wired to it.
- **Grid Power and the per-phase meter values stop updating.** They do not go unavailable: on the unit measured, Grid Power held a single value taken shortly after startup and never moved again. Treat it as stale rather than as a reading.

Home Power is unaffected and keeps arriving at the usual rate, as does everything read from the flow matrix, which is why battery power, grid import and grid export and their counters keep working normally with no meter at all.

### The rest

- **The setpoint is a scheduled task**, because that is the only power control this device has. A power write replaces the schedule rather than editing around it: the task it writes always covers the whole day and is always enabled, so the value asked for is the value that acts, whatever window or disabled flag was set in the app. A setpoint that only applied inside somebody else's window would not be a setpoint. The one thing carried over is the charge task's own SoC target, because that decides what charging does rather than whether it happens. On a device with no task at all, one is created.
- **So do not run the app's own scheduler alongside these controls.** Every power write here overwrites whatever schedule the app holds, and the app will happily overwrite it back. Drive this device from Home Assistant or from the app's scheduler, not from both.
- **Zero means idle**, not "no setting". Writing 0 stops the battery entirely rather than falling back to self-consumption. That is the way to park it.
- **A setpoint is only acted on in Custom mode.** In self-powered or Intelligent Mode+ the device follows its own logic. The write is still accepted, so nothing reports an error; a warning goes to the log instead.
- **A setpoint above one of the app's power limits is clamped, not refused.** Max Grid-tied Output Power and Max Grid Input Power report them, and both are set in the app. The output limit additionally has an account ceiling that only EcoFlow can raise.

Response is quick: a change settles in 10 to 20 seconds in either direction.

## Both limits travel together

Max Charge SoC and Min Discharge SoC are one setting on the wire, so changing either sends both. The one you did not touch goes out at the value the device last reported. If that value has not arrived yet the write is refused rather than guessed at, and the device itself rejects a discharge limit at or above the charge limit.

## Notes

- **Grid import and export come from the flow matrix, not from the meter.** The device reports the grid split as separate paths, so both counters are non-negative by construction, which is what the Energy Dashboard needs. Grid Power carries the signed meter reading alongside them.
- **Solar can appear on a unit with no PV wired to the EcoFlow.** The device derives a solar figure of its own from the house flows, and the app shows it too, as "Solar generation" on the home screen. It is reported here as the device reports it. On an installation whose PV is a separate system this figure is the EcoFlow's inference, not a measurement of that system. That is also why there is no lifetime Solar Energy counter: a counter of that kind only ever goes up and cannot be corrected later, so an inferred figure would permanently credit the Energy Dashboard with production that never happened. Solar Power carries the same information without that risk.
- **Cycle count is not reported.** One field looks like one but was observed at 497, 499 and 1311 within minutes, so it is left out rather than exposed as a counter that would read as a meter reset.
- **Deleting one task of several leaves its last values behind, deleting the last one clears them.** The device reports one task per message and simply stops mentioning a task that no longer exists, which is indistinguishable from not having mentioned it yet, so a task deleted in the app leaves the Scheduled Charge or Scheduled Discharge Power reading at its last value until a task of that kind is reported again. Writing a power setpoint recreates the task, so the value becomes true again at that point. When the last task goes, the device sends an empty task block instead of no block at all, and that is unambiguous: all nine scheduled-task readings go to unknown.
- **A task deleted from Home Assistant clears immediately**, because the integration knows what it deleted and does not wait for a readback that will never come. Writing one power setpoint removes the other kind's task first, since two whole-day tasks overlap and the device then acts on neither, and the removed kind's power, window and enabled readings go to unknown at that point. The charge task's SoC target survives, so a task set to stop at 80% does not quietly become one that charges to 100%.
- **Linked units are reported as one system.** EcoFlow allows several of these on one account, and the device then describes them as a single battery: Battery SOC is the mean across the units and Battery Power their sum, which is what the app calls System SOC. Each unit still gets its own device page with the full entity set, and the readings that come from its own battery hardware are already its own there: Battery Voltage, Battery Temp, Battery Current, the cell values, Remaining Capacity, and the state of charge under Battery SOC (Precise). Unit Battery Power adds the one figure that was missing, and both it and the precise state of charge are off by default because on a single unit they repeat the system reading. If a unit's own entry never arrives its Unit Battery Power stays empty rather than showing a neighbour's value, and a diagnostics download says which of the two cases it is under `linked_units`.
- **Battery Power is derived from the flow matrix**, not taken from the device's own live battery field. That field stops being sent the moment the unit goes idle and holds its last active value, so it reports a charge or discharge that has already stopped. The flow edges do not have that problem.
