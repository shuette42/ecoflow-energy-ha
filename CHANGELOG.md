# Changelog

All notable changes to this project will be documented in this file.

## [1.1.2] - 2026-03-28

### Fixed
- Smart Plug: `watts` unit corrected from raw to deciWatt (/10) per API spec "0.1 W"
- Delta 2 Max: MPPT fields scaling corrected per API spec — `outWatts`, `carOutWatts`, `pv2InWatts` (/10), `dcdc12vWatts`, `pv2InAmp` (/100), `dcdc12vVol`, `pv2MpptTemp` (/10)

### Added
- Smart Plug: `maxCur` field parsed (deciAmpere → Ampere)
- PowerOcean: reactive power (VAr) and apparent power (VA) for all 3 grid phases

## [1.1.1] - 2026-03-28

### Fixed
- HTTP API nonce format corrected to 6-digit numeric per EcoFlow API spec (was 16-char alphanumeric, causing intermittent signature errors on some backend servers)
- MQTT keepalive reduced from 120s to 60s — prevents broker disconnect due to ~200s inactivity timeout with insufficient PINGREQ frequency

## [1.1.0] - 2026-03-27

### Added
- **Delta 2 Max MQTT push** — real-time data via IoT MQTT subscription alongside HTTP polling (dual-source)
- MQTT credential refresh on AUTH error (rc=5) with rate-limited retry

### Fixed
- HTTP API nonce collision causing `code=8521 signature is wrong` — nonce upgraded from 6-digit numeric to 16-char alphanumeric (matching IoT API client)
- HA Recorder warnings for `total_increasing` sensors (battery cycles, energy totals) — monotonic filter drops micro-regressions from API

### Changed
- Delta devices now subscribe to `/open/.../quota` MQTT topic for event-driven updates (~1–30 s)
- HTTP polling (~30 s) remains as automatic fallback when MQTT is unavailable

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
