# Reading a PowerOcean locally over Modbus

This guide covers the Local (Modbus/TCP) connection for a three-phase PowerOcean. It reads the inverter directly over your home network. No EcoFlow account is involved and nothing goes through the cloud. It is read-only: it shows values and never changes a setting.

I built it for my own PowerOcean, the only unit it has been measured on. Treat it as an option for owners who want a connection that does not depend on EcoFlow's servers, not as a replacement for the account sign-in.

## Before you start

| What | Why |
|---|---|
| A three-phase PowerOcean | Single-phase and Plus units, and the Ocean 2, are not offered yet because nobody has measured them |
| Modbus switched on by EcoFlow support | The inverter does not answer on the Modbus port until support enables it. Ask them through the support chat and quote EcoFlow's Open Modbus Protocol document |
| A fixed address for the inverter | Give it a reserved address in your router, or a name your network resolves. The entry stores that address |
| No other Modbus tool on the inverter | The inverter serves one Modbus client at a time. A second tool holding the connection makes this entry unavailable |

EcoFlow's protocol document also describes a Modbus control mode in the EcoFlow Pro app. This integration only reads, and reading worked on my unit once support had enabled Modbus.

## Setting it up

1. Add the integration and choose **Local - three-phase PowerOcean via Modbus/TCP (read-only)**.
2. Enter the inverter's host name or IP address. Leave the port at 502 and the unit ID at 1 unless EcoFlow told you otherwise.
3. The integration reads the serial number and the model from the inverter. If another entry already holds this inverter, the integration refuses to add it a second time.

A Local entry holds no keys and no account. It has one device, the PowerOcean.

## What you get

The entry shows solar, home, grid and battery power, the battery charge level, grid frequency, the two solar strings (voltage and current), battery charge and discharge energy, battery capacity, backup reserve and the number of batteries online. It also shows the inverter's own lifetime counters for solar, grid import and grid export, and a fault count.

The values refresh every 10 seconds. Each refresh opens a short connection and closes it again, so the inverter's single slot is free almost all of the time.

I compared these values with the same inverter's account connection over about 19 hours. The charge level was within 1 percent. Power was on average within 7 W for solar, 11 W for battery, 17 W for grid and 25 W for home. Single readings differ more when the load changes fast, most likely because the two connections are not read in the same second. The battery energy counters matched within 0.01 kWh.

## What you do not get

- **Per-pack charge levels.** The inverter reports the level the app shows, while the account connection reports the battery's own level, up to 5 points higher at a low charge. They are not the same quantity, so they are not shown.
- **Grid phase voltage and current, and the working mode.** They read zero on my unit, so the entry does not create them.
- **Any control.** Writing over Modbus needs a keep-alive signal every minute, and EcoFlow's protocol document warns that writing can affect the inverter's internal scheduling. This mode never writes.
- **The cloud entry's other sensors.** The entities only the account connection provides are unavailable while the entry is Local.

## Switching between account and Local

Open the integration entry and choose **Reconfigure**. If the entry holds only this one PowerOcean, you can switch it to Local and back. The entities both connections share keep their names and history. An entry that holds other devices of the same account cannot be switched. Add a separate Local entry for the PowerOcean instead.

Three energy sensors do not carry over, because the two connections measure different totals. The account connection adds up energy itself since it was set up, while the inverter keeps its own lifetime counters. On my unit the two differ by thousands of kWh. The Local entry therefore has its own **Solar Lifetime Energy**, **Grid Import Lifetime Energy** and **Grid Export Lifetime Energy**. If you use the Energy Dashboard, pick these once after you switch.

## If it does not connect

- **Cannot connect.** Check the address and that EcoFlow support has enabled Modbus for this inverter.
- **It was working and is now unavailable.** Another Modbus tool may be holding the connection, or Modbus is no longer enabled on the inverter. The log names the likely cause once. A serial mismatch in the log means the address now points at a different inverter.
- **The device answered with a Modbus error.** Check the unit ID and that Modbus is enabled for your inverter.
- **Unsupported device.** The inverter reports a model this integration does not read yet.

The decision behind this mode is recorded as ADR-030 in the architecture notes.
