# Delta 3 Max Plus - Entity Reference

Full list of all entities created for Delta 3 Max Plus devices (D3M1 series).

**Totals:** 20 sensors, 7 switches, 3 numbers

> Entities marked with *diagnostic* appear in the diagnostics section of the device page.

> **Other Delta 3 models:** only the Delta 3 Max Plus has been checked against real hardware. Other devices of the Delta 3 generation are recognized and should populate the same entities, but individual fields may be missing. Raw quota diagnostics are available to help extend the mapping.

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

---

## Notes

- **Standard Mode (~30 s)** delivers all sensors and controls. Commands go through the official HTTP endpoint.
- **Enhanced Mode (~2 s)** delivers the same sensors with the same entity IDs, so switching modes keeps history and dashboards intact. Switches and numbers work here as well: commands travel on the live device connection instead of the HTTP endpoint, and the device confirms each one.
- No energy (kWh) sensors. The device exposes no native energy counters, so nothing is published to the Energy Dashboard.
- Remaining charge and discharge times are only reported while the battery is actually charging or discharging. The device keeps both values populated at all times and parks the inactive one on a placeholder, which would otherwise show a runtime of several hundred hours on an idle unit.
