# Ocean 2 - Entity Reference

Full list of all entities created for the EcoFlow Ocean 2.

**Totals:** 20 system sensors, plus 12 per battery module

The Ocean 2 reports through the account connection only, so it needs **Enhanced Mode**. The Developer API answers error 1006 for this device, the same position the `J32D`/`J32E` PowerOcean variants are in, so Standard Mode creates no usable entities.

Three serial prefixes, one device: `RE11` for the 10 kW unit, `RE17` for the 12 kW one and `RE41` for the 8 kW single-phase Ocean 2 Plus. They differ in power rating and phase count, and in nothing this integration reads - the per-phase block is not among the entities below.

`RE11` is confirmed on two installations, `RE41` on one through an owner's diagnostics download on #145. `RE17` is routed on EcoFlow's own device list, which separates it from the `RE11` by power rating alone; no frame from an `RE17` exists yet, so if you own one, a note either way is welcome. `RE43`, the 12 kW Plus, is not routed here: it has been reported by an owner, but no frame from one exists on either side.

Read-only. No write frame from an Ocean 2 has been observed, so there are no controls.

Battery modules are read as well: 12 readings each, created once a module actually reports. The count is an installation choice - two on the unit this was mapped on - so nothing is declared for modules you do not have. Captures from other systems show bundles of up to fourteen per-module headers in one frame, a heartbeat backlog rather than fourteen distinct modules.

**Despite the name, this is not a PowerOcean.** It carries its own telemetry frame on `cmd_func` 254 and shares no field layout with the `HJ31`/`J32x` line, which is why it has its own parser and its own entity set.

---

## Sensors

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Battery SOC | % | - | enabled | Charge of the house battery |
| Battery Power | W | - | enabled | Signed: positive charges the battery, negative discharges it |
| Battery Remaining Energy | Wh | - | enabled | Energy left in the pack. Not its capacity |
| Solar Power | W | - | enabled | Total across all strings |
| Grid Power | W | - | enabled | Signed: positive draws from the grid, negative feeds into it |
| Home Power | W | - | enabled | As the device reports it, not derived from the other three |
| PV 1 Power | W | - | enabled | Per-string solar |
| PV 2 Power | W | - | enabled | Per-string solar |
| PV 3 Power | W | diagnostic | disabled | Only wired on some installations, see the note below |
| PV 4 Power | W | diagnostic | disabled | Only wired on some installations, see the note below |
| Grid Import Power | W | - | enabled | The positive half of Grid Power |
| Grid Export Power | W | - | enabled | The negative half of Grid Power, as a positive number |
| Battery Charge Power | W | - | enabled | The positive half of Battery Power |
| Battery Discharge Power | W | - | enabled | The negative half of Battery Power, as a positive number |
| Solar Energy | kWh | - | enabled | Riemann sum over Solar Power |
| Home Energy | kWh | - | enabled | Riemann sum over Home Power |
| Grid Import Energy | kWh | - | enabled | For the Energy Dashboard's grid consumption slot |
| Grid Export Energy | kWh | - | enabled | For the Energy Dashboard's return-to-grid slot |
| Battery Charge Energy | kWh | - | enabled | For the Energy Dashboard's battery slot |
| Battery Discharge Energy | kWh | - | enabled | For the Energy Dashboard's battery slot |

---

## Sensors - Battery Modules (up to 16)

Each module creates 12 sensors (5 enabled by default, 7 disabled). They are created once a module actually reports, so a system with two modules gets two sets and nothing else - the count is an installation choice, not a model difference.

**Enabled by default:**

| Entity | Unit | Description |
|:---|:---:|:---|
| Module N SoC | % | Charge of this module |
| Module N Power | W | Signed: positive charges, negative discharges |
| Module N Remaining Energy | Wh | Energy left in this module, not its capacity |
| Module N State of Health | % | Ageing state, separate from the charge above |
| Module N Charge Cycles | - | Full cycles the module counts |

**Disabled by default:**

| Entity | Unit |
|:---|:---:|
| Module N Cell Temperature | °C |
| Module N Min Cell Temperature | °C |
| Module N Max Cell Temperature | °C |
| Module N Power Electronics Temperature | °C |
| Module N Voltage | V |
| Module N Current | A |
| Module N Max Cell Voltage | mV |

> **The module SoC carries no battery device class.** Home Assistant shows one battery figure per device, and that is the system state of charge at the top of this page. A per-module class would put up to sixteen competing battery icons on one device.

---

## Notes

### Why grid and battery power exist twice

The device reports one signed value per path. The Energy Dashboard needs two one-directional counters instead, because a Riemann sum over a signed value cancels itself out: an hour of charging followed by an hour of discharging would integrate to nothing. Both halves are written on every update, including the zero, so neither sensor freezes at its last non-zero reading when the flow stops.

### PV 3 and PV 4 are disabled by default

A unit populates as many strings as its MPPT inputs are wired. An entity that never fills reads as a broken sensor rather than an unused input, so the two higher strings ship disabled. Enable them under **Settings > Devices & services > Entities** if your installation uses them.

### Per-string energy counters are deliberately absent

**Solar Energy** already covers the total. A dashboard that summed per-string counters would under-report on a unit whose higher strings are disabled, which is the default.

### The field that looks like a grid meter is not one

The device carries a value that reads like grid power and is the configured feed-in limit: constant `10000` on a unit capped at 10 kW, constant `0` on a zero-export unit. On the latter it is indistinguishable from a real meter reading at rest, which is how it survives casual checking. In the app the same value is resolved per serial and used to bound a slider, so it is the owner's own setting rather than a measurement, and no sensor is built on it. Grid Power comes from the inverter block instead.

### Home Power is reported, not calculated

The device sends its own house-load figure, and this integration publishes that rather than deriving it from solar minus battery minus grid. The two disagree during load changes, because the readings arrive in separate submessages with their own measurement instants: in a quiet moment the difference is a few watts, during a 500 W load step it runs into the hundreds. The reported figure is the one the EcoFlow app shows.

### Where the field mapping comes from

The assignments were worked out from observed traffic on live hardware and cross-checked against the EcoFlow app, then confirmed against a seven hour recording from a second, unrelated installation. The standalone integration they come from, [jensfr1/ha-ecoflow-ocean2](https://github.com/jensfr1/ha-ecoflow-ocean2), decodes the full frame set including the per-module battery data; either integration can be used on its own.

### Three module readings were originally the wrong quantity

Not the wrong scale - the wrong thing entirely, which is worth naming because each one looks plausible until it is checked against a moving system.

The field read as **capacity is the energy remaining**: measured 4114 Wh at 81.5 % and 4137 Wh at 82.0 %, putting the full module near 5046 Wh. Read as a capacity it falls while the module discharges, which is harmless on a desk and permanently misleading in service. The field read as **state of charge is the state of health**: constant 100.0 across a whole measurement while the neighbouring field moved. And the **cell voltage is a cell voltage, not the pack** - it follows the load, and the divide-by-ten it used to carry was the warning sign, since a float needs no scaling.

### Why the module voltage reads about 16.5 V

That is low for a home battery, which is why the field was overlooked at first. The modules are wired 5S: 16.46 V over 3.311 V per cell is exactly five cells in series. Confirmed through the power balance - pack voltage times current hits the reported module power within 1 %, on two modules independently.

### The temperatures were separated by a load test, not by their averages

45 minutes of wallbox charging at up to 3.6 kW per module. What tells the readings apart is how they move. Three of them hold the order min ≤ average ≤ max in every frame and rise together and slowly - those are the cells. Four others follow the load within a minute, swinging 11 to 17 K at up to 7 K per minute, which is power electronics rather than cells. Those four sit on different parts of the same board; the integration publishes the hottest as one reading, because four near-identical entities per module would be noise at sixteen modules.

### What is not mapped yet

Per-phase measurements. Those need their own entity set and follow separately.
