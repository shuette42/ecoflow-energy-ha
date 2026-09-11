# Documentation

User-facing documentation for the EcoFlow Energy integration.

For installation and quick-start, see the main [README](../README.md).

## Entity Reference

Complete list of all sensors, switches, numbers, and binary sensors per device:

- [PowerOcean](entities/powerocean.md) - 243 sensors, 21 binary sensors, 18 numbers, 16 switches, 1 select (`HJ31`, `HJ32`, `HJ35`, `HJ36`, `HJ37`, `J32B`, `J327`, `J329`, `J32D`, `J32E`, and the Plus variants `R371`, `R372`, `R374`, `HJ3C`)
- [Delta 2 Max](entities/delta-2-max.md) - 94 sensors, 4 binary sensors, 7 switches, 8 numbers (`R351`, `R331`)
- [Delta 3 Max Plus](entities/delta-3-max-plus.md) - 47 sensors, 7 switches, 4 numbers, 5 selects (`D3M1`, `D3N1`, `P321`, `P231`), plus 3 switches, 3 numbers and 1 binary sensor for port priority on `D3M` serials
- [Smart Plug](entities/smart-plug.md) - 11 sensors, 1 binary sensor, 1 switch, 2 numbers (`HW52`)
- [Stream](entities/stream.md) - 55 sensors, 2 binary sensors, 1 number (`BK11`, `BK31`, `BK41`, `BK51`, `BK61`); `BK31` adds 3 numbers and 2 switches
- [Stream AC Pro outlet notes](entities/stream-ac-pro.md) - Enhanced Mode outlet controls and telemetry (`BK31`)
- [Stream Micro](entities/stream.md#stream-micro-bk01) - 21 sensors (`BK01`) - a grid-tie inverter without a battery, so it gets a reduced version of the Stream entity set
- [PowerStream](entities/powerstream.md) - 25 sensors (`HW51`) - a microinverter, read-only, Standard Mode only
- [STREAM AC 5000](entities/stream-ac-5000.md) - 56 sensors, 2 binary sensors, 2 switches, 7 numbers, 1 select (`ES22` and `ES21`) - shares the Stream name but not the Stream protocol, so it has its own parser and entity set
- [Smart Meter](entities/smart-meter.md) - 18 sensors, 3 binary sensors (`BK21`) - a grid meter, read-only, Enhanced Mode only
- [Solar Tracker](entities/solar-tracker.md) - 6 sensors (`HZ31` and `S02F`) - one product under two serial prefixes, read-only in this release, Enhanced Mode only
- [WAVE 3](entities/wave-3.md) - 18 sensors, 5 binary sensors, 4 switches, 5 numbers, 5 selects, 1 climate (`AC71`) - a portable air conditioner, Enhanced Mode only
- [PowerPulse 2](entities/powerpulse-2.md) - 17 sensors, 1 binary sensor, 2 buttons (`C376`, `C374`) - a wallbox; start and stop with one PowerOcean in the same integration entry, Enhanced Mode only, no PowerOcean required for the readings

Counts are the device-specific entity definitions. Every device additionally exposes 2 universal diagnostic sensors (connection status and active mode) that are not included above.

## Architecture

- [Architecture decisions](architecture/decisions.md) - the decision register (`ADR-NNN`) that code comments and tests cite: what was decided, why, what was rejected, and what it changed in the code. Start with [architecture/README.md](architecture/README.md) for how to read it.

## Guides

- [Moving from another EcoFlow integration](migration-from-another-integration.md) - what carries over, what has to be re-pointed by hand, and an order of steps that keeps the old entities running until the new ones are confirmed
- [Adding your device](add-your-device.md) - every file a new device touches, with the WAVE 3 as the worked example, and the four things a contributor cannot guess: the capture, the scaling, the read-back, the mode
