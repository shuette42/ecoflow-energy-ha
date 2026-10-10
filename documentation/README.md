# Documentation

User-facing documentation for the EcoFlow Energy integration.

For installation and quick-start, see the main [README](../README.md).

## Entity Reference

Complete list of all sensors, switches, numbers, and binary sensors per device:

- [PowerOcean](entities/powerocean.md) - 249 sensors, 21 binary sensors, 18 numbers, 16 switches, 1 select (`HJ31`, `HJ32`, `HJ35`, `HJ36`, `HJ37`, `J32B`, `J327`, `J329`, `J32D`, `J32E`, and the Plus variants `R371`, `R372`, `R374`, `HJ3C`) on an account entry. A Local (Modbus/TCP) entry for a three-phase PowerOcean has 28 sensors, 4 binary sensors, 2 numbers and 1 switch
- [Delta 2 Max](entities/delta-2-max.md) - 94 sensors, 4 binary sensors, 7 switches, 8 numbers (`R351`, `R331`)
- [Delta 3 Max Plus](entities/delta-3-max-plus.md) - 47 sensors, 7 switches, 4 numbers, 5 selects (`D3M1`, `D3N1`, `P321`, `P231`, `P351`), plus 3 switches, 3 numbers and 1 binary sensor for port priority on `D3M` serials
- [Smart Plug](entities/smart-plug.md) - 11 sensors, 1 binary sensor, 1 switch, 2 numbers (`HW52`)
- [Stream](entities/stream.md) - 59 sensors, 2 binary sensors, 1 number (`BK11`, `BK31`, `BK41`, `BK51`, `BK61`); `BK31` adds 3 numbers and 2 switches
- [Stream AC Pro outlet notes](entities/stream-ac-pro.md) - Enhanced Mode outlet controls and telemetry (`BK31`)
- [Stream Micro](entities/stream.md#stream-micro-bk01) - 25 sensors (`BK01`) - a grid-tie inverter without a battery, so it gets a reduced version of the Stream entity set
- [PowerStream](entities/powerstream.md) - 25 sensors (`HW51`) - a microinverter, read-only, Standard Mode only
- [STREAM AC 5000](entities/stream-ac-5000.md) - 57 sensors, 2 binary sensors, 2 switches, 7 numbers, 1 select (`ES22` and `ES21`) - shares the Stream name but not the Stream protocol, so it has its own parser and entity set
- [Smart Meter](entities/smart-meter.md) - 18 sensors, 3 binary sensors (`BK21`) - a grid meter, read-only, Enhanced Mode only
- [Solar Tracker](entities/solar-tracker.md) - 6 sensors (`HZ31` and `S02F`) - one product under two serial prefixes, read-only in this release, Enhanced Mode only
- [WAVE 3](entities/wave-3.md) - 18 sensors, 5 binary sensors, 4 switches, 5 numbers, 5 selects, 1 climate (`AC71`) - a portable air conditioner, Enhanced Mode only
- [PowerPulse 2](entities/powerpulse-2.md) - 18 sensors, 1 binary sensor, 4 numbers, 2 selects, 3 switches, 2 buttons (`C376`, `C374`) - a wallbox, no PowerOcean required for the readings. Start and stop work with no PowerOcean or exactly one in the same integration entry. With exactly one PowerOcean, the maximum current, the solar minimum and custom charging currents, the charging mode, the phase setting, Continuous Charging, Block Battery Discharge and Plug-and-Play can be set. With none, the charging current can. Enhanced Mode only
- [PowerPulse 2](entities/powerpulse-2.md) - 18 sensors, 1 binary sensor (`C371`) - the same readings as the `C376` and `C374`, read-only for now, Enhanced Mode only
- [Ocean 2](entities/ocean-2.md) - 48 sensors plus 12 per battery module (`RE11`, `RE17`, `RE41`, `RE42`) - a home battery, read-only, Enhanced Mode only
- [OCEAN Smart Electrical Panel 40](entities/smart-panel-40.md) - 19 sensors plus 3 sensors and 1 binary sensor per circuit (`HR61`) - a US split-phase load panel, read-only, Enhanced Mode only
- [DELTA Pro Ultra](entities/delta-pro-ultra.md) - 21 sensors plus 2 per battery pack (`Y711`) - a home backup power station, read-only, Enhanced Mode only
- [Smart Home Panel 2](entities/smart-home-panel-2.md) - 9 sensors plus 2 per circuit and 1 per storage channel (`HD31`) - a US split-phase load panel with battery backup, read-only, Enhanced Mode only
- [RIVER 3](entities/delta-3-max-plus.md) - 31 sensors and 1 binary sensor (`R655`) - a portable power station that reports through the Delta 3 messages. It gets the Delta 3 sensors its recorded frames confirm: battery level, input, output, AC input, USB-C and USB-A power, AC input and output energy, charge state, remaining times and the battery readings, plus whether the AC output is on. Read-only, Enhanced Mode only, with no switches, numbers or selects
- [RIVER 3 Plus](entities/delta-3-max-plus.md) - 31 sensors and 1 binary sensor (`R631`) - sends the same Delta 3 messages as the RIVER 3 and gets the same sensors, plus whether the AC output is on. Read-only, Enhanced Mode only, with no switches, numbers or selects

Counts are the device-specific entity definitions. Every account entry additionally exposes 2 universal diagnostic sensors (connection status and active mode) that are not included above. A Local entry exposes only the active mode sensor.

## Architecture

- [Architecture decisions](architecture/decisions.md) - the decision register (`ADR-NNN`) that code comments and tests cite: what was decided, why, what was rejected, and what it changed in the code. Start with [architecture/README.md](architecture/README.md) for how to read it.

## Guides

- [Moving from another EcoFlow integration](migration-from-another-integration.md) - what carries over, what has to be re-pointed by hand, and an order of steps that keeps the old entities running until the new ones are confirmed
- [Adding your device](add-your-device.md) - every file a new device touches, with the WAVE 3 as the worked example, and the four things a contributor cannot guess: the capture, the scaling, the read-back, the mode
- [A caravan on a limited campsite hookup](guides/caravan-limited-shore-power.md) - buffering a small pillar fuse with a Delta and a smart plug, the charge power regulation that keeps the plug from tripping, and the load shedding that keeps the lights on when it does anyway
- [Charging an electric car from solar surplus](guides/pv-surplus-ev-charging.md) - a package that hands PowerOcean surplus to a third-party wallbox, with the one number that decides between the car and the home battery, why the thresholds sit above the 6 A floor, and why a PowerPulse owner does not need any of it
- [Connecting a PowerOcean locally over Modbus](guides/powerocean-local-modbus.md) - the Local connection for a three-phase PowerOcean: what EcoFlow support has to enable, what it shows and leaves out, the Modbus Control switch and the two numbers it can set, why three energy sensors are separate, and how to switch with Reconfigure
