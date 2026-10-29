# PowerPulse 2 - Entity Reference

Full list of entities for the EcoFlow PowerPulse 2 wallbox (`C376`, `C374` and `C371` series).

**`C371`: read-only.** It gets the same sensors and binary sensor. Charging power and session energy were checked against the app over one overnight charge. The buttons, numbers, selects and switches below work on the `C376` and `C374` only, until a write to a `C371` is confirmed.

**Totals:** 21 sensors, 1 binary sensor, 2 buttons (with no PowerOcean or exactly one in the same integration entry, none with two or more), 2 selects and 5 switches (with exactly one PowerOcean in the entry), and six numbers: Wallbox Maximum Current, Wallbox Solar Minimum Current, Wallbox Custom Charging Current, Wallbox Screen Brightness and Wallbox LED Indicator Brightness with exactly one PowerOcean in the entry, Wallbox Charging Current Setpoint with none

> **Enhanced Mode only.** The wallbox reports through the account connection, not through the EcoFlow Developer API. A Standard Mode setup gets error 1006 and no entities fill; set the integration up with an EcoFlow account e-mail and password instead.

The PowerPulse 2 is its own device type and does not need a paired PowerOcean. Earlier releases only saw these readings once a PowerOcean was coupled to relay them, so a wallbox without one showed nothing at all. The integration now reads the wallbox's own reporting channel directly.

Four of the sensors below - Wallbox Charging Power, Wallbox Session Energy, Wallbox Session Duration and Wallbox Charging Status - used to live on the PowerOcean device when a PowerPulse 2 was coupled to one. They now live here, under entity IDs built from the wallbox's own serial number, and the PowerOcean-side entries, those four and the Wallbox Vehicle entry the relay filled with a placeholder, are removed from the entity registry on the first start after the update. History and statistics recorded under the old IDs do not carry over, and an automation, a dashboard card or an Energy Dashboard slot that named one of them needs the new entity. The PowerPulse 2 has no vehicle reading; only the earlier PowerPulse reports one, on its PowerOcean. Nothing changes for the earlier PowerPulse (`AC31` series): it keeps reporting through its PowerOcean on the same five entities as before.

Start and stop of a charging session are available as two buttons with no PowerOcean in the integration entry or with exactly one (see Buttons). With exactly one PowerOcean, Home Assistant can set the maximum current, the solar minimum current, the custom charging current, the charging mode, the phase setting, Continuous Charging, Block Battery Discharge, Plug-and-Play, the screen and the LED indicator, and the brightness of both. With none, it can set the charging current (see Numbers, Selects and Switches). The Smart mode's departure time and target cannot.

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
| Wallbox Charging Current Setpoint | A | diagnostic | enabled | The charging current set for the session, not a live measurement. It can read 6 A while the wallbox is idle. The measured currents are Wallbox Current L1, L2 and L3. Equals the maximum current while charging at the limit and can sit below it otherwise |
| Wallbox Phase Mode | - | diagnostic | enabled | `single_phase` or `three_phase`: the phase mode in effect right now. The phase selection you configured (automatic, single or three) is the Wallbox Phase Setting select below; under automatic this reads whichever mode the wallbox is in |
| Wallbox Session Status | - | diagnostic | enabled | `idle`, `charging` or `finished` |
| Wallbox Session Duration | s | diagnostic | enabled | How long the current session has been running. Absent while no session is running, see the note below |
| Wallbox Charging Status | - | - | enabled | Unplugged, vehicle connected, charging, paused by charger, paused by vehicle, charging session ended, or fault |
| Wallbox Charging Mode | - | - | enabled | The charging mode set on the wallbox: `fast`, `solar`, `custom` or `smart`, the four modes the EcoFlow app offers. With a PowerOcean in the entry it follows the wallbox's settings report, about once a second, so a change shows within seconds. The wallbox's heartbeat, about once a minute, fills in while the latest settings report is missing, carries no mode or is more than 10 s old. Without a PowerOcean the heartbeat is the only source |
| Wallbox Session Start | - | diagnostic | enabled | When the current session began. Absent while no session is running |
| Wallbox Session Energy | Wh | - | enabled | Energy delivered in the current session. Restarts at 0 with every new session and is a `total_increasing` sensor, so Home Assistant books each restart as a new cycle. For the Energy Dashboard the lifetime counter Wallbox Total Energy is the better source. Absent while no session is running |
| Wallbox Session Solar Energy | Wh | - | enabled | Energy delivered in the current session from solar. Restarts at 0 with every new session. Absent while no session is running. See Session energy by source below |
| Wallbox Session Grid Energy | Wh | - | enabled | Energy delivered in the current session from grid. Restarts at 0 with every new session. Absent while no session is running. See Session energy by source below |
| Wallbox Session Battery Energy | Wh | - | enabled | Energy delivered in the current session from battery. Restarts at 0 with every new session. Absent while no session is running. See Session energy by source below |
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
| Wallbox Charging Current Setpoint | 6-16 A | 1 A | Sets the charging current for the running session. Same reading as the Wallbox Charging Current Setpoint sensor above; a write returns once the wallbox's own heartbeat reports the new value, or fails if it does not. In the one recording on file, a write sent during an active session confirmed in under a second; a write sent to an idle wallbox left the reading unchanged for close to an hour, and the recording cannot say whether that was a slow but effective write or one the wallbox never took - the first real write to an idle wallbox is what settles it |
| Wallbox Solar Minimum Current | 6-16 A | 1 A | The minimum charging current stored on the wallbox for Solar mode. It is a stored setting, and the integration does not limit it to one charging mode. In the hardware runs on file, the wallbox accepted it in Solar and in Fast mode, with Continuous Charging off, and it kept a value above the Maximum Current, so no such check applies. A write returns once the wallbox reports the new value on its settings report, or fails if it does not |
| Wallbox Custom Charging Current | 6-16 A | 1 A | The charging current stored on the wallbox for Custom mode, the current it uses once it is in that mode. It is a stored setting, and the integration does not limit it to one charging mode. In the hardware runs on file, the wallbox accepted it while in Solar mode. It does not store a value above the Maximum Current the wallbox reports (15 A over 14 A was never reported back on a C376), so such a value is refused before anything is sent, and the message names both values. A write returns once the wallbox reports the new value on its settings report, or fails if it does not |
| Wallbox Screen Brightness | 25-100 % | 25 % | The brightness of the wallbox's screen, in the four steps the wallbox reports: 25, 50, 75 and 100 %. It is a slider, so it only offers those four. The integration sends it whether the screen is on or off. The run on file changed the LED indicator brightness with the screen off, not this one. A write returns once the wallbox reports the new value on its settings report, or fails if it does not. Any other value is refused before anything is sent, and the message names the allowed ones |
| Wallbox LED Indicator Brightness | 25-100 % | 25 % | The brightness of the wallbox's LED indicator, in the same four steps. It can be changed while the Wallbox Screen switch is off, which is how the run on file changed it. A write returns once the wallbox reports the new value on its settings report, or fails if it does not. Any other value is refused before anything is sent |

The four current controls set four different things. Wallbox Maximum Current is the upper limit of the wallbox itself. Wallbox Charging Current Setpoint is the target for the running session and can change while it charges. Wallbox Custom Charging Current and Wallbox Solar Minimum Current are stored settings, one for Custom mode and one for Solar mode.

Wallbox Maximum Current is created only when the integration entry holds exactly one PowerOcean - the sibling route the two buttons below also use. Unlike the buttons, this control has no second route: an entry with no PowerOcean, or with two or more, gets no number, because no route from the wallbox's own channel has been observed for this write. Availability follows the sibling PowerOcean's connection, the same rule as the buttons on that route. Wallbox Solar Minimum Current and Wallbox Custom Charging Current have the same condition, exactly one PowerOcean in the entry. They also appear only after the wallbox has sent its settings report.

Wallbox Screen Brightness and Wallbox LED Indicator Brightness have the same condition: exactly one PowerOcean in the entry, and they appear only once the wallbox's settings report has carried the display values. They show what the wallbox reports and never a value of their own.

Wallbox Charging Current Setpoint is the mirror image: created only when the integration entry holds no PowerOcean at all, the wallbox's own channel. An entry with one PowerOcean, or with two or more, gets no charging-current number, because no route through a PowerOcean has been observed for this write. It appears with the wallbox's first heartbeat rather than waiting on a separate settings report, since the reading is already in that first heartbeat.

---

## Selects

| Entity | Options | Description |
|:---|:---|:---|
| Wallbox Charging Mode | `fast`, `solar`, `custom`, `smart` | Sets the charging mode, the same setting the EcoFlow app calls Fast charging, Solar Mode, Custom and Smart Mode. Same reading as the Wallbox Charging Mode sensor above. A change returns once the wallbox reports the new mode in its settings report, within seconds. Otherwise it fails after 20 s with the mode the wallbox still reports. `smart` is shown when the wallbox is in it but cannot be chosen from here: the app always sends a departure time and a charging target with it, and those are set in the app |
| Wallbox Phase Setting | `auto`, `one_phase`, `three_phases` (shown as Auto, One phase, Three phases) | Sets which phases the wallbox charges on. This select shows the setting you configured, while the Wallbox Phase Mode sensor shows the phase in effect right now. Changing it during a running session makes the wallbox interrupt charging briefly while it switches. In the one run on file, on a `C376`, charging resumed by itself. A change returns once the wallbox reports the new setting on its settings report, or fails if it does not |

Created under the same condition as the number above: exactly one PowerOcean in the integration entry, availability following its connection. The integration sends the mode alone, without the current or solar settings the app repeats beside it. An owner's run confirmed that the wallbox takes the bare mode, read back from the device for Fast, Solar and Custom. The Charging Mode select confirms on the settings report, which comes about once a second, so a change returns within seconds. The heartbeat, about once a minute, can confirm it only while the latest settings report is missing, carries no mode or is more than 10 s old. While the select waits, the maximum-current number and the two buttons report a write in progress. Wallbox Phase Setting has the same condition, exactly one PowerOcean in the entry, and appears only after the wallbox has sent its settings report.

---

## Switches

| Entity | Description |
|:---|:---|
| Wallbox Continuous Charging | The wallbox's Continuous Charging setting, shown under the same name in the EcoFlow app. It shows only what the wallbox reports. It stays unknown until the wallbox has reported it, and it changes only when the wallbox says so. A change returns once the wallbox reports the new state on its settings report, or fails if it does not |
| Wallbox Block Battery Discharge | The wallbox's "Disable battery discharge" setting in the EcoFlow app. On means the wallbox may not draw from the linked PowerOcean battery for charging. It shows only what the wallbox reports and changes only when the wallbox says so. A change returns once the wallbox reports the new state on its settings report, or fails if it does not |
| Wallbox Plug-and-Play | The wallbox's plug-and-play setting. On means a car that is plugged in can start charging without authorization in the app or by RFID card. With a car plugged in and idle, turning it on can start a charging session. Turning it off does not stop a session that is already running. It shows only what the wallbox reports and changes only when the wallbox says so. A change returns once the wallbox reports the new state on its settings report, or fails if it does not |
| Wallbox Screen | The wallbox's screen, on or off. It shows only what the wallbox reports. It stays unknown until the wallbox has reported it, and it changes only when the wallbox says so. A change returns once the wallbox reports the new state on its next settings report, or fails if it does not. The screen's brightness is a separate number and is sent whether the screen is on or off |
| Wallbox LED Indicator | The wallbox's LED indicator, on or off. It shows only what the wallbox reports. It stays unknown until the wallbox has reported it, and it changes only when the wallbox says so. A change returns once the wallbox reports the new state on its next settings report, or fails if it does not. Its brightness is a separate number |

Created only when the integration entry holds exactly one PowerOcean, and only after the wallbox has sent its settings report (for Wallbox Screen and Wallbox LED Indicator, a report that carried the display values). A change is built from the wallbox's latest settings report. If no report has arrived yet, or the last one is too old, the change waits up to 5 seconds for the next report; if none arrives, it fails with a message and nothing is sent. For a wallbox whose settings arrive only through the PowerOcean, the switches and the display controls (Screen and LED Indicator, with their brightness) accept a report up to 60 seconds old, because that report comes less often than the wallbox's own, and every settings change waits up to 60 seconds for the PowerOcean's next report to confirm it. If it does not come, the change fails with the usual "not confirmed" message and the control keeps the last reported value.

---

## Buttons

| Entity | Default | Description |
|:---|:---:|:---|
| Wallbox Start Charging | enabled | Starts a charging session, while Wallbox Charging Status reads Charging session ended (state value `finishing`) or Vehicle connected (`preparing`). The press returns once the wallbox itself reports `charging` or `suspended_charger` (Paused by Charger, a Solar-mode start that waits for surplus), or fails after 30 s if it does not |
| Wallbox Stop Charging | enabled | Stops the running session, while Wallbox Charging Status reads `charging`, or cancels a start that waits for solar surplus while it reads `suspended_charger` (Paused by Charger). The press returns once the wallbox reports Charging session ended (`finishing`) or Unplugged (`available`), or fails after 15 s if it does not |

The command takes one of two routes, the way the EcoFlow app sends it in each setup, and the confirmation is always read from the wallbox's own reporting: nothing is shown as done until the wallbox says so, and a press in a state the action does not fit (a start while charging, a stop while idle) fails with a message saying the current charging status does not allow it, instead of sending anything.

- **One PowerOcean in the same integration entry:** the command travels through the PowerOcean, addressed to the wallbox by the address the wallbox reports. The buttons appear once the wallbox has reported that address, and are unavailable while the PowerOcean's connection is down.
- **No PowerOcean in the entry:** the command goes on the wallbox's own channel. The buttons appear with the wallbox's first status report, and are unavailable while the wallbox's own connection is down.
- **Two or more PowerOceans in the entry:** no buttons; neither route has been observed on such an account.

A start is offered only from `finishing`, the state a stopped session sits in with the cable attached, and from `preparing` (Vehicle connected), where the app starts a session too. Starting from `available` (no session) has not been observed and is not offered.

---

## Notes

### Session energy by source

Wallbox Session Solar Energy, Wallbox Session Grid Energy and Wallbox Session Battery Energy are `total_increasing` sensors like Wallbox Session Energy. They add up to Wallbox Session Energy within a few Wh while the session runs, and exactly once it has ended. The mapping was checked against the EcoFlow app on one PowerPulse 2 (`C371`). In recordings of a `C374` and a `C376` the three add up to the session energy within 9 Wh, and on the `C374` exactly once a session had ended.

### The session readings are absent, not zero, between sessions

Wallbox Session Start, Wallbox Session Duration, Wallbox Session Energy, Wallbox Session Solar Energy, Wallbox Session Grid Energy, Wallbox Session Battery Energy and Wallbox Session Start Meter only exist while a session is actually running. Once the session ends, the wallbox stops reporting them and the entities go unavailable instead of settling on the last session's numbers - showing the previous session's duration or energy as if it were still counting would be worse than showing nothing. In `preparing` (Vehicle connected: cable attached, nothing flowing yet) the wallbox still reports them, and Wallbox Session Start is withheld when the wallbox reports it as zero, which it does before the first session after a restart.

### The cable lock can take a while to appear

Wallbox Cable Lock is only carried in the wallbox's bundled status report, never in a single push update. A setup that only sees pushes will not see this entity until the wallbox happens to send its bundle, which can take a while after startup.

### The wallbox is quiet between changes

The PowerPulse 2 reports when something changes and otherwise stays silent, charging or not. Over a complete seven-hour recording the longest silence was 50 minutes. The integration therefore waits 20 minutes before it asks the wallbox for a fresh report and keeps the entities available through an hour of silence; on every other device type that watch is much shorter. A wallbox that has gone offline is shown as unavailable after an hour, not after ten minutes.

### Several changes in a row

The wallbox takes one settings change at a time and confirms each on its next settings report. A change you make while an earlier one still waits for that confirmation is held and sent once the earlier one has finished, up to about 25 seconds, instead of failing. If you move the same control again while a change for it is held, only the last value is sent. The Screen and LED Indicator switches and brightness sliders and the three settings switches (Continuous Charging, Block Battery Discharge, Plug-and-Play) build on the wallbox's last settings report. When it is older than 10 seconds, or the wallbox has not reported since the previous change, such a change waits up to 5 seconds for the next report before it fails with the "settings report is too old" error; nothing is sent while it waits. The holding of changes covers those controls and also Wallbox Solar Minimum Current, Wallbox Custom Charging Current and Wallbox Phase Setting. Wallbox Start Charging, Wallbox Stop Charging, Wallbox Maximum Current, Wallbox Charging Current Setpoint and Wallbox Charging Mode are still refused with "An earlier command is still waiting for the wallbox to confirm it" while any write waits, and a settings change is refused the same way while one of those waits.

### Where the settings report comes from

A wallbox sends its settings report on its own connection, about once a second. A wallbox linked to the PowerOcean as a system component (PowerOcean settings, System Components) may instead report its settings only to the PowerOcean, which publishes them in its own message. For such a wallbox that copy is the settings report: the settings controls are created from it, show it and confirm changes on it. Once a wallbox has sent its settings on its own connection, that stays the only source until Home Assistant restarts or the integration is reloaded, and the PowerOcean's copy is ignored; both carried the same settings in the recordings that have both. The copy is used only when the entry holds exactly one PowerOcean, the same condition the settings controls have. The PowerOcean's copy is only read for the settings: power, status and the session readings still come from the wallbox itself. The display values arrive either in the usual six-byte form or in a 32-byte form, which is how a PowerOcean reports them: the Screen and LED Indicator controls are created from either, and a change is sent in the form the last report used. A display block of any other length is not read and the controls are not created from it. The PowerOcean also forwards the app's own copy of the settings, which can carry older values; that copy is ignored, so only the wallbox's real reports change what is shown. The diagnostics download shows the counts, and the longest gap between two copies, under `relayed_wallbox_settings`.

### Single-phase charging

When the vehicle charges single-phase, only Wallbox Current L1 carries a real reading; Wallbox Current L2 and Wallbox Current L3 read 0 A.

## Completed energy per vehicle profile

With EcoFlow account sign-in, enable **Track completed PowerPulse charging energy
by vehicle** in the integration options and select the charger. The option is
only shown while the entry has a PowerPulse 2 selected. Each profile
found in completed cloud orders gets a **Wallbox Completed Charging Energy**
sensor in kWh, followed by the profile name (for example **Wallbox Completed
Charging Energy Example EV**). A **Wallbox Completed Charging Energy Unassigned**
sensor collects the vendor's Other/unassigned profile. These dynamic sensors are additional to the
fixed sensor count above. No sensor is created for a profile with no completed
records. Once opted in, newly discovered profiles appear without reloading.
Profiles with blank names display as **Wallbox Completed Charging Energy Unnamed Vehicle**.

The history is read for every PowerPulse 2 that Home Assistant has set up from
this integration, the `C371` included. Live history has been checked on a
`C371`. The `C376` and `C374` use the same endpoint but have not been checked
yet. Do not use `watthCharge` as delivered energy; it is a different field.

The sensor attributes include the completed-session count and the name on the
latest completed record. Names come from charging records, not a live profile
catalogue; the entity label is set on discovery, while the `profile_name`
attribute follows the latest returned record. Reload after a rename to refresh
the label (or give the entity a custom name in Home Assistant). Entity identity follows the profile ID, so equal names do not merge separate cars.
Attribution follows the profile selected in EcoFlow for that session, not vehicle
identification: select the correct profile before charging another car.

Saved totals and entities restore before the first cloud fetch runs in the
background, so a slow history request does not hold up sensor setup. History is
polled every five minutes, independently of the live MQTT connection. All history
readers in an entry share one API client and sign-in. Failed sign-in or a rejected
refreshed session pauses authentication attempts for one hour across those readers.
The last read time for each charger and the shared authentication backoff deadline
are saved per entry as wall-clock timestamps, so reloads and restarts do not reset
either limit. The sensors become unavailable during the sign-in backoff and recover when authentication is reattempted.
Every page must succeed before any totals change. Requests use the verified
one-based `page` and `size` parameters. Inconsistent pagination, malformed records,
API failures, a 60-second overall timeout and the 100-page safety limit mark
the sensors unavailable and retain the ledger, rather than publishing incomplete totals. A subsequent successful
poll recovers automatically.

The initial total includes completed records still available from EcoFlow. It is
not a guarantee of lifetime history. Home Assistant stores a deduplicated ledger
under `.storage/ecoflow_energy_charging_history_*`; subsequent polls upsert orders
rather than adding the same energy again. Cloud retention or profile deletion
does not remove already collected orders. Back up this ledger with Home Assistant;
deleting it loses records the cloud no longer has. The ledger stores hashed order
and profile identities, names, energy and completion times, not raw API responses,
account IDs or RFID data. Invalid stored ledger data is ignored with a warning,
and the next successful fetch starts a fresh ledger.

Turning the option off unloads the history sensors but retains their entity
registry entries and saved ledger. Re-enabling it restores those totals. Removing
the integration entry deletes its saved history, including ledgers for chargers
previously selected in that entry. Back up Home Assistant before removing an entry
if you need to preserve records no longer held by EcoFlow.

These sensors use state class `total`, since a corrected record can reduce one
profile's total or move energy to another. They can be used as individual-device
energy counters. HA statistics record changes when orders are imported, not at
the historical charge times; initial history is a baseline and completed sessions
arrive as increments, not a live power curve. Do not add per-car counters and the
charger's overall energy as separate consumption sources, which would count the
same energy twice. The running session remains on the existing session-energy
sensor and is excluded here until it has an end time.
