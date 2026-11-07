# Integrating a PowerOcean Solar/Battery System with Predbat

Predbat is a home battery automation program that can run as an app on Home
Assistant

It runs every 5 minutes and, by default, forecasts 48 hours ahead
(configurable), with the plan typically covering at least 24 hours after the
first charge slot.

Predbat will automatically decide when to charge and discharge your battery to
achieve the best (lowest) cost spend within the parameters you have set. It uses
the solar production forecast from Solcast, Open-Meteo, or Forecast.solar
combined with your historical energy usage to make this prediction. It can take
account of EV charging and/or schedule that charging. It can work with
specialised tariffs including fully variable tarrifs like Agile Octopus.

## Scope of PowerOcean Integration with PredBat

PredBat does not, at this time, directly support EcoFlow devices. This guide
describes how to set an EcoFlow PowerOcean system up as a generic inverter
system which will allow PredBat to:-

- Control the charging of the PowerOcean batteries
- Correctly handle EV charging under Octopus Intelligent Go (ie prevent the
  PowerOcean battery charge being sent to the EV during an Octopus charging
  cycle)

At this time it does not handle:-

- Predbat controlled energy export from the battery to the grid - I have predbat set to `Control Charge` mode.

The configuration in this document has been running in this form on my system
without issues for more than two weeks (and previous less sophisticated versions
of it for rather longer before).

The `Work Mode` for the PowerOcean should be set to `Self-use` - I have no idea
what would happen if you had the system in the AI mode but I suspect that
predbat and EcoFlow fighting over control would not lead to a good result.

## Mechanism Of Control

The PredBat configuration allows it to see the current power (in KW) of the
PowerOcean Battery, Load (Home), Solar and Grid, and today's energy usage for
Battery, Load (Home), Solar and Grid. Additionally PredBat can also see the
PowerOcean battery SOC (State of Charge percentage) and see and change the
PowerOcean Backup Reserve (percentage of battery reserved).

There is a custom schedule set up in the EcoFlow app to charge the battery at
5KW covering the whole day. There are custom automations defined in Home
Assistant to enable/disable this schedule, and PredBat uses these automations to
switch on/off charging the battery.

There are stub automations set up to handle export to grid, but at this time I
do not have those working, so they currently just log an action to Home
Assistant.

## Configuration

This assumes you have installed and configured the `ecoflow-energy-ha` (EcoFlow
Energy) integration in Home Assistant, and the basic `PredBat` app installation.

In my installation the EcoFlow PowerOcean is marked as being in the Garage (an
Area defined within Home Assistant) - this means that some of the entity names
are prefixed (for me) with `garage`. For example while the SOC (State of Charge)
is entity `sensor.ecoflow_powerocean_battery_soc` the home energy used today is
`sensor.garage_ecoflow_powerocean_home_energy_usage_today`. When taking this
config and using it for your own system please check that the entity names are
right for your installation.

### EcoFlow Automations

To set up the capability for Home Assistant to request that the PowerOcean
charge its battery packs, do the following operations within the app:-

1. Go to the home item, selecting the PowerOcean you wish to control
2. Go into the settings (within the home item)
3. Select `Automation`
4. Select `Scheduled Tasks`
5. Press `set` (confusingly labeled `No tasks` on the current version of the IOS
   app I am testing with)
6. Press `Schedule charing/discharging +` 7. Under `If`, change the times to be
   `00:00` - `24:00` (ie select the whole day)
8. Under `Then` select `Charge the battery`, set `Max power` to `5000W`
9. Under `Repeat` select `Daily`
10. Save it (touch the Tick at the top right of the screen)
11. Set the schedule as disabled.

If you now check the EcoFlow Energy integration in Home Assistant you should see
your schedule appear, probably as `Schedule 1` in the controls part of the
integration - so there would be two entries `Schedule 1 Charge Power` and
`Schedule 1 Enabled`

### Home Assistant Scripts

I define 4 Home Assistant Scripts to perform the following PowerOcean functions:-

- Start charging - with the charging power defined in watts (defaults to 5000)
- Stop charging
- Start export (currently placeholder only)
- Stop export (currently placeholder only)

To create these in Home Assistant go to `Settings` -> `Automations and scenes`
-> `Scripts`. For each script click `Create script` -> `Create new script` and
then use the top right ⋮ menu item to select `Edit in YAML`. Copy the YAML
content for one script from the below examples and save it. (If your schedule
created in the previous step is not `Schedule 1` please change the name in the
script).

```yaml
---
alias: script.predbat_powerocean_charge_start
description: Predbat PowerOcean - Charge Start
mode: restart
fields:
  power:
    name: Charge power
    description: Charge power to set on the EcoFlow PowerOcean schedule
    required: false
    default: 5000
    selector:
      number:
        min: 500
        max: 6000
        step: 500
        mode: box
sequence:
  - variables:
      charge_power: >-
        {% set snapped = (power | float(5000) / 500) | round(0) * 500 %}
        {{ [[snapped, 500] | max, 6000] | min }}
  - action: system_log.write
    data:
      message: "Predbat Start Charge on EcoFlow - charge power {{ charge_power }} W"
      level: info
  - action: number.set_value
    target:
      entity_id: number.garage_ecoflow_powerocean_schedule_1_charge_power
    data:
      value: "{{ charge_power }}"
  - action: switch.turn_on
    target:
      entity_id: switch.garage_ecoflow_powerocean_schedule_1_enabled

---
sequence:
  - action: system_log.write
    data:
      message: Predbat Stop Charge on EcoFlow
      level: info
  - action: switch.turn_off
    target:
      entity_id: switch.garage_ecoflow_powerocean_schedule_1_enabled
alias: script.predbat_powerocean_charge_stop
description: Predbat Stop Charge on EcoFlow
mode: restart

---
sequence:
  # Currently a placeholder - just generates a log message
  - action: system_log.write
    data:
      message: Predbat Start Export on EcoFlow Placeholder
      level: info

alias: script.predbat_powerocean_export_start
description: Predbat Start Export on EcoFlow
mode: restart

---
sequence:
  # Currently a placeholder - just generates a log message
  - action: system_log.write
    data:
      message: Predbat Stop Export on EcoFlow Placeholder
      level: info
alias: script.predbat_powerocean_export_stop
description: Predbat Stop Export on EcoFlow
mode: restart
```

### PredBat `apps.yaml`

The basic setup configures the Inverter for PredBat using the scripts defined
above to control it. My installation has a 5kW single phase hybrid inverter and
20kW of battery (4 units). It can apparently charge at 6kW so that is set as the
maximum charge rate.

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
  load_today: sensor.garage_ecoflow_powerocean_home_energy_usage_today
  import_today: sensor.garage_ecoflow_powerocean_import_energy_today
  export_today: sensor.garage_ecoflow_powerocean_export_energy_today
  pv_today: sensor.garage_ecoflow_powerocean_solar_energy_today
  reserve: number.ecoflow_powerocean_backup_reserve

  # --- Battery model ---
  soc_max: 20.0
  battery_size: 20.0 # usable kWh of your PowerOcean battery
  inverter_limit: 5000 # max AC output (W)
  inverter_limit_charge:
    - 6000 # max AC charge (W)
  inverter_limit_discharge:
    - 5000 # max AC discharge (W)
  battery_charge_power_curve:
    100: 1.0
  battery_discharge_power_curve:
    4: 1.0
  battery_scaling_auto: true

  # --- Control (Service API) ---
  charge_start_service:
    - service: script.script_predbat_powerocean_charge_start
      power: "{power}"
      repeat: True
  charge_stop_service:
    - service: script.script_predbat_powerocean_charge_stop
  discharge_start_service:
    - service: script.script_predbat_powerocean_export_start
  discharge_stop_service:
    - service: script.script_predbat_powerocean_export_stop

  # --- Control behavior ---
  set_soc_enable: true
  set_reserve_enable: true
  battery_power_invert: true # this fixes the direction of arrows on predbats power flow diagram
  grid_power_invert: true # this fixes the direction of arrows on predbats power flow diagram

  # Inverter clock skew in minutes, e.g. 1 means it's 1 minute fast and -1 is 1 minute slow
  inverter_clock_skew_start: 0
  inverter_clock_skew_end: 0
  inverter_clock_skew_discharge_start: 0
  inverter_clock_skew_discharge_end: 0
  clock_skew: 0
```

Additionally I have some extra sections defined, which may be more peculiar to
my setup. Firstly I have the configuration for Open Meteo (no API key needed)
and National Grid to get Solar and Carbon intensity forecasts. _NB: the postcode
used here is not my house._

```yaml
  # Solar forecast via Open-Meteo
  open_meteo_forecast:
    - postcode: YO1 7HH
      kwp: 3.22
      declination: 42
      azimuth: -165
    - postcode: YO1 7HH
      kwp: 1.38
      declination: 42
      azimuth: -75
    - postcode: YO1 7HH
      kwp: 1.38
      declination: 42
      azimuth: 105

  # Carbon Intensity data direct from National grid
  carbon_postcode: "YO1 7HH"
  carbon_automatic: true
```

For car charging you may need to set up configuration for your car charger. In
my case all the defaults work and all I need to set are the battery size and the
sensor that points to the car battery state of charge:

```yaml
  car_charging_battery_size:
    - 58
  car_charging_soc: sensor.iddy_battery
```

I use Octopus Intelligent Go tariff, and have the Octopus integration from
`BottlecapDave` installed and configured. So predbat requires some configuration
to make use of that - this is the basic default config taken from predbad
documentation/sample config.

```yaml
  # Or if using bottle cap dave Home Assistant Octopus Energy plugin set the entities here
  # If you have Octopus intelligent, enable the intelligent slot information to add to pricing
  # Will automatically disable if not found, or comment out to disable fully
  # When enabled it overrides the 'car_charging_planned' feature and predict the car charging based on the intelligent plan (unless octopus intelligent charging is False)
  # This matches either the intelligent slot from the Octopus Plugin or from the Intelligent plugin
  octopus_intelligent_slot: "re:(binary_sensor.octopus_energy([0-9a-z_]+|)_intelligent_dispatching)"
  octopus_ready_time: "re:((select|time).octopus_energy_([0-9a-z_]+|)_intelligent_target_time)"
  octopus_charge_limit: "re:(number.octopus_energy([0-9a-z_]+|)_intelligent_charge_target)"
  octopus_slot_low_rate: true
  octopus_slot_max: 12

  # Octopus saving session points to the saving session Sensor in the Octopus plugin, when enabled saving sessions will be at the assumed
  # Rate is read automatically from the add-in and converted to pence using the conversion rate below (default is 8)
  octopus_saving_session: "re:(event.octopus_energy([0-9a-z_]+|)_saving_session_event(s|))"
  octopus_saving_session_octopoints_per_penny: 8

  # Octopus free session points to the free session Sensor in the Octopus plugin
  # Note: You must enable this event sensor in the Octopus Integration in Home Assistant for it to work
  octopus_free_session: "re:(event.octopus_energy_([0-9a-z_]+|)_octoplus_free_electricity_session_events)"

  # Set import and export entity to point to the Octopus Energy plugin import and export sensors
  metric_octopus_import: "re:(sensor.(octopus_energy_|)electricity_[0-9a-z]+_[0-9a-z]+_current_rate)"
  metric_octopus_export: "re:(sensor.(octopus_energy_|)electricity_[0-9a-z]+_[0-9a-z]+_export_current_rate)"

  # Standing charge in pounds, can be set to a sensor or manually entered (e.g. 0.50 is 50p)
  metric_standing_charge: "re:(sensor.(octopus_energy_|)electricity_[0-9a-z]+_[0-9a-z]+_current_standing_charge)"
```

### PredBat settings

We set predbat to use a variable charging rate rather than always just charging
at maximum rates. Interestingly predbat always appears to set the charge rate
to around 2.5kW - and ecoflow then sees around 2350W going to the battery, but
this seems less aggressive than my first attempt that always charged at maximum
rate.

- In PredBat UI, under `Settings` -> `Config` find
  `switch.predbat_set_charge_low_power` and set it to on.
- I also change `input_number.predbat_charge_low_power_margin` to 30 (minutes),
  to give some extra headroom on charging.

## Log Warnings

Initially there were a lot of warnings emitted of the form `Found 1 periods of
zero load with power data, filling using power integration`. These appear to be
safe to ignore, and once the system has picked up more history from your
installation they go away.
