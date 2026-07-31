# Documentation

User-facing documentation for the EcoFlow Energy integration.

For installation and quick-start, see the main [README](../README.md).

## Entity Reference

Complete list of all sensors, switches, numbers, and binary sensors per device:

- [PowerOcean](entities/powerocean.md) - 208 sensors, 2 numbers, 1 select (`HJ31`, `HJ32`, `J32D`, `J32E`, and the Plus variants `R371`, `R374`, `HJ3C`)
- [Delta 2 Max](entities/delta-2-max.md) - 94 sensors, 4 binary sensors, 7 switches, 8 numbers (`R351`, `R331`)
- [Delta 3 Max Plus](entities/delta-3-max-plus.md) - 24 sensors, 7 switches, 3 numbers (`D3M1`, `P321`)
- [Smart Plug](entities/smart-plug.md) - 11 sensors, 1 binary sensor, 1 switch, 2 numbers (`HW52`)
- [Stream](entities/stream.md) - 47 sensors, 2 binary sensors, 1 number (`BK11`, `BK31`, `BK41`, `BK51`, `BK61`)

Counts are the device-specific entity definitions. Every device additionally exposes 2 universal diagnostic sensors (connection status and active mode) that are not included above.
