# Changelog

All notable changes to this project will be documented in this file.

## [1.17.0] - 2026-08-06

### Added
- The Stream AC Pro (serial prefix `BK31`) gains switches for its two AC outlets in Enhanced Mode, so the sockets the EcoFlow app can switch are switchable from Home Assistant rather than only readable. Both writes are reproductions of frames captured from the app's own traffic, on ConfigWrite fields `380` and `381`, carrying the header naming the app that sent them rather than dropping it, and both were confirmed on live AC Pro hardware by the contributor who built them: acknowledged by the device, read back on the telemetry stream and checked in the EcoFlow app, with per-outlet power verified against a known load. The relay states come back on telemetry fields `980` and `982` and the per-outlet power on `1210` and `1211`, so what the switch shows is what the device reports rather than what it was last told. The other Stream models stay read-only on their outlets. They report the same telemetry, but reporting alike is no evidence of accepting the same write, so they share the one allowlist that already governs the AC Pro limit controls. (Ref #98)
- The Stream AC Pro (serial prefix `BK31`) gains a charge limit and a discharge limit in Enhanced Mode, so the two bounds the EcoFlow app offers can be set from Home Assistant rather than only watched. The device does not hold them as two separate settings: a recording of the app's own traffic shows the charge limit, the discharge limit and the backup reserve travelling together in one write alongside a timestamp, and that recording also carries a header naming the app that sent it, which is reproduced here rather than dropped. Every write therefore carries all three values, and the two the user did not touch are taken from what the device last reported instead of from a default, so changing one limit cannot quietly move another setting. Until the device has reported all three, the control refuses to write and says so rather than filling in the missing one. Two of these set in the same moment do not overwrite each other either, because Home Assistant runs a service call to two entities as two concurrent tasks and each of these writes has to read the current state before it can build its frame. The other Stream models stay read-only. They report the same telemetry, but reporting alike is no evidence of accepting the same write, and this one was confirmed on live AC Pro hardware by the contributor who built it, checked both in the EcoFlow app and in the telemetry the device sent afterwards. (Ref #98)
- The EcoFlow STREAM 5000 (serial prefix `ES21`) is recognized instead of being skipped as an unsupported device, and gets the 50 sensors and 2 binary sensors of the STREAM AC 5000. It is the same product as that one on a different model number, and a recording from a live unit sends the same four telemetry messages, so the readings come from the parser that is already there rather than from a guess based on the shared name. Replayed through it, that recording gives 26 readings that agree across independent messages: the house total the unit reports matches the sum of the flows feeding it to within five watts, and the reading from its grid meter matches the derived grid export to within a fifth of a watt. It does not get the controls the AC 5000 has. Every write this integration sends to that family is a rebuild of a frame captured from an AC 5000, and a power setpoint on these devices writes a scheduled task into the battery rather than flipping a display setting, so reading alike is not enough to assume writing alike, and nothing yet confirms a STREAM 5000 accepts one. Three blocks of readings stay unmapped on both models, and the owner who reported this one expects the solar strings among them. Naming them needs a reading taken off the EcoFlow app at a known moment to anchor them against, not simply a longer recording: there are already thirty recorded messages in which those values move, and comparing them against everything the integration already reads produces nothing but coincidences. If you own one, please say whether the readings match your EcoFlow app, and a recording taken alongside what the app shows at the same time is what would add the missing readings and, separately, the controls. Like the AC 5000 it needs the EcoFlow account sign-in, since the Developer API does not expose it. (Ref #231)
- The PowerOcean single-phase 5 kW hybrid inverter (serial prefix `J327`) is recognized instead of being skipped as an unsupported device, and gets the PowerOcean entity set. A recording from one of these units shows it speaking the PowerOcean command family, including the heartbeat this integration already reads, so the readings come from the existing parser rather than from a guess about the model. It reports more than that parser currently covers, so some of what the unit sends is not shown yet. If you own one, a note about whether the readings match your EcoFlow app would settle what is still missing. Like the other single-phase variants it needs the EcoFlow account sign-in, since the Developer API does not expose it. (Ref #225)
- PowerOcean Plus units with serial prefix `R372` are recognized instead of being skipped as an unsupported device. This is the 20 kW model, and it sits between the 15 kW and the 30 kW variants that were already supported, on the same product type and the same product family as both of them. That is the evidence it rests on rather than a recording from one of these units, so if you own one, a note about whether the readings match your EcoFlow app would settle it. Like the other Plus units it needs the EcoFlow account sign-in, since the Developer API does not expose it. (Ref #205)
- The DELTA 3 Max (serial prefix `D3N1`) is recognized and gets the Delta 3 entity set. It belongs to the same product family as the Max Plus, the Classic and the base unit, all three of which were already supported, which is what it is recognized on rather than a recording from one. It does not get the port priority controls: the EcoFlow app offers that menu only on serials starting `D3M` or `D51`, so on this model those entities would have been created and stayed empty forever. A smaller unit also has fewer ports than a Max Plus, so entities for ports it does not have stay empty. If you own one, please say whether the readings match your app. (Ref #216)
- The EcoFlow STREAM AC 5000 (serial prefix `ES22`) is supported: 50 sensors and 2 binary sensors. Until now it was skipped as an unsupported device, and it needed more than a serial prefix: despite the shared product name it has nothing in common with the BK-series Stream devices at the protocol level. It sends none of their messages, and where a Stream reports each power reading as its own value, this device sends a matrix of flows between grid, battery, house and solar, nested two to five levels deep. It therefore has its own device type and parser. The mapping comes from a capture of a live unit alongside the EcoFlow app, cross-checked against an independent smart meter reading the same grid: house consumption equals the sum of the flows into the house to within 2 W across every push frame checked. Solar and per-phase smart meter readings are created only once the device actually reports them, because which meter is linked in the app is an installation choice, not a model difference, and an entity Home Assistant once created stays in the registry forever. The solar reading is a figure the device works out for itself and reports even with no PV wired to the EcoFlow, so on an installation whose panels are a separate system it is the EcoFlow's inference rather than a measurement of it, which is why there is a live solar reading but no lifetime solar counter: a counter of that kind only ever counts up and could not be corrected later. Grid import and export are taken from the flow matrix rather than from the meter total, so both count in one direction only, which is what the Energy Dashboard needs. Battery power comes from the same flow matrix rather than from the device's own live battery field, which stops being sent the moment the unit goes idle and then reports a charge or discharge that has already stopped. It needs the EcoFlow account sign-in, since the device is not reachable through the Developer API. (Ref #177)
- A diagnostics download now shows where each energy counter stands and when it last moved. Many of the kWh readings are worked out here rather than read off the device, by adding up the power readings it sends over time, and the running state of that sum lived only in a file inside the Home Assistant configuration folder. Every counter now appears in the download with three figures: its current total, the power reading last added to it, and how many seconds ago that happened. The last of those is what makes a stalled counter readable, because a total near zero looks the same from the outside whether the device is reporting nothing or the reading stopped arriving nine hours ago. Counters that took a single reading and nothing since are listed too, which is the case that was invisible until now, since the sensor reads zero either way. No counter, entity or stored value changes. It only affects what a support download can answer on its own, instead of through a round of questions. (Ref #177)
- The STREAM AC 5000 gains controls, so the device can be driven from Home Assistant rather than only watched: the work mode, both SoC limits, the backup reserve and its level, the app's backup socket, and a charge and a discharge power setpoint. Every one of them is named after the reading that already reports it back, so the powers are Scheduled Charge Power and Scheduled Discharge Power rather than the app's "Max grid charging power" and "Max discharging power". A number in Home Assistant carries no description, so its name is the only text anyone reads while setting it, and one value under two names reads as two settings. The two power entities are setpoints and not limits, which is worth saying plainly because the device also reports two readings called Max Grid-tied Output Power and Max Grid Input Power. Those two are the ceilings held in the EcoFlow account and the app, neither is writable over this protocol, and both stay read-only sensors. A setpoint asking for more than a ceiling allows is accepted, acknowledged and then clamped by the device itself, measured here as a standing 2500 W charge request delivering 1218 W while the app's input limit sat at 1200 W, so nothing added here can drive a unit past the limit its owner agreed to. The setpoint is the interesting part, because this device has no direct power control at all: a scheduled task is its setpoint, so setting a power writes a task covering the whole day, enabled, at that power. The value asked for is therefore the value that acts, whatever the device happened to be holding, and a schedule made in the app is replaced rather than edited around, since a setpoint that only applies inside somebody else's window is not a setpoint. The one thing carried over from an existing task is the charge target SoC, because that decides what charging does rather than whether it happens, and a charge task set to stop at 80% should not be reset to 100% by a change of power. Deleting the last task in the app stops the readback rather than zeroing it, so the two setpoints report unknown once the device says its task list is empty, instead of going on showing a task that no longer exists. Whether a smart meter is linked in the EcoFlow app decides what the setpoint means, and it is the one thing worth knowing before automating this device. With a meter linked the device runs closed loop against it and will not discharge into an export, so the setpoint acts only as a ceiling on covering house load: 1400 W requested into a house needing 200 W delivers 200 W, and enabling feed-in does not change it. With no meter linked it runs open loop, which the app's own help text describes as any unused power flowing to the grid, and the setpoint becomes an absolute power command: unlinking a Tibber Pulse turned that same 1400 W request into a measured 1400 W discharge. An optimiser that wants to command power rather than cap it should therefore do its own metering. Charge and discharge are separate tasks that both cover the whole day, so setting one removes the other: two whole-day tasks are an overlap the device acts on by doing nothing at all. The removed one's readings go to unknown as it happens rather than at the next report, because a device that has deleted a task stops mentioning it instead of reporting it empty, so nothing would ever retract it and the entity would go on showing a task this integration had itself deleted. The charge task's target SoC survives that, so the 80% above is not quietly lost either. Zero is a real setpoint rather than an absence, since a 0 W discharge task parks the battery while removing every task leaves it running its own 200 W base output. A setpoint only acts while the device is in custom mode; in the other modes the write is accepted and ignored, and since the device reports no error, a warning goes to the log instead. Two of these controls set in the same moment do not overwrite each other, which takes some care on this device: Home Assistant runs a service call to two entities as two concurrent tasks, and every one of these writes has to read what the device currently reports before it can build its frame, because three of the settings travel two to a field and a power setpoint is a removal followed by a write. Left to interleave, each would act on the state from before the other's change, so setting both power setpoints together would leave the device holding both tasks, which is the overlap it answers by doing nothing at all, while both entities reported success. The two SoC limits and the backup reserve would lose one of the two changes instead, and the reserve is the one that no per-platform guard could have covered, since its on and off is a switch while its level is a number. All of these now run one at a time. Every write is a reproduction of a frame the app itself sent, captured from its own traffic and rebuilt byte for byte, then confirmed on live hardware three ways: acknowledged by the device, read back on the telemetry stream, and checked in the EcoFlow app. It needs the EcoFlow account sign-in, since the device is not reachable through the Developer API. (Ref #177)
- A PowerOcean's module report is read rather than discarded. The unit sends a list of the parts it is built from - its own control module, its inverter module and each attached battery - and each entry can carry either that part's serial or an error code for it. Until now the message was not decoded at all, so a module reporting a fault said so in a message this integration threw away, and a support download showed it as raw bytes rather than as something readable. No sensor comes from it. Every entry in every recording checked carried a serial and none carried an error code, so what a fault actually looks like here is not known, and an entity built on a value nobody has seen would be a guess. The serials are masked out of a support download as device identifiers, so they are not what this is for. It is read so that the first fault report anyone receives is legible instead of being a row of hex.

### Fixed
- A support download no longer cuts off the one message that carries a device's whole state. When a device answers with everything it knows at once, that answer arrives as a single frame holding many messages, and the download kept 4 KB of it. That number has now been too small twice. It was raised from 2 KB after a PowerOcean sent 2906 bytes, and it cut every such frame an EcoFlow Ocean 2 sends: a 16 hour recording from one holds nine of them, 12 to 14 messages each at 4956 to 5899 bytes, all nine truncated. A fixed number cannot know how wide a device's state is, so it is no longer fixed. A frame that demonstrably carries several messages may now claim one message budget for each of them, which is the width the frame itself declares, bounded at sixteen messages so a single frame cannot claim the whole download. The previous 4 KB stays as the lower bound, so nothing that fit before fits less well now. This changes only what a support download contains. No device, entity or reading is affected, and nothing more is recorded than before: the same frames are kept, in full rather than in part. (Ref #145)
- Restarting Home Assistant no longer puts a burst of warnings in your log. Every restart produced a run of lines reading "MQTT message handler error ... Event loop is closed", one for each reading that happened to arrive while Home Assistant was shutting down: 21 of them across five connections in ten seconds on the installation this was found on. Nothing was wrong and there was nothing to do about it. Home Assistant does not unload an integration when it stops, it announces the shutdown and then closes down the machinery underneath, so the connections to EcoFlow were never told to close and went on handing over readings that had nowhere left to go. They are now closed when that announcement arrives, and a reading that still arrives in the moments after is dropped quietly instead of being reported as a fault. This was only ever visible on a full restart, never on a reload of the integration, which is why it survived this long. No device, entity or reading is affected.
- A PowerOcean's settings report is read rather than skipped. This message carries the parameters the EcoFlow app shows on its settings pages, and until now one value of it was read: the surplus percentage. Thirteen more are now, among them the rating of the main breaker your system is wired behind, which reads 35 amps on one unit and 63 on another, and a block of six values describing peak shaving that used to arrive as an unreadable lump of 27 bytes. No sensor comes from any of them. The breaker rating is set once when the system is commissioned and never moves, and everything else reads zero until the matching feature is switched on, so a sensor today would be a row of zeros. They are read so that a support download names them instead of listing bare numbers, which is what makes the next one worth asking for. The scheduled tasks this message also carries are deliberately still skipped, because nothing here has ever seen one. This message arrives rarely and on no schedule anyone has worked out: three recordings of the same system contain it twice and once not at all, both times with identical contents and neither at the moment the connection opened. Guessing the shape of something never observed would produce a reading that looks right and is not, which is worse than the gap. If you use scheduled charging or discharging on a PowerOcean, a diagnostics download is the missing piece, and one taken shortly after you change a schedule in the app has the best chance of containing it. (Ref #225)
- A PowerOcean's most frequent message is no longer read as empty. These units send a change report several times a second, and on one single-phase inverter every single one of them arrived carrying two values this integration did not know: the state of an IEEE 2030.5 link, which is how a network operator controls an inverter enrolled in a flexibility scheme. Unknown values are not an error in this format, so the message parsed cleanly and contained nothing, 1484 times over five minutes. Forty-three further values are now read as well: the per-string solar power that sits alongside the solar total, and the grid protection settings the inverter was commissioned with, meaning its voltage and frequency trip thresholds and their trip times. None of these becomes a sensor. The protection settings are set once by an installer and never move, and per-string solar is already published from another message that reports voltage and current per string as well. They are read so that a support download stops listing them as values of unknown meaning, which is what buries the ones whose meaning genuinely is unknown. The grid control link is the interesting one and does not become a sensor yet either: only a unit actually enrolled in such a scheme reports it, and an entity that stays empty forever on everyone else's system is worse than no entity. If your PowerOcean is on a network operator scheme, a diagnostics download would settle whether this is one installation or a whole region. (Ref #225)
- A diagnostics download from a PowerOcean now contains the whole of its full state message. The limit on how much of a bundled message is kept was set at 2048 bytes, taken from the widest one known at the time, which came from a STREAM AC 5000 and ran to about 1465. A PowerOcean sends far more in one go: a download from a live unit carried 2906 bytes, and everything past the limit was dropped. That tail held eight of the twenty-four message types the device sends, so a third of what it talks about reached the file as names with nothing behind them. The download did say the message had been shortened, which is how this was found at all, but a warning that fires on every PowerOcean is a limit set too low rather than a warning. The limit is now 4096 bytes, which carries the observed message with room for a unit reporting more battery packs or more solar strings. Nothing about your devices or entities changes, only what a support download can answer. (Ref #225)
- The device picker in the integration options names each device by its model again when the fresh device list cannot be fetched from EcoFlow. That fallback labelled every device with the generic name of its family, so a STREAM 5000 appeared as "STREAM AC 5000" and a DELTA 3 as "Delta 3 Series", while the rest of the integration already derives the right name from the serial number. The fallback now uses the same derivation, and the family name remains only for devices whose serial prefix has no name of its own.
- The connection to EcoFlow now goes to the server your account was actually assigned to, instead of to one fixed address. Both ways of signing in ask EcoFlow for credentials, and the answer names the server those credentials belong to alongside them. That part of the answer was thrown away and a built-in address used instead, which is correct for accounts served from Europe and wrong for the rest. An account served elsewhere gets refused there, and the refusal is as quiet as it could possibly be: the connection opens, the server closes it again without saying anything, and the whole thing repeats. On a supported device that reads as a device that never comes online. It surfaced on an unsupported one, where a raw data capture ran for over nine hours across 559 attempts without a single session, and the diagnostics could only report the last dropped link rather than the reason. Nothing changes for anyone whose connection already worked: both credential endpoints were checked against a live account and hand back exactly the address that was hardcoded, so the same server is used as before. (Ref #184)
- A raw data capture that never connects now says which server refused it and how. The download named neither, so a capture that came back empty looked the same whether the address was wrong, the credentials were, or the device simply had nothing to say. It now reports the server it was talking to, and it keeps a refused login apart from the dropped connections that follow it. That distinction is the one that was missing: a refusal is answered by thousands of retries, each of which ends in a dropped link, so the one event that explains the empty capture was overwritten by the last of many copies of its own aftermath. A capture that was refused now says so, with the reason the server gave, and a capture whose link died before the server ever answered says that instead of claiming a refusal nobody sent. A supported device names its server in the download too, for the same reason: when this goes wrong on one of those, the device simply never comes online, which from the outside looks like a device with nothing to say. (Ref #184)
- The STREAM AC 5000's four energy counters no longer stay at zero while the power readings above them are correct. Grid export ran between one and five kilowatts in bursts across a whole day with its counter reading zero the entire time, and only one long unbroken export block moved it at all. Most kWh readings are worked out here rather than read off the device, by adding up the power readings it sends over time, and the rule doing that adding treated any change of more than half between two readings as untrustworthy and credited the lower of the two instead. When one of them was zero, the lower one was zero, so that stretch of time counted for nothing. This device reports each flow between grid, battery, house and solar as a separate reading, and a flow that is not running is a real zero, so its readings alternate between zero and a value all day and nearly every stretch was thrown away. Each stretch is now counted at the average of the two readings that bound it, which is the honest estimate when a load switches at a moment nobody recorded: what that average over-credits as something switches on, it gives back as the same thing switches off. Other devices met the same rule far less often and will count slightly more from now on wherever a reading returns to zero or swings hard between two updates. That is mainly grid import and export and battery charge and discharge, which come in pairs where one of the two is always exactly zero, so every change of direction crossed it. The Smart Plug's energy counter and the input and output counters of the portable units meet it too, every time the appliance behind them starts or stops, because a standby reading of a few watts next to a running load is also a change of more than half. Steady running is unaffected: 37 consecutive readings from a PowerOcean exporting in daylight give the same total to the last digit under either rule. Counters that already exist keep their values and nothing is recalculated backwards, so only what accumulates from here on differs. (Ref #177)
- Readings that can only ever be whole numbers no longer show a decimal place. A battery cycle count read 412.0 instead of 412, and the number of battery packs online read 3.0 instead of 3, which invites the question of what 3.5 packs would be. It affects 42 readings across PowerOcean, Delta 2 Max, Delta 3 and the Smart Plug: the cycle counters, the error, warning and fault codes, the pack and cell counts, the fan and LED levels, the raw state and mode readings of a PowerOcean, and the charge and health readings of the expansion batteries on a Delta 2 Max. The cause is that every reading is carried as a decimal number on its way through, whatever it represents, and these particular readings had never been told how many decimal places they have, so they were shown with the one the conversion left behind. The numbers themselves do not change, only how they are written, so history and statistics carry over untouched and no entity is added, removed or renamed. (Ref #220)
- The STREAM AC 5000 solar reading no longer stays at its last daylight value all night. The device stops sending the figure when there is nothing to report rather than sending a zero, and a reading that stops arriving simply keeps its previous value, so on a system with panels the sensor showed the afternoon's production at three in the morning. It now falls to zero. The entity is still created only on a system that has solar, which is what withholding the zero used to achieve, except that it now waits for a reading that is actually solar rather than for the reading to appear at all. One diagnostic sensor is gone with it: Home From Solar rested on a field that appears in none of the 1239 recorded messages, and a guessed field feeding an entity Home Assistant keeps forever is not worth the one reading it would add. It comes back when a recording from a system with panels shows where that value really sits. (Ref #177)
- The STREAM AC 5000's Max Grid Input Power reading reported a different number than the setting it is named after. The device sends several power figures side by side, and the one being read is a ceiling that moves only when EcoFlow raises the account limit, not the charge-side limit set in the app. Until someone lowers that limit the two hold the same value, so the reading looked correct on the unit it was built from. Set the app's Max grid input power to anything below the ceiling and the sensor went on showing the ceiling. It now reads the setting, confirmed on hardware by moving it to 1200 W and back to 2500 W and watching that one field follow while the others stood still. This matters to anyone reading it to decide how hard to charge the battery, because it was reporting headroom the device would not accept. (Ref #177)
- A diagnostics download now contains the whole of the message a device sends when it is asked for its full state. Every captured message was kept only up to its first 512 bytes. That was enough for the small updates a device pushes as things change, but not for the one message that carries everything at once: on a STREAM AC 5000 that message is around 1440 bytes and holds six parts, so two thirds of it, including several settings, never reached the download. Nothing in the file said it had been shortened either, and that is what turned a size limit into wrong answers: settings the device does report were read as settings it does not have, and the owner was asked to confirm something the download had thrown away. How much of a message is kept now depends on what the message carries, so a full state arrives whole while an ordinary update costs no more space than before, and anything that still had to be shortened now says so. This changes nothing about your devices or your entities, only what a support download is worth. (Ref #177)
- A charge or discharge limit changed in the EcoFlow app took minutes to reach Home Assistant instead of seconds. The device reports both limits on two different messages, and the one being read only appears in the full state dump it sends when asked, which happens on connect and seldom after that. The other message carries them continuously. Measured on hardware over one thirteen-minute window: the continuous message carried the limits 83 times, the state dump 4, and a limit changed in the app showed up 4 minutes 27 seconds later than it needed to. The limits are now taken from the message that actually carries them, and the state dump stays unmapped, so there is still exactly one source for the value. (Ref #177)

### Changed

- Log messages write an aside with a plain hyphen instead of a long dash. Purely cosmetic, and noted only because these lines are written into your Home Assistant log rather than kept here. Nothing about what is logged, when, or at which level changes, and no text you see in the interface is affected.

## [1.16.0] - 2026-08-06

### Added

- Delta 3: the four idle shutdowns are now available from Home Assistant, which completes the EcoFlow app's "automatic shutdown" page alongside the screen timeout below. The unit itself, both AC outlets and the 12 V group each get the app's own eight steps: 30 min, 1 h, 2 h, 4 h, 6 h, 12 h, 24 h and Never. These are idle shutdowns rather than timers, and the difference matters before anyone automates them: the device switches an output off only when it detects no load connected and no activity for the configured span, so something that keeps drawing power keeps its output alive. The case to watch is a load the device does not count as one, such as a trickle charger or a standby draw below its detection threshold, which looks like an empty outlet and loses power. That is how the device behaves from the app too. Never means it never switches off, the same way round as the screen timeout, and it sits last in the list for that reason. The eight steps and the unit were read off the device rather than derived: the 12 V page showed two hours ticked while its field carried 120, so the unit is minutes, and all four writes were confirmed on hardware with the device echoing back which setting it had applied.

- Delta 3: the screen timeout is now available from Home Assistant, the setting the EcoFlow app calls "LCD screen timeout" under automatic shutdown. Six steps, the app's own: 10 s, 30 s, 1 min, 5 min, 30 min and Never. This is worth being precise about, because it is the closest the device gets to a dark display and it is not a switch. There is no command anywhere in this device family that powers the panel down, the app offers no such control either, and turning the brightness to zero does not do it - that is a backlight level, and the panel stays lit at the bottom of its range, which was confirmed on hardware with the value sitting at zero on the wire while the screen was visibly on. Pressing the physical button does switch the display, but the device never reports that state, so it cannot be shown or driven from outside. What the shortest timeout gives you is a screen that goes dark ten seconds after the last touch, and that part is now automatable. Note that Never means the screen never switches off, not that it never comes on: it is the last option in the list for that reason, with the shortest timeout first. Every value behind the six steps was read off a device rather than derived from the protocol, by holding a screenshot of the app's own settings page against a recording taken minutes earlier - all five rows of that page matched their fields, which is also what settled what Never means. The write was confirmed the same way: the device acknowledged it and reported the new value back within about two seconds. It needs the EcoFlow account sign-in, since this setting never appears in the polled data and there would be nothing to confirm a write against with developer keys.

- A diagnostics download now reports the device firmware, and the bug report form asks for it. Neither of the two device list endpoints EcoFlow offers returns a firmware version under any name, which was checked against a five device account: the one reached with developer keys answers with the serial, the name and whether the device is online, and the one behind account sign-in adds a creation date and a product id. The integration had been reading a field that is never sent, so the version shown in the Home Assistant device page was blank for everyone, in both modes, since the beginning. The polled API data does carry revisions on some models, one per subsystem, and those are now collected and shown in a diagnostics download with the raw value alongside the decoded one, so a reader can check the reading rather than trust it. A DELTA 2 Max reports four of them. A PowerOcean sends 347 values in the same response and not one of them is a version, and the live connection used by account sign-in does not carry one either, which was checked against 80 messages from a running system. For those devices the only place a firmware version exists is the EcoFlow app itself, under the gear icon of the device, and the bug report form now asks for it there. This matters because several reports of readings that stay empty have turned out to depend on what the device runs, and until now nothing in an issue said what that was. The device list dropdown on the same form also caught up with reality and now offers PowerOcean Plus, Delta 3 Max Plus, Stream and Stream Micro.

- Delta 3: port priority is now available from Home Assistant, the setting the EcoFlow app calls "Port priority". Each output port - AC 1, AC 2 and the DC group - can be marked non-essential and given a battery level at which it stops being powered, so a fridge keeps running while a non-essential outlet is dropped instead of both draining the pack together. Three switches and three cutoff levels, plus a diagnostic sensor for whether the feature is currently in effect. It only engages when the unit runs on battery or solar with no AC or grid input and no smart generator connected, which is why that sensor reads off on a grid-connected device even though the settings apply. That sensor turns on the moment the AC input drops rather than when a port is actually switched off, and the device reports it within about a second, which makes it usable as a fast mains-failure trigger for automations. The cutoff range is not fixed: the device derives it from the battery's own charge and discharge limits, and so does this integration, which is why a slider whose scale reads 0 % to 100 % actually stops at 5 % and 95 % on a unit with default limits. Changing either half of a port's setting sends both, because the device treats them as one value, and the other two ports are never touched by a write. In the first minutes after a restart, before the device has reported where a port currently stands, a change is refused with a note to try again in a moment rather than guessing the other half and quietly overwriting a setting made in the app. Available on units whose serial starts with `D3M`, matching where the app offers the setting, and it needs the EcoFlow account sign-in because these values only travel on the live connection. Every value was checked against a device: the wire meaning was read off hardware with the app open on the same screen, and both writes were confirmed by the device acknowledging them and reporting the new value back.

- Delta 3: the AC charge power is now adjustable from Home Assistant, 200 W to 2400 W in 100 W steps. It is the same setting as the charge speed slider in the EcoFlow app, which is what makes grid charging drivable from a dynamic tariff or from solar surplus rather than from the app's fixed schedules. Setting it switches the device to the app's custom charging mode, exactly as moving the slider there does: the device treats the wattage and the charging mode as a single setting, ignores a change to either one on its own, and does so silently, without accepting or refusing anything. Switching back to battery-optimised or silent charging stays in the app, because the device reports its charging mode only when that mode changes, which is far too rarely for Home Assistant to show a mode selector that could be trusted. The charge power itself is read back from the device within a second of every write. It needs the EcoFlow account sign-in, since this setting never appears in the polled data and there would be nothing to confirm a write against with developer keys. (Ref #111, #100, #181)

- PowerOcean units with serial prefix `J32B` are recognized instead of being skipped as an unsupported device. A raw capture from @Jemster68's unit settled it: among the messages it sends is one on the command family that only PowerOcean systems use, and this integration already decodes it, so nothing but the serial prefix was missing. The same capture also shows three message types this integration does not read, which means the unit reports more than it currently delivers. If you own a `J32B`, please say whether the readings match your EcoFlow app once you update, since that is what turns this from a well-founded assumption into a confirmed device. (Ref #194)

- PowerOcean gateways with serial prefix `HJ35` are recognized instead of being skipped as an unsupported device. This one rests on a report from another project rather than on a capture from an owner of this integration, so it is the assumption that the layout matches the other PowerOcean gateways, not a demonstration. If you have an `HJ35` unit, a short note about whether the readings look plausible would settle it. (Ref #165)

- The base DELTA 3 (serial prefix `P231`) is recognized and gets the Delta 3 entity set. Until now it was skipped with "no parser available" although the parser it needs already existed. A capture from @peterwoodhome-ux settled it: his unit sends the same three message types as a Delta 3 Max Plus, and the status message decodes through the existing field map with 23 values, so nothing but the serial prefix was missing. A base unit has fewer ports than a Max Plus, so entities for ports it does not have stay empty. (Ref #182)

- Delta 3: the AC charge power limit, the value behind the charge speed slider in the EcoFlow app, as a diagnostic sensor. @NatsumiCH pinned it down by moving the slider from 1000 W to 1200 W between two diagnostics downloads and showing that exactly one value in the device's own report followed, which is proof rather than a plausible guess, and a capture from a second, unrelated device carries the same value. It arrives on the live connection only, so it needs the EcoFlow account sign-in and stays empty with developer keys. The setting is also adjustable in this release, see the entry above. (Ref #181, #111)

- Support for the Stream Micro (`BK01`). It is a grid-tie solar inverter rather than a battery, and it reports both solar strings with power, voltage and current, its single-phase grid connection with voltage, current, frequency and power, the grid connection state, the feed-in limit configured in the EcoFlow app, and the WiFi signal strength. Solar production per string is available for the Energy Dashboard. The Stream Micro is not exposed through the EcoFlow Developer API at all, so it needs the EcoFlow account sign-in. It gets no battery, backup reserve or AC outlet entities, because it has none of those and Home Assistant keeps an entity in the registry once it has been created. Built entirely from a six hour recording contributed by @ManuelScholz8794, who ran the capture on his own unit. (Ref #141)

- Delta 3 Max Plus: battery health readings from the battery management system, available with EcoFlow account sign-in. State of health, charge cycles and the lifetime charge and discharge energy the battery itself counts, plus cell voltage spread, cell and MOSFET temperatures and the capacity values as disabled diagnostics. The lifetime energy counters are read from the device rather than calculated from power, which makes them usable in the Energy Dashboard without the drift that integrating over time introduces. None of this exists in the polled API data, so these entities stay empty with developer keys.

- PowerOcean: arc-fault detector state per solar string, MPPT warning codes, battery line and relay fault flags, and the self-check, heating, calibration and parallel-mode states. All disabled by default, because they only say something when the system is in trouble. The PCS and MPPT error and warning codes now also work with EcoFlow account sign-in, where they previously stayed empty because the message that carries them was not being read.

- PowerOcean: SG Ready state and enabled flag, and the reason the system is limiting the battery. All three travel in a message the integration already read but had not decoded.

- PowerOcean: the highest internal temperature the system reports, as a diagnostic sensor.

- PowerOcean: water temperature, target power and target temperature for an attached PowerGlow heating rod, alongside the power reading. All four are read-only, disabled by default, and only created on systems that actually report a heating rod. They need Standard Mode, because they come from the API quota and that quota is only polled with developer keys. With EcoFlow account sign-in they do not appear. The heating rod does not report the same field names on every system, so each reading is looked up under every name the accessory is known to use.

- PowerOcean diagnostics now include the raw API quota, the same section Delta 3 and Stream already had. Accessories report through the PowerOcean rather than as devices of their own, and their field names are documented nowhere, so a diagnostics download from an owner is the only way to learn what an accessory actually contributes.

- A diagnostics download taken with EcoFlow account sign-in now lists the message fields a device sends that the integration does not decode yet, by field number and current value. Until now those fields were dropped before they reached anything: a value the integration has no name for was indistinguishable from a value the device never sent, so the only way to find out whether a device reports a setting was to ask its owner to register for developer keys and dump the polled API instead. That detour does not even work for settings that never appear in the polled data, which is most of the ones people ask to control. The new section makes a single diagnostics download answer the question directly, on the connection the device already uses. Values are shown as sent for plain numbers; anything else is shown by size only, so a download stays safe to attach to a public issue. (Ref #111)

- PowerOcean: optional Heating Rod Power sensor for systems with a PowerGlow accessory. The heating rod reports its power through the PowerOcean itself rather than as a device of its own, so no separate setup is needed. The sensor is read-only and disabled by default, and it is only created on systems that report a heating rod. Enable it under Settings, Devices, PowerOcean, Entities. Contributed by @Xygen. (Ref #7)

### Removed

- A Delta 3 "AC charge mode" selector that briefly existed in v1.16.0-beta.11 and beta.12 is removed instead of sitting on the device page permanently unavailable. It was withdrawn during that beta because the device reports its charge mode only when the mode changes, far too rarely for a control that has to show where the device stands - but Home Assistant keeps a record of every entity it has ever seen, and nothing deletes one when it disappears from the code. Anyone who ran one of those two betas has been looking at a dead selector ever since. It is now cleaned up on the next start. Only people who installed a beta are affected; no released version ever had it.

### Fixed

- A Stream could reset its two lifetime battery capacity readings and make the long-term statistics count the standing total a second time. The battery management system reports how much charge has passed through the pack since the beginning, and it reports a zero when there is nothing to report yet, which is what a factory-new pack sends and what a pack whose management system has been reset sends. The protocol declares those two fields in a way that puts the zero on the wire rather than leaving it out, so the zero arrived and was published on a reading that only ever counts up. Home Assistant reads a value of zero on such a reading as a meter change and adds the old total to everything that follows. Both readings are diagnostic and switched off by default, so this only reaches installations that turned them on. A zero is now treated as what it means, nothing to report, and the reading stays empty until the pack has something to say.

- A device variant that cannot produce a setting can no longer be given a permanent control for it anyway. Four of the five kinds of entity already drop what a variant never fills, using a list held per serial prefix, and the dropdowns did not. Home Assistant remembers every entity it has ever created, so the first variant without one of the automatic shutdown settings would have received that dropdown once and kept it unavailable forever. No device is affected today, because nothing on that list is a dropdown. This closes it before one is.

- The raw data capture for an unrecognised device usually came back empty, no matter how long it had been left running. The capture keeps a connection open for up to 24 hours, and every connection of that kind gets dropped at some point in a day. Rebuilding it needs a fresh session identifier, because the server refuses one it has already seen, and the part of the integration that does this only exists for devices that are supported. An unrecognised device has none, so the first drop was the end of the recording, and whoever had switched the capture on downloaded a file with nothing in it. It now re-establishes the connection on its own for as long as the capture runs. A recording that comes back empty is also no longer silent about why: the download says whether the connection was refused and for what reason, whether it was ever up at all, how long it has been running, and how often it dropped. Before this it said only that the connection was down at that moment, which fits a refused login, a link lost hours earlier and a device with nothing to say equally well. This matters because a recording from the device is the only thing support for a new model can be built on, and asking for a second attempt without knowing what went wrong in the first one costs the person who volunteered another day. (Ref #184)

- A diagnostics download from a system with more than one battery pack reported only one of them. Serial numbers are replaced before the file is written, and every serial was replaced with the same placeholder. The PowerOcean data addresses each pack by its serial in the key itself, so both packs ended up under one identical key and only the last one survived. The file said one pack, plainly and without any sign that something had been dropped, and it is the file used to answer questions about how many packs a system has. Distinct serials now get distinct placeholders, so a two-pack system reports two packs. Nothing about the masking itself is weakened: the same serial reads the same throughout one download, no placeholder can be traced back to a serial, and nothing carries over between downloads.

- Log messages no longer contain full device serial numbers or the address they were sent to. Thirty-four places wrote the complete serial into the log, and the message topics carried it a second time along with the EcoFlow account identifier. This matters because a bug report often starts with a request to switch on debug logging and attach the result, which makes a log line as public as the diagnostics download that was fixed for the same reason earlier in this release. Serials are now shortened to their first four characters, which is what identifies a device in a report anyway, and addresses are written with the serial and the account replaced. Message bodies are covered too: two lines wrote the content of a command and of a device reply straight into the log, and a command carries the serial inside it, so shortening it everywhere else would have left it in the one place that is quoted verbatim. The content is still logged, because it is what makes a failed setting debuggable, with anything shaped like a serial replaced before the line is shortened rather than after. A test now holds every logging call in the integration against all of these rules, so a line added later is covered whether or not anyone remembers.

- The message about an unsupported device no longer asks half the people who read it to find a switch that is not there. It told everyone to turn on the raw data capture in the integration options, but that checkbox is only offered with EcoFlow account sign-in, so anyone using developer keys went looking for a control their setup never shows. In that mode nothing has to be turned on at all: a diagnostics download already carries the raw data an unrecognised device reports, because the same keys that fetch the readings can fetch it directly. The message now says whichever of the two applies. This matters beyond the wording, since a recording from the device is the only thing support for a new model can be built on, and the previous instruction quietly cost the ones that never arrived. (Ref #188)

- A PowerStream is no longer mistaken for a Stream battery. Device detection matches product names by substring, and "PowerStream" contains "stream", so the microinverter was given the whole Stream entity set. It connected, reported nothing that parser understands, and settled into a stale state, which left the owner looking at a fully created device with several dozen readings that could never fill. The PowerStream is a different product line and is not supported. It is now reported as an unsupported device, which is the state that actually asks for something: attach a diagnostics download to an issue, with the raw data capture switched on first if you use EcoFlow account sign-in, and there is a path to real support. (Ref #188)

- A diagnostics download no longer contains the serial number of the device or the account it belongs to in the event log. Every time a control was used, the device acknowledged it and the integration recorded that acknowledgement together with the full address it arrived on. That address contains the complete serial and the account identifier, and the event log was the one section of a diagnostics download that nothing checked before writing it out. Anyone who followed the request to attach a diagnostics download to an issue published both. The acknowledgement is still recorded, without the address. Beyond that, the whole download now goes through the same check on the way out instead of each section looking after itself: three separate leaks in this release were all found in a section next to the one being worked on, so a section added later is covered whether or not anyone thinks about it. Captured device frames are the one exception, because they are already masked when they are recorded and running the check over them a second time would corrupt them. In the same pass, the log messages that report a rejected or undeliverable setting now name a device by the first four characters of its serial rather than in full, which is what the rest of the integration has always done and matters because logs get attached to public issues too. Present since v1.3.3, which means it also affects v1.15.0 and every earlier version.

- Live data could stop arriving from a device and never come back until Home Assistant was restarted. The capture that keeps a sample of every message type sorts frames by the time they arrived, and it assumed time only moves forward. When the system clock stepped backwards, which is routine on a Raspberry Pi without a real time clock as soon as it syncs time after booting, one frame ended up before the start of the recording and the capture tried to make room for it forever. It runs on the connection's own thread and holds a lock while it does, so that device stopped receiving anything, and a diagnostics download afterwards would hang on the same lock. Reproduced with a single one second correction. Frames older than the start of a recording now simply share the oldest slot.

- Thirty-seven entities that can never have a value with developer keys are no longer created in that mode, eight of them previously switched on by default. The Delta 3 battery health readings, the PowerOcean maximum internal temperature, the MPPT warning codes and the arc fault, self check, heating, calibration, parallel mode and SG Ready states all travel exclusively on the live connection used by EcoFlow account sign-in. With developer keys they were created and stayed empty, which reads as a device that has not reported yet rather than a reading that is never coming. This is the same mistake as the heating rod above with the two modes swapped, so there is now a test that holds every entity offered in a mode against what that mode can actually deliver. Entities already in place are not removed, so nothing is lost for anyone who has them.

- The two PowerOcean sliders now really do fall back to the device value when a write fails. The fallback shipped in this release took its idea of the previous value a moment too late, after the slider had already been moved to the requested position, so it restored exactly the value that had just failed. In practice the slider kept showing a setting the device had refused, which is the problem the fallback was added to solve. It now remembers the value before the move, releases the slider immediately instead of holding the requested position for five more seconds, and a slow failing write can no longer undo a newer one that has already succeeded.

- A write that never reached the device no longer looks like it worked. Two things went wrong together. A command was counted as sent once it had been handed to the local message queue, which says nothing about whether it left the machine: on a connection that had gone dead without being noticed, every write was accepted and none arrived. And a control whose command could not be delivered simply returned, so Home Assistant went on showing the value that had been requested while the device kept the old one, with nothing but a log line to show for it. Reading kept working the whole time, which made the setting look like it had been applied and then reverted on its own. Commands are now confirmed by the message broker before they count as sent, and every switch, slider and select reports an error when a command was not delivered. The value shown falls back to what the device last reported, and an automation that depends on the write now fails instead of continuing on a value the device never accepted. The two PowerOcean sliders are sent a moment after the request so that dragging one produces a single command rather than a dozen; their result arrives too late to be reported as an error, so on failure they simply fall back to the value the device reports instead of keeping the one that did not arrive. Reproduced on a DELTA 2 Max and a Smart Plug with the connection deliberately cut: the same writes that previously reported success and changed nothing now report the failure. (Ref #185)

- The four PowerGlow heating rod readings were created on every PowerOcean, including systems with no heating rod attached. They were disabled by default, so nobody was forced to look at them, but anyone browsing the entity list of their PowerOcean found four entities for an accessory they do not own, and enabling one produced a permanently empty sensor. The heating rod is now treated as what it is: an optional accessory. Its entities are created once the system actually reports it, and they appear on their own if the accessory is added later or reports for the first time after Home Assistant has started. Entities that already exist are never removed, so a heating rod owner keeps the history recorded so far. On installations using EcoFlow account sign-in these entities no longer appear at all, because the heating rod reports through the polled API quota and that quota is not polled in that mode. That is the honest state of it: an empty sensor suggested the reading was on its way, and it was not. (Ref #7)

- Stream AC outlet states were partly read from the wrong two fields. Two fields that carry the solar input voltage and current of string 1 were mapped to the AC Outlet 1 and AC Outlet 2 flags alongside the actual relay fields. On an AC-coupled Stream both read zero, so the relay state won and nothing looked wrong, but on a unit with solar input the same fields switch both outlet sensors on for a device that may have no outlets at all. Outlet state now comes from the relay fields only, and the two solar fields are reported as PV Voltage and PV Current. Owners of an AC Pro, Ultra, Max, AC or Ultra X: please check that AC Outlet 1 and AC Outlet 2 still follow reality, since that path changed.

- Stream battery discharge was inflated by the WiFi signal strength. When a live update left the battery power out, the integration filled the gap from a field that actually carries the WiFi signal of the device's radio module. A signal of -68 dBm became a battery discharge of 68 W, continuously, and that phantom discharge was integrated into Battery Discharge Energy - a lifetime counter that only ever counts up. Depending on how often the battery power was omitted, an affected unit accumulated on the order of one and a half kilowatt hours per day since Stream support went live on 23 July. The field is now reported as its own WiFi Signal sensor and battery power has a single source again. The integration cannot repair a total that has already been recorded: setting it back to zero would make Home Assistant read a meter reset and count the old value a second time. Anyone whose Stream shows battery discharge energy it never produced should delete the entries for that sensor under Developer Tools, Statistics. The live battery power reading itself was never affected, and installations that always received the battery power directly have nothing to correct.

- The raw data capture for unsupported devices now keeps a sample of every message type across the whole recording instead of the last few minutes of the most frequent one. A device sends different message types at very different intervals, and a single shared buffer filled up with whichever arrives most often: a six hour recording came back holding three minutes of one message type, with everything needed to identify the device missing. Long recordings are now genuinely usable for adding support for a device, and the memory the capture uses stays bounded.

- The raw message capture in a diagnostics download now keeps a sample of every message type for supported devices too. This is the same problem as the one fixed above for unsupported devices, and it applied just as much to a device the integration already supports: a PowerOcean pushes its live telemetry every few seconds while reports such as the EMS state arrive minutes apart, so a shared buffer of two dozen frames held the last minute of the frequent message and nothing else. A rare message now competes only with itself, and the download reports how many frames arrived per message type against how many were kept, so a quiet device and a thinned-out capture no longer look alike. This only affects what a diagnostics download contains with EcoFlow account sign-in, not any sensor value.

- Diagnostics no longer expose the serial numbers of battery packs or attached accessories. Raw message capture masked the serial of the device itself and the account id, but a message also carries the serial of every battery pack, and those were left in place. Anything shaped like a serial is now masked, whether or not the integration knows what it belongs to. Masking keeps the original length, so the capture stays usable for diagnosing a device.

- Diagnostics no longer leak a device serial through a key name. Serial numbers were replaced inside values but not inside the key itself, and the PowerOcean quota addresses battery packs by serial in the key (`bp_addr.<serial>`). Anyone attaching a diagnostics download to a public issue would have published that serial. Keys are now redacted the same way values are.

## [1.15.0] - 2026-07-31

### Added
- Support for the Stream family: Stream AC Pro (BK31), Stream Ultra (BK11), Stream Max (BK41), Stream AC (BK51) and Stream Ultra X (BK61). Devices are detected automatically and report battery state, power flows, per-outlet state and power, LED brightness as a diagnostic, and a signed AC grid connection power that matches the "Netz-Anschluss" value in the EcoFlow app. The Backup Reserve level can be set from Home Assistant. Stream units are modeled as AC-coupled batteries, so battery charge and discharge energy are available for the Energy Dashboard, while the meter-dependent solar and home energy values stay disabled diagnostics unless an EcoFlow-compatible meter is paired in the app. Battery capacity counters in Ah are disabled diagnostics as well. (Ref #98)
- Per-string solar telemetry for Stream devices in Standard mode, covering up to four strings, plus an optional kWh counter per string for the Energy Dashboard. PV 3 and PV 4 power are disabled by default because only the larger units use them, and the per-string energy counters are disabled by default because the existing solar energy sensor already covers the total. Enhanced mode reports the solar total only, since the live telemetry stream does not break it down per string.
- Support for the Delta 3 Max Plus (D3M1) in both Standard and Enhanced mode, with sensors for battery state of charge, input and output power, AC, solar, 12 V, USB and Anderson port power, port flow states, charge and discharge state with remaining times, and the SoC limits. Both AC outputs, the 12 V output, X-Boost, the buzzer, the bypass lock and the backup reserve can be switched, and the charge limit, discharge limit and backup reserve level are adjustable. Entity IDs are identical in both modes, so switching between them keeps your history and dashboards intact. Remaining charge and discharge time are reported only while the battery is actually charging or discharging, because the device parks the inactive one on a placeholder that would otherwise read as a runtime of over 200 hours. Every value and every command was verified against a Delta 3 Max Plus across idle, discharge, bypass and charge operation. (Ref #100, #110, #111)
- Energy Dashboard sensors for the Delta 3 Max Plus covering solar, solar 2, AC input and total output energy, integrated from the live power telemetry because the device reports no native energy counters.
- Serial prefixes J32D and J32E (PowerOcean and PowerOcean Single Phase) are now recognized as PowerOcean. These European variants report an empty product name through the app API and were previously skipped with "no parser available". Both are unavailable through the EcoFlow Developer API (error 1006) and therefore require Enhanced mode. Verified on real hardware with full live telemetry; single-phase units report only the phases that are physically present.
- Support for PowerOcean Plus (serial prefixes R371, R374 and HJ3C). These higher-power 3-phase hybrid units were previously skipped with "no parser available". They answer a state request with a single reply that bundles up to 19 separate messages, all of which are now decoded, which brings a tested R374 to 47 reported values. Reactive and apparent power are available per phase, and solar strings 3 and 4 report power, voltage and current. Those two strings are disabled by default because ordinary PowerOcean units have two strings and would otherwise gain permanently empty sensors: on a Plus unit, enable them under Settings, Devices, PowerOcean, Entities. Like J32D and J32E, PowerOcean Plus is not exposed through the EcoFlow Developer API and requires Enhanced mode.
- Diagnostics now include the raw API quota of devices that are discovered but have no parser yet, as well as the raw quota of Stream and Delta 3 devices and the most recent raw messages received in Enhanced mode. Device variants within a serial family do not always use the same telemetry layout, so a wrongly decoded device can now be checked from a diagnostics download alone. Serial numbers are shortened or masked, messages are truncated, and credentials are never exposed.
- Optional raw data capture for devices that have no parser yet, available when the integration runs with EcoFlow account sign-in. Such a device gets no entities, which also meant the data needed to add support for it could never be collected. Once enabled in the integration options, a listen-only connection records what the device sends and puts it into the diagnostics download. Nothing is ever sent to the device. The capture is off by default because it costs an extra connection per unsupported device, and it switches itself off 24 hours after being enabled so it cannot be left running and forgotten. Restarting Home Assistant or saving the options for an unrelated reason does not extend a running window; turning the capture off and on again starts a fresh one. The message about an unsupported device now points at this option, so it explains what would actually make the device supportable.

### Changed
- Internal restructuring of the device coordinator and the configuration flow into smaller, single-purpose modules; no functional or user-facing change.

### Fixed
- Percentage sensors no longer show a placeholder as a reading. When the device leaves a percentage field empty, it carries the maximum value the field can hold, and that was passed through unchanged: a backup ratio could read as 4,294,967,295 percent. Values outside 0 to 100 percent are now discarded rather than capped, so the sensor keeps its last valid reading instead of showing a wrong one that looks believable. This covers the backup ratio, the charge and discharge limits, the keep-SoC and feed-in ratio, battery health and real SoC, and the per-pack state of charge and health values.
- Energy counters no longer count part of their own history a second time. Whenever the stored energy state was missing, for example on a fresh install or after clearing the cache, the first reading published a zero to a counter that Home Assistant still held a restored value for. Home Assistant reads a zero on that kind of counter as a meter reset and books the previous total a second time, which inflates the long-term statistics for that sensor. The first reading after a state loss now publishes nothing at all and the restored value stands until real energy accumulates on top of it. A doubling already recorded stays in the statistics, so anyone affected should delete the entries for that sensor under Developer Tools, Statistics. The live sensor readings themselves were always correct.
- An energy counter can no longer be frozen by a single impossible reading. Energy totals only ever count up, which is correct for a lifetime counter but meant that one wrong value took the counter with it: every correct reading afterwards was lower and therefore ignored, so a counter that once reported 54,501,280 kWh kept showing that number forever. Readings far beyond what any device can physically deliver are now rejected, and an installation carrying such a value drops it on the next restart and counts on from zero. Genuine lifetime counters, including decades of production, are unaffected. A stored statistic cannot be corrected from the integration, so anyone whose energy sensor showed an absurd figure should also delete the affected long-term statistics under Developer Tools.
- Entities now go unavailable and recover promptly when the connection degrades or comes back. Availability changes with unchanged sensor values were filtered out by the state-write deduplication, so entities could keep showing as available long after the data stream stopped, and a recovery was only reflected once a value happened to change. The same filtering could leave entities unavailable indefinitely after a Home Assistant restart that happened while their device was between transmissions, which needed a manual reload to clear.
- Switches and numbers now restore their last known state after a Home Assistant restart instead of showing unknown until the first full device status arrives; live device data always replaces the restored value.
- Devices that the app API reports with an unknown device type are now classified locally using their product name or supported serial-number prefix. This also covers the base DELTA 3 ("Classic"), which reports an empty product name and was silently dropped as "no parser available for this model yet" even though the Delta 3 parser handles it fine once routed. The P321 serial prefix is recognized as a Delta 3 device.
- Unsupported devices are no longer skipped silently. Setup now emits one clear warning per unsupported device and lists them in a new `skipped_devices` diagnostics section, so a device that produces no entities is visible instead of vanishing without a trace.
- Devices added to your EcoFlow account after the initial setup can now be added through the integration options when you signed in with your EcoFlow account. Previously the options screen only listed the devices selected during setup, so a newly bought device could only be added by deleting and re-creating the integration. If the account cannot be reached, the options screen keeps working and offers the devices you already had.
- The PowerOcean battery state sensor (charging/discharging/standby) is now stable through short power swings around zero. At dawn and dusk, when solar production and house load balance, the sensor could change state dozens of times a day. A state change now has to persist for ten minutes before the sensor follows it; sustained changes still come through reliably.
- PowerOcean grid detection now considers all three phase voltages instead of only phase A, so a setup fed on phase B or C, or a single-phase outage on A, reports the grid state correctly.
- The Delta 2 Max AC Charge Speed slider now reads and writes the actually configured charging speed. It previously displayed the rated maximum (2400 W), never picked up changes made in the EcoFlow app, and every value written from Home Assistant effectively ended up at 400 W because the command hardcoded the governing parameter.
- Delta 2 Max no longer breaks its state sensors when the firmware reports a charging or charger state the integration does not know yet. Unknown values previously passed through as raw numbers and made Home Assistant reject every state update for that sensor; they are now skipped, keeping the sensor on its last valid state.
- The Delta 2 Max configured AC output voltage is now also available when polling over HTTP, matching what the push channel already reported.
- Negative readings from the Smart Plug no longer appear as astronomically large numbers. The binary telemetry encodes values below zero in a form the decoder read as unsigned, so a temperature of -5 C surfaced as roughly 18 quintillion. Signed values now decode correctly.
- English installs now show proper connectivity state labels. The WiFi, Ethernet and 4G status sensors displayed raw state keys because the English translation carried state names copied from the grid status sensor.
- Energy counters survive a lost or corrupted cache file. The totals are re-seeded from the last value Home Assistant restored instead of restarting at zero; a stale restored value can never lower a live total.
- Controls no longer show a state the device rejected. The Smart Plug switch and the Delta switch and slider paths applied the new state before checking whether the command was sent, so a failed send displayed a wrong state for several seconds.
- Expired live-connection credentials now recover automatically. The MQTT library reports connection results in a format the integration could not process, which silently stopped the connection thread and broke the automatic credential refresh, so an expired login stayed broken until a restart. Authorization failures are now recognized correctly and trigger the refresh as designed, and the refresh itself no longer runs into the internal rate limit and discards working credentials, which could lead to an unnecessary re-authentication prompt.
- Accounts registered on the global EcoFlow endpoint can now complete setup with EcoFlow account sign-in. Login tried both the EU and global hosts, but all follow-up requests went to the EU host only, so a global-account login succeeded and everything after it failed. All requests now stay on the host that accepted the login.
- Reconnect attempts after repeated connection failures now respect the documented 60 second maximum. The backoff multiplier was applied after the cap, stretching the effective wait to up to 120 seconds.
- Devices that send telemetry slowly no longer end up in a constant reconnect loop in Enhanced mode. When a connected device stayed silent, the integration dropped and rebuilt the whole session every time; it now first repeats the requests it normally sends after connecting, which wakes the device up without tearing down the connection, and only reconnects if that does not help.
- The log stays quiet about conditions that resolve on their own. A device that briefly goes silent and turns its entities unavailable is a normal step in the graduated availability handling and is now logged at info level, and a failed reconnect attempt caused by a temporary name resolution failure is no longer an error. Both are reported as warnings once attempts start piling up. The log line about a silent device also reads correctly when the device has sent no data since connecting, instead of showing an infinite age.
- Smaller fixes: diagnostics downloads now shorten device serial numbers to a 4-character prefix, enum sensors ignore unknown values from the device instead of breaking their state updates, only the main battery SoC drives the device-card header on the Delta 2 Max, and every lifetime counter is covered by the never-decrease guard.
- Internal parser hardening: malformed or truncated binary frames and unexpected message envelopes are now discarded cleanly instead of raising, work-mode values delivered as text map correctly again, and PowerOcean battery pack numbering no longer depends on the order the cloud returns them.
- The per-pack lifetime charge and discharge counters on PowerOcean no longer lose their decimals. Both were stored as whole kilowatt hours while every other energy sensor keeps two decimals, so each reading carried up to half a kilowatt hour of rounding. The battery reports these counters to the watt hour, and comparing two days on a three-pack system could therefore be off by several kilowatt hours in either direction, which made the counters unusable for exactly the comparison they are meant for. Existing readings continue upwards from where they are, so no history is lost. (Ref #88)
- Internal connection hardening: simultaneous reconnect triggers no longer race each other, refreshed credentials are applied to the live connection immediately, and the connection keepalive no longer feeds its own echo back into the data pipeline.

## [1.14.0] - 2026-05-18

### Changed
- Dropped `paho-mqtt` and `protobuf` from `manifest.json` requirements. Both libraries are already shipped with Home Assistant core (paho-mqtt via the built-in `mqtt` integration, protobuf via several core integrations), so listing them in the integration manifest was redundant and could cause unnecessary dependency resolution work on setup.

### Removed
- Removed legacy brand assets `icon.png`, `icon@2x.png`, `logo.png`, `logo@2x.png` from the integration root. Brand artwork is now sourced exclusively from the `brand/` subdirectory, matching the layout used by the [home-assistant/brands](https://github.com/home-assistant/brands) repository.

## [1.13.0] - 2026-05-06

### Added
- PowerOcean controls verified against live app traffic. The 3-field `SysBatChgDsgSet` payload (cmd_id=112) replicates the official EcoFlow app byte-for-byte, ensuring writes are accepted reliably. (beta.1)
  - `number.backup_reserve` - emergency reserve SoC (0-100%, step 5). Maps to the "Backup-Reserve" slider in the EcoFlow app.
  - `number.solar_surplus_threshold` - SoC threshold above which surplus solar is routed to controllable devices (0-100%, step 5). Maps to "Überschüssige Solarenergie" in the app.
  - `select.work_mode` - operational strategy. Phase 1 supports Self-use ("Eigenstromversorgung") and AI Schedule ("Intelligenter Modus"). TOU and Backup require additional sub-parameters and are deferred.

### Fixed
- Enum sensors no longer crash with `ValueError: state value not in options` when the device emits a previously-unseen enum value. Unknown values are now dropped (sensor stays unavailable until a known value arrives) rather than being passed through as raw integers. Affects `ems_work_mode`, `ems_work_state`, `pcs_run_state`, and other enum fields. (beta.1)
- PowerOcean `number.solar_surplus_threshold` ("Überschüssige Solarenergie") slider reverted to the device's stored value after every change. The 3-field `SysBatChgDsgSet` payload (cmd_id=112) wrote the surplus percentage to wire field 4, which the device silently accepts but ignores. The value now goes to wire field 3 (`sys_bat_backup_ratio`) and is reflected back via `JTS1EmsChangeReport` within ~0.5s. The Backup-Reserve slider was unaffected. (beta.2)
- `CryptographyDeprecationWarning: CFB has been moved to cryptography.hazmat.decrepit.ciphers.modes` no longer appears in the HA log on Enhanced Mode setup. The AES-CFB import now resolves via `decrepit.ciphers.modes` on cryptography ≥ 43 with a fallback to the legacy path. The cipher itself is unchanged - EcoFlow's portal protocol mandates CFB, so we keep using it. (beta.3)
- PowerOcean `number.solar_surplus_threshold` value not applied in the EcoFlow app. The fix in beta.2 wrote the surplus percentage only to wire field 3 (`sys_bat_backup_ratio`), which the device's EMS reads. The EcoFlow app, however, reads the value from a separate cloud-quota key (`socDev` / `dev_soc`, wire field 4). The two fields are independent views of the same logical slider and must be written together; otherwise the app and the device drift apart. The payload now writes both wire fields in a single `SysBatChgDsgSet`, keeping HA, the device EMS, and the EcoFlow app aligned. Verified against live cloud quota: writing surplus=33 propagates `sys_bat_backup_ratio=33`, `socDev=33`, and `dev_soc=33` simultaneously. (beta.4)
- PowerOcean `number.solar_surplus_threshold` did not reflect changes made in the EcoFlow app. The app sends `cmd_id=112` over MQTT but only includes wire field 4 (`dev_soc`), which leaves the EMS-side `sys_bat_backup_ratio` (wire field 3) on the previous threshold; HA observes the EMS field and stayed out of sync. The device does mirror the app's value back via `cmd_id=13` `EmsParamChangeReport` field 10. The proto decoder now handles `cmd_id=13` (`JTS1EmsParamChangeReport`) and exposes `dev_soc` as `ems_app_surplus_pct`. Whenever this differs from `ems_backup_ratio_pct`, the coordinator pushes a corrective both-field SET so the EMS catches up to whatever the app set, keeping HA, app, and device aligned. The auto-sync respects a 30 s throttle and a 5 s grace period after a user-initiated SET so it does not race the device echo. (beta.4)
- PowerOcean SoC sliders (`number.solar_surplus_threshold` and `number.backup_reserve`) sent one MQTT SET per 5 %-step while the user dragged the slider in HA, producing 5-10 SETs in <1 s. The PowerOcean firmware cannot keep wire field 3 (EMS) and field 4 (App-Layer) in sync at that cadence, so the two fields drifted apart and HA, the EcoFlow app, and the device EMS ended up showing different values for the same slider. SET delivery is now coalesced through a 300 ms debouncer in the coordinator: every Number-Entity call updates the pending (backup, solar) pair and resets a timer, so only the final value reaches the device. The optimistic UI value is still applied immediately, so the slider feels responsive. (beta.5)
- PowerOcean `number.solar_surplus_threshold` slider was pulled back to a previous value about 30 s after the user set a new value in HA. The auto-sync from beta.4 reissued a both-field SET whenever the cached `ems_app_surplus_pct` (from `EmsParamChangeReport`) differed from `ems_backup_ratio_pct`, but it did not check whether the ParamChange frame that produced the cached app value was actually fresh. After a drag-race left the device's app-layer field stuck on a stale value, the auto-sync would dutifully reissue that obsolete value, dragging HA back to it. The auto-sync now records the timestamp of every incoming ParamChange and only fires when that timestamp is more recent than the last user SET - so genuine app-side changes still trigger a sync, but a stale frame can no longer override the user's HA SET. (beta.6)
- PowerOcean `number.solar_surplus_threshold` showed 90 instead of 100 when the user set 100 in either HA or the EcoFlow app. Live diagnosis revealed that the two cmd_id=112 wire fields are semantically different: `dev_soc` (field 4 / cloud-quota `socDev`) is the user-facing slider value the EcoFlow app reads and writes, while `sys_bat_backup_ratio` (field 3) is a derived EMS status that the device internally clamps at edge cases (notably 100 %, where it caps at 90). HA was reading the EMS-side value and therefore showed the clamped state instead of the user's intent. The slider now sources from `ems_app_surplus_pct` (the user-side mirror), matching what the EcoFlow app shows. The SET still writes both fields so the EMS follows the user value where it can; at the 0 %/100 % edges the auto-sync no longer schedules futile reissues since the EMS-side divergence is by design at those boundaries. (beta.7)

### Removed
- `number.min_discharge_soc` (legacy) - sent only 2 of the 3 fields the device requires, causing writes to be silently ignored. Use the new `number.backup_reserve` instead. The wire field and read sensor are unchanged; only the entity name and range (was 0-30%, now 0-100%) differ. (beta.1)

### Migration
- After upgrade, the old `number.ecoflow_powerocean_min_entladezustand` (DE) / `min_discharge_soc` (EN) entity will appear as "unavailable" in HA. It is safe to delete via Settings → Devices & Services → EcoFlow Energy → ⋮ → Delete on the entity. Any automation referencing it should be updated to use `number.ecoflow_powerocean_backup_reserve` instead.

## [1.12.0] - 2026-04-22

### Added
- Human-readable state translations for enum sensors: battery charge/discharge state, grid status, inverter state, work mode, feed mode, connectivity (WiFi/Ethernet/4G), and charger type now show descriptive labels instead of raw numeric values.
- German translations for all new state values.
- Delta 2 Max enum sensors: charge/discharge state, EMS charge state, MPPT charge state, and charger type now show translated labels.

### Fixed
- Work mode and inverter state sensors blocked by HA validation after upgrade. RestoreSensor loaded old raw values (e.g. "WORKMODE_SELFUSE") that are not in the new options list. Invalid restored values are now discarded so the sensor can start fresh. (beta.3)
- Proto path for work mode and inverter state used string-keyed maps but proto sends integer values. Added integer-keyed mapping tables for the proto decode path. (beta.3)
- Enum sensors no longer inject false zero-defaults. EMS change reports typically contain only bp_soc, not status fields. Injecting defaults overwrote correct values from HTTP. Enum fields are now only mapped when actually present in the message. (beta.5)
- Connectivity sensors (WiFi, Ethernet, 4G) added to proto decoder. Previously missing from EmsChangeReport proto definition (stopped at field 23, connectivity at field 224/225/187). PowerOcean latestQuotas response now parsed through HTTP parser for correct enum mapping. (beta.6)
- WiFi/Ethernet connectivity semantics corrected: 0 = connected, non-zero = disconnected. 4G uses inverted logic (1 = connected). Verified against live device probe and EcoFlow portal code. (beta.6)
- Feed mode mapping corrected to 4 values: off/no_limit/zero/limit (was using wrong enum class). Based on EcoFlow portal protocol analysis. (beta.6)
- Grid status now derived from phase voltage (> 50V = ok) in Enhanced Mode heartbeat. The raw sys_grid_sta field is unreliable (always reports 0). (beta.6)
- Work state mapping corrected to 10 values from EMS_WORK_STATE enum (was using wrong 5-value bms_SysState enum). (beta.6)
- Proto get_reply parsing for PowerOcean: extracts EmsChangeReport from multi-header response for initial state on startup. (beta.6)
- Protobuf version guard restored for compatibility with protobuf < 5.29. (beta.6)
- Delta 2 Max charger_type value 255 (no charger) now mapped to "unknown" instead of causing ValueError. (beta.7)
- Energy sensors (solar, grid import/export, home) stopped increasing after host reboot. The monotonic clock resets on reboot but the energy integrator state file retained the old timestamp, producing a negative time delta that silently skipped all integration. (beta.8)
- Battery charge/discharge state showing inverted values: "discharging" while charging and vice versa. The EMS field reports the controller mode, not the physical state. State is now derived from actual battery power: charging when charge power > 50W, discharging when discharge power > 50W, standby otherwise. (beta.9)
- Added diagnostic logging for battery state derivation to help debug device-specific sign convention differences. Logs raw power values on each state transition at DEBUG level. (beta.10)
- Battery state flipping rapidly between charging/discharging/standby when solar production is close to house load. Raised power threshold from 50W to 200W to filter inverter balancing currents, and added hysteresis requiring two consecutive identical derivations before changing state. (beta.11)
- Battery state still flickering at sunrise/sunset when power oscillates around the 200W threshold. Added two-layer debounce: 3 consecutive confirmations (~9s) plus 60-second minimum hold time before state transitions. (beta.13)
- Battery state still flipping ~250 times/day despite debounce. Root cause: the EMS raw field bp_chg_dsg_sta (which reports controller mode, not physical state) overwrote the power-derived state on every EMS report and HTTP poll, bypassing the debounce entirely. The raw EMS value is now stripped before it reaches device data. (beta.15)
- Battery state flipping every 60s on steady discharge. The derivation read two separate fields (batt_charge_power_w, batt_discharge_power_w) that were both set to 0 when a single heartbeat reported batt_w=0 (proto3 omission or message-type variation). Derivation now reads signed batt_w directly with asymmetric hysteresis bands: charging >200W, discharging <-200W, standby only when |batt_w|<50W, 50-200W is a deadband that keeps the previous state. Intermittent zero spikes no longer accumulate standby confirmations. (beta.16)
- Battery state still flipping 50-90 times/day with beta.16 asymmetric hysteresis. Root cause: instantaneous batt_w swings between +1000W and -300W within seconds when solar and house load balance (morning/evening), so the 9s confirmation window was trivial to meet and the 60s hold gate simply set a minimum flip interval of 60s. Derivation now uses a 3-minute rolling average of batt_w instead of the instantaneous value, with 150W outer / 50W inner thresholds and a 120s minimum hold time between transitions. Short peaks no longer dominate the derived state. Regression test replays a real 15-minute production timeline that previously produced 8 flips and asserts at most 3 in the new logic. (beta.17)

### Changed
- Entity display names follow a consistent naming convention: suffix qualifiers (max./min./real), no internal abbreviations (EMS/PCS/MPPT), app-aligned names where possible. Entity IDs unchanged - automations and dashboards are not affected. (beta.12)
- Renamed "Backup Reserve" sensor to "Backup Reserve (EMS)" to distinguish it from the controllable backup reserve settings. (beta.14)

### Changed
- Enum sensors use HA `device_class: enum` with `options` for proper state handling and translation support.
- **Breaking:** Automations using raw numeric state values (e.g. `state == "1"`) for these sensors must update to the new string values (e.g. `state == "charging"`). All affected sensors are diagnostic and disabled by default.

## [1.11.0] - 2026-04-05

### Added
- Snapshot continuity layer: explicit last-known-good data contract for the Graduated Availability degraded stage. Device data snapshots are captured on every update with metadata (timestamp, source, key count) and expire only at hard-unavailable. Formalizes the implicit guarantee that entities retain values during stream interruptions.
- Snapshot metadata in diagnostics: source, age, key count, and capture status for debugging availability behavior.
- `documentation/` folder for public user-facing documentation.

### Fixed
- DeviceSnapshot dataclass now frozen for immutability guarantee (beta.2)
- Coordinator docstring accuracy: _log_event max entries corrected to 50 (beta.2)
- Proper __init__ declaration for _auth_method and _last_mqtt_event_ts, removing defensive getattr calls (beta.2)
- PowerOcean battery device_class limited to primary soc_pct sensor; pack SoC and bp_real_soc_pct no longer carry battery class (beta.2)

## [1.10.0] - 2026-04-04

### Fixed
- App-auth MQTT client now triggers credential refresh on auth errors (rc=5). Previously, expired WSS credentials caused indefinite reconnect failures because the `auth_error_handler` callback was not wired for the app-auth path.

### Added
- Three-state MQTT status: diagnostics now report `receiving`, `connected_stale`, or `disconnected` instead of binary `connected`/`disconnected`, making it easier to identify connected-but-silent MQTT sessions.
- `data_receiving` property on the coordinator for programmatic stale detection.
- Enhanced connectivity event log: stale detection triggers, force-reconnect events, credential refresh outcomes, and recovery transitions are now captured in the per-device event log.
- ISO timestamps in diagnostic event log entries for human-readable debugging.
- Proactive credential refresh for app-auth: credentials older than 20 hours are refreshed automatically every 12 hours, preventing brief outages from token expiry.

### Changed
- Graduated availability degradation replaces the binary 95-second hard-unavailable cutoff. Entities now remain available with last-known-good values during stream interruptions, degrading through stages (healthy -> stale -> degraded -> unavailable). Hard unavailable is now 10 minutes instead of 95 seconds, matching observed device behavior where PowerOcean telemetry gaps can exceed 600 seconds.
- Device-specific degradation thresholds: PowerOcean/Delta soft=5min hard=10min, Smart Plug soft=6min hard=10min.
- Extracted PowerOcean proto remapping logic (~150 lines) from `coordinator.py` into `ecoflow/parsers/powerocean_proto.py` for cleaner core/device separation.
- Moved SET command templates from `switch.py` and `number.py` into `const.py` alongside entity definitions.

## [1.9.3] - 2026-04-04

### Fixed
- Delta 2 Max device header in Home Assistant showed wrong battery percentage (100% SoH instead of actual SoC) because multiple sensors had `device_class: battery`. Only the primary `soc` sensor now carries the battery device class; SoH and secondary SoC variants use `device_class: None`.

### Changed
- App-auth stale warning wording simplified: removed mode suffix from the user-facing message. Logs now state only the concrete condition and action (`MQTT stream interrupted ... marking device unavailable`).

## [1.9.2] - 2026-04-03

### Fixed
- App-auth MQTT health checks now run on a short dedicated interval (5s) instead of the device stale threshold, so reconnect attempts start promptly after disconnects.
- App-auth stale handling now uses a time-based grace window (`stale_threshold + 60s`) before marking a device unavailable, reducing false temporary unavailability during short broker interruptions.
- Connected-but-silent app-auth MQTT sessions now trigger a forced reconnect attempt, improving recovery from stalled WSS sessions.

### Changed
- Improved stale warning context in app-auth mode by including reconnect attempt count in the warning message.
- Removed legacy "no HTTP fallback" wording from app-auth stale warnings; logs now describe only the actual state/actionable mode context.
- App-auth stale/recovery logs now include both device name and serial number for unambiguous per-device troubleshooting.

## [1.9.1] - 2026-04-02

### Changed
- Smart Plug in app-auth now uses a device-specific stale threshold (180s) instead of the global 35s threshold to avoid false temporary unavailability on sparse telemetry bursts.
- Smart Plug app-auth keepalive now adds periodic `get-all` full-state refreshes (every 120s) alongside `latestQuotas`.

### Fixed
- Reduced false "device unavailable (no HTTP fallback)" transitions for Smart Plug while MQTT is still healthy but temporarily quiet.
- Improved Smart Plug control-state freshness (switch/brightness/max limit) during long-running sessions.

## [1.9.0] - 2026-04-02

### Added
- **Enhanced Mode for all devices** - set up with just your EcoFlow email and password, no Developer API keys needed. Real-time updates: PowerOcean ~3 s, Delta 2 Max ~2 s, Smart Plug ~3 s
- New setup flow: choose between Standard (official API) or Enhanced (community-driven real-time) at first setup
- Auto-discovery of all devices bound to your EcoFlow account (Enhanced Mode)
- Smart Plug full control in Enhanced Mode: on/off switch, LED brightness, max power limit
- Automatic upgrade: existing Enhanced Mode setups migrate seamlessly on restart

### Changed
- Config flow redesigned: mode selection (Standard vs Enhanced) is now the first step
- Smart Plug LED brightness now shows 0-100% instead of raw device values
- Enhanced Mode and Standard Mode are fully separated - clean architecture, no hybrid paths
- Reduced log noise: transient MQTT reconnects no longer flood the log with warnings

### Fixed
- Smart Plug switch and number controls now work reliably in Enhanced Mode
- Delta 2 Max receives real-time updates (~2 s) in Enhanced Mode instead of only HTTP polling
- Options flow mode switch correctly handles credential changes
- Device type detection works even when the EcoFlow API returns empty device names
- Enhanced Mode no longer creates coordinators for unsupported devices (e.g. PowerGlow, PowerPulse) - eliminates WARNING spam about MQTT stale data (#28)

### Upgrade notes
- **Standard Mode users**: No action needed. Your setup continues to work exactly as before.
- **Enhanced Mode users** (v1.8.x with Developer Keys + email/password): The integration automatically upgrades to the new app-auth flow. Developer Keys are no longer needed for Enhanced Mode.
- **Config entry migration**: The integration migrates your configuration automatically (v1/v2 to v3). This is transparent and non-breaking.

## [1.8.3] - 2026-03-31

### Fixed
- HTTP error 1006 ("device not linked to API key") no longer triggers false re-authentication - classified as a configuration issue with an actionable log message instead of counting toward the auth failure threshold (#2)
- Enhanced Mode: HTTP fallback failures no longer trigger re-authentication when MQTT is actively delivering data (#2)
- Error 1006 logged once per device with clear guidance instead of repeating every 30 seconds

## [1.8.2] - 2026-03-31

### Fixed
- PowerOcean Enhanced Mode: stable per-pack sensor numbering via battery serial number - each physical pack now consistently maps to the same `pack{n}_*` sensors across heartbeats, fixing Pack 2 sensors not updating (#10)

## [1.8.1] - 2026-03-31

### Fixed
- PowerOcean Enhanced Mode: idle battery packs no longer falsely filtered as phantoms - replaced numeric non-zero check with identity key presence check (bp_soc, bp_design_cap, bp_sn, etc.) so packs with zero power/SoC are still recognized (#10)
- PowerOcean Enhanced Mode: aggregate `bp_remain_watth` now computed from accumulated device data instead of per-message - partial heartbeats (single pack reporting) no longer cause the total to revert to one pack's value (#10)

## [1.8.0] - 2026-03-31

### Changed
- State update deduplication: entities only write to HA recorder when their value actually changes, reducing state writes by ~60-80% (previously every coordinator update triggered a state write for all entities regardless of value change)
- Energy integration precision reduced from 3 to 2 decimal places (0.01 kWh resolution) to further reduce fractional churn on total_increasing sensors
- Optimistic writes (switch, number) now sync dedup state to prevent one redundant write on the next coordinator tick

### Fixed
- MQTT fallback logging reduced from WARNING to INFO: transient stale/recovery transitions are self-healing and no longer clutter the HA log - both "switching to HTTP fallback" and "MQTT recovered" now log at INFO level as a matched pair

## [1.7.1] - 2026-03-31

### Fixed
- Protobuf bindings backward compatibility: runtime version check now wrapped in try/except for protobuf <5.29
- Coordinator: encapsulated `_device_data` access via public `set_device_value()` method
- Docstrings updated: `async_set_soc_limits` (2 fields, not 4) and `build_soc_limit_set_payload` (min discharge confirmed, max charge pass-through only)

## [1.7.0] - 2026-03-31

### Fixed
- PowerOcean: SoC limit 0% now correctly synced in both directions - `optional` proto3 field presence on `sys_bat_dsg_down_limit` and `sys_bat_chg_up_limit` ensures `MessageToDict` includes zero values instead of silently omitting them
- PowerOcean: "Battery Remaining Capacity" (`bp_remain_watth`) now shows total capacity across all battery packs instead of only Pack 1 - affects both Standard Mode (HTTP) and Enhanced Mode (Protobuf) (#10)

### Removed
- Temporary workarounds from v1.6.5–v1.6.8 (proto3 global flag, optimistic lock, zero-fill, HTTP sync loop) - all replaced by proper `optional` field presence

## [1.6.7] - 2026-03-31

### Fixed
- PowerOcean: Min Discharge SoC 0% now persists permanently - optimistic value is written to `_device_data` so it survives coordinator refresh cycles (proto3 omits zero-valued fields from MQTT readback, but the merge no longer overwrites the SET value)

### Removed
- Temporary 10-second optimistic lock from v1.6.6 (no longer needed)

## [1.6.6] - 2026-03-31

### Fixed
- Revert `always_print_fields_with_no_presence` from v1.6.5 - it flooded all proto fields with default 0, overwriting real sensor values
- PowerOcean: number entities now use a 10-second optimistic lock after SET commands to prevent proto3 zero-omission readback from reverting the displayed value

## [1.6.5] - 2026-03-31

### Fixed
- Proto3 zero-value readback: `MessageToDict` now includes fields with value 0 - previously, setting Min Discharge SoC to 0% was accepted by the device but HA reverted to the previous value because the proto3 decoder omitted zero-valued fields

## [1.6.4] - 2026-03-31

### Fixed
- PowerOcean: revert to 2-field SysBatChgDsgSet payload (charge upper + discharge lower only) - the 4-field version from v1.6.1 caused the device to reject discharge lower limit value 0
- Proto3 zero-value readback: `MessageToDict` now includes fields with value 0 - previously, setting Min Discharge SoC to 0% was accepted by the device but HA reverted to the previous value because the proto3 decoder omitted zero-valued fields

## [1.6.3] - 2026-03-31

### Fixed
- PowerOcean: revert proto field swap from v1.6.2 - both values were broken after swap
- PowerOcean: remove Max Charge SoC number entity - device firmware does not reliably accept charge upper limit via SysBatChgDsgSet (requires portal traffic capture for further investigation)

### Changed
- PowerOcean: SoC control reduced to Min Discharge SoC only (Enhanced Mode) until charge limit SET protocol is verified

## [1.6.2] - 2026-03-31

### Fixed
- PowerOcean: swap proto field order in SysBatChgDsgSet - device reads field 1 as discharge lower and field 2 as charge upper (opposite of proto definition labels)
- PowerOcean: fix dev_soc lookup in SET payload - was reading unmapped key, now uses coordinator-mapped `soc_pct`

## [1.6.1] - 2026-03-31

### Fixed
- PowerOcean: Max Charge SoC SET now includes all 4 required protobuf fields (charge upper, discharge lower, backup ratio, device SoC) - previously only 2 fields were sent, causing the device to silently reject the charge limit change

## [1.6.0] - 2026-03-30

### Added
- PowerOcean: battery SoC limit control - Max Charge SoC (50–100%) and Min Discharge SoC (0–30%) as number entities (Enhanced Mode only)
- PowerOcean: SysBatChgDsgSet protobuf SET command (cmd_func=96, cmd_id=112) for real-time SoC limit adjustment via WSS

### Changed
- MQTT client: refactored `send_energy_stream_switch` to use generic `send_proto_set` method for all protobuf SET commands

## [1.5.3] - 2026-03-30

### Fixed
- False "Authentication expired" reauth trigger when MQTT data is flowing but HTTP polling has transient failures (#2)
- EcoFlow API error 8521 (intermittent server error) is now retried instead of immediately counted as a failure

## [1.5.2] - 2026-03-30

### Fixed
- Sensor precision: `native_value` now rounds numeric values based on `suggested_display_precision` - power sensors show integers (e.g. "2347 W"), energy sensors show 2 decimal places (e.g. "15.23 kWh")

### Changed
- Diagnostics: event log capacity increased from 20 to 50 entries for better support troubleshooting

## [1.5.1] - 2026-03-30

### Fixed
- PowerOcean: battery pack numbering now starts at "Pack 1" instead of "Pack 2" - phantom/empty API entries (EMS module) are skipped before numbering (#5)
- PowerOcean: aggregate battery sensors (bp_*) now correctly select the first real battery pack, not a phantom entry
- PowerOcean: Enhanced Mode (Protobuf) now delivers multi-pack data correctly - previously silently discarded by internal key filter
- Config flow: narrowed exception handling with OSError coverage for SSL/socket errors

### Note for multi-pack users
- If you have multiple battery packs and previously saw "Pack 2"/"Pack 3" instead of "Pack 1"/"Pack 2", the entity IDs will change after this update (e.g. `pack2_soc_pct` becomes `pack1_soc_pct`). You may need to update any dashboard cards or automations that reference pack entities.

### Changed
- Logging convention: renamed `logger` to `_LOGGER` across all modules (Home Assistant standard)
- Timing: interval measurements now use `time.monotonic()` instead of `time.time()` for NTP-resilient elapsed time tracking
- Entity type hints: `device_info` return type corrected to `DeviceInfo` across all entity platforms
- Import order: PEP 8 compliant import ordering in `const.py`
- Typing: modern `from __future__ import annotations` and union syntax used consistently
- Energy integrator: state file migration handles pre-v1.5.1 epoch timestamps gracefully

### Added
- Protobuf import failure now logs a warning instead of failing silently
- `async_migrate_entry` stub for future config schema migrations
- Proto decoder functions documented with docstrings
- `CONTRIBUTING.md` with development setup, code style, and PR guidelines
- Security note in README about Enhanced Mode credential storage

## [1.5.0] - 2026-03-30

### Added
- PowerOcean: multi-battery-pack support - per-pack sensors for up to 5 BP5000 packs (120 new sensors, 7 enabled for Pack 1)
- PowerOcean: 19 additional EMS/system diagnostic sensors (SoC limits, fault codes, connectivity, system capabilities)
- PowerOcean: lifetime energy counters per battery pack (accumulated charge/discharge kWh)
- PowerOcean: multi-pack data in Enhanced Mode (Protobuf heartbeat extracts all packs)

## [1.4.0] - 2026-03-30

### Added
- Delta 2 Max: beeper, X-Boost, AC auto restart, backup reserve switches (4 new)
- Delta 2 Max: screen brightness, screen timeout, 12V port timeout, backup reserve level numbers (4 new)
- Delta 2 Max: expansion battery pack support - 32 sensors for up to 2 slave packs (disabled by default)

### Changed
- Delta 2 Max: X-Boost promoted from read-only binary sensor to controllable switch

## [1.3.4] - 2026-03-30

### Added
- Smart Plug: LED brightness control (0-1023) via number entity
- Smart Plug: overload protection / max power limit (0-2500W) via number entity
- Smart Plug: MQTT real-time data subscription for near-instant updates alongside HTTP polling

## [1.3.3] - 2026-03-30

### Added
- Diagnostic sensors for MQTT connection status and connection mode (disabled by default)
- Event history (last 20 events) in diagnostics download for troubleshooting
- SET command reply tracking via MQTT set_reply topic subscription

### Fixed
- Startup log correctly reports Enhanced vs Standard device count
- Re-auth trigger fires exactly once after 5 HTTP failures (no repeated warnings)
- MQTT event log rate-limited to prevent flooding in Enhanced Mode

## [1.3.2] - 2026-03-29

### Added
- README troubleshooting entry for manual credential update path via Reconfigure menu

### Fixed
- German translation placeholder mismatch in `reconfigure_confirm.description` (`{developer_portal_url}`) to prevent HA translation validation errors

## [1.3.1] - 2026-03-29

### Added
- Reconfigure flow - update API credentials via Settings > Integrations > EcoFlow Energy > Reconfigure
- Entity availability tracking - entities show "unavailable" when device is unreachable
- Optimistic state update for number entities (charge speed, SoC limits)
- `suggested_display_precision` for all sensors - cleaner UI values
- `disabled_by_default` for diagnostic sensors - less overwhelming for new users
- Entity categories for diagnostic binary sensors
- `configuration_url` in device info - clickable link on device page
- German translations for re-authentication and reconfigure flows

### Fixed
- Protobuf decode errors logged at DEBUG instead of WARNING (zero-noise logging)
- HTTP retry attempts logged at DEBUG instead of WARNING
- Startup summary log downgraded from INFO to DEBUG
- Diagnostics `http_fallback_active` now correctly reflects actual fallback state

## [1.3.0] - 2026-03-29

### Added
- Re-authentication flow for expired API credentials (Standard and Enhanced Mode)
- Automatic re-auth trigger after 5 consecutive HTTP failures, MQTT credential refresh failure, or Enhanced login failure

### Changed
- Modernized OptionsFlow to use current Home Assistant pattern
- Modernize type hints: `Optional[X]` → `X | None`, `Dict`/`List`/`Tuple` → builtins across all source files
- Centralize `_safe_float()` into shared parser module - removes 3 duplicate definitions
- Add missing return type hints to MQTT client and proto decoder methods
- Replace bare `except Exception` with specific exception types in proto decoder and runtime
- Unify parser return types to `dict[str, Any]` for consistency

### Fixed
- Downgrade MQTT auth error (rc=5) log from ERROR to WARNING - auto-recovery follows
- Downgrade transient MQTT message handler and connection errors to appropriate log levels
- Remove unused typing imports

## [1.2.8] - 2026-03-29

### Changed
- Reduce log noise: downgrade ~22 operational info messages to debug level across MQTT, auth, coordinator, and API modules
- Add startup summary log with device count and mode breakdown (Enhanced/Standard)

## [1.2.7] - 2026-03-28

### Fixed
- Remove license badge from README - renders as "?" in HACS due to image proxy limitations

## [1.2.6] - 2026-03-28

### Fixed
- Revert homeassistant field in manifest - not allowed for custom integrations (hassfest rejects it)

## [1.2.4] - 2026-03-28

### Changed
- Updated hero screenshots with higher quality images

## [1.2.3] - 2026-03-28

### Fixed
- License badge shows static "MIT" instead of dynamic query that rendered as "?" in HACS

## [1.2.2] - 2026-03-28

### Fixed
- README uses pure markdown only - no HTML tables or emoji shortcodes that HACS cannot render

## [1.2.1] - 2026-03-28

### Changed
- README redesigned for HACS store rendering - hero screenshots, feature grid, compact structure, standard markdown for full compatibility

## [1.2.0] - 2026-03-28

### Added
- Energy Dashboard support for Delta 2 Max - 4 kWh sensors (solar, solar 2, AC input, AC output) via Riemann sum integration
- Energy Dashboard support for Smart Plug - 1 kWh energy sensor via Riemann sum integration
- Entity translations for all 135 entities (English + German) using HA translation_key system
- Firmware version display in HA device page (extracted from API response)

### Changed
- Energy integrator now active for all device types (was PowerOcean only)
- Power-to-energy mappings extracted to const.py as per-device-type constants
- DeviceInfo centralized in coordinator (removed 4x duplication across entity platforms)

## [1.1.2] - 2026-03-28

### Fixed
- Smart Plug: `watts` unit corrected from raw to deciWatt (/10) per API spec "0.1 W"
- Delta 2 Max: MPPT fields scaling corrected per API spec - `outWatts`, `carOutWatts`, `pv2InWatts` (/10), `dcdc12vWatts`, `pv2InAmp` (/100), `dcdc12vVol`, `pv2MpptTemp` (/10)

### Added
- Smart Plug: `maxCur` field parsed (deciAmpere → Ampere)
- PowerOcean: reactive power (VAr) and apparent power (VA) for all 3 grid phases

## [1.1.1] - 2026-03-28

### Fixed
- HTTP API nonce format corrected to 6-digit numeric per EcoFlow API spec (was 16-char alphanumeric, causing intermittent signature errors on some backend servers)
- MQTT keepalive reduced from 120s to 60s - prevents broker disconnect due to ~200s inactivity timeout with insufficient PINGREQ frequency

## [1.1.0] - 2026-03-27

### Added
- **Delta 2 Max MQTT push** - real-time data via IoT MQTT subscription alongside HTTP polling (dual-source)
- MQTT credential refresh on AUTH error (rc=5) with rate-limited retry

### Fixed
- HTTP API nonce collision causing `code=8521 signature is wrong` - nonce upgraded from 6-digit numeric to 16-char alphanumeric (matching IoT API client)
- HA Recorder warnings for `total_increasing` sensors (battery cycles, energy totals) - monotonic filter drops micro-regressions from API

### Changed
- Delta devices now subscribe to `/open/.../quota` MQTT topic for event-driven updates (~1–30 s)
- HTTP polling (~30 s) remains as automatic fallback when MQTT is unavailable

## [1.0.0] - 2026-03-26

### Added
- **PowerOcean** support with 57 sensors and Energy Dashboard integration (6 energy sensors)
- **Delta 2 Max** support with 58 sensors, 5 binary sensors, 3 switches, 4 number entities
- **Smart Plug** support with 9 sensors, 1 binary sensor, 1 switch
- **Standard Mode** - official IoT Developer API, HTTP polling every ~30 s
- **Enhanced Mode** - unofficial WSS MQTT push with ~3 s real-time updates (PowerOcean only)
- Auto-discovery of all devices bound to EcoFlow account
- Energy Dashboard ready sensors (`total_increasing` for solar, grid, battery, home)
- Riemann-sum energy integration with persistent state and gap/jump detection
- 4-tier MQTT reconnect strategy (auto-reconnect, force-reconnect, counter-reset, HTTP fallback)
- Three parallel MQTT keepalives for Enhanced Mode (EnergyStreamSwitch 20s, latestQuotas 30s, ping 60s)
- Portal credential authentication (Login -> JWT -> AES-CFB decrypt -> app-* MQTT credentials)
- Config Flow with device selection, mode selection, and Enhanced Mode login
- Options Flow for runtime mode switching and device management
- Optimistic lock for switch entities (5 s anti-flicker)
- Full protobuf extraction (energy_stream, EMS heartbeat, battery heartbeat, change reports)
- Diagnostics download (no credentials exposed)
- German and English translations
- 332 unit tests covering parsers, proto decoder, API client, energy integrator, manifest
