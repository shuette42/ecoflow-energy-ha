# PowerOcean - Entity Reference

Full list of all entities created for PowerOcean devices.

**Serial prefixes:** `HJ31`, `HJ32`, `HJ35`, `HJ36`, `HJ37` (standard) · `J32B`, `J327`, `J32D`, `J32E` (European variants) · `R371`, `R372`, `R374`, `HJ3C` (PowerOcean Plus). All share the entity set below.

> **Note:** The European variants (`J327`, `J32D`, `J32E`) and the PowerOcean Plus units (`R371`, `R372`, `R374`, `HJ3C`) are currently not exposed through the EcoFlow Developer API and therefore require Enhanced Mode. In Standard Mode these devices report error 1006 and all entities stay unavailable. Single-phase variants (`J327`, `J32E`) report only the phases that are physically present; the remaining phase entities stay empty. Whether `J32B` can be linked to an API key has not been tested, so Standard Mode is unproven for that prefix.

> **The heating rod readings are optional.** They belong to the PowerGlow
> accessory, so they are only created once your system actually reports a
> heating rod. A PowerOcean without one gets no heating rod entities. Heating
> Rod Power and Heating Rod Water Temperature work in both modes: account
> sign-in receives them on the existing PowerOcean stream, while Standard Mode
> reads them from the API quota. Target Power and Target Temperature are only
> reported by that quota, so they need Standard Mode and developer keys. If you
> ran an earlier 1.16.0 beta, the four entities were created
> on every PowerOcean; leftover ones are removed automatically as soon as your
> system reports data without a heating rod, unless you had enabled them, in
> which case they are kept along with their history.

> **The wallbox readings are optional and need Enhanced Mode.** They belong to
> a PowerPulse wallbox coupled to your PowerOcean, so they are only created once
> your system actually reports a charging session. A PowerOcean without a
> wallbox gets no wallbox entities. Coupled to a PowerOcean the wallbox reports
> its session through the PowerOcean rather than on its own, and that message
> does not reach the Developer API, so it needs the EcoFlow account sign-in. All
> five readings describe the **current charging session** and reset when the next
> one starts, so the session energy is not a meter and is not meant for the
> Energy Dashboard. There are no wallbox controls.

> **PowerOcean Plus** units report more of the same entity set than a standard PowerOcean: per-phase reactive power (var) and apparent power (VA), plus MPPT strings 3 and 4. Those entities are disabled by default, so enable the ones you need after adding a Plus device.

**Totals:** 227 sensors, 5 binary sensors, 2 numbers, 1 select

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
| MPPT String 3 Power | W | diagnostic (disabled) |
| MPPT String 3 Voltage | V | diagnostic (disabled) |
| MPPT String 3 Current | A | diagnostic (disabled) |
| MPPT String 4 Power | W | diagnostic (disabled) |
| MPPT String 4 Voltage | V | diagnostic (disabled) |
| MPPT String 4 Current | A | diagnostic (disabled) |

> **MPPT String 3 and 4** only carry data on PowerOcean Plus units (`R371`, `R372`, `R374`, `HJ3C`), which have more than two PV inputs. They are disabled by default because ordinary PowerOcean units report two strings and would leave these entities empty.
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
| MPPT 1/2 Fault Code | A raw code from the device, passed through unchanged. EcoFlow publishes no meaning for the values, so nothing here translates them, and "fault" is their field name rather than a verdict. An owner tracing one unit found the code following sunrise and sunset with no alert in the EcoFlow app, which points at a producing / not producing state rather than an error. Read it as unlabelled: for automations the power reading of the string itself is the better signal. | disabled |
| PCS AC/DC Error Code | Inverter error codes | disabled |
| PCS AC Warning Code | Inverter warnings | disabled |
| WiFi / Ethernet / 4G Status | Connectivity status | disabled |
| EMS LED Brightness | LED brightness setting | disabled |
| EMS Work State | Internal work state | disabled |
| Total Battery Capacity | System battery capacity | disabled |
| PCS Max Output/Input Power | Inverter power limits | disabled |
| Battery Max Charge/Discharge Power | Battery power limits | disabled |
| Battery Packs Online | Number of packs currently reporting | disabled |
| Battery Packs Online (EMS) | Pack count as reported by the EMS | disabled |
| Heating Rod Power | Power drawn by an attached PowerGlow heating rod, in watts. Reported through the PowerOcean itself, so no separate device is needed. Only created on systems that report a heating rod (see note above). | disabled |
| Heating Rod Water Temperature | Current water temperature at the heating rod, in whole degrees. | disabled |
| Heating Rod Target Power | The power limit configured for the heating rod. Standard Mode only. | disabled |
| Heating Rod Target Temperature | The water temperature the heating rod is set to reach. Standard Mode only. | disabled |
| EMS Max Internal Temp | Highest of the internal temperature probes | enabled |
| MPPT 1/2 Warning Code | MPPT warnings, separate from the fault codes. Unlabelled in the same way, and only reported on the real-time connection. | disabled |
| AFCI Self-Test Result | Result of the arc-fault detector self-test | disabled |
| EMS Self-Check State | Result of the system self-check | disabled |
| System Heating State | Whether the system is heating itself | disabled |
| SoC Calibration State | Whether a battery calibration run is active | disabled |
| Parallel Mode | Parallel-operation topology reported by the unit | disabled |
| Battery Limit Reason | Why the system is limiting the battery | disabled |
| SG Ready State | Current SG Ready operating state | disabled |

## Sensors - PowerPulse Wallbox (accessory, Enhanced Mode)

Created only once a coupled wallbox reports a charging session. See the note at the top.

| Entity | Description | Default |
|---|---|---|
| Wallbox Charging Power | Power currently going into the vehicle, in watts | enabled |
| Wallbox Session Energy | Energy delivered in the current charging session, in watt hours. Resets when the next session starts, so it is not a lifetime counter | enabled |
| Wallbox Session Duration | How long the current session has been running, in seconds | enabled |
| Wallbox Charging Status | Available, preparing, charging, paused by charger, paused by vehicle, finishing, or fault | enabled |
| Wallbox Vehicle | The vehicle the charger has recognized. Empty until a car is identified | enabled |

---

## Binary Sensors (diagnostic)

These come from the Enhanced Mode telemetry only. All are disabled by default,
because they say nothing while the system is healthy.

| Entity | Description | Default |
|:---|:---|:---:|
| AFCI Fault String 1 | Arc fault detected on solar string 1 | disabled |
| AFCI Fault String 2 | Arc fault detected on solar string 2 | disabled |
| Battery Line Disconnected | Battery line reported as disconnected | disabled |
| Battery Relay Fault | Battery relay failed to close | disabled |
| SG Ready Enabled | Whether SG Ready is switched on | disabled |

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

## Controls

| Entity | Type | Range / Options | Mode |
|:---|:---:|:---:|:---|
| Backup Reserve | Number | 0 - 100 % | Enhanced only |
| Solar Surplus Threshold | Number | 0 - 100 % | Enhanced only |
| Work Mode | Select | Self-use / AI Schedule | Enhanced only |

---

## Notes

- All power sensors show integers (no decimal places) for clean dashboard display
- Energy sensors show 2 decimal places (0.01 kWh resolution)
- Enhanced Mode (~3s updates) unlocks SoC limit control and faster data
- Standard Mode (~30s polling) provides all sensors except number controls
