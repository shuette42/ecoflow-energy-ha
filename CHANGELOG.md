# Changelog

All notable changes to this project will be documented in this file.

## [1.0.0] - 2026-03-26

### Added
- **PowerOcean** support with 57 sensors and Energy Dashboard integration (6 energy sensors)
- **Delta 2 Max** support with 58 sensors, 5 binary sensors, 3 switches, 4 number entities
- **Smart Plug** support with 9 sensors, 1 binary sensor, 1 switch
- **Standard Mode** — official IoT Developer API, HTTP polling every ~30 s
- **Enhanced Mode** — unofficial WSS MQTT push with ~3 s real-time updates (PowerOcean only)
- Auto-discovery of all devices bound to EcoFlow account
- Energy Dashboard ready sensors (`total_increasing` for solar, grid, battery, home)
- Riemann-sum energy integration with persistent state and gap/jump detection
- 4-tier MQTT reconnect strategy (auto-reconnect, force-reconnect, counter-reset, HTTP fallback)
- Three parallel MQTT keepalives for Enhanced Mode (EnergyStreamSwitch 20s, latestQuotas 30s, ping 60s)
- Portal credential authentication (Login -> JWT -> AES-CFB decrypt -> app-* MQTT credentials)
- Config Flow with device selection, mode selection, and Enhanced Mode login
- Options Flow for runtime mode switching and device management
- Optimistic lock for switch entities (5 s anti-flicker)
- Full protobuf extraction (energy_stream, EMS heartbeat, battery heartbeat, change reports)
- Diagnostics download (no credentials exposed)
- German and English translations
- 332 unit tests covering parsers, proto decoder, API client, energy integrator, manifest
