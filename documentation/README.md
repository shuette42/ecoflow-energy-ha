# Documentation

User-facing documentation for the EcoFlow Energy integration.

For installation and quick-start, see the main [README](../README.md).

## Entity Reference

Complete list of all sensors, switches, numbers, and binary sensors per device:

- [PowerOcean](entities/powerocean.md) - 222 sensors, 5 binary sensors, 2 numbers, 1 select (`HJ31`, `HJ32`, `HJ35`, `J32B`, `J327`, `J32D`, `J32E`, and the Plus variants `R371`, `R372`, `R374`, `HJ3C`)
- [Delta 2 Max](entities/delta-2-max.md) - 94 sensors, 4 binary sensors, 7 switches, 8 numbers (`R351`, `R331`)
- [Delta 3 Max Plus](entities/delta-3-max-plus.md) - 47 sensors, 7 switches, 4 numbers, 5 selects (`D3M1`, `D3N1`, `P321`, `P231`), plus 3 switches, 3 numbers and 1 binary sensor for port priority on `D3M` serials
- [Smart Plug](entities/smart-plug.md) - 11 sensors, 1 binary sensor, 1 switch, 2 numbers (`HW52`)
- [Stream](entities/stream.md) - 54 sensors, 2 binary sensors, 1 number (`BK11`, `BK31`, `BK41`, `BK51`, `BK61`); `BK31` adds 2 numbers
- [Stream Micro](entities/stream.md#stream-micro-bk01) - 21 sensors (`BK01`) - a grid-tie inverter without a battery, so it gets a reduced version of the Stream entity set
- [STREAM AC 5000](entities/stream-ac-5000.md) - 55 sensors, 2 binary sensors, 2 switches, 5 numbers, 1 select (`ES22`); the STREAM 5000 (`ES21`) shares the sensors and gets no controls yet - shares the Stream name but not the Stream protocol, so it has its own parser and entity set

Counts are the device-specific entity definitions. Every device additionally exposes 2 universal diagnostic sensors (connection status and active mode) that are not included above.
