# Integrating a PowerOcean Solar/Battery System with Predbat

Predbat is a home battery automation program. It can run as an app on Home Assistant.

It runs every 5 minutes. By default it forecasts 48 hours ahead (configurable). The plan typically covers at least 24 hours after the first charge slot.

Predbat decides on its own when to charge and discharge the battery. Its goal is the lowest cost within the parameters you set. For this prediction it combines a solar forecast from Solcast, Open-Meteo or Forecast.solar with your historical energy usage. It can take EV charging into account or schedule it. It also works with specialised tariffs, including fully variable ones such as Agile Octopus.

## Scope of PowerOcean Integration with Predbat

Predbat does not support EcoFlow devices directly at this time. This guide sets up a PowerOcean as a generic inverter. With that setup Predbat can:

- Control the charging of the PowerOcean batteries
- In the author's setup, with Intelligent Octopus Go configured as the Predbat documentation describes, Predbat also keeps the PowerOcean battery from discharging into the EV during an Octopus charging cycle

Not covered by this guide:

- Predbat-controlled energy export from the battery to the grid

## Mechanism of Control

With this configuration Predbat sees the current power (W) of the PowerOcean Battery, Load (Home), Solar and Grid. It also sees today's energy for home load, grid import, grid export and solar. It reads the battery SOC (state of charge, %), and it reads and changes the PowerOcean Backup Reserve (the percentage of the battery held in reserve).

The integration writes the Backup Reserve number as a pair with Solar Surplus Threshold. Writing a reserve above the Solar Surplus Threshold raises the threshold to the same value. Until the PowerOcean has reported the threshold, a write is refused with an error.

A custom schedule in the EcoFlow app charges the battery at 6000 W for the whole day. Scripts in Home Assistant enable and disable this schedule and set its charge power. Predbat calls these scripts to switch battery charging on and off.

The export scripts (`predbat_powerocean_export_start` and `predbat_powerocean_export_stop`) are placeholders. They only write a log entry in Home Assistant. Export control is not covered by this guide.

## Prerequisites

- The EcoFlow Energy integration (`ecoflow-energy-ha`) is installed and configured in Home Assistant and connected in `Enhanced` mode. The schedules this guide uses are not visible in `Standard` mode.
- The PowerOcean runs in `Self-use` work mode. You can set this in the Home Assistant integration or in the EcoFlow app.
- The Predbat app is installed, see https://springfall2008.github.io/batpred/install/#PredBat-app-install
- Tariff, car charger and car integrations (for example the [Octopus Energy integration](https://bottlecapdave.github.io/HomeAssistant-OctopusEnergy/) for an Octopus tariff) are installed and configured as Predbat's own documentation describes.

## Configuration

All configuration below assumes that the EcoFlow sensor entity ids start with `sensor.ecoflow_powerocean_`. For example, the battery SOC (state of charge) is `sensor.ecoflow_powerocean_battery_soc`. If your ids differ, change them in the Home Assistant configuration and in the Predbat configuration to match your installation.

### EcoFlow Automations

To let Home Assistant ask the PowerOcean to charge its battery packs, set up a schedule in the EcoFlow app. Menu labels can differ by app version:

1. Open the home item for the PowerOcean you want to control
2. Open its settings
3. Select `Automation`
4. Select `Scheduled Tasks`
5. Press `set` (in the author's iOS app this button read `No tasks`)
6. Press `Schedule charging/discharging +`
7. Under `If`, set the times to `00:00` - `24:00` (the whole day)
8. Under `Then` select `Charge the battery`, set `Max power` to `6000W`
9. Under `Repeat` select `Daily`
10. Save it with the tick at the top right of the screen
11. Disable the schedule.

The schedule should now appear in the EcoFlow Energy integration in Home Assistant, most likely as `Schedule 1` in the controls section. That gives two entities: `Schedule 1 Charge Power` (entity id `number.ecoflow_powerocean_schedule_1_charge_power`) and `Schedule 1 Enabled` (entity id `switch.ecoflow_powerocean_schedule_1_enabled`).

If your entity ids differ from these two, change them in the scripts in the next section.

### Home Assistant Helpers

Predbat needs a daily resetting energy meter for each of home load, import, export and solar PV. The EcoFlow integration does not provide these, so you define 4 helpers. Home Assistant builds the entity id from the helper name. The names below give the entity ids that the `apps.yaml` further down uses:

- Helper name `EcoFlow PowerOcean Home Energy Usage Today`, input sensor
  `Home Energy` (`sensor.ecoflow_powerocean_home_energy`), resulting entity id
  `sensor.ecoflow_powerocean_home_energy_usage_today`
- Helper name `EcoFlow PowerOcean Import Energy Today`, input sensor `Grid
  Import Energy` (`sensor.ecoflow_powerocean_grid_import_energy`), resulting
  entity id `sensor.ecoflow_powerocean_import_energy_today`
- Helper name `EcoFlow PowerOcean Export Energy Today`, input sensor `Grid
  Export Energy` (`sensor.ecoflow_powerocean_grid_export_energy`), resulting
  entity id `sensor.ecoflow_powerocean_export_energy_today`
- Helper name `EcoFlow PowerOcean Solar Energy Today`, input sensor `Solar
  Energy` (`sensor.ecoflow_powerocean_solar_energy`), resulting entity id
  `sensor.ecoflow_powerocean_solar_energy_today`

For each of these:

- Go to `Settings` -> `Devices & Services` -> `Helpers`
- Click the `Create Helper` button at the bottom right
- Choose `Utility Meter`
- Enter the name from the list above
- In `Input Sensor`, enter the input sensor name or entity id from the list
- Set `Meter Reset Cycle` to `Daily`
- Turn on both `Periodically Resetting` and `Sensor Always Available`
- Click `Submit` and check that the entity id matches the one in the list. Change it if it does not.

### Home Assistant Scripts

Define 4 Home Assistant scripts for these PowerOcean functions:

- Start charging, with the charge power in W (default 6000): `script.predbat_powerocean_charge_start`
- Stop charging: `script.predbat_powerocean_charge_stop`
- Start export (placeholder only): `script.predbat_powerocean_export_start`
- Stop export (placeholder only): `script.predbat_powerocean_export_stop`

To create them, go to `Settings` -> `Automations and scenes` -> `Scripts`. For each script:

- Click `Create script` -> `Create new script`
- Select `Edit in YAML` from the ⋮ menu at the top right
- Paste the YAML of one script from the examples below and save it.

The charge start script limits the power to 500 - 6000 W in steps of 500 W. These limits depend on the PowerOcean model and the number of battery packs. Adjust the minimum, maximum and step to your system. The integration's charge power number takes steps of 100 W.

```yaml
---
alias: predbat_powerocean_charge_start
description: Predbat PowerOcean - Charge Start
mode: restart
fields:
  power:
    name: Charge power
    description: Charge power to set on the EcoFlow PowerOcean schedule
    required: false
    default: 6000
    selector:
      number:
        min: 500
        max: 6000
        step: 500
        mode: box
sequence:
  - variables:
      charge_power: >-
        {% set snapped = (power | float(6000) / 500) | round(0, "ceil") * 500 %}
        {{ [[snapped, 500] | max, 6000] | min }}
  - action: system_log.write
    data:
      message: "Predbat Start Charge on EcoFlow - charge power {{ charge_power }} W ({{ power }})"
      level: info
  - action: number.set_value
    target:
      entity_id: number.ecoflow_powerocean_schedule_1_charge_power
    data:
      value: "{{ charge_power }}"
  - action: switch.turn_on
    target:
      entity_id: switch.ecoflow_powerocean_schedule_1_enabled

---
sequence:
  - action: system_log.write
    data:
      message: Predbat Stop Charge on EcoFlow
      level: info
  - action: switch.turn_off
    target:
      entity_id: switch.ecoflow_powerocean_schedule_1_enabled
alias: predbat_powerocean_charge_stop
description: Predbat Stop Charge on EcoFlow
mode: restart

---
sequence:
  # Currently a placeholder - just generates a log message
  - action: system_log.write
    data:
      message: Predbat Start Export on EcoFlow Placeholder
      level: info

alias: predbat_powerocean_export_start
description: Predbat Start Export on EcoFlow
mode: restart

---
sequence:
  # Currently a placeholder - just generates a log message
  - action: system_log.write
    data:
      message: Predbat Stop Export on EcoFlow Placeholder
      level: info
alias: predbat_powerocean_export_stop
description: Predbat Stop Export on EcoFlow
mode: restart
```

### Predbat `apps.yaml`

The basic setup configures the inverter in Predbat and controls it through the scripts above.

The example assumes a 5000 W single phase hybrid inverter and 25 kWh of battery (5 units). The system charges at up to 6000 W, so that is the maximum charge rate. If your installation differs, change the values under `# --- Battery model ---` to match your system.

```yaml
pred_bat:
  module: predbat
  class: PredBat

  # Sets the prefix for all created entities in HA - only change if you want to run more than once instance
  prefix: predbat

  # Timezone to work in
  timezone: Europe/London

  # Currency, symbol for main currency and second symbol for 1/100s e.g. $ c, £ p or € c
  currency_symbols:
    - "£"
    - "p"

  # Number of threads to use in plan calculation
  # Can be auto for automatic, 0 for off or values 1-N for a fixed number
  threads: auto

  # --- INVERTER CONFIGURATION ---
  # --- Inverter: custom / service-based ---
  inverter_type: "EF"
  inverter:
    name: "EcoFlow PowerOcean"
    has_rest_api: false
    has_mqtt_api: false
    has_service_api: true
    output_charge_control: "power"
    has_charge_enable_time: false
    has_discharge_enable_time: false
    has_target_soc: false
    has_reserve_soc: true
    charge_time_format: "S"
    charge_time_entity_is_option: false
    soc_units: "%"
    num_load_entities: 1
    has_ge_inverter_mode: false
    has_fox_inverter_mode: false
    time_button_press: false
    clock_time_format: "%d-%m-%y %H:%M:%S"
    write_and_poll_sleep: 2
    has_time_window: false
    support_charge_freeze: false
    support_discharge_freeze: false
  num_inverters: 1

  # --- Telemetry (adjust entity IDs to your integration) ---
  soc_percent: sensor.ecoflow_powerocean_battery_soc
  battery_power: sensor.ecoflow_powerocean_battery_power
  load_power: sensor.ecoflow_powerocean_home_power
  pv_power: sensor.ecoflow_powerocean_solar_power
  grid_power: sensor.ecoflow_powerocean_grid_power
  load_today: sensor.ecoflow_powerocean_home_energy_usage_today
  import_today: sensor.ecoflow_powerocean_import_energy_today
  export_today: sensor.ecoflow_powerocean_export_energy_today
  pv_today: sensor.ecoflow_powerocean_solar_energy_today
  reserve: number.ecoflow_powerocean_backup_reserve

  # --- Battery model ---
  soc_max: 25.0 # usable kWh of your PowerOcean battery
  battery_size: 25.0 # usable kWh of your PowerOcean battery
  inverter_limit: 5000 # max AC output (W)
  inverter_limit_charge:
    - 6000 # max AC charge (W)
  inverter_limit_discharge:
    - 5000 # max AC discharge (W)
  battery_rate_max: 6000 # max charge rate (W)
  battery_charge_power_curve:
    100: 1.0
  battery_discharge_power_curve:
    4: 1.0
  battery_scaling_auto: true

  # --- Control (Service API) ---
  charge_start_service:
    - service: script.predbat_powerocean_charge_start
      power: "{power}"
      repeat: True
  charge_stop_service:
    - service: script.predbat_powerocean_charge_stop
  discharge_start_service:
    - service: script.predbat_powerocean_export_start
  discharge_stop_service:
    - service: script.predbat_powerocean_export_stop

  # --- Control behavior ---
  set_soc_enable: true
  set_reserve_enable: true
  battery_power_invert: true # this fixes the direction of arrows on Predbat's power flow diagram
  grid_power_invert: true # this fixes the direction of arrows on Predbat's power flow diagram

  # Inverter clock skew in minutes, e.g. 1 means it's 1 minute fast and -1 is 1 minute slow
  inverter_clock_skew_start: 0
  inverter_clock_skew_end: 0
  inverter_clock_skew_discharge_start: 0
  inverter_clock_skew_discharge_end: 0
  clock_skew: 0
```

With `repeat: True`, Predbat calls the charge start script again in every cycle. Each call writes the charge power and the schedule switch to the PowerOcean again.

Solar forecast, carbon intensity, Octopus billing data, tariff comparison, car charging and Octopus Intelligent are general Predbat settings. They are not specific to the PowerOcean. Add them to the same `apps.yaml` as the [Predbat documentation](https://springfall2008.github.io/batpred/) describes.

### Predbat settings

This guide sets Predbat to a variable charge rate instead of always charging at the maximum rate. The author chose this because it seemed less aggressive for the battery system.

- In the Predbat UI, open `Settings` -> `Config`, find `switch.predbat_set_charge_low_power` and turn it on.
- Set `input_number.predbat_charge_low_power_margin` to 30 (minutes). That leaves some extra headroom for charging.

## Log Warnings

At first there can be many warnings of the form `Found 1 periods of zero load with power data, filling using power integration`. In the author's installation they stopped once Predbat had collected more history.

## Using Predbat

You can start with Predbat in `Monitor` mode (set it in the `Status` section of the Predbat Dash page). In this mode Predbat makes predictions and plans but does not control the PowerOcean.

Once the plan looks right and no errors are logged, switch Predbat to `Control charge` mode. `Control charge & discharge` has no use here, because the export scripts are placeholders and export control is not covered by this guide.
