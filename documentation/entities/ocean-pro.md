# OCEAN Pro - Entity Reference

Full list of all entities created for the EcoFlow OCEAN Pro (inverter and battery, the E7 P2S in the EcoFlow app).

**Totals:** 30 sensors (19 enabled by default, 11 diagnostic and disabled by default)

The unit reports through the account connection, so it needs **Enhanced Mode**. The only data on record came over that connection. What the Developer API returns for this device has not been observed, so Standard Mode does not offer it.

Serial prefix `HR51`. The inverter is a US split-phase unit: its output is read per leg, L1 and L2. In the installations recorded so far, the unit sits behind an OCEAN Smart Electrical Panel 40 (`HR61`). The panel has its own page: [OCEAN Smart Electrical Panel 40](smart-panel-40.md). A home with both reads the five system readings from the same fields on both device pages.

Read-only. No command to the unit with a known effect has been recorded, so there are no controls.

Mapped from two diagnostics downloads of one owner's installation on #427. That installation has the Smart Electrical Panel 40.

---

## Which entity is the battery level

**Battery SOC** is the charge level of the system as the unit reports it. It is read from the same field as Battery SOC on the panel page. The battery pack reports its own charge as well. That reading is **Battery Pack SOC**, a diagnostic that is disabled by default, and it does not have to equal Battery SOC. Use Battery SOC for dashboards and automations.

## Grid Power is confirmed with the panel only

**Grid Power** is confirmed only on an installation that has an OCEAN Smart Electrical Panel 40. The unit's own grid meter readings carried only a placeholder value in both recordings, so Grid Power is taken from the system report instead. On a unit installed without the panel, Grid Power has not been seen yet.

## Sensors

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Grid Power | W | - | enabled | Signed: positive draws from the grid, negative feeds into it. Confirmed only with the panel, see the note above |
| Home Power | W | - | enabled | Total load of the house as the unit reports it |
| Solar Power | W | - | enabled | Total solar power of all strings |
| Battery Power | W | - | enabled | Signed: positive charges the battery, negative discharges it |
| Battery SOC | % | - | enabled | Charge level of the system, see above |
| PV String 1 Power | W | - | enabled | Power of the first solar string |
| PV String 2 Power | W | - | enabled | Power of the second solar string |
| PV String 3 Power | W | - | enabled | Power of the third solar string |
| PV String 4 Power | W | - | enabled | Power of the fourth solar string |
| PV String 5 Power | W | - | enabled | Power of the fifth solar string |
| Inverter L1 Voltage | V | - | enabled | Output voltage of leg L1 |
| Inverter L1 Current | A | - | enabled | Output current of leg L1 |
| Inverter L1 Power | W | - | enabled | Output power of leg L1, signed as the unit reports it: negative while the inverter delivers |
| Inverter L2 Voltage | V | - | enabled | Output voltage of leg L2 |
| Inverter L2 Current | A | - | enabled | Output current of leg L2 |
| Inverter L2 Power | W | - | enabled | Output power of leg L2, signed like leg L1 |
| Battery Remaining Energy | Wh | - | enabled | Energy stored in the battery |
| Battery Charge Energy | kWh | - | enabled | Lifetime energy charged into the battery, a total that only grows |
| Battery Discharge Energy | kWh | - | enabled | Lifetime energy taken out of the battery, a total that only grows |
| Backup Reserve | % | diagnostic | disabled | Battery level kept back for a grid outage |
| Battery Pack SOC | % | diagnostic | disabled | The battery pack's own charge reading, separate from Battery SOC |
| Battery SOH | % | diagnostic | disabled | Battery state of health |
| Battery Cycles | - | diagnostic | disabled | Charge cycles the battery has completed |
| Grid Frequency | Hz | diagnostic | disabled | Grid frequency measured by the unit |
| Inverter Temp | °C | diagnostic | disabled | Temperature of the inverter |
| Device Temperature 1 | °C | diagnostic | disabled | First of five temperature readings of the unit, in the order it reports them |
| Device Temperature 2 | °C | diagnostic | disabled | Second temperature reading |
| Device Temperature 3 | °C | diagnostic | disabled | Third temperature reading |
| Device Temperature 4 | °C | diagnostic | disabled | Fourth temperature reading |
| Device Temperature 5 | °C | diagnostic | disabled | Fifth temperature reading |

The unit does not say which part of the hardware each of the five Device Temperature readings belongs to, so they are numbered and not named.

## Energy Dashboard

Battery Charge Energy and Battery Discharge Energy are lifetime totals and work as the battery energy in the Energy Dashboard. There are no solar, grid or home energy counters: none appear in what the unit sends, and the integration does not calculate energy from power for this unit. Solar Power, Grid Power and Home Power are power readings only.
