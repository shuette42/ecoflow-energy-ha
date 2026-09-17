# Ocean 2 - Entity Reference

Full list of all entities created for the EcoFlow Ocean 2.

**Totals:** 20 sensors

The Ocean 2 reports through the account connection only, so it needs **Enhanced Mode**. The Developer API answers error 1006 for this device, the same position the `J32D`/`J32E` PowerOcean variants are in, so Standard Mode creates no usable entities.

Two serial prefixes, one device: `RE11` for the 10 kW unit and `RE17` for the 12 kW one. They differ in power rating and in nothing this integration reads.

`RE11` is confirmed on two installations. `RE17` is routed on EcoFlow's own device list, which separates the two by power rating alone; no frame from an `RE17` exists yet, so if you own one, a note either way is welcome.

Read-only. No write frame from an Ocean 2 has been observed, so there are no controls.

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

### What is not mapped yet

The unit reports considerably more than this: per-module cell voltages, temperatures and cycle counts, and per-phase measurements. Those need their own entity set and follow separately.
