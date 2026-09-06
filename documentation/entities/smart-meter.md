# Smart Meter - Entity Reference

Full list of all entities created for the EcoFlow Smart Meter (BK21 series).

**Totals:** 18 sensors, 3 binary sensors

> Entities marked with *disabled* are available but hidden by default. Enable them in **Settings > Devices > EcoFlow Smart Meter > Entities**.

The meter is read-only. It reports through the account connection only, so it needs **Enhanced Mode**; the Developer API answers for it with an empty response, which is why a Standard Mode setup skips it and says so in the log.

The phases are keyed L1, L2 and L3 on the wire and lettered A, B and C in the EcoFlow app. The entity names follow the app.

---

## Sensors

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Grid Power | W | - | enabled | Total power at the grid connection, the figure the app shows |
| Phase A Power | W | - | enabled | Active power on phase A |
| Phase B Power | W | - | enabled | Active power on phase B |
| Phase C Power | W | - | enabled | Active power on phase C |
| Phase A Voltage | V | - | enabled | Line voltage on phase A |
| Phase B Voltage | V | - | enabled | Line voltage on phase B |
| Phase C Voltage | V | - | enabled | Line voltage on phase C |
| Phase A Current | A | - | enabled | Current on phase A |
| Phase B Current | A | - | enabled | Current on phase B |
| Phase C Current | A | - | enabled | Current on phase C |
| Power Factor | - | diagnostic | enabled | Power factor at the grid connection, see the note below |
| Grid Connection State | - | diagnostic | enabled | Drawing from the grid, feeding into it, not connected, or invalid |

## Sensors - Energy Dashboard

| Entity | Unit | Default | Description |
|:---|:---:|:---:|:---|
| Grid Import Energy | Wh | enabled | Lifetime counter, energy drawn from the grid |
| Grid Export Energy | Wh | enabled | Lifetime counter, energy fed back into the grid |
| Grid Net Energy | Wh | enabled | Lifetime counter, import minus export |
| Phase A Net Energy | Wh | enabled | Lifetime counter, phase A import minus export |
| Phase B Net Energy | Wh | enabled | Lifetime counter, phase B import minus export |
| Phase C Net Energy | Wh | enabled | Lifetime counter, phase C import minus export |

> **Grid Import Energy** and **Grid Export Energy** are the two entities for the Energy Dashboard, on the grid consumption and return-to-grid slots. Both are lifetime counters the meter keeps itself, so they survive restarts and gaps without being rebuilt from power readings. **Grid Net Energy** and the three phase figures are import minus export, so they fall whenever the house feeds power back into the grid. That is a real reading, not a fault, which is exactly why these four are published as a plain total and not as a monotonic one, and why they do not belong in the dashboard's grid slots.

---

## Binary Sensors

| Entity | Category | Default | Description |
|:---|:---:|:---:|:---|
| Phase A Connected | diagnostic | enabled | Whether the meter sees phase A |
| Phase B Connected | diagnostic | enabled | Whether the meter sees phase B |
| Phase C Connected | diagnostic | enabled | Whether the meter sees phase C |

---

## Controls

None. The meter measures and reports; it has nothing to set.

---

## Notes

**Where the readings come from.** Support was built from a recording an owner took on 2026-08-31 with the EcoFlow app open beside it, and the values above match what the app showed at that moment: 407 W total, 318 W on phase B and 89 W on phase C with phase A idle, and 1345 Wh imported. The meter sends a short frame every few seconds and a full one less often; voltages, currents, the power factor and the connection flags only appear in the full frames, so those entities update more slowly than the power readings. A second recording of 18 hours and 40 minutes spanning local midnight, together with a short deliberate export test, settled the six energy counters: none of them reset at midnight, and the export test moved only the export counter and the exporting phase's net figure.

**No daily counter is on the wire.** All six energy entities are lifetime counters; the meter does not send anything that resets at midnight. The midnight recording showed every counter holding its value across the reset point instead of dropping back to zero. Owners who want a daily figure can derive one from **Grid Import Energy** with the Energy Dashboard's own daily view or a Utility Meter helper.

**Grid Export Energy is a genuine counter, not a derived value.** It stays at zero on an installation that has never fed power back into the grid. During the export test it climbed from 25 Wh to 28 Wh while the exporting phase's own net figure fell, and the app agreed on the import, export and net figures at that moment. **Grid Connection State** already reports feeding into the grid on its own, so together the two show both that export happened and how much.

**Power Factor reads zero.** The field is sent in every full frame and was zero throughout the recording. It is kept as a diagnostic entity rather than dropped, because zero on an idle phase is a plausible reading and the entity is the cheapest way for an owner to confirm whether it ever moves.

**Readings that are defined but never sent.** The meter's message also has room for reactive power, apparent power and a signal strength value. None of them appeared once in the recording, so none of them became entities. A recording from a second installation is what settles whether they exist on other units or nowhere at all.
