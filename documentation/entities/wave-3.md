# WAVE 3 - Entity Reference

Full list of all entities created for the EcoFlow WAVE 3 portable air conditioner.

**Totals:** 18 sensors, 5 binary sensors, 4 switches, 5 numbers, 5 selects

The WAVE 3 reports through the account connection only, so it needs **Enhanced Mode**. The Developer API lists the unit but refuses every reading from it with error 1006, which means a Standard Mode entry cannot select it and its entities would never fill.

Nothing is polled. The device sends two messages by itself: the full status every 120 seconds, and a runtime block every 300 seconds carrying the temperatures inside the refrigerant loop, the supply voltage and current, and the firmware. While the unit is running it also pushes its input power roughly every 2 seconds.

Every control sends the frame the EcoFlow app sends for the same setting, one setting per frame, and the device answers with its new value in a push of its own, usually within 2 seconds. Home Assistant shows the requested value for 5 seconds and from then on what the device reports.

---

## Switches

| Entity | Default | Description |
|:---|:---:|:---|
| Power | enabled | On starts the unit. Off puts it into standby, which is what the button in the app does; the hard power-off is never sent. Reads back from the running flag |
| Buzzer | enabled | The beep on every key press |
| Automatic Drainage | enabled | Lets the unit pump out condensate on its own |
| Pet Care | enabled | The pet care alarm, with its threshold in Pet Care Warning Temperature |

---

## Numbers

| Entity | Unit | Range | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Target Temperature | °C | 15.5 to 30, in steps of 0.5 | enabled | The setpoint of the mode that is running. Written in cooling and heating. Constant temperature shows its own setpoint here but is set through a band of two temperatures the app writes as a pair, which is not offered yet; fan and dehumidify have no setpoint |
| Fan Speed | % | 20, 40, 60, 80 or 100 | enabled | The five steps the app offers. Refused in constant temperature mode, where the unit picks the speed itself |
| Target Humidity | % | 40 to 80 | enabled | The setpoint of dehumidify mode, and accepted in that mode only |
| Screen Brightness | % | 10 to 100 | enabled | Brightness of the unit's own display |
| Pet Care Warning Temperature | °C | 25 to 45 | enabled | The room temperature at which the pet care alarm triggers |

---

## Selects

| Entity | Options | Default | Description |
|:---|:---|:---:|:---|
| Operating Mode | cooling, heating, fan, dehumidify, constant temperature | enabled | Switching the mode brings that mode's own setpoint, fan speed and submode with it |
| Operating Submode | none, normal, max, sleep, eco | enabled | Shown for every mode. Written in cooling and heating only, and only as max, sleep or eco, the three the app itself sends |
| Display Temperature | ambient, outlet | enabled | Which temperature the unit's display shows |
| Mood Light | off, on, screen time | enabled | The light strip; screen time means it follows the display |
| Screen Timeout | 10s, 30s, 1min, 5min, 10min, never | enabled | How long the display stays on after the last key press |

---

## Sensors

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Ambient Temperature | °C | - | enabled | The room temperature the unit measures |
| Ambient Humidity | % | - | enabled | The room humidity the unit measures |
| AC Input Power | W | - | enabled | What the unit draws from the mains right now |
| Outdoor Temperature | °C | - | enabled | The temperature on the exhaust side |
| AC Input Energy | kWh | - | enabled | Lifetime energy taken from the mains, integrated from the input power. Ready for the Energy Dashboard |
| Supply Air Temperature | °C | diagnostic | enabled | The air leaving the unit |
| Return Air Temperature | °C | diagnostic | enabled | The air entering the unit |
| AC Input Voltage | V | diagnostic | enabled | Mains voltage at the unit |
| AC Input Current | A | diagnostic | enabled | Mains current at the unit |
| Condenser Temperature | °C | diagnostic | disabled | Inside the refrigerant loop |
| Evaporator Temperature | °C | diagnostic | disabled | Inside the refrigerant loop |
| Compressor Discharge Temperature | °C | diagnostic | disabled | Inside the refrigerant loop |
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
| AC Input Connected | diagnostic | enabled | Whether it is plugged into the mains |
| Fault | diagnostic | enabled | The unit reports a fault |
| Draining | diagnostic | enabled | The unit is pumping out condensate |
| Pet Care Alarm | diagnostic | disabled | The pet care alarm has triggered |
| Battery Communication Error | diagnostic | disabled | Reads on for every unit without an add-on battery, which is why it is off by default |

---

## Notes

### Four of the values belong to the mode that is running

Target Temperature, Fan Speed, Target Humidity and Operating Submode are the values of the currently active mode. The device keeps a separate set for each mode, so switching from cooling to heating changes all four at once, and Home Assistant follows within one push. A setpoint you saw in cooling is not lost when you switch away; it simply is not the one being reported any more.

The submode presets carry their own values. Choosing sleep or max also sets that preset's temperature and fan speed (sleep in cooling set 26 °C and 20 % on the unit here), and writing a temperature or a fan speed afterwards leaves the preset, so the submode reads normal again. That is the device's own behaviour, the app does the same.

A write goes to the running mode as well. That is why a setpoint is refused outside cooling and heating, a humidity target outside dehumidify, and a fan speed in constant temperature: the app does not offer those either, and the one it does offer differently, the constant temperature band, is not offered here yet. The refusal arrives as an error on the entity naming the reason, and nothing is sent to the device.

### What the energy counter samples

AC Input Energy is integrated from AC Input Power at whatever rate the device sends that power. That is every 2 seconds while the unit runs, measured with the EcoFlow app closed as well as open, and every 120 seconds in standby, where the draw is under a watt.

### Why the battery entities are off by default

The add-on battery is optional. A unit without one still reports the battery fields, and reports them as zero, which reads as a flat battery rather than as an absent one. The battery communication alarm on the same unit reads on for the same reason. All five stay disabled by default; if your unit has the battery, enable them under **Settings > Devices & services > Entities**.

### If you installed the first pre-release

v1.20.0-beta.1 shipped the WAVE 3 read-only, with Operating Mode, Target Temperature, Fan Speed, Operating Submode, Target Humidity and Screen Brightness as sensors and Running as a binary sensor. Those seven are the controls now, under the same identifiers, so on the first start after the update the old sensor entities are removed and the number, select and switch entities take their place. A dashboard or automation that referred to one of them by entity id needs the new domain, for example `select.` instead of `sensor.` for the operating mode.

### One unit on record

Everything here was measured on a single WAVE 3. If you own one of the other AC71 variants and something reads differently, or an entity stays empty that should not, please say so on [#161](https://github.com/shuette42/ecoflow-energy-ha/issues/161).
