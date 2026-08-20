# PowerStream - Entity Reference

Full list of all entities created for PowerStream microinverters (HW51 series).

**Totals:** 25 sensors

> Entities marked with *disabled* are available but hidden by default. Enable them in **Settings > Devices > EcoFlow PowerStream > Entities**.

The PowerStream is read-only in this integration. It reports through the official EcoFlow Developer API, so it needs **Standard Mode** with developer keys; there is no Enhanced Mode path for it.

---

## Sensors

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Solar Power | W | - | enabled | Total PV production, the sum of the two strings |
| PV 1 Power | W | - | enabled | Solar string 1 |
| PV 2 Power | W | - | enabled | Solar string 2 |
| Inverter Output Power | W | - | enabled | AC power leaving the inverter |
| Grid Power | W | - | enabled | Signed, negative while the unit feeds into the grid |
| Smart Plug Load Power | W | diagnostic | disabled | Power taken by the smart plugs attached to the inverter |
| Battery SOC | % | - | enabled | State of charge of the attached battery |
| Battery Power | W | - | enabled | Positive while charging, see the note below |
| Battery Voltage | V | diagnostic | enabled | Battery operating voltage |
| Battery Temp | C | diagnostic | enabled | Battery temperature |
| Custom Load Power | W | diagnostic | enabled | The constant output set in the EcoFlow app |
| Discharge Limit | % | diagnostic | enabled | Lower state-of-charge limit set in the app |
| Charge Limit | % | diagnostic | enabled | Upper state-of-charge limit set in the app |
| Power Supply Priority | - | diagnostic | enabled | Whether the unit prioritizes supplying power or charging the battery |
| LED Brightness | % | diagnostic | disabled | Indicator brightness |
| Rated Power | W | diagnostic | disabled | The model's rated output, e.g. 800 W |
| AC Voltage | V | diagnostic | disabled | Grid voltage at the inverter output |
| AC Frequency | Hz | diagnostic | disabled | Grid frequency |
| PV 1 Voltage | V | diagnostic | disabled | Solar string 1 input voltage |
| PV 2 Voltage | V | diagnostic | disabled | Solar string 2 input voltage |
| WiFi Signal | dBm | diagnostic | disabled | WiFi signal strength |

## Sensors - Energy Dashboard

| Entity | Default | Dashboard Section |
|:---|:---:|:---|
| Solar Energy | enabled | Solar production |
| Inverter Output Energy | enabled | Individual devices, or solar production if the unit feeds the house directly |
| PV 1 Energy | disabled | Solar production, per string |
| PV 2 Energy | disabled | Solar production, per string |

> The per-string counters are off by default because **Solar Energy** already covers the PV total. Enable them only if you want each string tracked separately, and do not add both the total and the strings to the same dashboard section.

All four are integrated from the live power readings. The device sends lifetime counters of its own, but every one of them read zero in the recording this support was built from, so nothing is known about them yet.

---

## Controls

None. The device documents five settings that can be written - custom load power, supply priority, the two state-of-charge limits and the indicator brightness - and all five are readable above, but writing them is not implemented. Nothing here has been confirmed on a PowerStream, and a control that silently does nothing is worse than no control at all. If you own one and want to help, say so in [issue #230](https://github.com/shuette42/ecoflow-energy-ha/issues/230).

---

## Notes

**Battery Power sign.** Positive means charging. The battery was idle throughout the recording this support was built from, so that direction follows the name EcoFlow gives the field rather than an observed value. It is deliberately kept out of the energy counters until an owner confirms it: a lifetime counter that has been fed a wrong sign cannot be corrected afterwards.

**Grid Power has no import/export split.** The reading is signed and negative while the unit exports, which is what the recording shows. What a positive value would mean on this device has not been observed, so there is one signed reading rather than two counters built on a guess.

**Readings that are deliberately missing.** The device reports currents, remaining charge and discharge times, per-subsystem status codes and lifetime counters. None of them became entities: the currents do not reconcile with the power and voltage on the same reading, both remaining times stay populated while the battery is idle, the status codes have values but no known meaning, and the lifetime counters all read zero. A second recording from a unit with a working battery is what settles them.
