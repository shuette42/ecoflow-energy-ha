# STREAM AC 5000 - Entity Reference

Full list of all entities created for the STREAM AC 5000.

**Models:** STREAM AC 5000 (`ES22`), STREAM 5000 (`ES21`).

**Both models get everything below.** The STREAM 5000 is the same product on a different model number and sends the same telemetry, so every reading applies to it. The controls did not, at first: each of those rebuilds a frame recorded from a real app session, a power setpoint writes a scheduled task into the battery rather than flipping a display setting, and no recording from a STREAM 5000 had shown one being accepted. One since has. An owner recorded his unit taking a setting change from the app and reporting the new value back, on the envelope and command these controls use, so they are enabled for both model numbers.

**This is not the Stream entity set.** Despite the shared product name, an `ES22` speaks a different protocol from the BK-series Stream devices: it sends none of their telemetry messages and describes power as a flow matrix rather than as individual readings. It therefore has its own device type, parser and entity list. See [Stream](stream.md) for the BK series.

**Totals:** 56 sensors, 2 binary sensors, 2 switches, 7 numbers, 1 select

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
| Third-Party Solar Power | W | - | *accessory* | Solar the app attributes to panels that are not wired to this unit. It shows this as "Other" beside the unit's own strings and adds the two for its solar total. The app notes that third-party solar is not detected accurately: the figure is what is left of the house's solar after home consumption, so it is inferred rather than measured. On a unit with no PV wired to the EcoFlow it is the whole solar reading. See the note below |

## Sensors - PV Strings

The MPPT reading, and a different quantity from Third-Party Solar Power above. The EcoFlow app shows the unit's own strings and a separate third-party figure side by side, and adds them for the solar total it displays. Each of these five appears once that string has actually produced something, so a unit with no PV on the EcoFlow gains none of them and a string that is never wired does not become an entity reading zero forever. Once a string has its entity it keeps it, and reads 0 W overnight rather than holding the last daylight value.

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
| Max Grid-tied Output Power | W | diagnostic | enabled | The app's "Max grid-tied output power". Also writable, see the number of the same name. Its ceiling is raised by asking EcoFlow. A task power above the setting is clamped, not refused |
| Max Grid Input Power | W | diagnostic | enabled | The app's "Max grid input power", and the setting that actually governs grid charging. Also writable, see the number of the same name. In the app it sits under "netzgekoppelte Eingangsleistung" |
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

> **The two power numbers are setpoints, not the device's limits.** The EcoFlow app calls them "Max discharging power" and "Max grid charging power". For discharging, a maximum is what it is only while a smart meter is linked; with none linked the device delivers the figure asked for. For charging that has not held up in measurement, and the note further down says what is known. They are named after the sensors that report them back instead, and they sit close enough to the Max Grid-tied Output Power and Max Grid Input Power readings to be mistaken for those. Those two are the limits, and both can now be set from Home Assistant as well, on the numbers of the same name. The input one is the one that governs grid charging. Asking a setpoint for more than a limit allows is accepted, acknowledged and then clamped by the device, so no value set here can drive the unit past the limit its owner configured.

| Entity | Unit | Range | Description |
|:---|:---:|:---:|:---|
| Scheduled Discharge Power | W | 0-2500 | Discharge power setpoint, not a limit. This is the entity an external optimiser writes. The app calls it "Max discharging power" |
| Scheduled Charge Power | W | 0-2500 | Charge power setpoint. Grid charging, so it is meant to drive a cheap-tariff charge, but whether the device acts on the figure is unsettled: see the note below. The app calls it "Max grid charging power" |
| Max Charge SoC | % | 50-100 | Upper SoC limit. Both limits are one setting on the wire, so changing either sends both |
| Min Discharge SoC | % | 0-50 | Lower SoC limit |
| Backup Reserve | % | 0-100 | Level held back for a power cut. Field 30 holds this and the on/off flag together, so changing either sends both |
| Max Grid-tied Output Power | W | 0 to the device's ceiling | The app's "Max grid-tied output power". The upper end is not the model's rated 2500 W but the ceiling the device reports, which only EcoFlow raises; it has been seen at 600 W on a unit rated 2500 |
| Max Grid Input Power | W | 0-2600, in 100 W steps | The app's "netzgekoppelte Eingangsleistung", and what a grid charge is actually held to. The steps are the app's own. Nothing on the wire reports a ceiling for it the way the output side has one, so the range ends at the highest figure an app has offered and a device has acknowledged: an owner raised it from 2200 to 2600 W on a unit rated 2500, and the app took it without the authorisation step it asks for on the output side. An acknowledgement is not proof the device kept that figure, since a setpoint it dislikes is clamped silently rather than refused. That a value set here is kept at all is confirmed at 1800 W, where the device reported the written value back |

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

**With no meter linked**, the device runs open loop. The app's own help text says it plainly: *"When no meter is linked, power from the system's grid-tied ports goes to the home, and any unused power will flow to the grid."* The discharge setpoint then becomes an absolute power command. Confirmed on hardware: unlinking a Tibber Pulse turned a 1400 W request that had been delivering nothing extra into a measured 1400 W discharge. A second owner reproduced it independently, measuring 789 W of discharge against an 800 W setpoint while 1.8 kW was going to the grid.

**The charge setpoint is a different case, and it is open.** The same owner set it to 1250 W and measured a full overnight grid charge running at about 2330 W anyway, with an external meter in the supply line. An earlier charge with no setpoint at all ran at about 2570 W for the same energy, so the setting made no difference either way. The value does reach the device: the frame is a reproduction of a write recorded from the EcoFlow app for this setting, and the device reports the new figure back within seconds. So it is accepted and stored, and then not acted on. Stored is meant literally: the figure was still standing nine hours later, across the whole charge and well past it.

What outranked it is not established, and there are three candidates. Backup Reserve stood at 100% in that run, which is a standing demand to keep the pack full. The charge block carries two further fields whose meaning is unknown here. And the window is one the app itself was never recorded sending: a power write here always covers the whole day, while the recorded app write covered an afternoon. That last one is weak evidence against the whole-day window, since the discharge setpoint uses exactly the same window and does act. A fourth candidate has been ruled out: a task is only acted on in Custom mode at all, and the run above was in Custom mode.

**What does limit grid charging is Max Grid Input Power.** The app calls it "netzgekoppelte Eingangsleistung" and adjusts it in 100 W steps. Set to 1200 W in the app, the running charge followed it to 1200 W, and the reading in Home Assistant picked the change up about 30 seconds later. That is the same installation whose 1250 W charge setpoint changed nothing. So the limit that works is the one to set, and it can now be set from here: the number of the same name writes it, reproducing the app's own frame from a recording of an owner changing it four times in a row.

Until it is settled, treat this control as unproven and verify against your own metering before building an automation on it. The discharge side is unaffected.

So for an optimiser that wants to command power rather than cap it, **unlink the meter from the EcoFlow app** and let the optimiser do the metering. The device then no longer self-consumes on its own, which is the point.

Two readings go with the meter, and it is worth knowing which before unlinking. Measured on a unit four days after its meter was removed:

- **Third-Party Solar Power disappears entirely**, and the sensor goes unavailable. This figure is inferred from the house flows rather than measured, so with no meter there is nothing to infer it from. The PV String sensors are unaffected: those come from the unit's own MPPT and need no meter.
- **Grid Power and the per-phase meter values stop updating.** They do not go unavailable: on the unit measured, Grid Power held a single value taken shortly after startup and never moved again. Treat it as stale rather than as a reading.

Home Power is unaffected and keeps arriving at the usual rate, as does everything read from the flow matrix, which is why battery power, grid import and grid export and their counters keep working normally with no meter at all.

### The rest

- **The setpoint is a scheduled task**, because that is the only power control this device has. A power write replaces the schedule rather than editing around it: the task it writes always covers the whole day and is always enabled, so the value asked for is the value that acts, whatever window or disabled flag was set in the app. A setpoint that only applied inside somebody else's window would not be a setpoint. The one thing carried over is the charge task's own SoC target, because that decides what charging does rather than whether it happens. On a device with no task at all, one is created.
- **So do not run the app's own scheduler alongside these controls.** Every power write here overwrites whatever schedule the app holds, and the app will happily overwrite it back. Drive this device from Home Assistant or from the app's scheduler, not from both.
- **Zero means idle**, not "no setting". Writing 0 stops the battery entirely rather than falling back to self-consumption. That is the way to park it.
- **A setpoint is only acted on in Custom mode.** In self-powered or Intelligent Mode+ the device follows its own logic. The write is still accepted, so nothing reports an error; a warning goes to the log instead.
- **A setpoint above one of the app's power limits is clamped, not refused.** Max Grid-tied Output Power and Max Grid Input Power report them, and both are controls here. The input one is what a grid charge is actually held to. Above the output limit sits an account ceiling that only EcoFlow can raise, and the output control offers that ceiling as its upper end rather than the model's rated 2500 W, because a range above it would do nothing. The input side has no such reported ceiling, so its control stops at the highest figure an app has offered and a device has acknowledged. Setting it from here does take effect: at 1800 W the device reported the written value back, with the output setpoint beside it untouched.

Response is quick: a change settles in 10 to 20 seconds in either direction.

## Both limits travel together

Max Charge SoC and Min Discharge SoC are one setting on the wire, so changing either sends both. The one you did not touch goes out at the value the device last reported. If that value has not arrived yet the write is refused rather than guessed at, and the device itself rejects a discharge limit at or above the charge limit.

## Notes

- **Grid import and export come from the flow matrix, not from the meter.** The device reports the grid split as separate paths, so both counters are non-negative by construction, which is what the Energy Dashboard needs. Grid Power carries the signed meter reading alongside them.
- **Third-party solar can appear on a unit with no PV wired to the EcoFlow.** The device derives this figure from the house flows, and the app shows it too, under "Other" in its solar breakdown. It is reported here as the device reports it. On an installation whose PV is a separate system this figure is the EcoFlow's inference, not a measurement of that system. That is also why there is no lifetime Solar Energy counter: a counter of that kind only ever goes up and cannot be corrected later, so an inferred figure would permanently credit the Energy Dashboard with production that never happened. Third-Party Solar Power carries the same information without that risk.
- **Cycle count is not reported.** One field looks like one but was observed at 497, 499 and 1311 within minutes, so it is left out rather than exposed as a counter that would read as a meter reset.
- **Deleting one task of several leaves its last values behind, deleting the last one clears them.** The device sends its whole schedule at once, one block per task, but it simply stops mentioning a task that no longer exists, which is indistinguishable from not having mentioned it yet, so a task deleted in the app leaves the Scheduled Charge or Scheduled Discharge Power reading at its last value until a task of that kind is reported again. Writing a power setpoint recreates the task, so the value becomes true again at that point. When the last task goes, the device sends an empty task block instead of no block at all, and that is unambiguous: every scheduled-task reading goes to unknown.
- **A task set to zero watts reports zero, not nothing.** Zero is a real setpoint on this device rather than an absence, and the wire format leaves a zero out altogether, so a task parked at 0 W arrives with its settings block and no power inside it. It is read back as the zero it is. That matters more than it sounds: the power reading is what says a task of that kind exists at all, so a task whose power went missing would never be replaced and a later setpoint would land on top of it.
- **Only one task per kind is tracked.** The device can hold more than one charge or more than one discharge task; these readings hold the last of each, and writing a setpoint replaces one task of that kind, so a second task of the same kind survives the write. One charge and one discharge task, which is what the app's own screen offers, is unaffected.
- **A task deleted from Home Assistant clears immediately**, because the integration knows what it deleted and does not wait for a readback that will never come. Writing one power setpoint removes the other kind's task first, since two whole-day tasks overlap and the device then acts on neither, and the removed kind's power, window and enabled readings go to unknown at that point. The charge task's SoC target survives, so a task set to stop at 80% does not quietly become one that charges to 100%. Both the removal and the replacement name the task by the number the device itself reported for it rather than one worked out from whether it charges or discharges, because the app numbers tasks its own way and a frame naming the wrong one would silently miss.
- **Linked units are reported as one system.** EcoFlow allows several of these on one account, and the device then describes them as a single battery: Battery SOC is the mean across the units and Battery Power their sum, which is what the app calls System SOC. Each unit still gets its own device page with the full entity set, and the readings that come from its own battery hardware are already its own there: Battery Voltage, Battery Temp, Battery Current, the cell values, Remaining Capacity, and the state of charge under Battery SOC (Precise). Unit Battery Power adds the one figure that was missing, and both it and the precise state of charge are off by default because on a single unit they repeat the system reading. If a unit's own entry never arrives its Unit Battery Power stays empty rather than showing a neighbour's value, and a diagnostics download says which of the two cases it is under `linked_units`.
- **Battery Power is derived from the flow matrix**, not taken from the device's own live battery field. That field stops being sent the moment the unit goes idle and holds its last active value, so it reports a charge or discharge that has already stopped. The flow edges do not have that problem.
