# Smart Home Panel 2 - Entity Reference

Full list of all entities created for the EcoFlow Smart Home Panel 2.

**Totals:** 9 panel sensors, plus 2 sensors per circuit (up to 12) and 1 sensor per storage channel (up to 3)

The panel reports through the account connection, so it needs **Enhanced Mode**. The only data on record came over that connection, and Standard Mode does not offer the device.

Serial prefix `HD31`. The panel is a US split-phase unit, so the grid current is read per leg, L1 and L2.

Read-only. There are no controls.

There are no energy counters. None appear in what the panel sends, and the integration does not derive energy from power for it.

Mapped from one owner's two diagnostics downloads on #464. The circuits add up to the home power within 1.5 W on every frame checked, and the grid leg currents match the circuit currents. The battery behind the panel was idle while they were recorded, so the storage channel's charging and discharging power is not read yet.

---

## Sensors

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Grid Power | W | - | enabled | Power drawn from the grid |
| Home Power | W | - | enabled | Total power of the circuits behind the panel |
| Grid L1 Current | A | - | enabled | Leg current |
| Grid L2 Current | A | - | enabled | Leg current |
| Grid Voltage | V | - | enabled | Grid voltage as the panel reports it |
| Battery SOC | % | - | enabled | Charge of the batteries connected to the panel, taken together |
| Battery Remaining Energy | Wh | - | enabled | Energy left in those batteries |
| Battery Full Capacity | Wh | diagnostic | enabled | Capacity of those batteries when full |
| Backup Runtime | min | - | enabled | How long the batteries would carry the current home load, as the panel estimates it |

## Sensors - Circuits (up to 12)

Each circuit creates 2 sensors (1 enabled by default, 1 disabled). They are created once the panel reports the circuit and its name.

The circuit's name from the EcoFlow app follows its number in the entity name, for example "Circuit 5 Kitchen Power". A circuit that still has its default name shows only its number. The name is read when the entity is created. A name changed later in the app shows after the next reload of the integration.

**Enabled by default:**

| Entity | Unit | Description |
|:---|:---:|:---|
| Circuit N Power | W | Power the circuit draws |

**Disabled by default:**

| Entity | Unit |
|:---|:---:|
| Circuit N Current | A |

## Sensors - Storage Channels (up to 3)

Each storage channel with a battery attached creates 1 sensor, once the panel reports the channel as ready or connected.

| Entity | Unit | Description |
|:---|:---:|:---|
| Storage Channel N SOC | % | Charge of the battery on this channel |
