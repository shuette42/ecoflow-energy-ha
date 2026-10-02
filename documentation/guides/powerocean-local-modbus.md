# Connecting a PowerOcean locally over Modbus

This guide covers the Local (Modbus/TCP) connection for a three-phase PowerOcean. It talks to the inverter directly over your home network. No EcoFlow account is involved and nothing goes through the cloud. It shows the inverter's values and can change two settings, Backup Reserve and Indicator Brightness. A switch called Modbus Control takes control of the inverter over Modbus. While it is on, the EcoFlow app is locked.

I built it for my own PowerOcean, the only unit it has been measured on. Treat it as an option for owners who want a connection that does not depend on EcoFlow's servers, not as a replacement for the account sign-in.

## Before you start

| What | Why |
|---|---|
| A three-phase PowerOcean | Single-phase and Plus units, and the Ocean 2, are not offered yet because nobody has measured them |
| Modbus switched on by EcoFlow support | The inverter does not answer on the Modbus port until support enables it. Ask them through the support chat and quote EcoFlow's Open Modbus Protocol document |
| A fixed address for the inverter | Give it a reserved address in your router, or a name your network resolves. The entry stores that address |
| No other Modbus tool on the inverter | The inverter serves one Modbus client at a time. A second tool holding the connection makes this entry unavailable. The exception is two integrations on Home Assistant's shared Modbus connection, see [Sharing the inverter with another Modbus integration](#sharing-the-inverter-with-another-modbus-integration) |

The integration writes only these: the keep-alive signal behind the Modbus Control switch, Backup Reserve and Indicator Brightness. See [Control from Home Assistant](#control-from-home-assistant). Reading and writing both worked on my unit once support had enabled Modbus.

## Setting it up

1. Add the integration and choose **Local - three-phase PowerOcean via Modbus/TCP**.
2. Enter the inverter's host name or IP address. Leave the port at 502 and the unit ID at 1 unless EcoFlow told you otherwise.
3. The integration reads the serial number and the model from the inverter. If another entry already holds this inverter, the integration refuses to add it a second time.

A Local entry holds no keys and no account. It has one device, the PowerOcean.

## What you get

The entry shows solar, home, grid and battery power, the battery charge level, grid frequency, the two solar strings (voltage and current), battery charge and discharge energy, battery voltage, current and temperature, battery capacity, backup reserve, the feed power limit and the number of batteries online. It also shows the inverter's own lifetime counters for solar, grid import and grid export, and a fault count.

Two binary sensors come from the inverter's status word. Off-Grid turns on when the inverter reports that it runs without the grid, so an outage automation can trigger on it with no cloud involved. System Abnormal turns on when the inverter reports a fault. After five failed reads in a row the entry goes unavailable, and both sensors with it. An automation should treat unavailable as "unknown", not as "grid is back". Feed Power Limit is disabled by default. Enable it in the entity settings if you use it. The four entities under [Control from Home Assistant](#control-from-home-assistant) come on top.

The values refresh every 2 seconds. The inverter updates its power readings about once a second, so this stays close to its own pace. On Home Assistant 2026.9 or newer the connection stays open while the entry is loaded. On older versions each refresh opens a short connection and closes it again, so the inverter's single slot is free almost all of the time.

I compared these values with the same inverter's account connection over about 19 hours. The charge level was within 1 percent. Power was on average within 7 W for solar, 11 W for battery, 17 W for grid and 25 W for home. Single readings differ more when the load changes fast, most likely because the two connections are not read in the same second. The battery energy counters matched within 0.01 kWh.

## Control from Home Assistant

A Local entry has four entities for this: the switch **Modbus Control**, the numbers **Backup Reserve** and **Indicator Brightness**, and the diagnostic binary sensor **Modbus Control Active**.

**Modbus Control** takes control of the inverter over Modbus. It is always off after a restart. While it is on, the EcoFlow app is locked: it shows "control via Modbus" and some functions are not available. After you switch it off, the inverter hands control back after about a minute, and the app stays locked until then. On my unit the lock was gone between 30 and 65 seconds after Home Assistant stopped sending the keep-alive signal.

**Backup Reserve** and **Indicator Brightness** each take a value from 0 to 100. They work with Modbus Control off. After each change the integration reads the value back from the inverter, and a change the inverter does not take shows as an error. The value you write stays after Modbus Control ends. Switching it off does not undo it. The backup reserve reading and the Backup Reserve number show the same value from the inverter. With Modbus Control off, the EcoFlow app shows the Backup Reserve you set here. I checked that on my own unit. While Modbus Control is on, the app hides it.

**Modbus Control Active** shows whether the inverter itself reports that Modbus control is on. It can stay on for up to about a minute after you switch Modbus Control off. That is the time the app stays locked.

## Sharing the inverter with another Modbus integration

The inverter serves one Modbus client at a time, and a second client gets no answer. On Home Assistant 2026.9 or newer, this integration uses the Modbus connection that Home Assistant shares between Modbus integrations. A second Modbus integration for the same inverter then works beside it, as long as both are set up with exactly the same host string. If one has an IP address and the other a name, they open two connections and lock each other out. Older Home Assistant versions keep the integration's own connection, and a second Modbus client locks it out.

## What you do not get

- **Per-pack charge levels.** The inverter reports the level the app shows, while the account connection reports the battery's own level, up to 5 points higher at a low charge. They are not the same quantity, so they are not shown.
- **Grid phase voltage and current, and the working mode.** They read zero on my unit, so the entry does not create them.
- **Other controls.** Power setpoints, shutdown and the working mode are not written. EcoFlow's protocol document warns that writing can affect the inverter's internal scheduling.
- **The cloud entry's other sensors.** The entities only the account connection provides are unavailable while the entry is Local.

## Switching between account and Local

Open the integration entry and choose **Reconfigure**. If the entry holds only this one PowerOcean, you can switch it to Local and back. The entities both connections share keep their names and history. An entry that holds other devices of the same account cannot be switched. Add a separate Local entry for the PowerOcean instead.

Three energy sensors do not carry over, because the two connections measure different totals. The account connection adds up energy itself since it was set up, while the inverter keeps its own lifetime counters. On my unit the two differ by thousands of kWh. The Local entry therefore has its own **Solar Lifetime Energy**, **Grid Import Lifetime Energy** and **Grid Export Lifetime Energy**. If you use the Energy Dashboard, pick these once after you switch.

## If it does not connect

- **Cannot connect.** Check the address and that EcoFlow support has enabled Modbus for this inverter.
- **It was working and is now unavailable.** Another Modbus tool may be holding the connection, a second Modbus integration may use a different host string than this one, or Modbus is no longer enabled on the inverter. The log names the likely cause once. A serial mismatch in the log means the address now points at a different inverter.
- **The device answered with a Modbus error.** Check the unit ID and that Modbus is enabled for your inverter.
- **Unsupported device.** The inverter reports a model this integration does not read yet.

The decisions behind this mode are recorded in the [architecture notes](../architecture/decisions.md).
