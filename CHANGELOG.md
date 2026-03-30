# Changelog

All notable changes to this project will be documented in this file.

## [1.6.3] - 2026-03-31

### Fixed
- PowerOcean: revert proto field swap from v1.6.2 — both values were broken after swap
- PowerOcean: remove Max Charge SoC number entity — device firmware does not reliably accept charge upper limit via SysBatChgDsgSet (requires portal traffic capture for further investigation)

### Changed
- PowerOcean: SoC control reduced to Min Discharge SoC only (Enhanced Mode) until charge limit SET protocol is verified

## [1.6.2] - 2026-03-31

### Fixed
- PowerOcean: swap proto field order in SysBatChgDsgSet — device reads field 1 as discharge lower and field 2 as charge upper (opposite of APK proto labels)
- PowerOcean: fix dev_soc lookup in SET payload — was reading unmapped key, now uses coordinator-mapped `soc_pct`

## [1.6.1] - 2026-03-31

### Fixed
- PowerOcean: Max Charge SoC SET now includes all 4 required protobuf fields (charge upper, discharge lower, backup ratio, device SoC) — previously only 2 fields were sent, causing the device to silently reject the charge limit change

## [1.6.0] - 2026-03-30

### Added
- PowerOcean: battery SoC limit control — Max Charge SoC (50–100%) and Min Discharge SoC (0–30%) as number entities (Enhanced Mode only)
- PowerOcean: SysBatChgDsgSet protobuf SET command (cmd_func=96, cmd_id=112) for real-time SoC limit adjustment via WSS

### Changed
- MQTT client: refactored `send_energy_stream_switch` to use generic `send_proto_set` method for all protobuf SET commands

## [1.5.3] - 2026-03-30

### Fixed
- False "Authentication expired" reauth trigger when MQTT data is flowing but HTTP polling has transient failures (#2)
- EcoFlow API error 8521 (intermittent server error) is now retried instead of immediately counted as a failure

## [1.5.2] - 2026-03-30

### Fixed
- Sensor precision: `native_value` now rounds numeric values based on `suggested_display_precision` — power sensors show integers (e.g. "2347 W"), energy sensors show 2 decimal places (e.g. "15.23 kWh")

### Changed
- Diagnostics: event log capacity increased from 20 to 50 entries for better support troubleshooting

## [1.5.1] - 2026-03-30

### Fixed
- PowerOcean: battery pack numbering now starts at "Pack 1" instead of "Pack 2" — phantom/empty API entries (EMS module) are skipped before numbering (#5)
- PowerOcean: aggregate battery sensors (bp_*) now correctly select the first real battery pack, not a phantom entry
- PowerOcean: Enhanced Mode (Protobuf) now delivers multi-pack data correctly — previously silently discarded by internal key filter
- Config flow: narrowed exception handling with OSError coverage for SSL/socket errors

### Note for multi-pack users
- If you have multiple battery packs and previously saw "Pack 2"/"Pack 3" instead of "Pack 1"/"Pack 2", the entity IDs will change after this update (e.g. `pack2_soc_pct` becomes `pack1_soc_pct`). You may need to update any dashboard cards or automations that reference pack entities.

### Changed
- Logging convention: renamed `logger` to `_LOGGER` across all modules (Home Assistant standard)
- Timing: interval measurements now use `time.monotonic()` instead of `time.time()` for NTP-resilient elapsed time tracking
- Entity type hints: `device_info` return type corrected to `DeviceInfo` across all entity platforms
- Import order: PEP 8 compliant import ordering in `const.py`
- Typing: modern `from __future__ import annotations` and union syntax used consistently
- Energy integrator: state file migration handles pre-v1.5.1 epoch timestamps gracefully

### Added
- Protobuf import failure now logs a warning instead of failing silently
- `async_migrate_entry` stub for future config schema migrations
- Proto decoder functions documented with docstrings
- `CONTRIBUTING.md` with development setup, code style, and PR guidelines
- Security note in README about Enhanced Mode credential storage

## [1.5.0] - 2026-03-30

### Added
- PowerOcean: multi-battery-pack support — per-pack sensors for up to 5 BP5000 packs (120 new sensors, 7 enabled for Pack 1)
- PowerOcean: 19 additional EMS/system diagnostic sensors (SoC limits, fault codes, connectivity, system capabilities)
- PowerOcean: lifetime energy counters per battery pack (accumulated charge/discharge kWh)
- PowerOcean: multi-pack data in Enhanced Mode (Protobuf heartbeat extracts all packs)

## [1.4.0] - 2026-03-30

### Added
- Delta 2 Max: beeper, X-Boost, AC auto restart, backup reserve switches (4 new)
- Delta 2 Max: screen brightness, screen timeout, 12V port timeout, backup reserve level numbers (4 new)
- Delta 2 Max: expansion battery pack support — 32 sensors for up to 2 slave packs (disabled by default)

### Changed
- Delta 2 Max: X-Boost promoted from read-only binary sensor to controllable switch

## [1.3.4] - 2026-03-30

### Added
- Smart Plug: LED brightness control (0-1023) via number entity
- Smart Plug: overload protection / max power limit (0-2500W) via number entity
- Smart Plug: MQTT real-time data subscription for near-instant updates alongside HTTP polling

## [1.3.3] - 2026-03-30

### Added
- Diagnostic sensors for MQTT connection status and connection mode (disabled by default)
- Event history (last 20 events) in diagnostics download for troubleshooting
- SET command reply tracking via MQTT set_reply topic subscription

### Fixed
- Startup log correctly reports Enhanced vs Standard device count
- Re-auth trigger fires exactly once after 5 HTTP failures (no repeated warnings)
- MQTT event log rate-limited to prevent flooding in Enhanced Mode

## [1.3.2] - 2026-03-29

### Added
- README troubleshooting entry for manual credential update path via Reconfigure menu

### Fixed
- German translation placeholder mismatch in `reconfigure_confirm.description` (`{developer_portal_url}`) to prevent HA translation validation errors

## [1.3.1] - 2026-03-29

### Added
- Reconfigure flow — update API credentials via Settings > Integrations > EcoFlow Energy > Reconfigure
- Entity availability tracking — entities show "unavailable" when device is unreachable
- Optimistic state update for number entities (charge speed, SoC limits)
- `suggested_display_precision` for all sensors — cleaner UI values
- `disabled_by_default` for diagnostic sensors — less overwhelming for new users
- Entity categories for diagnostic binary sensors
- `configuration_url` in device info — clickable link on device page
- German translations for re-authentication and reconfigure flows

### Fixed
- Protobuf decode errors logged at DEBUG instead of WARNING (zero-noise logging)
- HTTP retry attempts logged at DEBUG instead of WARNING
- Startup summary log downgraded from INFO to DEBUG
- Diagnostics `http_fallback_active` now correctly reflects actual fallback state

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
