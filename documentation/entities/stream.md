# Stream - Entity Reference

Full list of all entities created for Stream devices.

**Models:** Stream AC Pro (`BK31`), Stream Ultra (`BK11`), Stream Max (`BK41`), Stream AC (`BK51`), Stream Ultra X (`BK61`). All five share the entity set below.

**Totals:** 47 sensors, 2 binary sensors, 1 number

> Entities marked with *disabled* are available but hidden by default. Enable them in **Settings > Devices > EcoFlow Stream > Entities** (click the filter icon and show disabled entities).

> **Both modes are supported.** Standard Mode polls the official Developer API (~30 s), Enhanced Mode uses the real-time connection (~3 s). Both create the same entities. The difference is solar detail: only Standard Mode breaks solar down per string (PV 1-4). Enhanced Mode reports the solar total only.

---

## Sensors - Battery

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Battery SOC | % | - | enabled | State of charge (shown in device header) |
| Battery SOC (Precise) | % | diagnostic | disabled | High-resolution SoC |
| Battery SoH | % | - | enabled | State of health |
| Battery Power | W | - | enabled | Signed battery power (positive = charging, negative = discharging) |
| Battery Charge Power | W | - | enabled | Charging power (always >= 0) |
| Battery Discharge Power | W | - | enabled | Discharging power (always >= 0) |
| Battery Voltage | V | - | enabled | Pack voltage |
| Battery Temp | C | - | enabled | Battery temperature |
| Backup Reserve | % | diagnostic | enabled | Current reserve level (mirrors the number) |
| Battery Charge/Discharge State | - | diagnostic | disabled | `standby`, `charging` or `discharging` |

## Sensors - Solar

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| PV 1 Power | W | - | enabled | Solar string 1 (Standard Mode only) |
| PV 2 Power | W | - | enabled | Solar string 2 (Standard Mode only) |
| PV 3 Power | W | diagnostic | disabled | Solar string 3, larger units only (Standard Mode only) |
| PV 4 Power | W | diagnostic | disabled | Solar string 4, larger units only (Standard Mode only) |
| Solar Power | W | diagnostic | disabled | Total solar input, meter-dependent |
| PV Voltage | V | diagnostic | disabled | Summed PV input voltage |

## Sensors - Power Flow

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| AC Grid Connection Power | W | - | enabled | Signed grid connection ("Netz-Anschluss": negative = input, positive = output/feed-in) |
| Home Power | W | diagnostic | disabled | Total home consumption, meter-dependent |
| Grid Power | W | diagnostic | disabled | Net grid power, meter-dependent |
| Home From Solar | W | diagnostic | disabled | Home consumption covered by solar |
| Home From Battery | W | diagnostic | disabled | Home consumption covered by the battery |
| Home From Grid | W | diagnostic | disabled | Home consumption covered by the grid |
| Grid Connection Power | W | diagnostic | disabled | Raw grid connection reading |
| System Grid Connection Power | W | diagnostic | disabled | System-level grid connection reading |

## Sensors - AC Output

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| AC Voltage | V | - | enabled | AC line voltage |
| AC Frequency | Hz | - | enabled | AC line frequency |
| AC Outlet 1 Power | W | diagnostic | disabled | Power drawn by outlet 1 |
| AC Outlet 2 Power | W | diagnostic | disabled | Power drawn by outlet 2 |
| LED Brightness | % | diagnostic | disabled | Current LED brightness |

## Sensors - Battery Diagnostics

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Design Capacity | mAh | diagnostic | disabled | Nominal pack capacity |
| Remaining Capacity | mAh | - | disabled | Remaining capacity |
| Full Capacity | mAh | - | disabled | Full charge capacity |
| Max Cell Temp / Min Cell Temp | C | diagnostic | disabled | Cell temperature range |
| Max MOSFET Temp | C | diagnostic | disabled | MOSFET temperature |
| Max Cell Voltage / Min Cell Voltage | mV | diagnostic | disabled | Cell voltage range |

## Sensors - Energy Dashboard

| Entity | Unit | Default | Dashboard Section |
|:---|:---:|:---:|:---|
| Battery Charge Energy | kWh | enabled | Battery charge |
| Battery Discharge Energy | kWh | enabled | Battery discharge |
| Solar Energy | kWh | disabled | Solar production (needs a paired meter) |
| Home Energy | kWh | disabled | Home consumption (needs a paired meter) |
| PV 1 Energy to PV 4 Energy | kWh | disabled | Per-string solar production (Standard Mode only) |

Also available as disabled diagnostics: **Battery Charge Capacity** and **Battery Discharge Capacity** (Ah). These are cumulative charge counters, not energy, and are not suitable for the Energy Dashboard.

> The Stream is modeled as an AC-coupled battery. For normal use select the two battery energy sensors. Solar and home energy only report meaningful values when an EcoFlow-compatible meter is paired in the app, which is why they ship disabled.

---

## Binary Sensors

| Entity | Default | Description |
|:---|:---:|:---|
| AC Outlet 1 | enabled | Outlet 1 on/off state (read-only) |
| AC Outlet 2 | enabled | Outlet 2 on/off state (read-only) |

---

## Switches

None. The AC outlets are exposed read-only as binary sensors, because the write path for third-party control is not confirmed.

---

## Numbers

| Entity | Unit | Range | Step | Description |
|:---|:---:|:---:|:---:|:---|
| Backup Reserve | % | 3 - 95 | 1 | Minimum SoC the system keeps in reserve. **Enhanced Mode only.** |

---

## Notes

- Every device additionally exposes 2 universal diagnostic sensors (connection status and active mode) that are not included in the totals above.
- Per-string solar (PV 1-4) is reported by the Developer API in Standard Mode only. In Enhanced Mode these entities exist but stay empty, and solar is reported as a single total.
- Home, grid and total solar values depend on an EcoFlow-compatible meter being paired in the app. Without one they may be absent or misleading, so they ship as disabled diagnostics.
- The Standard Mode values are read as plain watts. If a power reading looks off by a constant factor compared to the EcoFlow app, please report it with a diagnostics download so the scaling can be corrected.
- Switching a device between Standard and Enhanced Mode does not duplicate entities, since both paths produce the same entity keys.
