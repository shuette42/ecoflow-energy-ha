# Connecting a PowerOcean locally over Modbus

This guide covers the Local (Modbus/TCP) connection for a three-phase PowerOcean. It talks to the inverter directly over your home network. No EcoFlow account is involved and nothing goes through the cloud. It shows the inverter's values and can change two settings, Backup Reserve and Indicator Brightness. It can also run the battery (see [Battery control](#battery-control)). A switch called Modbus Control takes control of the inverter over Modbus. While it is on, the EcoFlow app is locked.

I built it for my own PowerOcean, the only unit it has been measured on. Treat it as an option for owners who want a connection that does not depend on EcoFlow's servers, not as a replacement for the account sign-in.

## Before you start

| What | Why |
|---|---|
| A three-phase PowerOcean | Single-phase and Plus units, the DC Fit and the Ocean 2 are not offered yet because nobody has measured them. A read-only scan can change that, see [Trying a model that is not offered yet](#trying-a-model-that-is-not-offered-yet) |
| Modbus switched on by EcoFlow support | The inverter does not answer on the Modbus port until support enables it. Ask them through the support chat and quote EcoFlow's Open Modbus Protocol document |
| A fixed address for the inverter | Give it a reserved address in your router, or a name your network resolves. The entry stores that address |
| No other Modbus tool on the inverter | The inverter serves one Modbus client at a time. A second tool holding the connection makes this entry unavailable. The exception is two integrations on Home Assistant's shared Modbus connection, see [Sharing the inverter with another Modbus integration](#sharing-the-inverter-with-another-modbus-integration) |

Thanks to [@jensfr1](https://github.com/jensfr1), who already helped with the Ocean 2 support. For this local connection he showed how EcoFlow enables Modbus on the inverter and made the Modbus protocol description available.

The integration writes only these: the keep-alive signal behind the Modbus Control switch, Backup Reserve, Indicator Brightness, and the battery commands behind Battery Mode and Battery Power Setpoint. See [Control from Home Assistant](#control-from-home-assistant) and [Battery control](#battery-control). Reading and writing both worked on my unit once support had enabled Modbus.

## Setting it up

1. Add the integration and choose **Local - three-phase PowerOcean via Modbus/TCP**.
2. Enter the inverter's host name or IP address. Leave the port at 502 and the unit ID at 1 unless EcoFlow told you otherwise.
3. The integration reads the serial number and the model from the inverter. If another entry already holds this inverter, the integration refuses to add it a second time.

A Local entry holds no keys and no account. It has one device, the PowerOcean.

## What you get

The entry shows solar, home, grid and battery power, the battery charge level, grid frequency, the two solar strings (voltage and current), battery charge and discharge energy, battery voltage, current and temperature, battery capacity, backup reserve, the feed power limit and the number of batteries online. It also shows the inverter's own lifetime counters for solar, grid import and grid export, and a fault count.

Three binary sensors come from the inverter's status word. Off-Grid turns on when the inverter reports that it runs without the grid, so an outage automation can trigger on it with no cloud involved. System Abnormal turns on when the inverter reports a fault. Battery Connection shows whether the inverter sees its battery.

The sensor System Alerts reads a second status word. It lists what the inverter currently reports, such as `fan_failure` or `battery_overheating`, or `none` when all is well. A notification can then name the actual problem.

The diagnostic sensor Active Fault Codes lists the fault codes the inverter reports in decimal, or `none`. Its `faults` attribute adds a name for each code where one is known, in your Home Assistant language where a translation exists and in English otherwise. The integration reads the list only while the fault count is above zero, so an inverter without a fault is not asked for it. The names follow the list that @MaxGrmm published in his EF-PowerOcean-TcpModbus integration. I could not check them against EcoFlow's own material, so a name can be missing or off. The code itself is what the inverter reports.

After five failed reads in a row the entry goes unavailable, and these entities with it. An automation should treat unavailable as "unknown", not as "grid is back". Four of the sensors are disabled by default: Total Battery Capacity, Battery Packs Online (EMS), EMS Backup Ratio and Feed Power Limit. Enable the ones you use in the entity settings. The Backup Reserve number shows the same value as EMS Backup Ratio and is enabled. The six entities under [Control from Home Assistant](#control-from-home-assistant) come on top.

The values refresh every 2 seconds. The inverter updates its power readings about once a second, so this stays close to its own pace. On Home Assistant 2026.9 or newer the connection stays open while the entry is loaded. On older versions each refresh opens a short connection and closes it again, so the inverter's single slot is free almost all of the time.

A lifetime counter only grows. A reading below the last total it showed would look like a meter reset in the statistics, so the integration drops it. If the inverter really reset its counter, every reading stays lower and the entity stays frozen. After a day of that, Home Assistant shows a repair notice for the counter with both totals. Select **Submit** to take the inverter's value from now on. The Energy dashboard then records a counter reset. Select **Ignore** to keep the current total. The timer starts again when Home Assistant restarts.

I compared these values with the same inverter's account connection over about 19 hours. The charge level was within 1 percent. Power was on average within 7 W for solar, 11 W for battery, 17 W for grid and 25 W for home. Single readings differ more when the load changes fast, most likely because the two connections are not read in the same second. The battery energy counters matched within 0.01 kWh.

## Control from Home Assistant

A Local entry has six entities for this: the switch **Modbus Control**, the numbers **Backup Reserve**, **Indicator Brightness** and **Battery Power Setpoint**, the select **Battery Mode**, and the diagnostic binary sensor **Modbus Control Active**. Battery Mode and Battery Power Setpoint are described under [Battery control](#battery-control).

**Modbus Control** takes control of the inverter over Modbus. It is always off after a restart. While it is on, the EcoFlow app is locked: it shows "control via Modbus" and some functions are not available. After you switch it off, the inverter hands control back after about a minute, and the app stays locked until then. On my unit the lock was gone between 30 and 65 seconds after Home Assistant stopped sending the keep-alive signal.

**Backup Reserve** and **Indicator Brightness** each take a value from 0 to 100. They work with Modbus Control off. After each change the integration reads the value back from the inverter, and a change the inverter does not take shows as an error. The value you write stays after Modbus Control ends. Switching it off does not undo it. The backup reserve reading and the Backup Reserve number show the same value from the inverter. With Modbus Control off, the EcoFlow app shows the Backup Reserve you set here. I checked that on my own unit. While Modbus Control is on, the app hides it.

**Modbus Control Active** shows whether the inverter itself reports that Modbus control is on. It can stay on for up to about a minute after you switch Modbus Control off. That is the time the app stays locked.

## Battery control

With Modbus Control on, a Local entry can run the battery. Two entities do this: the select **Battery Mode** and the number **Battery Power Setpoint**. Nothing is written while Modbus Control is off, and a change is refused with the message "Battery control needs the Modbus Control switch to be on."

**Battery Mode** has four options:

- **Automatic.** The PowerOcean decides, as it does without Modbus control.
- **Hold.** The battery stays at rest. It neither charges nor discharges.
- **Charge.** The battery charges at the power of Battery Power Setpoint.
- **Discharge.** The battery discharges at the power of Battery Power Setpoint.

**Battery Power Setpoint** goes from -5000 to 5000 W. A positive value charges and a negative value discharges. The box steps by 50 W, and any whole watt from 50 W is accepted. A value closer to zero than 50 W is refused, so use Hold or Automatic to stop. Charge and Discharge use the size of the setpoint, whichever sign it has. If the number is below 50 W or has not been set, they use 500 W. After a restart the number starts from the value the inverter holds when that is at least 50 W. While Battery Mode is Hold, the number shows -1 W.

In Hold, Charge or Discharge, setting the number switches Battery Mode at once: to Charge for a positive value, to Discharge for a negative one. In Automatic the number only stores the value, and nothing starts until you choose Charge or Discharge.

### From an automation

The examples use the placeholders `select.<powerocean>_battery_mode`, `number.<powerocean>_battery_power_setpoint` and `switch.<powerocean>_modbus_control`. Replace each one with your own entity id, angle brackets included. Entity ids depend on the language of your Home Assistant and on the device name, so copy them from the entity's settings page (Settings > Devices & Services, then the PowerOcean). Turn Modbus Control on before these calls, for example with `switch.turn_on`.

```yaml
automation:
  - alias: "Charge the battery at 1000 W"
    trigger:
      - platform: time
        at: "02:00:00"
    action:
      - service: number.set_value
        target:
          entity_id: number.<powerocean>_battery_power_setpoint
        data:
          value: 1000
      - service: select.select_option
        target:
          entity_id: select.<powerocean>_battery_mode
        data:
          option: charge

  - alias: "Hold the battery"
    trigger:
      - platform: time
        at: "06:00:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.<powerocean>_battery_mode
        data:
          option: hold

  - alias: "Give the battery back to the inverter"
    trigger:
      - platform: time
        at: "08:00:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.<powerocean>_battery_mode
        data:
          option: automatic
```

### What to expect

- **A short pass through Automatic.** Every change goes through Automatic for a few seconds before the new mode or power takes hold. A command the inverter does not confirm shows the error "The battery command was not confirmed by the device. Check the connection and try again."
- **Backup Reserve still applies.** While the battery charge is at or below Backup Reserve, the inverter does not discharge, whatever you ask. The select keeps showing Discharge. Its attribute `discharge_blocked_by_reserve` is true while the charge is at or below the reserve and a discharge is asked for.
- **The return to Automatic.** The inverter does not stay in battery control without the keep-alive. It returns to Automatic when you switch Modbus Control off and when the integration is unloaded. The integration sends a keep-alive signal every 15 seconds. If it stops, the inverter returns to Automatic by itself about a minute after the last one. That is what covers a Home Assistant stop or restart: the integration tries to set Automatic, but by then the connection can no longer be used.
- **Battery Mode can be unknown.** It shows unknown until the inverter has reported its state.

### What has been measured

I measured this on one three-phase PowerOcean (HJ31, firmware V5.1.37.13), controlled from Home Assistant. Charge and Discharge at 1000, 2500 and 5000 W settled within a few watts of the setpoint. A 5000 W charge read 4994 to 5011 W over 150 seconds. A 2500 W discharge read 2497 to 2502 W. Hold kept the battery at 0 W. Sending the same value a second time worked, in about 3 seconds. Switching Modbus Control off or reloading the integration in the middle of a run returned the inverter to Automatic at once. A Home Assistant restart during a charge also ended in Automatic, through the inverter's own timeout. Other units are untested. On a unit that has not been tried yet, start with a small value and watch the battery power sensor before you rely on it.

## Sharing the inverter with another Modbus integration

The inverter serves one Modbus client at a time, and a second client gets no answer. On Home Assistant 2026.9 or newer, this integration uses the Modbus connection that Home Assistant shares between Modbus integrations. A second Modbus integration for the same inverter then works beside it, as long as both are set up with exactly the same host string. If one has an IP address and the other a name, they open two connections and lock each other out. Older Home Assistant versions keep the integration's own connection, and a second Modbus client locks it out.

## What you do not get

- **Per-pack charge levels.** The inverter reports the level the app shows, while the account connection reports the battery's own level, up to 5 points higher at a low charge. They are not the same quantity, so they are not shown.
- **Grid phase voltage and current, and the working mode.** They read zero on my unit, so the entry does not create them.
- **Other controls.** Shutdown and the working mode are not written. The battery commands under [Battery control](#battery-control) are the only power control. EcoFlow's protocol document warns that writing can affect the inverter's internal scheduling.
- **The cloud entry's other sensors.** The entities only the account connection provides are unavailable while the entry is Local.

## Switching between account and Local

Open the integration entry and choose **Reconfigure**. If the entry holds only this one PowerOcean, you can switch it to Local and back. The entities both connections share keep their names and history. An entry that holds other devices of the same account cannot be switched. Add a separate Local entry for the PowerOcean instead.

Three energy sensors do not carry over, because the two connections measure different totals. The account connection adds up energy itself since it was set up, while the inverter keeps its own lifetime counters. On my unit the two differ by thousands of kWh. The Local entry therefore has its own **Solar Lifetime Energy**, **Grid Import Lifetime Energy** and **Grid Export Lifetime Energy**. If you use the Energy Dashboard, pick these once after you switch.

## Trying a model that is not offered yet

If you own a single-phase PowerOcean, a PowerOcean Plus, a DC Fit or an Ocean 2, a short read-only scan shows whether this integration's register map fits your inverter. It needs Python 3.10 or newer and nothing else to install. It runs on any computer that can reach the inverter once EcoFlow support has enabled Modbus. Download [register_scan.py](../tools/register_scan.py) or fetch it from the command line, then run it:

```bash
curl -O https://raw.githubusercontent.com/shuette42/ecoflow-energy-ha/main/documentation/tools/register_scan.py
python3 register_scan.py <inverter address>
```

On Windows the command is `py register_scan.py <inverter address>`. If your Modbus port is not 502 or your unit ID is not 1, add `--port <port>` or `--unit <id>`, the same values as in the Local setup.

The inverter serves one Modbus client at a time. Switch off a Local entry and any other Modbus tool before the scan, or every block times out. See [Sharing the inverter with another Modbus integration](#sharing-the-inverter-with-another-modbus-integration). The scan shows one progress line per block. On an inverter that stays silent it takes a few minutes.

The scan sends only read requests, so it changes nothing on the inverter. It prints which registers answered, their values, and whether each value looks plausible. The report does not contain the address you scanned. The serial number is cut to its first four characters. If the model is not the one this integration serves, the scan leaves out the identity registers and prints "withheld, unknown layout" in their place. I cannot tell where such a model keeps its serial number. Add `--json` for a machine-readable version. Attach the output to a new issue, together with the model name from the EcoFlow app. That measurement is what lets me add a model. If you already run a Local entry, its diagnostics download carries the registers of its last poll too, with the serial number left out.

## If it does not connect

- **Cannot connect.** Check the address and that EcoFlow support has enabled Modbus for this inverter.
- **It was working and is now unavailable.** Another Modbus tool may be holding the connection, a second Modbus integration may use a different host string than this one, or Modbus is no longer enabled on the inverter. The log names the likely cause once. A serial mismatch in the log means the address now points at a different inverter. The inverter's Modbus service can also stop answering. After 30 minutes without an answer, Home Assistant shows a repair notice that names the inverter. The notice goes away when the inverter answers again. On the unit where this was seen, reloading the entry did not bring the answers back. A restart of the inverter did, and that restart came with a firmware update.
- **The device answered with a Modbus error.** Check the unit ID and that Modbus is enabled for your inverter.
- **Unsupported device.** The inverter reports a model this integration does not read yet.

The decisions behind this mode are recorded in the [architecture notes](../architecture/decisions.md).
