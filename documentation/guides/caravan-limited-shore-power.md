# A caravan on a limited campsite hookup

This is how I run my own caravan on campsite power that is smaller than what the caravan can draw, using a Delta between the pillar and the caravan as a buffer, and a smart plug in front of the Delta as the thing that actually protects the site's fuse. It took several attempts to get right. This guide is the version that runs, plus the two ideas that did not, so you do not have to repeat them.

## What this solves

The real pain point, with children in the caravan, goes like this: the kettle goes on, somebody switches the hair dryer on next to it, and a short peak trips the pillar breaker. The reset is somewhere else. Sometimes it is at the reception, sometimes the reception is closed for the night, and the caravan stays dark until morning.

I wanted to control that cleanly. And I wanted the important things to keep running when power gets short: the fridge, the lights, the internet, the things the children need.

I chose the Delta 3 Max Plus over a plain buffer for one more reason: it also covers a stop with no hookup at all. A panel set up next to the caravan, or a short overnight stop on a long trip, and there is still light and power for the essentials with the kids.

Campsite pillars are fused, typically at 2, 4, 6, 10 or 16 amps, and the pillar does not care why you went over. Put a Delta between the pillar and the caravan and you get a buffer. The battery carries every load peak: the air conditioner starting, the coffee machine and the fridge compressor kicking in together. The pillar only ever sees the steady, limited draw you set.

There is a second effect that matters as much as the first. What the pillar delivers is the battery's charge, nothing else, and where solar can cover that charge the hookup becomes a backup you barely draw on. Where a site bills per kilowatt hour, the hookup then only ever carries what the battery takes.

## Why there is a plug in front of the Delta at all

I wanted this without an extra device. It does not work, and the reason is physics, not software: the Delta will happily let me draw 3000 W on the output while I have told it to charge at 800 W, because the charge power setting is not an input limit on what the Delta pulls from the grid.

I measured this directly. With the charge power set to 500 W and an air conditioner running as the load, the grid draw peaked at 1415.8 W. That number is the load passing straight through plus the 500 W of charging on top, at the same time, through the same cable. The setting I had made was never a ceiling on total input, it was only a target for how fast the battery charges.

The obvious next idea is to block pass through entirely, so nothing reaches the outputs except from the battery. On a Delta that setting does stop the grid draw, but it does not give you a smaller, limited grid draw, it gives you exactly zero. The reason is that on this class of device the inverter and the AC charger are the same power stage, wired one way for charging and the other way for feeding the outputs, and it can only be one of those at a time. Blocking pass through switches it to inverter mode outright. This is consistent with what EcoFlow community forums describe about this hardware, as a second source.

So the two knobs the Delta gives you, charge power and blocking pass through, are either a target speed with no ceiling, or a hard off switch with no in between. Neither one limits what a pillar actually sees on the wire.

That is why the plug sits in front of the Delta rather than being skipped. It is the only thing in the whole chain that is a remotely controlled, reversible protection layer. Without it, the only thing left that can stop an overload is the pillar's own breaker, and that one only knows how to trip once, and it is out of your reach until you or somebody else walks over and resets it.

The plug I use is a Shelly Outdoor Plug S Gen3, model `S3PL-20112EU`, rated 2500 W and 12 A. It has two limits you can configure over its own HTTP interface, a power limit and a current limit, and it trips on whichever one is reached first. One pitfall worth knowing before you buy anything bigger for a 16 A site: if you write a limit above what the plug itself supports, it accepts the write without complaint and quietly keeps its own maximum instead. I found this out by writing 3520 W and 16 A into mine and watching nothing change. Other Shelly Gen3 plugs expose the same two limits over the same RPC interface, but I have only proven this on the Outdoor Plug S Gen3 above.

## What you need

| Item | Why |
|---|---|
| A Delta with account sign-in set up in this integration | On the Delta 3 the charge power number itself only exists with account sign-in, so layer 2 needs it, and the port priority controls in layer 3 need it too. See the entity doc for details. |
| A Delta 3 Max Plus, if you want the device-side load shedding in layer 3 | The port priority controls (marking an outlet non-essential, and the battery level at which it is cut) exist only on serials starting `D3M`. A plain Delta 3, a Delta 3 Plus or a Delta 3 Max without that prefix does not have them. See the Delta 2 Max section below for what to do without any of this. |
| A Shelly Gen3 smart plug with both a power limit and a current limit (I use the Outdoor Plug S Gen3, `S3PL-20112EU`) | This is layer 1, the thing that actually protects the pillar's fuse. |
| Home Assistant reachable from wherever the caravan is parked | I run mine over Starlink. Any uplink works, the only thing that changes with a slower or higher latency connection is how quickly the software layer reacts, covered below. |

How you reach your own Home Assistant from the road is your own setup to secure.

### The switch on the back of the Delta

Next to the AC input socket on the Delta 3 Max Plus there is a physical Charge Speed Switch with two positions. ADJUST charges the power station at a custom power level that is defined in the EcoFlow app, FAST charges it at the maximum supported power level. The manual adds that the switch only takes effect when charging via the AC input socket, and the display shows an "Adjustable Charging Speed" icon while the switch sits on ADJUST. The Delta 2 Max has the same thing under the name AC Charging Speed Switch, with the positions "Maximum X-Stream charging" and "Customized charging power", and again the custom value is set in the app.

This switch must be on ADJUST, or on the customized position on the Delta 2 Max. Otherwise the charge power number that this whole guide regulates has no effect, and the Delta pulls its full charging power the moment shore power arrives. It is an easy mistake to make if you do not know the switch is there, and it is the first thing to check when the regulation appears to do nothing.

## Amps are what trips, watts are what you set

A breaker trips on current, not on power, and campsite voltage moves around more than you would expect, because of long feeder cables and however many other pitches are drawing at the same time. The same 800 W load is 3.3 A at 243 V and 3.7 A at 215 V. If you only ever think in watts you will end up with more amps than you planned for on a weak site.

| Hookup rating | Watts at 230 V | Watts at 215 V |
|---|---|---|
| 4 A | 920 W | 860 W |
| 6 A | 1380 W | 1290 W |
| 10 A | 2300 W | 2150 W |
| 16 A | 3680 W | 3440 W |

A 2 A hookup exists too, it is simply the smallest step available, and the same arithmetic applies: the budget is computed the same way, just at a smaller number.

Because the Shelly can hold both a power limit and a current limit and trips on whichever fires first, I write both, every time the hookup size changes. The power limit is computed at a fixed 220 V rather than whatever the site is actually delivering, on purpose, because that keeps the protection point stable and easy to reason about even while the real voltage sags under load.

## The three layers

### Layer 1: the Shelly as an emergency stop

The current limit and the power limit are set from whichever hookup size you have selected. This is what actually protects the pillar, and it trips before the pillar's own breaker ever sees the overload.

It is invisible to your appliances when it trips. The Delta is sitting in bypass and switches over to running everything from the battery within milliseconds, so the fridge, the cool box and the air conditioner do not restart, they just keep running from a different source. And unlike the pillar's breaker, this one can be switched back on from Home Assistant once the load has come back down.

### Layer 2: charge power regulation

The whole point of this layer is that layer 1 should almost never have to trip during normal use. It works out how much charging power is left inside the budget and writes that number to the Delta, continuously.

The formula:

```
free = budget - total_output - reserve
```

- If `free` is 200 W or more, shore power stays on and the charge power is set to `free`, rounded down to the nearest 100 W step, capped at whatever the Delta's charge power number currently allows as its maximum.
- If `free` drops below 200 W, the grid keeps carrying the load for as long as it physically can, meaning as long as load plus 200 W still fits inside the budget, with the charge setpoint held at its 200 W minimum, because the Delta has no grid-on-charging-off mode.
- If even that is not enough, the first-shed device is switched off, as the cheapest thing to give up. In my own setup that is the battery charger for the leisure battery. Point `switch.caravan_first_shed` at whatever you would rather lose first, or delete every reference to it below if you have nothing you would shed before cutting the grid entirely.
- If two minutes later the ten minute average still does not fit, shore power is switched off entirely. The average is slow to notice a 200 W charger going away, so in practice this rung mostly buys two minutes before the grid goes too, and that is what the charger is for. The battery carries the load from there, and solar keeps charging through its own MPPT input regardless of any of this.
- The grid comes back on as soon as it can carry the load again, using the same load plus 200 W test, and the first-shed device returns last, once there has been charging margin for ten minutes.

That ladder is walked one rung per two-minute tick, on the ten minute average. There is a second, faster path for a load that is already over budget on the instantaneous reading: it takes every rung at once, setpoint down, first-shed off, grid off, because a kettle on a 2 A hookup does not leave time for one rung per tick. That fast path is the one measured below.

Two things make the slow path reliable rather than jittery. First, the load figure it compares against is a rolling ten minute average of the Delta's total output, not the instantaneous reading. That single choice is what makes a cycling load, like an air conditioner compressor that pulses between roughly 900 W and 100 W, look like a steady 500 W average instead of a series of trips. Without the average, every single compressor start would look like a fresh overload.

Second, there are two separate binary sensors deciding when things are allowed to come back, not one, and they use different delays and different thresholds on purpose:

| Sensor | Condition | Delay before it fires | What it gates |
|---|---|---|---|
| grid may return | free charge power is 200 W or more | 10 minutes | Charging, including the first-shed device coming back on |
| grid can carry | ten minute average load plus 200 W fits the budget | 2 minutes | The grid connection itself coming back on |

The gap between those two conditions is exactly the reserve, 150 W by default, and it matters. If both used the same tight threshold, the first-shed device would cycle on the instant the grid returned and immediately push the load back over budget, switching everything off again a few minutes later. Keeping its return on the wider, slower condition means it only resumes once there is real headroom, not just enough to avoid an instant repeat trip.

The two-minute regulation tick sets the charge power and walks the ladder above on the ten minute average, one rung per tick. It never cuts the grid on a single instantaneous reading, that is the fast disconnect's job. The grid is switched back on by a separate recovery automation only, and that one always writes the charge power setpoint first before it turns the grid back on. Getting this order backwards was one of the mistakes I made early on, described below, and it produced a trip loop that repeated every five minutes.

### Layer 3: SoC staged load shedding

This layer exists for the case none of the above can prevent by itself: the grid is off, something is still running from the battery, the state of charge is falling, and there is not enough headroom to bring the grid back and charge at the same time. Without a plan for that, the end state is everything going dark, including your network gateway and your ability to fix any of it remotely.

| Stage | State of charge | Who acts | What happens |
|---|---|---|---|
| 1 | below the software threshold, 30 percent on my caravan | Home Assistant | The second AC outlet group is switched off, with a notification. If you switch it back on by hand afterwards, that choice is respected rather than immediately reversed. |
| 2 | below 20 percent | the device itself | The second AC outlet group is marked non essential, so the Delta sheds it on its own, without needing Home Assistant, your network or any cloud connection at all. |
| 3 | below 10 percent | the device itself | The discharge limit takes over, uncompromising, this is the device refusing to go any lower regardless of what is asking. |
| return | from 50 percent | Home Assistant | The second AC outlet group is switched back on. The gap between the shed threshold and the return threshold, twenty points with my values and never less than ten, is there so it does not flap around the edge. |

Stage 2 depends entirely on that second outlet actually being marked non essential in the device. If that marking gets flipped off, for whatever reason, stage 2 silently stops working and you only have stages 1 and 3 left. Worth checking after any firmware update or app interaction that might have touched port settings.

The first AC outlet, the one your fridge, cool box, air conditioner and anything else critical sits on, is never shed automatically by any of this. At a critical state of charge you get a notification about it and nothing more. In my own setup the things sharing that first outlet are the fridge, the cool box, the air conditioner, a Starlink dish on its own switched plug, and a battery charger also on its own switched plug. Having the dish and the charger on their own separate plugs, rather than hardwired into the same circuit as everything else, means the software layer can shed the battery charger first, as the cheapest and least noticeable thing to lose, while keeping the dish and therefore remote control alive as long as possible.

## What I measured and what I only expect

I ran a controlled test at home, on my own internet connection, at a 2 A hookup setting. A 635 W load came on at 22:10:55 and the fast disconnect automation had switched the grid off one second later, at 22:10:56, before the Shelly protection layer even had a chance to matter. That is genuinely fast. The reason is that this path reacts to every change of the instantaneous output reading and runs through the EcoFlow cloud, and on a home internet connection that round trip answers in under a second.

On the road, the same path runs over a mobile or satellite uplink instead, and I expect that same reaction to take somewhere between ten and sixty seconds rather than one. That is an expectation, not a measurement, because I have not timed it on a campsite yet.

| | at home, measured | on the road, expected |
|---|---|---|
| load step to the fast disconnect switching off the grid | about 1 second | 10 to 60 seconds |
| load step to the Shelly tripping | did not get the chance to fire | fires first |

On the road, the Shelly trip is not the rare exception it was in my home test, it is the everyday path. The software layer will usually be too slow to get ahead of a sudden load, so the plug protecting the pillar fuse is the thing that actually reacts first every time, and the return path that brings the grid back afterwards is the path you rely on daily, not a fallback for an unusual failure. The 150 W reserve exists specifically to buy back some of that latency, and how often the plug still ends up tripping despite it is the signal for whether the reserve is sized right for your own connection.

## Where Home Assistant runs

There are two ways to run this. One is Home Assistant inside the caravan, on its own. The other is what I do: Home Assistant stays at home, the caravan network is joined to the home network over a permanent VPN, and the EcoFlow integration runs centrally like everything else I have.

What does not change between the two is the Delta. It is reached through EcoFlow's cloud either way, this integration has no local connection to a Delta. What changes is the Shelly. With Home Assistant in the caravan the Shelly is on the local network and the protection layer needs no uplink at all. With Home Assistant at home, both the Delta and the Shelly depend on the caravan's uplink. In both cases the Shelly's own trip limits sit in the plug and work with no network whatsoever, which is the whole point of layer 1.

My uplink is Starlink, with a 5G router as the fallback so the VPN survives a Starlink outage. That uplink is where the latency in the previous section comes from, and the expectation stated there is the one to plan with.

## The package

Everything below is one Home Assistant package. It uses generic entity ids that match this integration's naming for a device shown as "EcoFlow Delta 3 Series", built from the device name plus the English entity name. Your own ids will carry your own device name instead if you renamed it, so check the real id from Developer Tools, States, before relying on any of this.

Layer 3 also depends on four one-time device settings that the package below does not write, it only relies on them already being set:

1. Switch `switch.ecoflow_delta_3_series_ac_2_non_essential` on.
2. Set `number.ecoflow_delta_3_series_ac_2_cutoff_level` to 20.
3. Set `number.ecoflow_delta_3_series_min_discharge_soc` to 10.
4. Leave `switch.ecoflow_delta_3_series_ac_1_non_essential` off.

These are stage 2 and stage 3 as described above, and they work on the device itself without Home Assistant once set.

```yaml
input_select:
  caravan_hookup:
    name: Campsite hookup size
    options:
      - unlimited
      - "16 A"
      - "10 A"
      - "6 A"
      - "4 A"
      - "2 A"
    icon: mdi:power-plug

input_number:
  caravan_reserve_w:
    name: Shore power reserve
    min: 0
    max: 500
    step: 10
    unit_of_measurement: "W"
  caravan_shed_soc:
    name: Software shed threshold
    min: 10
    max: 40
    step: 5
    unit_of_measurement: "%"
    # capped at 40 so the return at 50 percent below always keeps at
    # least a ten point gap. 30 is the value I run

input_boolean:
  caravan_regulation:
    name: Shore power regulation active
  caravan_grid_disconnected_by_regulation:
    name: Grid was disconnected by the regulation or by a trip
  caravan_shelly_limits_confirmed:
    name: Shelly protection limits match the current budget
  caravan_stored:
    name: Caravan in storage, plug unpowered

template:
  - sensor:
      - name: Caravan ampere limit
        unique_id: caravan_ampere_limit
        unit_of_measurement: "A"
        availability: >-
          {{ states('input_select.caravan_hookup') not in ['unknown', 'unavailable'] }}
        state: >-
          {% set sel = states('input_select.caravan_hookup') %}
          {% set chosen = 12 if sel == 'unlimited' else (sel.split(' ')[0] | int(4)) %}
          {{ [chosen, 12] | min }}
        # capped at 12 A on purpose: that is the Shelly Outdoor Plug S Gen3's
        # own rating, and a higher current limit is silently ignored by the
        # plug anyway. "unlimited" therefore means the plug's own limit, not
        # no limit

      - name: Caravan shore power budget
        unique_id: caravan_shore_power_budget
        unit_of_measurement: "W"
        availability: >-
          {{ states('sensor.caravan_ampere_limit') not in ['unknown', 'unavailable'] }}
        state: >-
          {% set watt = states('sensor.caravan_ampere_limit') | float(4) * 220 %}
          {{ [watt, 2500] | min | int }}
        # computed at a fixed 220 V, not the live site voltage, so the
        # protection point does not move around while the site sags under
        # load. unavailable while the ampere limit is, so nothing downstream
        # ever sees a made-up budget

      - name: Caravan free charge power
        unique_id: caravan_free_charge_power
        unit_of_measurement: "W"
        availability: >-
          {{ states('sensor.caravan_shore_power_budget') not in ['unknown', 'unavailable']
             and states('sensor.ecoflow_delta_3_series_output_total') not in ['unknown', 'unavailable']
             and states('input_number.caravan_reserve_w') not in ['unknown', 'unavailable'] }}
        state: >-
          {% set budget = states('sensor.caravan_shore_power_budget') | float(0) %}
          {% set last = states('sensor.ecoflow_delta_3_series_output_total') | float(99999) %}
          {% set reserve = states('input_number.caravan_reserve_w') | float(150) %}
          {% set free = budget - last - reserve %}
          {% set cap = state_attr('number.ecoflow_delta_3_series_ac_charge_power', 'max') | float(2400) %}
          {{ [[free, cap] | min, 0] | max | round(-2, 'floor') | int }}
        # fail closed on purpose: an unreadable budget defaults to 0 and an
        # unreadable output defaults to 99999, so a broken sensor never
        # produces a high charge setpoint by accident. the cap is read from
        # the number entity's own max attribute rather than hard coded,
        # because a write above the entity's real maximum is rejected
        # outright and the entity silently keeps its old value

  - binary_sensor:
      - name: Caravan grid may return
        unique_id: caravan_grid_may_return
        state: "{{ states('sensor.caravan_free_charge_power') | float(0) >= 200 }}"
        delay_on: "00:10:00"
        # the reserve limit, ten minutes on purpose: this one also gates the
        # first-shed device coming back on, and it needs the wider margin so
        # it does not immediately push the load back over budget

      - name: Caravan grid can carry
        unique_id: caravan_grid_can_carry
        availability: >-
          {{ states('sensor.caravan_shore_power_budget') not in ['unknown', 'unavailable']
             and (states('sensor.caravan_ten_minute_load') not in ['unknown', 'unavailable']
                  or states('sensor.ecoflow_delta_3_series_output_total') not in ['unknown', 'unavailable']) }}
        state: >-
          {{ (states('sensor.caravan_ten_minute_load') | float(states('sensor.ecoflow_delta_3_series_output_total') | float(99999)) + 200)
             <= states('sensor.caravan_shore_power_budget') | float(0) }}
        delay_on: "00:02:00"
        # the physical limit, two minutes on purpose: this is the one that
        # brings the grid connection itself back. it falls back to the
        # instantaneous output reading while the rolling average has no data
        # yet, right after a restart, so the return path is never dead for
        # the full ten minutes just because Home Assistant restarted. both
        # load fallbacks read as 99999 W when unavailable, never 0, so a
        # dropped sensor can never look like room to reconnect

      - name: Caravan SoC critical
        unique_id: caravan_soc_critical
        state: >-
          {{ states('sensor.ecoflow_delta_3_series_soc') | float(100)
             < (states('input_number.caravan_shed_soc') | float(30)) }}
        delay_on: "00:02:00"
        delay_off: "00:05:00"

sensor:
  - platform: filter
    name: Caravan ten minute load
    entity_id: sensor.ecoflow_delta_3_series_output_total
    filters:
      - filter: time_simple_moving_average
        window_size: "00:10:00"
    # ten-minute simple moving average, filter platform

rest_command:
  caravan_shelly_set_limits:
    url: "http://shelly-caravan.local/rpc/Switch.SetConfig"
    method: POST
    content_type: "application/json"
    payload: >-
      {"id":0,"config":{"power_limit":{{ power_limit | int }},"current_limit":{{ current_limit | float }}}}

  caravan_shelly_get_limits:
    url: "http://shelly-caravan.local/rpc/Switch.GetConfig?id=0"
    method: GET

automation:
  - alias: Caravan - write shore power limits
    id: caravan_write_shore_power_limits
    mode: restart
    trigger:
      - platform: state
        entity_id: sensor.caravan_shore_power_budget
        id: budget_changed
      - platform: time_pattern
        minutes: "/15"
        id: watchdog_tick
    condition:
      - condition: template
        value_template: >-
          {{ states('sensor.caravan_shore_power_budget') not in ['unknown', 'unavailable'] }}
      - condition: or
        conditions:
          - condition: trigger
            id: budget_changed
          - condition: and
            conditions:
              - condition: trigger
                id: watchdog_tick
              - condition: state
                entity_id: input_boolean.caravan_shelly_limits_confirmed
                state: "off"
              - condition: state
                entity_id: input_boolean.caravan_stored
                state: "off"
    action:
      - service: input_boolean.turn_off
        target:
          entity_id: input_boolean.caravan_shelly_limits_confirmed
      - repeat:
          until:
            - condition: template
              value_template: >-
                {{ repeat.index >= 3
                   or (caravan_shelly_readback is defined
                       and (caravan_shelly_readback.content.power_limit | float(-1))
                           == (states('sensor.caravan_shore_power_budget') | float(-2))
                       and (caravan_shelly_readback.content.current_limit | float(-1))
                           == (states('sensor.caravan_ampere_limit') | float(-2))) }}
          sequence:
            - service: rest_command.caravan_shelly_set_limits
              continue_on_error: true
              data:
                power_limit: "{{ states('sensor.caravan_shore_power_budget') }}"
                current_limit: "{{ states('sensor.caravan_ampere_limit') }}"
            - service: rest_command.caravan_shelly_get_limits
              continue_on_error: true
              response_variable: caravan_shelly_readback
      - service: rest_command.caravan_shelly_get_limits
        continue_on_error: true
        response_variable: caravan_shelly_final
      - if:
          - condition: template
            value_template: >-
              {{ caravan_shelly_final is defined
                 and (caravan_shelly_final.content.power_limit | float(-1))
                     == (states('sensor.caravan_shore_power_budget') | float(-2))
                 and (caravan_shelly_final.content.current_limit | float(-1))
                     == (states('sensor.caravan_ampere_limit') | float(-2)) }}
        then:
          - service: input_boolean.turn_on
            target:
              entity_id: input_boolean.caravan_shelly_limits_confirmed
        else:
          - service: notify.notify
            data:
              message: >-
                Caravan shore power limits could not be confirmed after 3 attempts.
                Target {{ states('sensor.caravan_shore_power_budget') }} W /
                {{ states('sensor.caravan_ampere_limit') }} A.
    # Switch.GetConfig answers with the config object itself, not wrapped
    # under a further "config" key, so the read-back path is
    # content.power_limit and content.current_limit. the loop stops as soon
    # as the read-back matches the target, or after three attempts. the
    # check after the loop does its own read-back into its own variable, so
    # nothing depends on how far a variable set inside the loop reaches. the
    # confirmed helper is cleared before every write and only set once that
    # final comparison holds, so the fifteen minute watchdog re-runs the loop
    # only while the plug still disagrees with the budget, and not at all
    # while the caravan is marked as stored: an unpowered plug cannot answer,
    # and a notification every quarter hour would tell you nothing new.
    # "unlimited" still writes 2500 W / 12 A, the plug's own rating

  - alias: Caravan - charge power regulation
    id: caravan_charge_power_regulation
    mode: single
    trigger:
      - platform: time_pattern
        minutes: "/2"
    condition:
      - condition: state
        entity_id: input_boolean.caravan_regulation
        state: "on"
    action:
      - choose:
          - conditions:
              - condition: state
                entity_id: binary_sensor.caravan_grid_may_return
                state: "on"
            sequence:
              - if:
                  - condition: template
                    value_template: >-
                      {{ (states('sensor.caravan_free_charge_power') | int(-1))
                         != (states('number.ecoflow_delta_3_series_ac_charge_power') | int(-2)) }}
                then:
                  - service: number.set_value
                    continue_on_error: true
                    target:
                      entity_id: number.ecoflow_delta_3_series_ac_charge_power
                    data:
                      value: "{{ states('sensor.caravan_free_charge_power') }}"
        default:
          - if:
              - condition: template
                value_template: >-
                  {{ (states('number.ecoflow_delta_3_series_ac_charge_power') | int(-1)) != 200 }}
            then:
              - service: number.set_value
                continue_on_error: true
                target:
                  entity_id: number.ecoflow_delta_3_series_ac_charge_power
                data:
                  value: 200
          - if:
              - condition: state
                entity_id: binary_sensor.caravan_grid_can_carry
                state: "off"
              - condition: state
                entity_id: switch.caravan_shore_power
                state: "on"
            then:
              - choose:
                  - conditions:
                      - condition: state
                        entity_id: switch.caravan_first_shed
                        state: "on"
                    sequence:
                      - service: switch.turn_off
                        target:
                          entity_id: switch.caravan_first_shed
                default:
                  - service: input_boolean.turn_on
                    target:
                      entity_id: input_boolean.caravan_grid_disconnected_by_regulation
                  - service: switch.turn_off
                    target:
                      entity_id: switch.caravan_shore_power
    # this tick raises or holds the charge setpoint and walks the ladder one
    # rung per tick: setpoint down to 200 W first, then the first-shed
    # device, and only if the ten minute mean still does not fit two minutes
    # later, the grid. it never disconnects on a single instantaneous
    # reading, that is the fast disconnect below. resuming charging needs
    # the ten minute confirmed margin (grid may return), so it does not
    # flicker on a single good reading. both branches only write the number
    # when the target differs from what the Delta currently reports, so a
    # tick that changes nothing writes nothing to the cloud. every cloud
    # write carries continue_on_error, because on the road the cloud round
    # trip is the thing most likely to time out, and a timeout must never
    # stop the local switch-off that follows it

  - alias: Caravan - fast disconnect on load spike
    id: caravan_fast_disconnect
    trigger:
      - platform: state
        entity_id: sensor.ecoflow_delta_3_series_output_total
    condition:
      - condition: state
        entity_id: input_boolean.caravan_regulation
        state: "on"
      - condition: state
        entity_id: switch.caravan_shore_power
        state: "on"
      - condition: template
        value_template: >-
          {{ states('sensor.caravan_shore_power_budget') not in ['unknown', 'unavailable'] }}
      - condition: template
        value_template: >-
          {% set load = states('sensor.ecoflow_delta_3_series_output_total') | float(99999) %}
          {{ (load + 200) > (states('sensor.caravan_shore_power_budget') | float(0)) }}
    action:
      - service: input_boolean.turn_on
        target:
          entity_id: input_boolean.caravan_grid_disconnected_by_regulation
      - service: number.set_value
        continue_on_error: true
        target:
          entity_id: number.ecoflow_delta_3_series_ac_charge_power
        data:
          value: 200
      - service: switch.turn_off
        target:
          entity_id: switch.caravan_first_shed
      - service: switch.turn_off
        target:
          entity_id: switch.caravan_shore_power
    # this is the fast path, the one behind the 22:10:55 to 22:10:56
    # measurement below: it reacts to every change of the instantaneous
    # reading and takes the whole ladder in one go, because a load that is
    # already over budget has no time for a rung per tick. the flag is set
    # first, so a cloud timeout on the setpoint write cannot leave the
    # recovery path unarmed, and the write carries continue_on_error so the
    # local switch-off still runs. fail closed on purpose: an unavailable
    # reading is treated as 99999 W, always over budget, so a dropped sensor
    # disconnects rather than leaving the load unmanaged. an unavailable
    # budget is different: that is the selector not restored yet during a
    # Home Assistant start, the plug's own limits are still in force, and
    # tripping the grid on every start would be a trip for nothing. the ten
    # minute mean stays reserved for the return path only

  - alias: Caravan - grid recovery
    id: caravan_grid_recovery
    mode: single
    trigger:
      - platform: state
        entity_id: binary_sensor.caravan_grid_can_carry
        to: "on"
      - platform: state
        entity_id: binary_sensor.caravan_grid_may_return
        to: "on"
      - platform: time_pattern
        minutes: "/2"
    condition:
      - condition: state
        entity_id: binary_sensor.caravan_grid_can_carry
        state: "on"
      - condition: state
        entity_id: binary_sensor.caravan_grid_may_return
        state: "on"
      - condition: state
        entity_id: input_boolean.caravan_grid_disconnected_by_regulation
        state: "on"
      - condition: state
        entity_id: switch.caravan_shore_power
        state: "off"
      - condition: template
        value_template: >-
          {{ (now() - states.input_boolean.caravan_grid_disconnected_by_regulation.last_changed).total_seconds() >= 120 }}
    action:
      - service: number.set_value
        target:
          entity_id: number.ecoflow_delta_3_series_ac_charge_power
        data:
          value: 200
      - service: switch.turn_on
        target:
          entity_id: switch.caravan_shore_power
      - service: input_boolean.turn_off
        target:
          entity_id: input_boolean.caravan_grid_disconnected_by_regulation
    # the grid only ever comes back here, gated on both hysteresis sensors
    # together: grid can carry (the load fits) and grid may return (there is
    # margin to charge again), plus the flag that says the regulation or a
    # trip put it down in the first place, plus two minutes since that flag
    # was set. the two minute tick is there for the trip that happens on a
    # spike the cloud-side reading never showed: both sensors may already be
    # on, so no edge would ever fire, and without the tick the grid would
    # stay off. the setpoint write here deliberately has no
    # continue_on_error: if the 200 W cannot be confirmed, the grid stays
    # off until the next tick, because a grid that comes back under an old
    # high setpoint is the trip loop from the pitfalls table. the first-shed
    # device comes back separately, below, on the wider margin

  - alias: Caravan - first-shed device returns
    id: caravan_first_shed_returns
    trigger:
      - platform: state
        entity_id: binary_sensor.caravan_grid_may_return
        to: "on"
      - platform: state
        entity_id: switch.caravan_shore_power
        to: "on"
      - platform: time_pattern
        minutes: "/2"
    condition:
      - condition: state
        entity_id: binary_sensor.caravan_grid_may_return
        state: "on"
      - condition: state
        entity_id: switch.caravan_shore_power
        state: "on"
      - condition: state
        entity_id: switch.caravan_first_shed
        state: "off"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.caravan_first_shed
    # only once the grid is back and there has been charging margin for ten
    # minutes, so the cheapest load to lose is also the last to return. the
    # tick is there because the slow ladder can shed this device without
    # the grid ever going off, and then neither edge would arrive. a manual
    # off is undone the same way, see the notes below the package

  - alias: Caravan - shore power tripped
    id: caravan_shore_power_tripped
    trigger:
      - platform: state
        entity_id: switch.caravan_shore_power
        to: "off"
    action:
      - service: input_boolean.turn_on
        target:
          entity_id: input_boolean.caravan_grid_disconnected_by_regulation
    # any trip sets this flag, no matter who or what switched the plug off,
    # otherwise the recovery automation above never runs after a protection
    # trip, and the grid only ever comes back by hand. that includes a
    # manual off you trigger yourself from Home Assistant: it is treated as
    # a trip and will be reversed once the grid can carry the load again.
    # turn input_boolean.caravan_regulation off first if you want the grid
    # to stay off

  - alias: Caravan - shed AC outlet 2 at the software threshold
    id: caravan_shed_ac2
    trigger:
      - platform: state
        entity_id: binary_sensor.caravan_soc_critical
        to: "on"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.ecoflow_delta_3_series_ac_output_2
      - service: notify.notify
        data:
          message: >-
            State of charge below {{ states('input_number.caravan_shed_soc') }} percent,
            second AC outlet shed.

  - alias: Caravan - restore AC outlet 2 at 50 percent
    id: caravan_restore_ac2_50
    trigger:
      - platform: numeric_state
        entity_id: sensor.ecoflow_delta_3_series_soc
        above: 50
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.ecoflow_delta_3_series_ac_output_2
```

A few notes on the block above that are easy to miss on a quick read:

- None of the helpers carry a stored default value, on purpose, see the pitfalls table below for why.
- `switch.caravan_shore_power` is whatever entity your Shelly integration gives you for the plug's relay, not something this package defines. Point every reference above at your own.
- `switch.caravan_first_shed` is the one thing this package sheds before it ever cuts the grid entirely. Point it at whatever you would rather lose first, on my caravan that is the leisure battery charger. Delete every reference to it if you have nothing you would shed before a full disconnect. Like the shore switch, it is brought back by the package whenever the margin allows, so switch `input_boolean.caravan_regulation` off first if you want it to stay off.
- The block above passes Home Assistant's configuration check as printed. It still needs your own entity ids in place of the placeholders before it does anything useful.
- `input_boolean.caravan_stored` silences the fifteen minute watchdog while the caravan is in storage with no shore power, where the plug is unpowered and cannot answer. Switch it on before storage and off when you plug in again. A hookup change still writes the plug either way, and `unlimited` still writes the plug's own rating, 2500 W and 12 A.
- The two solar sensors, `sensor.ecoflow_delta_3_series_solar_input_1` and `sensor.ecoflow_delta_3_series_solar_input_2`, are not wired into any automation above. Put them on a dashboard next to everything else, see the solar section below for why they do not need to be.
- `binary_sensor.ecoflow_delta_3_series_port_priority_active` and `sensor.ecoflow_delta_3_series_ac_charge_power_limit` are not consumed by this package either. The first exists on `D3M` serials with account sign-in only and is a fast, device reported signal that the mains input has dropped, useful as a diagnostic or a faster notification trigger than waiting on the Shelly. The second is the read back of what the Delta itself thinks its charge power setpoint currently is, worth a dashboard tile so you can see at a glance whether the regulation automation and the device agree.

## With a Delta 2 Max

My first unit in this role was a Delta 2 Max, and layer 2 maps onto it one to one. The entity ids simply follow the Delta 2 Max's own naming: `sensor.ecoflow_delta_2_max_output_total`, `number.ecoflow_delta_2_max_ac_charge_speed` in place of the charge power number, and `number.ecoflow_delta_2_max_min_discharge_soc` for stage 3. The charge speed number has the same 200 to 2400 W range in 100 W steps as the Delta 3's charge power number, so the free charge power template above works without changing a single number in it.

I ran exactly this setup on the Delta 2 Max first. What the Delta 3 Max Plus bought me is the two switchable AC output groups, AC1 and AC2, each with its own button on the unit and its own switch in this integration, so the awning circuit can be shed without touching the caravan circuit. On the Delta 2 Max I did the same with additional Shelly plugs on the appliance side, one per circuit I wanted to shed separately.

What the Delta 2 Max does not have is layer 3 as described above, and here is what that means:

| Missing | Consequence |
|---|---|
| No split between a first and second AC outlet, only one AC output switch, `switch.ecoflow_delta_2_max_ac_output` | You cannot shed one outlet while keeping the other running. Software shedding has to switch external plugs on the appliance side instead, the way I switch my Starlink dish and my battery charger, rather than shedding a second Delta outlet. |
| No non essential marking and no cutoff level on any outlet | Stage 2, the device shedding something on its own without Home Assistant, does not exist on this device. |
| No equivalent to Bypass Output Disabled | There is no way to command a hard zero grid draw from Home Assistant on a Delta 2 Max. The Shelly stays the only disconnect. |

What it has instead is a Backup Reserve switch and a Backup Reserve Level number, 5 to 100 percent in 5 point steps. I am naming these because they exist and are worth knowing about, not because I can tell you what they actually do on the wire. I have not run that mode myself and cannot describe its behaviour honestly, so if you build on it, treat it as something to test on your own hardware first.

## Adding portable solar

The two solar input sensors show what the panels are doing, and nothing about them needs to change when you add solar to this setup. The MPPT input is its own separate DC path into the battery, entirely independent from the AC side the rest of this guide is about. It keeps charging through both states the grid side can be in, whether the Delta is passing shore power straight through or has switched over to running everything from the battery.

That independence is exactly what makes solar useful here without touching a single automation above. When the grid is cut, either because the regulation decided to or because the Shelly tripped, the battery is not simply draining until the grid comes back, the panels are still feeding it in the background the whole time. And when solar is strong enough to cover the load on its own, the arithmetic in the free charge power template does the rest automatically: solar shows up as a lower net draw on the battery, which shows up as more headroom in the budget, which shows up as the regulation asking the pillar for less.

Two things from the manual are worth knowing before you buy panels. The Delta 3 Max Plus's charging priority lists solar via the XT60 port above AC via the AC input port. So on a sunny pitch the panels take the first share of the charge and the hookup only tops up. The solar input takes 11 to 60 V at 18 A maximum, up to 1000 W over two ports of 500 W each on the EU model.

I am not going to give you a yield number or a panel count here, because neither is something I have measured for this guide, only that the two sensors above will show you your own numbers once you have panels connected.

## Pitfalls

The first six I hit myself. The last two I have not: the switch is the mistake I would expect most often from somebody who does not know it is there, and the storage case is one the package is built around before it can happen.

| What happened | What I changed |
|---|---|
| Every Home Assistant restart reset the hookup selector to unlimited, switched the regulation off, and wrote the plug's own maximum limits back onto it | Removed the stored default from every helper above. A helper without a stored state should come back as it was, not silently reset to a value that undoes the whole point of the setup. |
| A chain of templates that all defaulted upward on a missing reading, the budget jumped straight to the plug's own maximum on one reload and put a large charge setpoint onto a small hookup | Every template above defaults toward zero or toward the safest reading, never upward, and the free charge power sensor has an explicit `availability:` so it goes unavailable rather than guessing when an input is missing. |
| On reconnect, the old high charge setpoint sat on the Delta for about ten seconds before the lower one took over, which was enough to trip the plug again, and that repeated every five minutes | The setpoint is always written before the grid switch, on every branch, not after. |
| The grid stayed off after a trip until the battery hit the emergency shedding threshold, because nothing had told the recovery automation that a trip had even happened | Any change of the shore power switch to off, from any cause, sets the same flag the regulation itself uses, so the recovery path runs after a Shelly trip exactly as it would after the regulation's own decision to disconnect. |
| A write to the plug that appeared to succeed had in fact not changed anything, once, right after a hookup change, four milliseconds before the sensor chain it depended on had actually finished recomputing | The write now reads the plug's configuration back after every attempt and only stops once the two agree, rather than trusting that a request without an error also took effect. |
| Arriving at a new site with the selector still on unlimited, the plug boots from its own stored configuration before Home Assistant is even reachable, and a pillar can trip in that gap | Set the hookup size for the next site before unplugging from the current one, while there is still power to confirm the write went through. On arrival, change it again and wait for confirmation while power is present, rather than trusting whatever the plug happened to boot with. |
| The Charge Speed Switch on the Delta is on FAST. The setpoint changes in Home Assistant, the Delta ignores it and charges flat out | Set the switch to ADJUST. The charge power number only takes effect in that position, see the section on the switch above. |
| The caravan is stored with no shore power at all, the Shelly is unpowered by design, and the fifteen minute watchdog would push a failed-write notification every quarter hour | `input_boolean.caravan_stored` silences the watchdog. Switch it on before storage and there is nothing left to fail, and the hookup selector keeps its meaning. |

## What is not in this guide

No price or saving figures, because I have not measured either. No solar yield numbers or panel counts, for the same reason. The solar input limits above are the manual's figures, not a measurement of mine. The ten to sixty second latency figure for a mobile or satellite connection is an expectation based on how the regulation works, not something I have timed on an actual campsite yet. And the exact behaviour of Backup Reserve on the Delta 2 Max is named above but not described, because I have not tested it myself.
