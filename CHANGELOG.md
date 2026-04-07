# Changelog

All notable changes to this project will be documented in this file.

## [1.12.0] - 2026-04-07

### Added
- Human-readable state translations for enum sensors: battery charge/discharge state, grid status, inverter state, work mode, feed mode, connectivity (WiFi/Ethernet/4G), and charger type now show descriptive labels instead of raw numeric values.
- German translations for all new state values.
- Delta 2 Max enum sensors: charge/discharge state, EMS charge state, MPPT charge state, and charger type now show translated labels.

### Fixed
- Enum sensors showing "unavailable" in Enhanced Mode due to proto3 zero-value omission. Fields like grid_status=0 were silently dropped by MessageToDict; EMS change reports now default missing enum fields to their zero-value label. (beta.2)
- Work mode and inverter state sensors blocked by HA validation after upgrade. RestoreSensor loaded old raw values (e.g. "WORKMODE_SELFUSE") that are not in the new options list. Invalid restored values are now discarded so the sensor can start fresh. (beta.3)
- Proto path for work mode and inverter state used string-keyed maps but proto sends integer values. Added integer-keyed mapping tables for the proto decode path. (beta.3)

### Changed
- Enum sensors use HA `device_class: enum` with `options` for proper state handling and translation support.
- **Breaking:** Automations using raw numeric state values (e.g. `state == "1"`) for these sensors must update to the new string values (e.g. `state == "charging"`). All affected sensors are diagnostic and disabled by default.

## [1.11.0] - 2026-04-05

### Added
- Snapshot continuity layer: explicit last-known-good data contract for the Graduated Availability degraded stage. Device data snapshots are captured on every update with metadata (timestamp, source, key count) and expire only at hard-unavailable. Formalizes the implicit guarantee that entities retain values during stream interruptions.
- Snapshot metadata in diagnostics: source, age, key count, and capture status for debugging availability behavior.
- `documentation/` folder for public user-facing documentation.

### Fixed
- DeviceSnapshot dataclass now frozen for immutability guarantee (beta.2)
- Coordinator docstring accuracy: _log_event max entries corrected to 50 (beta.2)
- Proper __init__ declaration for _auth_method and _last_mqtt_event_ts, removing defensive getattr calls (beta.2)
- PowerOcean battery device_class limited to primary soc_pct sensor; pack SoC and bp_real_soc_pct no longer carry battery class (beta.2)

## [1.10.0] - 2026-04-04

### Fixed
- App-auth MQTT client now triggers credential refresh on auth errors (rc=5). Previously, expired WSS credentials caused indefinite reconnect failures because the `auth_error_handler` callback was not wired for the app-auth path.

### Added
- Three-state MQTT status: diagnostics now report `receiving`, `connected_stale`, or `disconnected` instead of binary `connected`/`disconnected`, making it easier to identify connected-but-silent MQTT sessions.
- `data_receiving` property on the coordinator for programmatic stale detection.
- Enhanced connectivity event log: stale detection triggers, force-reconnect events, credential refresh outcomes, and recovery transitions are now captured in the per-device event log.
- ISO timestamps in diagnostic event log entries for human-readable debugging.
- Proactive credential refresh for app-auth: credentials older than 20 hours are refreshed automatically every 12 hours, preventing brief outages from token expiry.

### Changed
- Graduated availability degradation replaces the binary 95-second hard-unavailable cutoff. Entities now remain available with last-known-good values during stream interruptions, degrading through stages (healthy -> stale -> degraded -> unavailable). Hard unavailable is now 10 minutes instead of 95 seconds, matching observed device behavior where PowerOcean telemetry gaps can exceed 600 seconds.
- Device-specific degradation thresholds: PowerOcean/Delta soft=5min hard=10min, Smart Plug soft=6min hard=10min.
- Extracted PowerOcean proto remapping logic (~150 lines) from `coordinator.py` into `ecoflow/parsers/powerocean_proto.py` for cleaner core/device separation.
- Moved SET command templates from `switch.py` and `number.py` into `const.py` alongside entity definitions.

## [1.9.3] - 2026-04-04

### Fixed
- Delta 2 Max device header in Home Assistant showed wrong battery percentage (100% SoH instead of actual SoC) because multiple sensors had `device_class: battery`. Only the primary `soc` sensor now carries the battery device class; SoH and secondary SoC variants use `device_class: None`.

### Changed
- App-auth stale warning wording simplified: removed mode suffix from the user-facing message. Logs now state only the concrete condition and action (`MQTT stream interrupted ... marking device unavailable`).

## [1.9.2] - 2026-04-03

### Fixed
- App-auth MQTT health checks now run on a short dedicated interval (5s) instead of the device stale threshold, so reconnect attempts start promptly after disconnects.
- App-auth stale handling now uses a time-based grace window (`stale_threshold + 60s`) before marking a device unavailable, reducing false temporary unavailability during short broker interruptions.
- Connected-but-silent app-auth MQTT sessions now trigger a forced reconnect attempt, improving recovery from stalled WSS sessions.

### Changed
- Improved stale warning context in app-auth mode by including reconnect attempt count in the warning message.
- Removed legacy "no HTTP fallback" wording from app-auth stale warnings; logs now describe only the actual state/actionable mode context.
- App-auth stale/recovery logs now include both device name and serial number for unambiguous per-device troubleshooting.

## [1.9.1] - 2026-04-02

### Changed
- Smart Plug in app-auth now uses a device-specific stale threshold (180s) instead of the global 35s threshold to avoid false temporary unavailability on sparse telemetry bursts.
- Smart Plug app-auth keepalive now adds periodic `get-all` full-state refreshes (every 120s) alongside `latestQuotas`.

### Fixed
- Reduced false "device unavailable (no HTTP fallback)" transitions for Smart Plug while MQTT is still healthy but temporarily quiet.
- Improved Smart Plug control-state freshness (switch/brightness/max limit) during long-running sessions.

## [1.9.0] - 2026-04-02

### Added
- **Enhanced Mode for all devices** - set up with just your EcoFlow email and password, no Developer API keys needed. Real-time updates: PowerOcean ~3 s, Delta 2 Max ~2 s, Smart Plug ~3 s
- New setup flow: choose between Standard (official API) or Enhanced (community-driven real-time) at first setup
- Auto-discovery of all devices bound to your EcoFlow account (Enhanced Mode)
- Smart Plug full control in Enhanced Mode: on/off switch, LED brightness, max power limit
- Automatic upgrade: existing Enhanced Mode setups migrate seamlessly on restart

### Changed
- Config flow redesigned: mode selection (Standard vs Enhanced) is now the first step
- Smart Plug LED brightness now shows 0-100% instead of raw device values
- Enhanced Mode and Standard Mode are fully separated - clean architecture, no hybrid paths
- Reduced log noise: transient MQTT reconnects no longer flood the log with warnings

### Fixed
- Smart Plug switch and number controls now work reliably in Enhanced Mode
- Delta 2 Max receives real-time updates (~2 s) in Enhanced Mode instead of only HTTP polling
- Options flow mode switch correctly handles credential changes
- Device type detection works even when the EcoFlow API returns empty device names
- Enhanced Mode no longer creates coordinators for unsupported devices (e.g. PowerGlow, PowerPulse) - eliminates WARNING spam about MQTT stale data (#28)

### Upgrade notes
- **Standard Mode users**: No action needed. Your setup continues to work exactly as before.
- **Enhanced Mode users** (v1.8.x with Developer Keys + email/password): The integration automatically upgrades to the new app-auth flow. Developer Keys are no longer needed for Enhanced Mode.
- **Config entry migration**: The integration migrates your configuration automatically (v1/v2 to v3). This is transparent and non-breaking.

## [1.8.3] - 2026-03-31

### Fixed
- HTTP error 1006 ("device not linked to API key") no longer triggers false re-authentication — classified as a configuration issue with an actionable log message instead of counting toward the auth failure threshold (#2)
- Enhanced Mode: HTTP fallback failures no longer trigger re-authentication when MQTT is actively delivering data (#2)
- Error 1006 logged once per device with clear guidance instead of repeating every 30 seconds

## [1.8.2] - 2026-03-31

### Fixed
- PowerOcean Enhanced Mode: stable per-pack sensor numbering via battery serial number — each physical pack now consistently maps to the same `pack{n}_*` sensors across heartbeats, fixing Pack 2 sensors not updating (#10)

## [1.8.1] - 2026-03-31

### Fixed
- PowerOcean Enhanced Mode: idle battery packs no longer falsely filtered as phantoms — replaced numeric non-zero check with identity key presence check (bp_soc, bp_design_cap, bp_sn, etc.) so packs with zero power/SoC are still recognized (#10)
- PowerOcean Enhanced Mode: aggregate `bp_remain_watth` now computed from accumulated device data instead of per-message — partial heartbeats (single pack reporting) no longer cause the total to revert to one pack's value (#10)

## [1.8.0] - 2026-03-31

### Changed
- State update deduplication: entities only write to HA recorder when their value actually changes, reducing state writes by ~60-80% (previously every coordinator update triggered a state write for all entities regardless of value change)
- Energy integration precision reduced from 3 to 2 decimal places (0.01 kWh resolution) to further reduce fractional churn on total_increasing sensors
- Optimistic writes (switch, number) now sync dedup state to prevent one redundant write on the next coordinator tick

### Fixed
- MQTT fallback logging reduced from WARNING to INFO: transient stale/recovery transitions are self-healing and no longer clutter the HA log — both "switching to HTTP fallback" and "MQTT recovered" now log at INFO level as a matched pair

## [1.7.1] - 2026-03-31

### Fixed
- Protobuf bindings backward compatibility: runtime version check now wrapped in try/except for protobuf <5.29
- Coordinator: encapsulated `_device_data` access via public `set_device_value()` method
- Docstrings updated: `async_set_soc_limits` (2 fields, not 4) and `build_soc_limit_set_payload` (min discharge confirmed, max charge pass-through only)

## [1.7.0] - 2026-03-31

### Fixed
- PowerOcean: SoC limit 0% now correctly synced in both directions — `optional` proto3 field presence on `sys_bat_dsg_down_limit` and `sys_bat_chg_up_limit` ensures `MessageToDict` includes zero values instead of silently omitting them
- PowerOcean: "Battery Remaining Capacity" (`bp_remain_watth`) now shows total capacity across all battery packs instead of only Pack 1 — affects both Standard Mode (HTTP) and Enhanced Mode (Protobuf) (#10)

### Removed
- Temporary workarounds from v1.6.5–v1.6.8 (proto3 global flag, optimistic lock, zero-fill, HTTP sync loop) — all replaced by proper `optional` field presence

## [1.6.7] - 2026-03-31

### Fixed
- PowerOcean: Min Discharge SoC 0% now persists permanently — optimistic value is written to `_device_data` so it survives coordinator refresh cycles (proto3 omits zero-valued fields from MQTT readback, but the merge no longer overwrites the SET value)

### Removed
- Temporary 10-second optimistic lock from v1.6.6 (no longer needed)

## [1.6.6] - 2026-03-31

### Fixed
- Revert `always_print_fields_with_no_presence` from v1.6.5 — it flooded all proto fields with default 0, overwriting real sensor values
- PowerOcean: number entities now use a 10-second optimistic lock after SET commands to prevent proto3 zero-omission readback from reverting the displayed value

## [1.6.5] - 2026-03-31

### Fixed
- Proto3 zero-value readback: `MessageToDict` now includes fields with value 0 — previously, setting Min Discharge SoC to 0% was accepted by the device but HA reverted to the previous value because the proto3 decoder omitted zero-valued fields

## [1.6.4] - 2026-03-31

### Fixed
- PowerOcean: revert to 2-field SysBatChgDsgSet payload (charge upper + discharge lower only) — the 4-field version from v1.6.1 caused the device to reject discharge lower limit value 0
- Proto3 zero-value readback: `MessageToDict` now includes fields with value 0 — previously, setting Min Discharge SoC to 0% was accepted by the device but HA reverted to the previous value because the proto3 decoder omitted zero-valued fields

## [1.6.3] - 2026-03-31

### Fixed
- PowerOcean: revert proto field swap from v1.6.2 — both values were broken after swap
- PowerOcean: remove Max Charge SoC number entity — device firmware does not reliably accept charge upper limit via SysBatChgDsgSet (requires portal traffic capture for further investigation)

### Changed
- PowerOcean: SoC control reduced to Min Discharge SoC only (Enhanced Mode) until charge limit SET protocol is verified

## [1.6.2] - 2026-03-31

### Fixed
- PowerOcean: swap proto field order in SysBatChgDsgSet - device reads field 1 as discharge lower and field 2 as charge upper (opposite of proto definition labels)
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
