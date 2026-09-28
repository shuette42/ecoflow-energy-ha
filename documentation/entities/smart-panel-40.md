# OCEAN Smart Electrical Panel 40 - Entity Reference

Full list of all entities created for the EcoFlow OCEAN Smart Electrical Panel 40.

**Totals:** 19 panel sensors, plus 3 sensors and 1 binary sensor per circuit (up to 40)

The panel reports through the account connection, so it needs **Enhanced Mode**. The only data on record came over that connection; what the Developer API returns for this device has not been observed, so Standard Mode does not offer it.

Serial prefix `HR61`. The panel is a US split-phase unit (120/240 V): the grid side is read per leg, L1 and L2.

Read-only. No write from the panel has been observed with a known effect, so there are no controls.

There are no energy counters. None appear in what the panel sends, and this integration does not integrate power into energy for it.

Mapped from one owner's recording of a live installation on #434. Grid, solar and battery power close the panel's own power balance on every frame checked, and the grid legs add up to the grid total. The battery's charging direction has not been seen from the panel yet, only discharging and idle.

---

## Sensors

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Grid Power | W | - | enabled | Signed: positive draws from the grid, negative feeds into it |
| Home Power | W | - | enabled | Calculated by the panel from grid, solar and battery power, not a separate measurement |
| Solar Power | W | - | enabled | Total solar power of the connected source |
| Battery Power | W | - | enabled | Signed: positive charges the battery, negative discharges it |
| Battery SOC | % | - | enabled | Charge of the connected battery |
| Grid L1 Voltage | V | - | enabled | Leg voltage |
| Grid L2 Voltage | V | - | enabled | Leg voltage |
| Grid L1 Current | A | - | enabled | Leg current |
| Grid L2 Current | A | - | enabled | Leg current |
| Grid L1 Power | W | - | enabled | Leg active power, signed like Grid Power |
| Grid L2 Power | W | - | enabled | Leg active power, signed like Grid Power |
| Grid L1 Apparent Power | VA | diagnostic | disabled | Leg apparent power |
| Grid L2 Apparent Power | VA | diagnostic | disabled | Leg apparent power |
| Grid L1 Reactive Power | var | diagnostic | disabled | Leg reactive power |
| Grid L2 Reactive Power | var | diagnostic | disabled | Leg reactive power |
| Backup Reserve | % | diagnostic | disabled | Battery level kept back for a grid outage |
| Grid Nominal Voltage | V | diagnostic | disabled | The grid voltage the panel is configured for, not a measurement |
| Grid Nominal Frequency | Hz | diagnostic | disabled | The grid frequency the panel is configured for, not a measurement |
| Grid Code | - | diagnostic | disabled | Grid-code setting as the panel reports it |

## Sensors - Circuits (up to 40)

Each circuit creates 3 sensors (1 enabled by default, 2 disabled). They are created once the panel reports the circuit and its name, which a full status report does for every circuit the panel has.

The circuit's name from the EcoFlow app follows its number in the entity name, for example "Circuit 12 Oven Power". The name is read when the entity is created; a name changed later in the app shows after the next reload of the integration. A two-pole breaker appears as two circuits, one per leg, each with its own readings.

**Enabled by default:**

| Entity | Unit | Description |
|:---|:---:|:---|
| Circuit N Power | W | Signed from the circuit's side: positive while it draws power, negative while it feeds power into the panel |

**Disabled by default:**

| Entity | Unit |
|:---|:---:|
| Circuit N Voltage | V |
| Circuit N Current | A |

Current is reported in whole amps only and reads 0 below 1 A.

## Binary Sensors - Circuits (up to 40)

| Entity | Category | Default | Description |
|:---|:---:|:---:|:---|
| Circuit N On | - | enabled | Whether the circuit's breaker is closed, as the panel reports it. State only, no switching |
