# Changelog

All notable changes to this project will be documented in this file.

## [1.3.0] - 2026-03-29

### Added
- Re-authentication flow for expired API credentials (Standard and Enhanced Mode)
- Automatic re-auth trigger after 5 consecutive HTTP failures, MQTT credential refresh failure, or Enhanced login failure

### Changed
- Modernized OptionsFlow to use current Home Assistant pattern
- Modernize type hints: `Optional[X]` → `X | None`, `Dict`/`List`/`Tuple` → builtins across all source files
- Centralize `_safe_float()` into shared parser module — removes 3 duplicate definitions
- Add missing return type hints to MQTT client and proto decoder methods
- Replace bare `except Exception` with specific exception types in proto decoder and runtime
- Unify parser return types to `dict[str, Any]` for consistency

### Fixed
- Downgrade MQTT auth error (rc=5) log from ERROR to WARNING — auto-recovery follows
- Downgrade transient MQTT message handler and connection errors to appropriate log levels
- Remove unused typing imports

## [1.2.8] - 2026-03-29

### Changed
- Reduce log noise: downgrade ~22 operational info messages to debug level across MQTT, auth, coordinator, and API modules
- Add startup summary log with device count and mode breakdown (Enhanced/Standard)

## [1.2.7] - 2026-03-28

### Fixed
- Remove license badge from README — renders as "?" in HACS due to image proxy limitations

## [1.2.6] - 2026-03-28

### Fixed
- Revert homeassistant field in manifest — not allowed for custom integrations (hassfest rejects it)

## [1.2.4] - 2026-03-28

### Changed
- Updated hero screenshots with higher quality images

## [1.2.3] - 2026-03-28

### Fixed
- License badge shows static "MIT" instead of dynamic query that rendered as "?" in HACS

## [1.2.2] - 2026-03-28

### Fixed
- README uses pure markdown only — no HTML tables or emoji shortcodes that HACS cannot render

## [1.2.1] - 2026-03-28

### Changed
- README redesigned for HACS store rendering — hero screenshots, feature grid, compact structure, standard markdown for full compatibility

## [1.2.0] - 2026-03-28

### Added
- Energy Dashboard support for Delta 2 Max — 4 kWh sensors (solar, solar 2, AC input, AC output) via Riemann sum integration
- Energy Dashboard support for Smart Plug — 1 kWh energy sensor via Riemann sum integration
- Entity translations for all 135 entities (English + German) using HA translation_key system
- Firmware version display in HA device page (extracted from API response)

### Changed
- Energy integrator now active for all device types (was PowerOcean only)
- Power-to-energy mappings extracted to const.py as per-device-type constants
- DeviceInfo centralized in coordinator (removed 4x duplication across entity platforms)

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
