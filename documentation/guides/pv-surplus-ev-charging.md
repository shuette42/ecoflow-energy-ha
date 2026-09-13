# Charging an electric car from solar surplus

This guide builds a wallbox automation from four PowerOcean readings. The car charges when the roof produces more than the house and the home battery take. It stops when a cloud or the evening ends that. The wallbox is one that Home Assistant controls, which means any wallbox with its own integration. The PowerPulse section below says what is different when the wallbox is an EcoFlow PowerPulse.

One thing before reading on: I do not run a third-party wallbox myself. The caravan guide next to this one is a measured setup. This one is a template. The thresholds below are starting points with the reasoning behind each, not values I have watched a car charge on. Where a number depends on your hardware, the text says so.

## What this solves

A PowerOcean manages surplus on its own, for its own battery. Every watt beyond the house load goes into the battery first. Only what the battery cannot take reaches the grid as export, because it is full or its charge power is capped. The app shows the familiar picture: solar up, battery charging, export near zero until the afternoon.

A car on a wallbox is not part of that picture. Left alone, a wallbox charges at whatever it is set to, from whatever source is there. On a sunny noon that is mostly the grid, because the battery has already claimed the surplus. The automation in this guide gives the surplus to the car instead. It decides between the car and the home battery with one number: the battery state of charge at which the car may take the battery's share.

## The PowerPulse case first

If your wallbox is an EcoFlow PowerPulse or PowerPulse 2 on the same account, stop here and use the app. The PowerOcean already drives that wallbox from surplus. In the app the setting is called "Prioritize controllable devices". This integration exposes the same slider as the number **Solar Surplus Threshold** on the PowerOcean device. Above that battery SoC, surplus goes to the wallbox before it goes into the battery. Set it and the PowerOcean does everything this guide builds by hand. It has one advantage no automation has: it decides on the device, on the same readings, without a round trip through the cloud.

For a PowerPulse 2 this integration adds two buttons, **Wallbox Start Charging** and **Wallbox Stop Charging**, confirmed from the wallbox's own report. They are for a manual start and stop, or for an automation with a reason the surplus logic does not know, such as a departure time. Do not build the surplus loop below on top of them. Two controllers on one wallbox fight, and the one on the device wins.

## What you need

| Item | Why |
|---|---|
| A PowerOcean with account sign-in set up in this integration | The four readings below update every 3 seconds on that path. Developer keys update every 30 seconds. That still works for the start and stop decision, but the current regulation in the package will lag behind clouds |
| A wallbox with a Home Assistant integration that offers a charging switch, a charging current number, or both | The package writes to a switch and a number. Which of the two your wallbox exposes decides which half of the package applies. See "Adapting to your wallbox" |
| The four PowerOcean entities named below, with the entity ids your installation gave them | The ids in the package are placeholders |

The package uses these readings, all on the PowerOcean device:

| Entity | Role in the loop |
|---|---|
| **Grid Export Power** (W, always 0 or more) | The surplus nobody is using right now |
| **Battery Charge Power** (W, always 0 or more) | The surplus the home battery is taking. Above the hand-over SoC this is added to the car's budget |
| **Battery SOC** (%) | The hand-over decision |
| **Solar Power** (W) | A plausibility gate: no charging from surplus when the roof is dark, whatever the export reading says |

**Grid Power** and **Battery Power** also exist, as signed values. The package uses the split sensors on purpose. They cannot be misread for sign, and a wrong sign in a surplus loop starts the car at night.

## The one number that matters

The PowerOcean charges its battery before it exports. On most days the export reading stays near zero until the battery is nearly full. A loop that only watches export would not start the car before mid-afternoon. That is right for some households and wrong for others. The difference is a single threshold:

```
budget = grid_export_power + (battery_charge_power if battery_soc >= handover_soc else 0)
```

Below `handover_soc` the car gets only what would otherwise be exported. At or above it, the car also gets what the battery is currently taking. The battery stops taking it as soon as the car draws it, because the PowerOcean sees less surplus. Nothing is written to the PowerOcean for this. It simply responds to the house load rising.

A starting point is 80 %. Set it higher if you want the evening covered from the battery before the car sees any surplus. Set it lower if the car is what you want charged. The package keeps this number in an input helper, so you can change it from a dashboard without editing YAML.

## Why the thresholds are not small

A wallbox cannot charge slowly below a floor. The charging standard sets the minimum at 6 A. At 230 V that is about 1.4 kW on one phase and about 4.1 kW on three. A budget below that floor cannot be used. The car either charges at the minimum, partly from the grid, or not at all. So the start threshold in the package is the floor plus a margin:

| Wallbox | Watts per ampere | 6 A floor | Start threshold in the package |
|---|---|---|---|
| Single phase | about 230 | about 1380 W | 1600 W |
| Three phase | about 690 | about 4140 W | 4400 W |

A three-phase wallbox that can switch to one phase for surplus charging is the best hardware for this. It starts at 1.4 kW instead of 4.1 kW. If yours can, put the phase switch in the start action and use the single-phase row.

The stop threshold is lower than the start threshold. Both are held for minutes rather than acted on at once. A passing cloud takes the export to zero for a minute. That is no reason to open the relay. A contactor that opens and closes every few minutes wears out and annoys the car. The package waits 3 minutes above the start threshold before starting and 5 minutes below the stop threshold before stopping. Adjust to taste, not below one minute.

## The package

Save this as a package, for example `packages/ev_surplus.yaml`. `configuration.yaml` needs `homeassistant: packages: !include_dir_named packages`. Then replace every entity id marked `# CHANGE` with your own. Developer Tools > States lists them.

```yaml
# Charge the car from PowerOcean solar surplus.
# Every entity id marked CHANGE is a placeholder for your installation.

input_number:
  ev_surplus_handover_soc:
    name: "EV surplus: battery SoC hand-over"
    min: 0
    max: 100
    step: 1
    initial: 80
    unit_of_measurement: "%"
    icon: mdi:battery-arrow-down
  ev_surplus_watts_per_amp:
    name: "EV surplus: watts per ampere"
    min: 200
    max: 720
    step: 10
    initial: 690        # 230 for a single-phase wallbox
    unit_of_measurement: W
  ev_surplus_start_w:
    name: "EV surplus: start threshold"
    min: 1000
    max: 12000
    step: 100
    initial: 4400       # 1600 for a single-phase wallbox
    unit_of_measurement: W
  ev_surplus_stop_w:
    name: "EV surplus: stop threshold"
    min: 500
    max: 12000
    step: 100
    initial: 3800       # 1200 for a single-phase wallbox
    unit_of_measurement: W
  ev_surplus_min_a:
    name: "EV surplus: minimum current"
    min: 6
    max: 16
    step: 1
    initial: 6
    unit_of_measurement: A
  ev_surplus_max_a:
    name: "EV surplus: maximum current"
    min: 6
    max: 32
    step: 1
    initial: 16
    unit_of_measurement: A

input_boolean:
  ev_surplus_enabled:
    name: EV surplus charging
    icon: mdi:car-electric

template:
  - sensor:
      - name: EV surplus budget
        unique_id: ev_surplus_budget
        unit_of_measurement: W
        device_class: power
        state_class: measurement
        state: >
          {% set export = states('sensor.ecoflow_powerocean_grid_export_power') | float(0) %}          {# CHANGE #}
          {% set batt_chg = states('sensor.ecoflow_powerocean_battery_charge_power') | float(0) %}    {# CHANGE #}
          {% set soc = states('sensor.ecoflow_powerocean_battery_soc') | float(0) %}                  {# CHANGE #}
          {% set solar = states('sensor.ecoflow_powerocean_solar_power') | float(0) %}                {# CHANGE #}
          {% set handover = states('input_number.ev_surplus_handover_soc') | float(80) %}
          {% set car = states('sensor.wallbox_charging_power') | float(0) %}                          {# CHANGE #}
          {% if solar < 300 %}
            0
          {% else %}
            {{ (export + (batt_chg if soc >= handover else 0) + car) | round(0) }}
          {% endif %}

binary_sensor:
  - platform: template
    sensors:
      ev_surplus_above_start:
        friendly_name: EV surplus above start threshold
        delay_on:
          minutes: 3
        value_template: >
          {{ states('sensor.ev_surplus_budget') | float(0)
             >= states('input_number.ev_surplus_start_w') | float(0) }}
      ev_surplus_below_stop:
        friendly_name: EV surplus below stop threshold
        delay_on:
          minutes: 5
        value_template: >
          {{ states('sensor.ev_surplus_budget') | float(0)
             < states('input_number.ev_surplus_stop_w') | float(0) }}

automation:
  - id: ev_surplus_start
    alias: "EV surplus: start charging"
    mode: single
    trigger:
      - platform: state
        entity_id: binary_sensor.ev_surplus_above_start
        to: "on"
    condition:
      - condition: state
        entity_id: input_boolean.ev_surplus_enabled
        state: "on"
      - condition: state
        entity_id: switch.wallbox_charging                       # CHANGE
        state: "off"
    action:
      - service: number.set_value
        target:
          entity_id: number.wallbox_charging_current             # CHANGE, drop if the wallbox has no current number
        data:
          value: "{{ states('input_number.ev_surplus_min_a') | int }}"
      - service: switch.turn_on
        target:
          entity_id: switch.wallbox_charging                     # CHANGE

  - id: ev_surplus_stop
    alias: "EV surplus: stop charging"
    mode: single
    trigger:
      - platform: state
        entity_id: binary_sensor.ev_surplus_below_stop
        to: "on"
      - platform: state
        entity_id: input_boolean.ev_surplus_enabled
        to: "off"
    condition:
      - condition: state
        entity_id: switch.wallbox_charging                       # CHANGE
        state: "on"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.wallbox_charging                     # CHANGE

  - id: ev_surplus_regulate
    alias: "EV surplus: follow the budget"
    mode: single
    trigger:
      - platform: time_pattern
        seconds: "/30"
    condition:
      - condition: state
        entity_id: input_boolean.ev_surplus_enabled
        state: "on"
      - condition: state
        entity_id: switch.wallbox_charging                       # CHANGE
        state: "on"
    action:
      - variables:
          budget: "{{ states('sensor.ev_surplus_budget') | float(0) }}"
          wpa: "{{ states('input_number.ev_surplus_watts_per_amp') | float(690) }}"
          min_a: "{{ states('input_number.ev_surplus_min_a') | int(6) }}"
          max_a: "{{ states('input_number.ev_surplus_max_a') | int(16) }}"
          target_a: "{{ [max_a, [min_a, (budget / wpa) | int] | max] | min }}"
      - condition: template
        value_template: >
          {{ target_a != states('number.wallbox_charging_current') | int(0) }}   {# CHANGE #}
      - service: number.set_value
        target:
          entity_id: number.wallbox_charging_current             # CHANGE
        data:
          value: "{{ target_a }}"
```

Three details in the budget sensor are there for a reason:

- **The car's own charging power is added back.** Once the car charges, the export reading drops by exactly what the car takes. A loop that only read export would then see no surplus and stop the car it just started. Adding the wallbox's own reading back makes the budget the surplus that would exist without the car. That is the quantity the loop has to compare against.
- **Solar below 300 W forces the budget to zero.** Above the hand-over SoC the budget includes the battery's charge power. A battery charged from the grid, on a schedule or after a reserve change, is not surplus. The solar gate keeps the loop honest about where the power comes from, whatever the other two readings say.
- **The regulate automation only writes when the value changes.** A `number.set_value` every 30 seconds with the same number is a cloud or local round trip for nothing, and some wallbox integrations log every write.

## Adapting to your wallbox

The package assumes a switch for charging on and off and a number for the current in amperes. Wallbox integrations differ in three ways. Each one is a local edit:

- **Current is set through a service, not a number.** Replace the two `number.set_value` calls with the service the integration documents. Drop the change-only condition in the regulate automation, or rebuild it on the integration's current sensor.
- **No current control at all, only on and off.** Delete the regulate automation and the `min_a`, `max_a` and `watts_per_amp` helpers. Set the start threshold to what the wallbox draws at its fixed current plus a margin. This is the least efficient shape: every cloud either stops the car or pulls the difference from the grid. But it works.
- **Phase switching.** If the wallbox can switch between one and three phases, switch to one phase in the start action. Switch back to three in the regulate automation once `target_a` sits at `max_a` for a while. The details depend on how the integration exposes it. A wallbox that switches phases under load needs the pause the manufacturer specifies.

The same structure carries a heat pump, a heating rod or a pool pump. Replace the wallbox switch with the device's switch, delete the current regulation, and set the start threshold to the device's draw.

## Pitfalls

- **The PowerOcean's own Solar Surplus Threshold is still in play.** It only concerns EcoFlow devices, so a third-party wallbox is not affected by it. But the app enforces `Backup Reserve <= Solar Surplus Threshold`, and this integration enforces the same. A backup reserve set high pushes the surplus threshold with it. Neither touches the loop above. They are mentioned so nobody looks for the car's behaviour in the wrong slider.
- **The battery takes the surplus back.** Below the hand-over SoC the loop does not touch the battery's share. On a day when the battery is empty in the morning, the car starts late. That is the hand-over number doing its job, not a bug. If you want the car first, lower it.
- **The delays are the protection.** Shortening `delay_on` below a minute turns a passing cloud into a relay cycle. To ride out clouds, lengthen the stop delay rather than lowering the stop threshold. A low stop threshold lets the car run on grid power for as long as the delay.
- **Entity ids are guesses until checked.** The ids in the package follow this integration's naming (`sensor.ecoflow_powerocean_...`) for a device named "EcoFlow PowerOcean". A renamed device or a second PowerOcean gives different ids. Check every one in Developer Tools > States before enabling the boolean.
- **Developer keys update every 30 seconds.** The start and stop decision is fine at that rate. The regulate automation fires every 30 seconds on a reading that is up to 30 seconds old, so the current follows clouds a step late. Account sign-in brings the readings to every 3 seconds.

## What is not in this guide

- Vehicle state of charge and a charge limit. Most car integrations expose both. A condition `car_soc < car_limit` on the start automation is the whole addition.
- Charging by tariff, or a departure-time top-up from the grid. Both are conditions on the same switch. They coexist with the surplus loop as long as only one of them may turn the wallbox on at a time.
- The PowerPulse buttons in the surplus loop. See "The PowerPulse case first": the device already does this.

If you run this on your own hardware and find a threshold that works better, or a wallbox integration that needs a different shape, open an issue with the entity list and the values. A second setup is what turns this template into a measured guide.
