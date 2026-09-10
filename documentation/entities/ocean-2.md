# Ocean 2 - Entity Reference

Full list of all entities created for the EcoFlow Ocean 2 home battery (RE11 and RE17 series).

**Totals:** 93 sensors

> **Enhanced Mode only.** The Ocean 2 reports through the account connection, not through the EcoFlow Developer API. A Standard Mode setup gets error 1006 and no entities fill; set the integration up with an EcoFlow account e-mail and password instead.

The two serial prefixes are the 10 kW (`RE11`) and the 12 kW (`RE17`) rating of one product with one message family, so both get the same device type, the same name and the same entities. Every reading below was checked against a seven-hour recording from one owner's RE11; no RE17 recording is on file yet, so an RE17 owner's first report is the check on that assumption.

Where a reading means the same thing as on a PowerOcean, the entity is the same entity: same name, same unit, same class, so a dashboard or an Energy Dashboard configuration built for one carries over to the other.

Read-only. The integration does not send commands to the Ocean 2; not one write to this device has been observed on the wire, so there is nothing to build a control on.

---

## Sensors

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Solar Power | W | - | enabled | Power from all PV strings together |
| Home Power | W | - | enabled | Power the house is drawing, as the device reports it |
| Grid Power | W | - | enabled | Power at the grid connection, positive when importing |
| Battery Power | W | - | enabled | Power into or out of the battery, positive when charging |
| Battery Charge Power | W | - | enabled | Charging part of Battery Power, 0 W while discharging |
| Battery Discharge Power | W | - | enabled | Discharging part of Battery Power, 0 W while charging |
| Grid Import Power | W | - | enabled | Import part of Grid Power, 0 W while exporting |
| Grid Export Power | W | - | enabled | Export part of Grid Power, 0 W while importing |
| Battery SOC | % | - | enabled | State of charge of the whole battery |
| Battery Remaining Capacity | Wh | - | enabled | Energy left in the whole battery; the sum of the pack readings below |
| Inverter AC Power | W | - | enabled | Total AC output of the inverter, unrounded |
| Grid Frequency | Hz | diagnostic | enabled | Frequency at the grid connection |
| Solar Energy | kWh | - | enabled | Solar Power integrated over time |
| Home Energy | kWh | - | enabled | Home Power integrated over time |
| Grid Import Energy | kWh | - | enabled | Grid Import Power integrated over time |
| Grid Export Energy | kWh | - | enabled | Grid Export Power integrated over time |
| Battery Charge Energy | kWh | - | enabled | Battery Charge Power integrated over time |
| Battery Discharge Energy | kWh | - | enabled | Battery Discharge Power integrated over time |
| Grid Phase A Voltage | V | diagnostic | enabled | Voltage on phase A at the grid connection |
| Grid Phase A Current | A | diagnostic | enabled | Current on phase A at the grid connection |
| Grid Phase A Active Power | W | diagnostic | enabled | Active power on phase A at the grid connection |
| Grid Phase B Voltage | V | diagnostic | enabled | Voltage on phase B at the grid connection |
| Grid Phase B Current | A | diagnostic | enabled | Current on phase B at the grid connection |
| Grid Phase B Active Power | W | diagnostic | enabled | Active power on phase B at the grid connection |
| Grid Phase C Voltage | V | diagnostic | enabled | Voltage on phase C at the grid connection |
| Grid Phase C Current | A | diagnostic | enabled | Current on phase C at the grid connection |
| Grid Phase C Active Power | W | diagnostic | enabled | Active power on phase C at the grid connection |
| Inverter Phase A Voltage | V | diagnostic | disabled | Voltage on phase A at the inverter output |
| Inverter Phase A Current | A | diagnostic | disabled | Current on phase A at the inverter output |
| Inverter Phase A Active Power | W | diagnostic | disabled | Active power on phase A at the inverter output |
| Inverter Phase A Reactive Power | var | diagnostic | disabled | Reactive power on phase A at the inverter output |
| Inverter Phase A Apparent Power | VA | diagnostic | disabled | Apparent power on phase A at the inverter output |
| Inverter Phase B Voltage | V | diagnostic | disabled | Voltage on phase B at the inverter output |
| Inverter Phase B Current | A | diagnostic | disabled | Current on phase B at the inverter output |
| Inverter Phase B Active Power | W | diagnostic | disabled | Active power on phase B at the inverter output |
| Inverter Phase B Reactive Power | var | diagnostic | disabled | Reactive power on phase B at the inverter output |
| Inverter Phase B Apparent Power | VA | diagnostic | disabled | Apparent power on phase B at the inverter output |
| Inverter Phase C Voltage | V | diagnostic | disabled | Voltage on phase C at the inverter output |
| Inverter Phase C Current | A | diagnostic | disabled | Current on phase C at the inverter output |
| Inverter Phase C Active Power | W | diagnostic | disabled | Active power on phase C at the inverter output |
| Inverter Phase C Reactive Power | var | diagnostic | disabled | Reactive power on phase C at the inverter output |
| Inverter Phase C Apparent Power | VA | diagnostic | disabled | Apparent power on phase C at the inverter output |
| MPPT String 1 Voltage | V | diagnostic | enabled | Voltage of PV string 1 |
| MPPT String 1 Current | A | diagnostic | enabled | Current of PV string 1 |
| MPPT String 1 Power | W | diagnostic | enabled | Power of PV string 1 |
| MPPT String 2 Voltage | V | diagnostic | enabled | Voltage of PV string 2 |
| MPPT String 2 Current | A | diagnostic | enabled | Current of PV string 2 |
| MPPT String 2 Power | W | diagnostic | enabled | Power of PV string 2 |
| MPPT String 3 Voltage | V | diagnostic | disabled | Voltage of PV string 3, see the note below |
| MPPT String 3 Current | A | diagnostic | disabled | Current of PV string 3 |
| MPPT String 3 Power | W | diagnostic | disabled | Power of PV string 3 |
| Pack 1 SoC | % | - | enabled | State of charge of battery module 1 |
| Pack 1 Power | W | - | enabled | Power into or out of module 1, positive when charging |
| Pack 1 SoH | % | - | enabled | State of health of module 1 |
| Pack 1 Cycles | - | - | enabled | Charge cycles module 1 has completed |
| Pack 1 Remaining Capacity | Wh | - | enabled | Energy left in module 1 |
| Pack 1 Max Cell Temp | °C | diagnostic | disabled | The temperature module 1 reports |
| Pack 1 Max Cell Voltage | mV | diagnostic | disabled | Highest cell voltage in module 1 |
| Pack 2 SoC | % | - | enabled | State of charge of battery module 2 |
| Pack 2 Power | W | - | enabled | Power into or out of module 2, positive when charging |
| Pack 2 SoH | % | - | enabled | State of health of module 2 |
| Pack 2 Cycles | - | - | enabled | Charge cycles module 2 has completed |
| Pack 2 Remaining Capacity | Wh | - | enabled | Energy left in module 2 |
| Pack 2 Max Cell Temp | °C | diagnostic | disabled | The temperature module 2 reports |
| Pack 2 Max Cell Voltage | mV | diagnostic | disabled | Highest cell voltage in module 2 |
| Pack 3 SoC | % | - | disabled | State of charge of battery module 3, see the note below |
| Pack 3 Power | W | - | disabled | Power into or out of module 3, positive when charging |
| Pack 3 SoH | % | - | disabled | State of health of module 3 |
| Pack 3 Cycles | - | - | disabled | Charge cycles module 3 has completed |
| Pack 3 Remaining Capacity | Wh | - | disabled | Energy left in module 3 |
| Pack 3 Max Cell Temp | °C | diagnostic | disabled | The temperature module 3 reports |
| Pack 3 Max Cell Voltage | mV | diagnostic | disabled | Highest cell voltage in module 3 |
| Pack 4 SoC | % | - | disabled | State of charge of battery module 4 |
| Pack 4 Power | W | - | disabled | Power into or out of module 4, positive when charging |
| Pack 4 SoH | % | - | disabled | State of health of module 4 |
| Pack 4 Cycles | - | - | disabled | Charge cycles module 4 has completed |
| Pack 4 Remaining Capacity | Wh | - | disabled | Energy left in module 4 |
| Pack 4 Max Cell Temp | °C | diagnostic | disabled | The temperature module 4 reports |
| Pack 4 Max Cell Voltage | mV | diagnostic | disabled | Highest cell voltage in module 4 |
| Pack 5 SoC | % | - | disabled | State of charge of battery module 5 |
| Pack 5 Power | W | - | disabled | Power into or out of module 5, positive when charging |
| Pack 5 SoH | % | - | disabled | State of health of module 5 |
| Pack 5 Cycles | - | - | disabled | Charge cycles module 5 has completed |
| Pack 5 Remaining Capacity | Wh | - | disabled | Energy left in module 5 |
| Pack 5 Max Cell Temp | °C | diagnostic | disabled | The temperature module 5 reports |
| Pack 5 Max Cell Voltage | mV | diagnostic | disabled | Highest cell voltage in module 5 |
| Pack 6 SoC | % | - | disabled | State of charge of battery module 6 |
| Pack 6 Power | W | - | disabled | Power into or out of module 6, positive when charging |
| Pack 6 SoH | % | - | disabled | State of health of module 6 |
| Pack 6 Cycles | - | - | disabled | Charge cycles module 6 has completed |
| Pack 6 Remaining Capacity | Wh | - | disabled | Energy left in module 6 |
| Pack 6 Max Cell Temp | °C | diagnostic | disabled | The temperature module 6 reports |
| Pack 6 Max Cell Voltage | mV | diagnostic | disabled | Highest cell voltage in module 6 |

---

## Controls

None. The Ocean 2 reports; this integration does not send it commands.

---

## Notes

### Two kinds of report

The Ocean 2 pushes small updates every few seconds, each carrying only the readings that changed, sometimes just the home and battery power. The complete picture, with the inverter phases, the PV strings and the grid connection, only arrives as the answer to a request, which the integration sends every 30 seconds. So the four power readings move within seconds, and the diagnostic readings follow within half a minute.

### The battery module readings come from their own report

Pack readings arrive in a separate report the device sends every few seconds for each module in turn. The report is a backlog of module heartbeats rather than a list of modules, so the integration keys each module on its index and keeps the newest heartbeat per module. Two modules are on file; packs 3 to 6 exist so a larger installation fills them in, and they stay disabled until enabled by hand.

### PV string 3

The one recording on file has a third PV string connector with nothing on it: 33 to 36 V open-circuit voltage, 0 A, 0 W throughout. Its three entities are therefore disabled by default. An installation with three strings enables them.

### The feed-in limit is not a sensor

The device's reports carry the configured feed-in limit next to the readings. That figure is the app's own setting echoed back, not a measurement, so no entity is built on it.
