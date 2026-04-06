# PowerOcean - Entity Reference

Full list of all entities created for PowerOcean devices (HJ31 series).

**Totals:** 202 sensors, 1 number control

> Entities marked with *disabled* are available but hidden by default. Enable them in **Settings > Devices > EcoFlow PowerOcean > Entities** (click the filter icon and show disabled entities).

---

## Sensors - Core Power

| Entity | Unit | Description |
|:---|:---:|:---|
| Solar Power | W | Total solar input |
| Home Power | W | Total home consumption |
| Grid Power | W | Net grid power (positive = import, negative = export) |
| Battery Power | W | Net battery power (positive = charge, negative = discharge) |
| Battery Charge Power | W | Battery charging power (always >= 0) |
| Battery Discharge Power | W | Battery discharging power (always >= 0) |
| Grid Import Power | W | Power drawn from grid (always >= 0) |
| Grid Export Power | W | Power fed to grid (always >= 0) |

## Sensors - Battery

| Entity | Unit | Description |
|:---|:---:|:---|
| Battery SOC | % | State of charge (shown in device header) |
| Battery SOH | % | State of health |
| Battery Cycles | - | Charge cycle count (total increasing) |
| Battery Remaining Capacity | Wh | Remaining energy across all packs |

## Sensors - Energy Dashboard

These sensors are pre-configured for the HA Energy Dashboard (`total_increasing`, kWh).

| Entity | Dashboard Section |
|:---|:---|
| Solar Energy | Solar production |
| Home Energy | Home consumption |
| Grid Import Energy | Grid consumption |
| Grid Export Energy | Return to grid |
| Battery Charge Energy | Battery systems (charge) |
| Battery Discharge Energy | Battery systems (discharge) |

> **Tip:** Select "Two sensors" for battery power in the Energy Dashboard for higher accuracy.

## Sensors - Battery Diagnostics (disabled)

| Entity | Unit | Description |
|:---|:---:|:---|
| Battery Voltage | V | Pack voltage |
| Battery Current | A | Pack current |
| Battery Max Cell Temp | C | Highest cell temperature |
| Battery Min Cell Temp | C | Lowest cell temperature |
| Battery Environment Temp | C | Ambient temperature |
| Battery Max MOSFET Temp | C | Highest MOSFET temperature |
| Battery Cell Max Voltage | mV | Highest cell voltage |
| Battery Cell Min Voltage | mV | Lowest cell voltage |
| Battery Real SOC | % | Internal SOC (before calibration) |
| Battery Real SOH | % | Internal SOH (before calibration) |
| Battery Min SOC Limit | % | Configured minimum discharge limit |
| Battery Max SOC Limit | % | Configured maximum charge limit |

## Sensors - Grid (3-Phase)

| Entity | Unit | Category | Default |
|:---|:---:|:---:|:---:|
| Grid Phase A/B/C Voltage | V | diagnostic | enabled |
| Grid Phase A/B/C Active Power | W | diagnostic | enabled |
| Grid Phase A/B/C Current | A | diagnostic | enabled |
| Grid Phase A/B/C Reactive Power | var | diagnostic | disabled |
| Grid Phase A/B/C Apparent Power | VA | diagnostic | disabled |
| Grid Frequency | Hz | diagnostic | enabled |

## Sensors - MPPT (Solar Strings)

| Entity | Unit | Category |
|:---|:---:|:---:|
| MPPT String 1 Power | W | diagnostic |
| MPPT String 1 Voltage | V | diagnostic |
| MPPT String 1 Current | A | diagnostic |
| MPPT String 2 Power | W | diagnostic |
| MPPT String 2 Voltage | V | diagnostic |
| MPPT String 2 Current | A | diagnostic |
| PV Inverter Power | W | diagnostic (disabled) |

## Sensors - EMS / System (diagnostic)

| Entity | Description | Default |
|:---|:---|:---:|
| EMS Feed Mode | Current feed-in mode | enabled |
| EMS Work Mode | Current operating mode | enabled |
| Grid Status | Grid connection status | enabled |
| Battery Charge/Discharge State | Current battery direction | enabled |
| PCS Running State | Inverter running state | disabled |
| Power Factor | Grid power factor | disabled |
| Feed Power Limit | Max feed-in power | disabled |
| Feed Ratio | Feed-in ratio | disabled |
| EMS Charge Upper Limit | Configured max charge SOC | disabled |
| EMS Discharge Lower Limit | Configured min discharge SOC | disabled |
| EMS Keep SoC | Keep-alive SoC target | disabled |
| EMS Backup Ratio | Backup reserve ratio | disabled |
| MPPT 1/2 Fault Code | MPPT fault indicators | disabled |
| PCS AC/DC Error Code | Inverter error codes | disabled |
| PCS AC Warning Code | Inverter warnings | disabled |
| WiFi / Ethernet / 4G Status | Connectivity status | disabled |
| EMS LED Brightness | LED brightness setting | disabled |
| EMS Work State | Internal work state | disabled |
| Total Battery Capacity | System battery capacity | disabled |
| PCS Max Output/Input Power | Inverter power limits | disabled |
| Battery Max Charge/Discharge Power | Battery power limits | disabled |

## Sensors - Battery Packs (up to 5x BP5000)

Each battery pack creates 24 sensors (7 core + 17 diagnostic). Pack 1 core sensors are enabled by default, all others are disabled.

**Core sensors per pack:**

| Entity | Unit | Description |
|:---|:---:|:---|
| Pack N SoC | % | State of charge |
| Pack N Power | W | Charge/discharge power |
| Pack N SoH | % | State of health |
| Pack N Cycles | - | Charge cycle count |
| Pack N Voltage | V | Pack voltage |
| Pack N Current | A | Pack current |
| Pack N Remaining Capacity | Wh | Remaining energy |

**Diagnostic sensors per pack (all disabled):**

| Entity | Unit |
|:---|:---:|
| Pack N Max/Min Cell Temp | C |
| Pack N Environment Temp | C |
| Pack N Calendar/Cycle SoH | % |
| Pack N Lifetime Charge/Discharge Energy | kWh |
| Pack N Max MOSFET / HV MOSFET / LV MOSFET Temp | C |
| Pack N Bus Voltage | V |
| Pack N PTC Heater Temp | C |
| Pack N Max/Min Cell Voltage | mV |
| Pack N Design/Full Capacity | mAh |
| Pack N Error Code | - |

> **Multi-pack users:** Enable additional pack sensors in the entity list. Each physical BP5000 pack maps to Pack 1, Pack 2, etc.

---

## Number Controls

| Entity | Unit | Range | Step | Mode |
|:---|:---:|:---:|:---:|:---|
| Min Discharge SoC | % | 0 - 30 | 5 | Enhanced only |

---

## Notes

- All power sensors show integers (no decimal places) for clean dashboard display
- Energy sensors show 2 decimal places (0.01 kWh resolution)
- Enhanced Mode (~3s updates) unlocks SoC limit control and faster data
- Standard Mode (~30s polling) provides all sensors except number controls
