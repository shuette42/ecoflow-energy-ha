# Delta 3 Max Plus - Entity Reference

Full list of all entities created for Delta 3 devices. The entity set is shared
across the generation: Delta 3 Max Plus (`D3M1`, `P321`) and base DELTA 3 (`P231`).

**Totals:** 47 sensors, 7 switches, 4 numbers, 5 selects. Units whose serial
starts with `D3M` add the port priority set: 3 switches, 3 numbers and 1 binary
sensor.

> Entities marked with *diagnostic* appear in the diagnostics section of the device page.

> **Other Delta 3 models:** only the Delta 3 Max Plus has been checked against real hardware end to end. A base DELTA 3 (`P231`) was confirmed to push the same three frames and to decode through the same field map, but a smaller unit has fewer ports, so port entities it does not have stay empty. Raw quota diagnostics are available to help extend the mapping.

---

## Sensors - Battery

| Entity | Unit | Description |
|:---|:---:|:---|
| SoC | % | State of charge (shown in device header) |
| Charge/Discharge State | - | `idle`, `charging` or `discharging` |
| Charge Time Remaining | min | Only while charging, otherwise unavailable |
| Discharge Time Remaining | min | Only while discharging, otherwise unavailable |
| Charge Limit | % | Current charge limit (diagnostic, mirrors the number) |
| Discharge Limit | % | Current discharge limit (diagnostic, mirrors the number) |
| AC Charge Power Limit | W | The charge speed set in the EcoFlow app, 200 W to 2400 W (diagnostic, EcoFlow account sign-in only) |

## Sensors - Power

| Entity | Unit | Description |
|:---|:---:|:---|
| Input Total | W | Sum of all inputs |
| Output Total | W | Sum of all outputs |
| AC Input | W | AC charging power |
| Solar Input 1 | W | Solar / DC input 1 |
| Solar Input 2 | W | Solar / DC input 2 |
| AC Output 1 | W | First AC outlet group |
| AC Output 2 | W | Second AC outlet group |
| 12V Output | W | 12 V DC output |
| Anderson Output | W | Anderson port output |
| Type-C 1 / Type-C 2 / Type-C 3 | W | USB-C ports |
| USB QC 1 / USB QC 2 | W | USB Quick Charge ports |

## Sensors - Energy Dashboard

Integrated from the live power telemetry (the device exposes no native energy counters), so these accumulate over time and can be added to the Home Assistant Energy Dashboard.

| Entity | Unit | Description |
|:---|:---:|:---|
| Solar Energy | kWh | Cumulative solar input 1 |
| Solar 2 Energy | kWh | Cumulative solar input 2 |
| AC Input Energy | kWh | Cumulative AC charging |
| Output Energy | kWh | Cumulative total output (AC + DC + USB) |

---

## Sensors - Battery Health (Enhanced Mode only)

These come from the battery management system, which reports them only over the
live telemetry connection. With developer keys the polled data carries no
battery health at all, so these entities stay empty in Standard Mode.

The two lifetime energy counters are read from the battery rather than
calculated from power over time, so they carry the device's own history and can
be used in the Energy Dashboard without accumulating drift.

| Entity | Unit | Description | Default |
|:---|:---:|:---|:---:|
| Battery SoH | % | State of health | enabled |
| Battery Cycles | - | Charge cycles counted by the battery | enabled |
| Battery Lifetime Charge Energy | kWh | Energy charged over the battery's life | enabled |
| Battery Lifetime Discharge Energy | kWh | Energy discharged over the battery's life | enabled |
| Battery Voltage | V | Pack voltage | enabled |
| Battery Current | A | Pack current, negative while discharging | enabled |
| Battery Temp | °C | Pack temperature | enabled |
| Battery Max/Min Cell Temp | °C | Warmest and coldest cell | disabled |
| Battery Max/Min MOSFET Temp | °C | MOSFET temperatures | disabled |
| Battery Max/Min Cell Voltage | mV | Highest and lowest cell voltage | disabled |
| Battery Cell Voltage Spread | mV | Difference between the two, a balance indicator | disabled |
| Battery Remaining/Full/Design Capacity | mAh | Capacity values | disabled |
| Battery Cell Count | - | Cells in series | disabled |
| Battery Real Health | % | Health before the display rounding | disabled |
| Battery Calendar Health | % | Ageing attributed to time | disabled |
| Battery Cycle Health | % | Ageing attributed to use | disabled |
| Battery Error Code | - | Aggregated BMS error code | disabled |

> Environment temperature is not exposed. The hardware reports the "no sensor
> fitted" placeholder for it, which would show as -127 °C.

---

## Switches

| Entity | Description |
|:---|:---|
| AC Output | First AC outlet group on/off |
| AC Output 2 | Second AC outlet group on/off |
| 12V Output | 12 V DC output on/off |
| Backup Reserve | Enable the backup reserve function |
| X-Boost | X-Boost for high-power appliances |
| Beeper | Device buzzer |
| Bypass Output Disabled | Block pass-through output while charging from AC |
| AC 1 non-essential | Mark the first AC outlet group as non-essential |
| AC 2 non-essential | Mark the second AC outlet group as non-essential |
| DC non-essential | Mark the DC output as non-essential |

---

## Numbers

| Entity | Unit | Range | Step | Description |
|:---|:---:|:---:|:---:|:---|
| Backup Reserve Level | % | 0 - 50 | 1 | Reserve SoC kept for backup |
| Charge Limit | % | 50 - 100 | 1 | Stop charging at this level |
| Discharge Limit | % | 0 - 30 | 1 | Stop discharging at this level |
| AC Charge Power | W | 200 - 2400 | 100 | How fast to charge from the grid, the same setting as the charge speed slider in the EcoFlow app |
| AC 1 cutoff level | % | dynamic | 1 | Battery level at which a non-essential AC 1 stops being powered |
| AC 2 cutoff level | % | dynamic | 1 | Battery level at which a non-essential AC 2 stops being powered |
| DC cutoff level | % | dynamic | 1 | Battery level at which a non-essential DC output stops being powered |

---

## Selects

| Entity | Options | Description |
|:---|:---|:---|
| Screen Timeout | 10 s, 30 s, 1 min, 5 min, 30 min, Never | How long the display stays lit after the last interaction. The same setting as "LCD screen timeout" under automatic shutdown in the EcoFlow app. Enhanced Mode only |
| Device Idle Shutdown | 30 min, 1 h, 2 h, 4 h, 6 h, 12 h, 24 h, Never | Powers the whole unit down after this long with no load connected and no activity. Enhanced Mode only |
| AC 1 Idle Shutdown | same | Switches off AC outlet 1 under the same condition. Enhanced Mode only |
| AC 2 Idle Shutdown | same | Switches off AC outlet 2 under the same condition. Enhanced Mode only |
| 12 V Idle Shutdown | same | Switches off the 12 V DC group under the same condition. Enhanced Mode only |

These five are the EcoFlow app's whole "automatic shutdown" page. **Never means
it never switches off**, on all of them - it is the last option in each list,
with the shortest span first.

### The screen

**There is no way to switch the display off.** The device has no such command,
and the EcoFlow app has no such control either - the shortest timeout is as
close as it gets. Turning the brightness down to zero does not do it: that is a
backlight level and the panel stays lit at the bottom of the range.

### The four idle shutdowns

They are **idle** shutdowns rather than timers: the device only switches an
output off when it detects no load connected and no activity for the configured
span. Something that keeps drawing power keeps its output alive.

Worth knowing before automating them: a load the device does not detect as one -
a trickle charger, or a standby draw below its threshold - looks like nothing
connected, and the output goes away. That is how the device behaves in the app
too, not something this integration adds.

---

## Port priority

Available on units whose serial starts with `D3M`, matching where the EcoFlow
app offers the setting. It needs EcoFlow account sign-in: the device reports
these values on the live connection only.

Each output port is either essential or non-essential. Non-essential ports stop
being powered once the battery falls to their own cutoff level, which leaves
more runtime for the essential ones. The switch is on when a port is
non-essential, which is the wording the wire uses.

The feature only engages when the unit runs on battery or solar with no AC or
grid input and no smart generator or microinverter connected. The **Port
Priority Active** binary sensor reports whether it is currently in effect, so on
a grid-connected unit it stays off - the settings still apply, they are simply
not in play yet.

That sensor turns on as soon as the AC input drops, not when a port is actually
switched off, and the device reports the change within about a second. It is
therefore usable as a fast mains-failure trigger for automations, independently
of whether any port ever reaches its cutoff.

The cutoff range is not fixed. The device derives it from the battery's own
charge and discharge limits, and so does this integration: the lower end is the
discharge limit (capped at 30) plus 5, the upper end is the charge limit (at
least 50) minus 5. With the limits at their defaults of 0 and 100 that gives a
range of 5 % to 95 %, which is what the app's own slider allows even though its
scale is drawn from 0 to 100.

Changing either half of a port's setting sends both, because the device treats
them as one value. The other two ports are never touched by a write. The half
you did not change is taken from what the device last reported, so right after
a restart - before the first status frame arrives - a change is refused with a
note to try again in a moment. Guessing the other half there would overwrite a
setting made in the app.

---

## Notes

- **Standard Mode (~30 s)** delivers all sensors and controls except AC Charge Power. Commands go through the official HTTP endpoint.
- **AC Charge Power needs EcoFlow account sign-in.** The device reports it only on the live connection, never in the polled data, so with developer keys there would be nothing to read the setting back from.
- **Setting a charge power switches the device to the app's custom charging mode**, exactly as moving the slider in the app does. The device treats the wattage and the charging mode as one setting and ignores a change to either on its own, so both always travel together. Switching back to Battery optimised or Silent is done in the app: those modes report themselves only when they change, which is too rarely for an entity to show the truth.
- **Enhanced Mode (~2 s)** delivers the same sensors with the same entity IDs, so switching modes keeps history and dashboards intact. Switches and numbers work here as well: commands travel on the live device connection instead of the HTTP endpoint, and the device confirms each one.
- Energy Dashboard sensors (solar, solar 2, AC input, total output) are integrated from the live power telemetry, since the device exposes no native energy counters. Values accumulate over time and start at 0 on a fresh install.
- Remaining charge and discharge times are only reported while the battery is actually charging or discharging. The device keeps both values populated at all times and parks the inactive one on a placeholder, which would otherwise show a runtime of several hundred hours on an idle unit.
