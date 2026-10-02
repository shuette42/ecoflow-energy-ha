# DELTA Pro Ultra - Entity Reference

Full list of all entities created for the EcoFlow DELTA Pro Ultra.

**Totals:** 21 sensors, plus 2 sensors per battery pack (up to 5)

The DELTA Pro Ultra reports through the account connection, so it needs **Enhanced Mode**. The only data on record came over that connection, and Standard Mode does not offer the device.

Serial prefix `Y711`.

Read-only. There are no controls yet.

There are no energy counters. None appear in what the unit sends, and the integration does not derive energy from power for it.

Mapped from one owner's two diagnostics downloads on #464. The unit was idle while they were recorded, so every power reading was 0. Battery level, voltage and temperatures were cross-checked against the battery packs' own readings. The power readings are mapped by their names in the unit's messages and still need a check under load.

---

## Sensors

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Battery SOC | % | - | enabled | Charge of the whole system |
| Remaining Time | min | - | enabled | Remaining time as the unit reports it |
| Input Total | W | - | enabled | Total input power |
| Output Total | W | - | enabled | Total output power |
| AC Input Power | W | - | enabled | AC charging input |
| Power In/Out Port Input | W | - | enabled | Power flowing into the unit through the Power In/Out port |
| Power In/Out Port Output | W | - | enabled | Power flowing out of the unit through the Power In/Out port |
| Solar LV Input Power | W | - | enabled | Low-voltage solar input |
| Solar HV Input Power | W | - | enabled | High-voltage solar input |
| AC Output L1-1 Power | W | - | enabled | AC output L1-1, as the unit names it |
| AC Output L1-2 Power | W | - | enabled | AC output L1-2, as the unit names it |
| AC Output L2-1 Power | W | - | enabled | AC output L2-1, as the unit names it |
| AC Output L2-2 Power | W | - | enabled | AC output L2-2, as the unit names it |
| AC Output TT-30 Power | W | - | enabled | TT-30 output |
| AC Output L14-30 Power | W | - | enabled | L14-30 output |
| Battery Voltage | V | - | enabled | Battery voltage |
| Battery Charge Power | W | - | enabled | Power going into the batteries |
| Battery Discharge Power | W | - | enabled | Power coming out of the batteries |
| Inverter Temperature | °C | - | enabled | AC inverter temperature |
| System Temperature | °C | - | enabled | Main board temperature |
| Backup Reserve | % | diagnostic | enabled | Battery level kept back for a grid outage |

## Sensors - Battery Packs (up to 5)

Each pack creates 2 sensors. They are created once a pack actually reports, so a system with three packs gets three sets and nothing else.

| Entity | Unit | Description |
|:---|:---:|:---|
| Battery Pack N Level | % | Charge of this pack |
| Battery Pack N Temperature | °C | Temperature of this pack |
