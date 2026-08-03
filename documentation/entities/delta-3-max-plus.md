# Delta 3 Max Plus - Entity Reference

Full list of all entities created for Delta 3 devices. The entity set is shared
across the generation: Delta 3 Max Plus (`D3M1`, `P321`) and base DELTA 3 (`P231`).

**Totals:** 47 sensors, 7 switches, 4 numbers

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

---

## Numbers

| Entity | Unit | Range | Step | Description |
|:---|:---:|:---:|:---:|:---|
| Backup Reserve Level | % | 0 - 50 | 1 | Reserve SoC kept for backup |
| Charge Limit | % | 50 - 100 | 1 | Stop charging at this level |
| Discharge Limit | % | 0 - 30 | 1 | Stop discharging at this level |
| AC Charge Power | W | 200 - 2400 | 100 | How fast to charge from the grid, the same setting as the charge speed slider in the EcoFlow app |

---

## Notes

- **Standard Mode (~30 s)** delivers all sensors and controls except AC Charge Power. Commands go through the official HTTP endpoint.
- **AC Charge Power needs EcoFlow account sign-in.** The device reports it only on the live connection, never in the polled data, so with developer keys there would be nothing to read the setting back from.
- **Setting a charge power switches the device to the app's custom charging mode**, exactly as moving the slider in the app does. The device treats the wattage and the charging mode as one setting and ignores a change to either on its own, so both always travel together. Switching back to Battery optimised or Silent is done in the app: those modes report themselves only when they change, which is too rarely for an entity to show the truth.
- **Enhanced Mode (~2 s)** delivers the same sensors with the same entity IDs, so switching modes keeps history and dashboards intact. Switches and numbers work here as well: commands travel on the live device connection instead of the HTTP endpoint, and the device confirms each one.
- Energy Dashboard sensors (solar, solar 2, AC input, total output) are integrated from the live power telemetry, since the device exposes no native energy counters. Values accumulate over time and start at 0 on a fresh install.
- Remaining charge and discharge times are only reported while the battery is actually charging or discharging. The device keeps both values populated at all times and parks the inactive one on a placeholder, which would otherwise show a runtime of several hundred hours on an idle unit.
