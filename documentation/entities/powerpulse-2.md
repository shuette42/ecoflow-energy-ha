# PowerPulse 2 - Entity Reference

Full list of all entities created for the EcoFlow PowerPulse 2 wallbox (`C376` and `C374` series).

**Totals:** 18 sensors, 1 binary sensor, 1 select (with exactly one PowerOcean on the same account), 2 buttons (with a PowerOcean on the same account); one number with exactly one PowerOcean on the account (Wallbox Maximum Current) and a second, different number with none (Wallbox Charging Current)

> **Enhanced Mode only.** The wallbox reports through the account connection, not through the EcoFlow Developer API. A Standard Mode setup gets error 1006 and no entities fill; set the integration up with an EcoFlow account e-mail and password instead.

The PowerPulse 2 is its own device type and does not need a paired PowerOcean. Earlier releases only saw these readings once a PowerOcean was coupled to relay them, so a wallbox without one showed nothing at all. The integration now reads the wallbox's own reporting channel directly.

Four of the sensors below - Wallbox Charging Power, Wallbox Session Energy, Wallbox Session Duration and Wallbox Charging Status - used to live on the PowerOcean device when a PowerPulse 2 was coupled to one. They now live here, under entity IDs built from the wallbox's own serial number, and the PowerOcean-side entries, those four and the Wallbox Vehicle entry the relay filled with a placeholder, are removed from the entity registry on the first start after the update. History and statistics recorded under the old IDs do not carry over, and an automation, a dashboard card or an Energy Dashboard slot that named one of them needs the new entity. The PowerPulse 2 has no vehicle reading; only the earlier PowerPulse reports one, on its PowerOcean. Nothing changes for the earlier PowerPulse (`AC31` series): it keeps reporting through its PowerOcean on the same five entities as before.

Start and stop of a charging session are available as two buttons when the account also holds one PowerOcean (see Buttons). The maximum current and the charging mode are configurable from Home Assistant under the same condition (see Numbers and Selects). The wallbox's other settings (phase selection, the Smart mode's departure time and target) are not.

---

## Sensors

| Entity | Unit | Category | Default | Description |
|:---|:---:|:---:|:---:|:---|
| Wallbox Charging Power | W | - | enabled | Power currently going into the vehicle |
| Wallbox Voltage L1 | V | - | enabled | Line voltage on phase 1 |
| Wallbox Voltage L2 | V | - | enabled | Line voltage on phase 2 |
| Wallbox Voltage L3 | V | - | enabled | Line voltage on phase 3 |
| Wallbox Current L1 | A | - | enabled | Current on phase 1 |
| Wallbox Current L2 | A | - | enabled | Current on phase 2. Reads 0 A while charging single-phase |
| Wallbox Current L3 | A | - | enabled | Current on phase 3. Reads 0 A while charging single-phase |
| Wallbox Maximum Current | A | diagnostic | enabled | The maximum current configured on the wallbox, not a live measurement |
| Wallbox Charging Current | A | diagnostic | enabled | The charging current set for the session, not a live measurement. Equals the maximum current while charging at the limit and can sit below it otherwise |
| Wallbox Phase Mode | - | diagnostic | enabled | `single_phase` or `three_phase`: the phase mode in effect right now. The app's phase selection (automatic, single or three) is a setting the wallbox reports elsewhere and is not shown; under automatic this reads whichever mode the wallbox is in |
| Wallbox Session Status | - | diagnostic | enabled | `idle`, `charging` or `finished` |
| Wallbox Session Duration | s | diagnostic | enabled | How long the current session has been running. Absent while no session is running, see the note below |
| Wallbox Charging Status | - | - | enabled | Available, preparing, charging, paused by charger, paused by vehicle, finishing, or fault |
| Wallbox Charging Mode | - | - | enabled | The charging mode set on the wallbox: `fast`, `solar`, `custom` or `smart`, the four modes the EcoFlow app offers. Reported on the wallbox's heartbeat, about once a minute and right after a change |
| Wallbox Session Start | - | diagnostic | enabled | When the current session began. Absent while no session is running |
| Wallbox Session Energy | Wh | - | enabled | Energy delivered in the current session. Resets with every new session, so it is not a lifetime counter. Absent while no session is running |
| Wallbox Session Start Meter | Wh | diagnostic | enabled | The lifetime counter's reading at the moment the current session began. Absent while no session is running |
| Wallbox Total Energy | Wh | - | enabled | Lifetime counter, energy delivered by the wallbox over its whole life |

---

## Binary Sensors

| Entity | Category | Default | Description |
|:---|:---:|:---:|:---|
| Wallbox Cable Lock | diagnostic | enabled | Whether the cable's permanent lock, used as a theft deterrent, is switched on |

---

## Numbers

| Entity | Range | Step | Description |
|:---|:---:|:---:|:---|
| Wallbox Maximum Current | 6-16 A | 1 A | Sets the maximum current the wallbox may draw. Same reading as the Wallbox Maximum Current sensor above; a write returns once the wallbox reports the new value on its own settings report, or fails if it does not. The range is what one owner's sweep covered; a wallbox configured above 16 A in the app shows that value on the sensor and can be lowered from here, but not set above 16 A until such a write is on record |
| Wallbox Charging Current | 6-16 A | 1 A | Sets the charging current for the running session. Same reading as the Wallbox Charging Current sensor above; a write returns once the wallbox's own heartbeat reports the new value, or fails if it does not. In the one recording on file, a write sent during an active session confirmed in under a second; a write sent to an idle wallbox left the reading unchanged for close to an hour, and the recording cannot say whether that was a slow but effective write or one the wallbox never took - the first real write to an idle wallbox is what settles it |

Wallbox Maximum Current is created only when the account holds exactly one PowerOcean - the sibling route the two buttons above also use. Unlike the buttons, this control has no second route: an account with no PowerOcean, or with two or more, gets no number, because no route from the wallbox's own channel has been observed for this write. Availability follows the sibling PowerOcean's connection, the same rule as the buttons on that route.

Wallbox Charging Current is the mirror image: created only when the account holds no PowerOcean at all, the wallbox's own channel. An account with one PowerOcean, or with two or more, gets no charging-current number, because no route through a PowerOcean has been observed for this write. It appears with the wallbox's first heartbeat rather than waiting on a separate settings report, since the reading is already in that first heartbeat.

---

## Selects

| Entity | Options | Description |
|:---|:---|:---|
| Wallbox Charging Mode | `fast`, `solar`, `custom`, `smart` | Sets the charging mode, the same setting the EcoFlow app calls Fast charging, Solar Mode, Custom and Smart Mode. Same reading as the Wallbox Charging Mode sensor above; a change returns once the wallbox reports the new mode on its own heartbeat, within 75 s, or fails with the mode the wallbox still reports. `smart` is shown when the wallbox is in it but cannot be chosen from here: the app always sends a departure time and a charging target with it, and those are set in the app |

Created under the same condition as the number above: exactly one PowerOcean in the account, availability following its connection. The integration sends the mode alone, without the current or solar settings the app repeats beside it; whether the wallbox leaves those unchanged is confirmed on the first pre-release run. The confirmation comes from the heartbeat because that is the only place the wallbox reports the mode, and the heartbeat comes about once a minute, so a change can take up to a minute to return even when the wallbox took it at once. While it waits, the maximum-current number and the two buttons report a write in progress.

---

## Buttons

| Entity | Default | Description |
|:---|:---:|:---|
| Wallbox Start Charging | enabled | Starts a charging session that the wallbox has stopped, that is, while Wallbox Charging Status reads `finishing`. The press returns once the wallbox itself reports `charging`, or fails after 30 s if it does not |
| Wallbox Stop Charging | enabled | Stops the running session, while Wallbox Charging Status reads `charging`. The press returns once the wallbox reports `finishing` or `available`, or fails after 15 s if it does not |

The command takes one of two routes, the way the EcoFlow app sends it in each setup, and the confirmation is always read from the wallbox's own reporting: nothing is shown as done until the wallbox says so, and a press in a state the action does not fit (a start while charging, a stop while idle) fails with a message naming the state instead of sending anything.

- **One PowerOcean in the same integration entry:** the command travels through the PowerOcean, addressed to the wallbox by the address the wallbox reports. The buttons appear once the wallbox has reported that address, and are unavailable while the PowerOcean's connection is down.
- **No PowerOcean in the entry:** the command goes on the wallbox's own channel. The buttons appear with the wallbox's first status report, and are unavailable while the wallbox's own connection is down.
- **Two or more PowerOceans in the entry:** no buttons; neither route has been observed on such an account.

A start is offered only from `finishing`, the state a stopped session sits in with the cable attached. Starting from `available` (no session) has not been observed and is not offered.

---

## Notes

### The four session readings are absent, not zero, between sessions

Wallbox Session Start, Wallbox Session Duration, Wallbox Session Energy and Wallbox Session Start Meter only exist while a session is actually running. Once the session ends, the wallbox stops reporting them and the entities go unavailable instead of settling on the last session's numbers - showing the previous session's duration or energy as if it were still counting would be worse than showing nothing. In `preparing` (cable attached, nothing flowing yet) the wallbox still reports them, and Wallbox Session Start is withheld when the wallbox reports it as zero, which it does before the first session after a restart.

### The cable lock can take a while to appear

Wallbox Cable Lock is only carried in the wallbox's bundled status report, never in a single push update. A setup that only sees pushes will not see this entity until the wallbox happens to send its bundle, which can take a while after startup.

### The wallbox is quiet between changes

The PowerPulse 2 reports when something changes and otherwise stays silent, charging or not. Over a complete seven-hour recording the longest silence was 50 minutes. The integration therefore waits 20 minutes before it asks the wallbox for a fresh report and keeps the entities available through an hour of silence; on every other device type that watch is much shorter. A wallbox that has gone offline is shown as unavailable after an hour, not after ten minutes.

### Single-phase charging

When the vehicle charges single-phase, only Wallbox Current L1 carries a real reading; Wallbox Current L2 and Wallbox Current L3 read 0 A.
