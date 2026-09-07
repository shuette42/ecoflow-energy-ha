# WAVE 3 - Entity Reference

Full list of all entities created for the EcoFlow WAVE 3 portable air conditioner.

**Totals:** 24 sensors, 6 binary sensors

The WAVE 3 reports through the account connection only, so it needs **Enhanced Mode**. The Developer API lists the unit but refuses every reading from it with error 1006, which means a Standard Mode entry cannot select it and its entities would never fill.

Nothing is polled. The device sends two messages by itself: the full status every 120 seconds, and a runtime block every 300 seconds carrying the temperatures inside the refrigerant loop, the supply voltage and current, and the firmware. While the unit is running it also pushes its input power roughly every 2 seconds.

Read-only in this release. Mode, setpoint, fan speed and the settings follow once a write from Home Assistant has been confirmed on the hardware; the app's own commands have been recorded, so the shape is known.

---

## Sensors

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Ambient Temperature | °C | - | enabled | The room temperature the unit measures |
| Ambient Humidity | % | - | enabled | The room humidity the unit measures |
| Operating Mode | - | - | enabled | `cooling`, `heating`, `fan`, `dehumidify` or `constant temperature` |
| Target Temperature | °C | - | enabled | The setpoint of the mode currently running |
| Fan Speed | % | - | enabled | 20, 40, 60, 80 or 100, the five steps the app offers |
| AC Input Power | W | - | enabled | What the unit draws from the mains right now |
| Outdoor Temperature | °C | - | enabled | The temperature on the exhaust side |
| AC Input Energy | kWh | - | enabled | Lifetime energy taken from the mains, integrated from the input power. Ready for the Energy Dashboard |
| Supply Air Temperature | °C | diagnostic | enabled | The air leaving the unit |
| Return Air Temperature | °C | diagnostic | enabled | The air entering the unit |
| Operating Submode | - | diagnostic | enabled | `none`, `normal`, `max`, `sleep` or `eco`. Only cooling and heating have one |
| AC Input Voltage | V | diagnostic | enabled | Mains voltage at the unit |
| AC Input Current | A | diagnostic | enabled | Mains current at the unit |
| Condenser Temperature | °C | diagnostic | disabled | Inside the refrigerant loop |
| Evaporator Temperature | °C | diagnostic | disabled | Inside the refrigerant loop |
| Compressor Discharge Temperature | °C | diagnostic | disabled | Inside the refrigerant loop |
| Target Humidity | % | diagnostic | disabled | The setpoint used in dehumidify mode |
| Screen Brightness | % | diagnostic | disabled | Brightness of the unit's own display |
| Battery | % | diagnostic | disabled | Charge of the add-on battery. See the note below on why it is off by default |
| Battery Voltage | V | diagnostic | disabled | Add-on battery |
| Battery Current | A | diagnostic | disabled | Add-on battery |
| Battery Power | W | diagnostic | disabled | Add-on battery |
| PV Input Power | W | diagnostic | disabled | Solar going into the unit |
| Condensate Water Level | - | diagnostic | disabled | Carries no unit. The only value ever observed is 0 |

---

## Binary Sensors

| Entity | Category | Default | Description |
|:---|:---:|:---:|:---|
| Running | - | enabled | Whether the unit is working or idle |
| AC Input Connected | diagnostic | enabled | Whether it is plugged into the mains |
| Fault | diagnostic | enabled | The unit reports a fault |
| Draining | diagnostic | enabled | The unit is pumping out condensate |
| Pet Care Alarm | diagnostic | disabled | The pet care alarm has triggered |
| Battery Communication Error | diagnostic | disabled | Reads on for every unit without an add-on battery, which is why it is off by default |

---

## Notes

### Four of the values belong to the mode that is running

Target Temperature, Fan Speed, Target Humidity and Operating Submode are the values of the currently active mode. The device keeps a separate set for each mode, so switching from cooling to heating in the app changes all four at once, and Home Assistant follows within one push. A setpoint you saw in cooling is not lost when you switch away; it simply is not the one being reported any more.

### What the energy counter samples

AC Input Energy is integrated from AC Input Power at whatever rate the device sends that power. That is roughly every 2 seconds while the unit runs, and every 120 seconds in standby, where the draw is under a watt. The 2 second rate was measured with the EcoFlow app open, and it is not yet settled whether it holds with the app closed, so the 120 second rate is the one to count on when judging how closely the counter tracks a short cooling run.

### Why the battery entities are off by default

The add-on battery is optional. A unit without one still reports the battery fields, and reports them as zero, which reads as a flat battery rather than as an absent one. The battery communication alarm on the same unit reads on for the same reason. All five stay disabled by default; if your unit has the battery, enable them under **Settings > Devices & services > Entities**.

### One unit on record

Everything here was measured on a single WAVE 3. If you own one of the other AC71 variants and something reads differently, or an entity stays empty that should not, please say so on [#161](https://github.com/shuette42/ecoflow-energy-ha/issues/161).
