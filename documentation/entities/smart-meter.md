# Smart Meter - Entity Reference

Full list of all entities created for the EcoFlow Smart Meter (BK21 series).

**Totals:** 17 sensors, 3 binary sensors

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
| Grid Energy Total | Wh | enabled | Lifetime counter the meter keeps itself |
| Grid Energy Today | Wh | enabled | Energy since midnight, total for all phases |
| Phase A Energy Today | Wh | enabled | Energy since midnight, phase A |
| Phase B Energy Today | Wh | enabled | Energy since midnight, phase B |
| Phase C Energy Today | Wh | enabled | Energy since midnight, phase C |

> **Grid Energy Total** is the one to add to the Energy Dashboard. It is the meter's own lifetime counter, so it survives restarts and gaps without being rebuilt from power readings. The app labels this counter as import; whether it is really import only or a net figure will show on the first day the house exports, which is why it is published as a plain total and not as a monotonic one. The daily figures do reset to zero at midnight by design, which is the reset a monotonic counter is built for, so those carry it.

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

**Where the readings come from.** Support was built from a recording an owner took on 2026-08-31 with the EcoFlow app open beside it, and the values above match what the app showed at that moment: 407 W total, 318 W on phase B and 89 W on phase C with phase A idle, and 1345 Wh imported. The meter sends a short frame every few seconds and a full one less often; voltages, currents, the power factor and the connection flags only appear in the full frames, so those entities update more slowly than the power readings.

**Today and lifetime read the same for now.** In that recording the daily figure and the lifetime figure carried the same number, which is what you would expect on a meter commissioned the same day. Which of the two is really the lifetime counter is therefore not yet settled by observation; the field named as the lifetime one is the field used for the Energy Dashboard sensor. A recording taken across midnight is what confirms it, and until then a jump in **Grid Energy Today** at midnight is the thing to report.

**No separate export counter.** There is no export or feed-in total in the message definition, and no second counter to derive one from, so the Energy Dashboard gets one grid entry from this device and nothing on the return side. The house in the recording never exported, so whether the counters above hold import only or a net figure is not something that recording can settle. **Grid Connection State** does report feeding into the grid, so the direction is visible even where the energy is not.

**Power Factor reads zero.** The field is sent in every full frame and was zero throughout the recording. It is kept as a diagnostic entity rather than dropped, because zero on an idle phase is a plausible reading and the entity is the cheapest way for an owner to confirm whether it ever moves.

**Readings that are defined but never sent.** The meter's message also has room for reactive power, apparent power and a signal strength value. None of them appeared once in the recording, so none of them became entities. A recording from a second installation is what settles whether they exist on other units or nowhere at all.
