# Stream - Entity Reference

Full list of all entities created for Stream devices.

**Models:** Stream AC Pro (`BK31`), Stream Ultra (`BK11`), Stream Max (`BK41`), Stream AC (`BK51`), Stream Ultra X (`BK61`). All five share the sensor and binary-sensor set below; the hardware-confirmed writable controls are limited to the AC Pro.

**The Stream Micro (`BK01`) does not.** It is a grid-tie inverter with two solar strings and no battery, and it gets a reduced set. See [Stream Micro (BK01)](#stream-micro-bk01) at the end of this page.

**Totals:** 55 sensors, 2 binary sensors, 1 number. Stream AC Pro (`BK31`) adds 3 numbers. Four of the 55 are *accessory* entities (PV 3 and PV 4, power and energy) and exist only on a unit that reports those strings.

> Entities marked with *disabled* are available but hidden by default. Enable them in **Settings > Devices > EcoFlow Stream > Entities** (click the filter icon and show disabled entities).

> **Both modes are supported.** Standard Mode polls the official Developer API (~30 s), Enhanced Mode uses the real-time connection (~3 s). Writable numbers require Enhanced Mode. The difference is solar detail: Standard Mode reports all four strings, Enhanced Mode reports PV 1 and PV 2 plus their input voltage and current and carries no field for strings 3 and 4.

> **Entities marked *accessory* are created only once the device actually reports that reading**, and they appear on their own the moment it does, without a restart. Solar strings 3 and 4 carry that mark: only the larger units drive them, and the real-time connection has no field for them at all, so listing them for everyone would leave most owners with two entities that can never fill.

---

## Sensors - Battery

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Battery SOC | % | - | enabled | State of charge (shown in device header). On two units joined by a parallel cable this is the system figure the app shows for the pair |
| Unit Battery SOC | % | diagnostic | disabled | This unit's own battery state of charge. On a single unit it repeats Battery SOC, which is why it is off by default |
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
| PV 1 Power | W | - | enabled | Solar string 1 |
| PV 2 Power | W | - | enabled | Solar string 2 |
| PV 3 Power | W | diagnostic | *accessory*, disabled | Solar string 3, larger units only, and reported through the Developer API only |
| PV 4 Power | W | diagnostic | *accessory*, disabled | Solar string 4, larger units only, and reported through the Developer API only |
| Solar Power | W | diagnostic | disabled | Total solar input, meter-dependent |
| PV Voltage | V | diagnostic | disabled | Input voltage of string 1 |
| PV Current | A | diagnostic | disabled | Input current of string 1 |
| PV 2 Voltage | V | diagnostic | disabled | Input voltage of string 2 |
| PV 2 Current | A | diagnostic | disabled | Input current of string 2 |

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
| AC Current | A | - | enabled | AC line current |
| AC Frequency | Hz | - | enabled | AC line frequency |
| Grid Connection State | - | diagnostic | enabled | `feed_grid`, `grid_in`, `not_online` or `invalid` |
| Feed-in Power Limit | W | diagnostic | enabled | The feed-in cap configured in the EcoFlow app (read-only here) |
| AC Outlet 1 Power | W | diagnostic | disabled | Power drawn by outlet 1 |
| AC Outlet 2 Power | W | diagnostic | disabled | Power drawn by outlet 2 |
| LED Brightness | % | diagnostic | disabled | Current LED brightness |

## Sensors - Device Diagnostics

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| WiFi Signal | dBm | diagnostic | disabled | Signal strength of the device's WiFi module (negative, closer to zero is better) |

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
| PV 1 Energy to PV 4 Energy | kWh | disabled | Per-string solar production. The counters for strings 3 and 4 follow their power readings: *accessory*, so they exist only on a unit that reports those strings |

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

| Entity | Unit | Range | Step | Models | Description |
|:---|:---:|:---:|:---:|:---|:---|
| Max Charge SoC | % | 3 - 100 | 1 | Stream AC Pro (`BK31`) | Maximum charging SoC. **Enhanced Mode only.** |
| Min Discharge SoC | % | 0 - 95 | 1 | Stream AC Pro (`BK31`) | Minimum discharging SoC. **Enhanced Mode only.** |
| LED Brightness | % | 0 - 100 | 5 | Stream AC Pro (`BK31`) | LED brightness. **Enhanced Mode only.** |
| Backup Reserve | % | 3 - 95 | 1 | All battery Stream models | Minimum SoC the system keeps in reserve. **Enhanced Mode only.** |

The AC Pro holds both limits and Backup Reserve as one grouped setting, so every write carries all three. The two values you did not change are sent back exactly as the device last reported them, never as a default, and a write is refused until all three have arrived. A combination where the limits cross is rejected instead of silently changing another setting.

The capture baseline showed Max Charge SoC 95%, Min Discharge SoC 20% and Backup Reserve 23% in the app, matching raw ConfigWrite fields `33=95`, `34=20` and `102=23`. Both Home Assistant controls were then exercised on the live AC Pro and confirmed in the EcoFlow app and in the telemetry the device sent afterwards. The device's immediate reply can name field `33` even when field `34` was the value that changed, so it is the telemetry that follows, not the reply, that confirms a write.

The LED control reproduces the hardware-confirmed app ConfigWrite field `384` with its `from="ios"` header. The immediate reply carries the requested target, so only the subsequent live telemetry field `994` is treated as the actual brightness state.

---

## Notes

- Every device additionally exposes 2 universal diagnostic sensors (connection status and active mode) that are not included in the totals above.
- Solar strings 3 and 4 are reported by the Developer API in Standard Mode only. In Enhanced Mode those two entities exist but stay empty; PV 1 and PV 2 are reported in both modes.
- Home, grid and total solar values depend on an EcoFlow-compatible meter being paired in the app. Without one they may be absent or misleading, so they ship as disabled diagnostics.
- The Standard Mode values are read as plain watts. If a power reading looks off by a constant factor compared to the EcoFlow app, please report it with a diagnostics download so the scaling can be corrected.
- Switching a device between Standard and Enhanced Mode does not duplicate entities, since both paths produce the same entity keys.

---

## Stream Micro (`BK01`)

The Stream Micro is a grid-tie solar inverter: two solar strings, one single-phase grid connection, no battery and no AC outlets. It speaks the same protocol as the rest of the Stream family and shares this parser, but not the whole entity set.

**Totals:** 21 sensors, no binary sensors, no numbers.

**Enhanced Mode only.** The Stream Micro is not exposed through the EcoFlow Developer API, so it needs the EcoFlow account sign-in.

### What it reports

| Group | Entities |
|:---|:---|
| Solar | PV 1 Power, PV 2 Power (enabled) · PV Voltage, PV Current, PV 2 Voltage, PV 2 Current (disabled diagnostics) |
| Grid | AC Voltage, AC Current, AC Frequency, AC Grid Connection Power (enabled) · Grid Connection Power (disabled diagnostic) |
| Configuration | Grid Connection State, Feed-in Power Limit (enabled diagnostics) |
| Device | WiFi Signal, LED Brightness (disabled diagnostics) |
| Energy Dashboard | PV 1 Energy, PV 2 Energy (disabled by default, enable them for **Solar production**) |

PV 3 and PV 4 are not created here. They are *accessory* entities across the whole Stream family, so a two-string unit never gains them.

### What it deliberately does not get

No battery entities (state of charge, health, power, voltage, temperature, capacity, charge and discharge energy), no backup reserve sensor and no backup reserve number, no AC outlet binary sensors or outlet power, and none of the meter-dependent house flow values (Solar Power, Home Power, Grid Power, Home From Solar / Battery / Grid, Home Energy, Solar Energy).

Home Assistant keeps an entity in the registry once it has been created, even if a later release stops creating it. An entity this device can never fill would therefore sit at "unknown" forever on that installation, so it is not created in the first place.
