# Delta 2 Max - Entity Reference

Full list of all entities created for Delta 2 Max devices (R351/R331 series).

**Totals:** 94 sensors, 4 binary sensors, 7 switches, 8 numbers

> Entities marked with *disabled* are available but hidden by default. Enable them in **Settings > Devices > EcoFlow Delta 2 Max > Entities**.

> **Other Delta models:** Delta Pro, Delta 2, Delta Max, and similar devices should work automatically with this sensor set. Serial numbers starting with R331 use a slightly different command format (handled automatically).

---

## Sensors - Battery

| Entity | Unit | Description |
|:---|:---:|:---|
| SoC | % | State of charge (shown in device header) |
| Battery SoH | % | State of health |
| Precise SoC | % | High-resolution SoC |
| Battery Cycles | - | Charge cycle count (total increasing) |
| Battery Voltage | V | Pack voltage |
| Battery Current | A | Pack current |
| Battery Temp | C | Battery temperature |
| Remaining Capacity | mAh | Remaining capacity (disabled) |
| Full Capacity | mAh | Full charge capacity (disabled) |
| Max Cell Temp / Min Cell Temp | C | Cell temperature range (disabled) |
| Max MOSFET Temp | C | MOSFET temperature (disabled) |
| Max Cell Voltage / Min Cell Voltage | mV | Cell voltage range (disabled) |

## Sensors - Power

| Entity | Unit | Description |
|:---|:---:|:---|
| Input Total | W | Sum of all inputs |
| Output Total | W | Sum of all outputs |
| AC Input | W | AC charging power |
| AC Output | W | AC output power |
| Solar Input | W | MPPT 1 solar power |
| Solar 2 Input | W | MPPT 2 solar power |
| MPPT Output | W | Combined MPPT output |
| 12V Output | W | 12V DC output |
| DC-DC 12V | W | DC-DC converter power |
| Car Output | W | Car charger output |
| USB 1 / USB 2 | W | USB-A ports |
| USB QC 1 / USB QC 2 | W | USB Quick Charge ports |
| Type-C 1 / Type-C 2 | W | USB-C ports |
| AC Charge Rated Power | W | Current AC charge limit |

## Sensors - Voltage

| Entity | Unit | Description |
|:---|:---:|:---|
| AC Output Voltage | V | AC output |
| AC Input Voltage | V | AC input |
| DC Input Voltage | V | Solar/DC input |
| 12V Rail Voltage | V | 12V rail |

## Sensors - Current

| Entity | Unit | Description |
|:---|:---:|:---|
| AC Output Current | A | AC output |
| Solar Current | A | MPPT 1 input |
| Solar 2 Current | A | MPPT 2 input |

## Sensors - Temperature

| Entity | Unit | Description |
|:---|:---:|:---|
| Battery Temp | C | Battery pack |
| Inverter Temp | C | AC inverter |
| DC Input Temp | C | DC input stage |
| MPPT Temp | C | MPPT 1 controller |
| MPPT 2 Temp | C | MPPT 2 controller |

## Sensors - Timing

| Entity | Unit | Description |
|:---|:---:|:---|
| Remaining Time | min | Time to empty/full |
| Charge Time Remaining | min | Time to full charge |
| Discharge Time Remaining | min | Time to empty |

## Sensors - Frequency

| Entity | Unit | Description |
|:---|:---:|:---|
| AC Output Frequency | Hz | AC output |
| AC Input Frequency | Hz | AC input |

## Sensors - State / Diagnostic (disabled)

| Entity | Description |
|:---|:---|
| Fan Level | Current fan speed |
| Charge/Discharge State | Current battery direction |
| EMS Charge State | EMS-level charge state |
| Charger Type | Connected charger type |
| MPPT Charge State | Solar charge state |
| LCD SoC / EMS Precise SoC | Display/internal SoC |
| PD / Inverter / BMS / MPPT Error/Fault Code | Error indicators |

## Sensors - Energy Dashboard

These sensors are pre-configured for the HA Energy Dashboard (`total_increasing`, kWh).

| Entity | Dashboard Section |
|:---|:---|
| Solar Energy | Solar production (MPPT 1) |
| Solar 2 Energy | Solar production (MPPT 2) |
| AC Input Energy | Grid consumption |
| AC Output Energy | Device consumption |

## Sensors - Expansion Battery Packs (disabled)

Two expansion packs (Slave 1, Slave 2), each with 16 sensors. All disabled by default.

| Entity (per pack) | Unit | Description |
|:---|:---:|:---|
| Slave N SoC | % | State of charge |
| Slave N SoH | % | State of health |
| Slave N Voltage | V | Pack voltage |
| Slave N Current | A | Pack current |
| Slave N Temp | C | Pack temperature |
| Slave N Cycles | - | Charge cycles |
| Slave N Input / Output | W | Power in/out |
| Slave N Remaining / Full Capacity | mAh | Capacity |
| Slave N Max/Min Cell Voltage | mV | Cell voltage range |
| Slave N Max/Min Cell Temp | C | Cell temp range |
| Slave N Max MOSFET Temp | C | MOSFET temperature |
| Slave N Error Code | - | Error indicator |

---

## Binary Sensors

| Entity | Description |
|:---|:---|
| AC Enabled | AC output is on |
| DC Output Enabled | DC output is on |
| 12V Enabled | 12V output is on |
| UPS Enabled | UPS mode active (diagnostic, disabled) |

---

## Switches

| Entity | Description |
|:---|:---|
| AC Output | Turn AC output on/off |
| DC Output | Turn DC output on/off |
| 12V Output | Turn 12V output on/off |
| Beeper | Enable/disable beeper |
| X-Boost | Enable/disable X-Boost (higher AC output) |
| AC Auto Restart | Auto-restore AC on power recovery |
| Backup Reserve | Enable/disable backup reserve mode |

---

## Numbers

| Entity | Unit | Range | Step | Description |
|:---|:---:|:---:|:---:|:---|
| AC Charge Speed | W | 200 - 2400 | 100 | AC charging power limit |
| Max Charge SoC | % | 50 - 100 | 1 | Stop charging at this level |
| Min Discharge SoC | % | 0 - 30 | 1 | Stop discharging at this level |
| Standby Timeout | min | 0 - 720 | 1 | Auto-off delay (0 = never) |
| 12V Port Timeout | min | 0 - 720 | 30 | 12V auto-off delay |
| Screen Brightness | % | 0 - 100 | 10 | Display brightness |
| Screen Timeout | s | 0 - 1800 | 10 | Display auto-off delay |
| Backup Reserve Level | % | 5 - 100 | 5 | Reserve SoC for backup mode |

---

## Notes

- Both Standard Mode (~30s) and Enhanced Mode (~2s) provide all sensors
- Enhanced Mode adds real-time MQTT push for faster updates
- Standard Mode also receives MQTT push data alongside HTTP polling
- All switches and numbers work in both modes
- Delta devices with R331 serial numbers use a slightly different command protocol (detected automatically)
