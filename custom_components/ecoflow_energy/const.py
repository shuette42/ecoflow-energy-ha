"""Constants for the EcoFlow Energy integration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TypeVar

from homeassistant.const import Platform

from .ecoflow.const import (  # noqa: E402
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_STREAM,
    DEVICE_TYPE_STREAM_AC5000,
    DEVICE_TYPE_UNKNOWN,
    get_device_name,
    get_device_type,
)

DOMAIN = "ecoflow_energy"

# Top-level hass.data key for per-entry lists of skipped (unsupported)
# devices. Kept separate from hass.data[DOMAIN] (which is keyed by
# entry_id and holds coordinators) so a reserved string key cannot
# collide with an entry_id.
DATA_SKIPPED_DEVICES = "ecoflow_energy_skipped_devices"
# Listen-only probes capturing raw data of devices that have no parser yet.
DATA_DEVICE_PROBES = "ecoflow_energy_device_probes"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
]

# Config entry keys
CONF_ACCESS_KEY = "access_key"
CONF_SECRET_KEY = "secret_key"
CONF_DEVICES = "devices"
CONF_MODE = "mode"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_USER_ID = "user_id"
CONF_AUTH_METHOD = "auth_method"

# Raw capture for devices without a parser. Off unless the user turns it on,
# and it turns itself off again: the capture opens an extra connection that
# serves nobody except the person who volunteered to help add a device, so it
# must never become a permanent background load in someone's installation.
CONF_RAW_CAPTURE = "raw_capture"
CONF_RAW_CAPTURE_UNTIL = "raw_capture_until"
# Wall-clock, not monotonic: the deadline has to survive a restart.
RAW_CAPTURE_DURATION_S = 24 * 60 * 60

# Auth methods
AUTH_METHOD_DEVELOPER = "developer"
AUTH_METHOD_APP = "app"

# Device modes
MODE_STANDARD = "standard"
MODE_ENHANCED = "enhanced"

# Coordinator - Stale detection
STALE_THRESHOLD_S = 35.0  # MQTT data older than this → trigger reconnect + HTTP fallback
SMARTPLUG_STALE_THRESHOLD_S = 180.0  # Smart Plug app-auth: tolerate sparse telemetry bursts
MQTT_HEALTH_CHECK_INTERVAL_S = 5.0  # Run stale/reconnect health checks independently from stale threshold

# Graduated availability degradation thresholds (app-auth only).
# Entities remain available with last-known values until HARD_UNAVAILABLE.
# Observed PowerOcean telemetry has gaps up to 613s (cmd_id=33).
# The old 95s hard cutoff (35s stale + 60s grace) was too aggressive.
#
# Stages: healthy -> stale -> degraded -> unavailable
#   stale: age > STALE_THRESHOLD_S (reconnect attempts start)
#   degraded: age > SOFT_UNAVAILABLE_S (data is old but entities still visible)
#   unavailable: age > HARD_UNAVAILABLE_S (entities go unavailable in HA)
SOFT_UNAVAILABLE_S = 300.0  # 5 min: data old, entities visible but stale
SMARTPLUG_SOFT_UNAVAILABLE_S = 360.0  # 6 min: SmartPlug tolerates longer gaps
HARD_UNAVAILABLE_S = 600.0  # 10 min: entities go unavailable
SMARTPLUG_HARD_UNAVAILABLE_S = 600.0  # 10 min: SmartPlug hard cutoff
HTTP_FALLBACK_INTERVAL_S = 30

# Raw protobuf frame capture for diagnostics (app-auth push path), bucketed
# by message type. A shared ring holds whatever arrives most often: a
# PowerOcean pushes its live telemetry every few seconds while a command such
# as the EMS report arrives minutes apart, so 24 shared slots are the last
# minute of the frequent one and the rare command is never in a download.
#
# The key budget is derived from what one device actually produces, not
# picked for size, and the number comes from a measurement rather than from
# the command table: a ten minute listen-only recording of a PowerOcean
# (HJ31, 2026-08-01) delivered 60 frames in twelve distinct message types on
# `property` alone - cmd_func 96 with ids 1, 3, 7, 8, 13, 33 and 34, plus
# 53.14, 241.5, 241.36, 209.51 and 224.38. The decoded command list would
# have suggested seven. The `get_reply` topic class adds those types a
# second time, an unmapped command becomes a key of its own, and so does an
# attached accessory's report.
#
# Buckets are claimed in arrival order and never evicted, so a budget that
# merely matches the observed count is spent by the frequent pushes within
# seconds, and the rare reports - the ones this bucketing exists to keep -
# are dropped at the key gate minutes later, which is the failure the shared
# ring had in a different place. Twenty is the measured twelve plus room for
# the second topic class and for what a ten minute window did not show.
#
# Tightened against the unsupported-device probe below on the per-key axis
# instead, because that is the axis that costs bytes without costing
# coverage: three frames still hold the first, one middle and the newest
# frame of every type. This buffer is not opt-in and not time-limited - it
# runs on every device in Enhanced Mode for as long as the integration is
# loaded. Worst case 20 * 3 * 512 B = 30 720 B (30 KiB) of frame payload per
# device, roughly double that as hex text in the diagnostics download.
RAW_FRAME_LOG_KEYS_MAX = 20
RAW_FRAME_LOG_PER_KEY_MAX = 3
RAW_FRAME_MAX_BYTES = 512

# Undeclared protobuf field numbers: how many commands are tracked at once.
# The same twenty message types the frame capture budgets for would be the
# consistent number, but only the commands with a typed binding can report
# unknown fields at all, and no device class has more than a handful of those.
# Twelve leaves room for a device that surprises us.
UNKNOWN_FIELD_CMDS_MAX = 12

# And how many distinct field numbers are kept per command. This is the cap
# that actually bounds the memory: the per-message limit in the decoder only
# limits one message, and the summary accumulates across every message a
# device ever sends. Without this a device whose field numbers vary - a fault,
# or a decode that lands on the wrong message type - adds entries for as long
# as the integration is loaded, and both the process and the diagnostics
# download grow with it. Measured: 10 000 messages of 40 varying numbers reach
# 400 000 entries and a 5 MB download.
#
# Two hundred is well above the gap this exists to expose. A Delta 3 status
# frame declares 306 fields in our binding; a schema gap of more than 200
# further numbers is not "a setting is missing", it is the wrong binding.
UNKNOWN_FIELD_NUMBERS_MAX = 200

# Unsupported-device probe: budget per message type instead of one shared ring.
# A device that pushes one message type every two seconds fills a shared ring
# with its own tail within minutes, and everything a parser is built from is
# gone. Bucketing by message type makes a rare report compete only with itself.
# Worst case: 12 * 10 * 512 B = 61 440 B (60 KiB) of frame payload, roughly
# double that as hex text in the diagnostics download.
RAW_FRAME_KEYS_MAX = 12
RAW_FRAME_PER_KEY_MAX = 10

# How often the capture checks that its listen-only session is still up. The
# broker refuses a client id that has already been used, so a dropped session
# can only be restored by building a new one, and nothing else does that for a
# device that has no coordinator. Sixty seconds is well inside what the client
# backs off to on repeated failure, so the check costs nothing when the link is
# healthy and does not add a second retry schedule of its own.
PROBE_WATCHDOG_INTERVAL_S = 60
HTTP_SUPPLEMENT_INTERVAL_S = 60  # Enhanced Mode: HTTP supplement poll for detail sensors
ENERGY_STREAM_KEEPALIVE_S = 20  # Re-send EnergyStreamSwitch every 20s
QUOTAS_KEEPALIVE_S = 30  # latestQuotas poll interval (app-level keepalive)
APP_SURPLUS_SYNC_MIN_INTERVAL_S = 30.0  # min interval between auto-sync SETs that mirror EmsParamChangeReport.dev_soc into the EMS sysBatBackupRatio
APP_SURPLUS_SYNC_USER_GRACE_S = 5.0  # ignore discrepancy briefly after a user SET to wait for the device echo
POWEROCEAN_SOC_DEBOUNCE_S = 0.3  # coalesce slider-drag SETs into one frame; the device cannot keep up with 5%-step sets at 100ms cadence and the EMS/App-layer fields desync
# The two state keys the PowerOcean SoC sliders write. They are sent as one
# frame, so a failed write has to undo both. Kept next to the debounce window
# because the snapshot and the rollback have to agree on exactly this pair -
# they did not in v1.16.0-beta.10, and the rollback restored the failed value.
POWEROCEAN_SOC_STATE_KEYS = ("ems_discharge_lower_limit_pct", "ems_app_surplus_pct")
PING_KEEPALIVE_S = 60  # MQTT ping heartbeat interval
SMARTPLUG_GET_ALL_KEEPALIVE_S = 120.0  # Smart Plug app-auth: periodic full-state refresh
CREDENTIAL_REFRESH_CHECK_S = 43200.0  # Check every 12h whether credentials need proactive refresh
CREDENTIAL_MAX_AGE_S = 72000.0  # Proactively refresh credentials older than 20h

DEVICE_TYPE_DISPLAY_NAMES: dict[str, str] = {
    DEVICE_TYPE_POWEROCEAN: "PowerOcean",
    DEVICE_TYPE_DELTA: "Delta 2 Max",
    DEVICE_TYPE_DELTA3: "Delta 3 Series",
    DEVICE_TYPE_SMARTPLUG: "Smart Plug",
    DEVICE_TYPE_STREAM: "Stream",
    DEVICE_TYPE_STREAM_AC5000: "STREAM AC 5000",
}

# Delta write/profile variants.
# R351: newer Delta 2 Max-style operateType naming.
# R331: legacy Delta/Delta Max-style operateType naming.
DELTA_PROFILE_R351 = "r351"
DELTA_PROFILE_R331 = "r331"


def get_delta_profile(product_name: str, device_sn: str = "") -> str:
    """Return Delta command/profile variant for write/read compatibility."""
    name = product_name.lower()
    sn = device_sn.upper()

    if sn.startswith("R331"):
        return DELTA_PROFILE_R331
    if sn.startswith("R351"):
        return DELTA_PROFILE_R351

    if "delta 2 max" in name:
        return DELTA_PROFILE_R351
    if "delta max" in name or "deltamax" in name or "delta 2" in name:
        return DELTA_PROFILE_R331

    return DELTA_PROFILE_R351


# =====================================================================
# Entity definition dataclasses
# =====================================================================


@dataclass(frozen=True)
class EcoFlowSensorDef:
    key: str
    name: str
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    icon: str | None = None
    entity_category: str | None = None
    enhanced_only: bool = False
    suggested_display_precision: int | None = None
    disabled_by_default: bool = False
    options: list[str] | None = None
    # Optional accessory reading (e.g. the PowerGlow heating rod on a
    # PowerOcean). The base device exists without it, so the entity is only
    # created once the device has actually reported the key. See
    # _watch_for_accessory() in sensor.py.
    accessory: bool = False


@dataclass(frozen=True)
class EcoFlowBinarySensorDef:
    key: str
    name: str
    device_class: str | None = None
    icon: str | None = None
    entity_category: str | None = None
    disabled_by_default: bool = False
    # Same meaning as on the sensor, number and select definitions: the value
    # only exists on the app channel, so with developer keys the entity would
    # be created and never fill.
    enhanced_only: bool = False


@dataclass(frozen=True)
class EcoFlowSwitchDef:
    key: str
    name: str
    state_key: str
    icon: str | None = None
    # Same meaning as on the sensor, binary sensor and number definitions: the
    # read-back only exists on the app channel, so with developer keys the
    # switch would be created and never learn the device's actual position.
    enhanced_only: bool = False


@dataclass(frozen=True)
class EcoFlowNumberDef:
    key: str
    name: str
    state_key: str
    unit: str | None = None
    icon: str | None = None
    min_value: float = 0
    max_value: float = 100
    step: float = 1
    enhanced_only: bool = False


@dataclass(frozen=True)
class EcoFlowSelectDef:
    """Select entity definition - maps a state value to user-facing options."""
    key: str
    name: str
    state_key: str
    options: tuple[str, ...]
    icon: str | None = None
    enhanced_only: bool = False
    # Wire value -> option, for settings the device reports as a number rather
    # than as a label. The parser keeps the raw value so a diagnostics download
    # shows what the device actually said; the translation lives here. A value
    # outside the map leaves the entity unknown, which is the honest state for
    # a setting some other client put out of range.
    value_map: Mapping[int, str] | None = None


# =====================================================================
# PowerOcean sensor definitions (from ha_discovery.py)
# =====================================================================

POWEROCEAN_SENSORS: list[EcoFlowSensorDef] = [
    # --- Core Power (measurement) ---
    EcoFlowSensorDef("solar_w", "Solar Power", "W", "power", "measurement", "mdi:solar-power", suggested_display_precision=0),
    EcoFlowSensorDef("home_w", "Home Power", "W", "power", "measurement", "mdi:home-lightning-bolt", suggested_display_precision=0),
    EcoFlowSensorDef("grid_w", "Grid Power", "W", "power", "measurement", "mdi:transmission-tower", suggested_display_precision=0),
    EcoFlowSensorDef("batt_w", "Battery Power", "W", "power", "measurement", "mdi:battery", suggested_display_precision=0),
    EcoFlowSensorDef("batt_charge_power_w", "Battery Charge Power", "W", "power", "measurement", "mdi:battery-charging", suggested_display_precision=0),
    EcoFlowSensorDef("batt_discharge_power_w", "Battery Discharge Power", "W", "power", "measurement", "mdi:battery", suggested_display_precision=0),
    EcoFlowSensorDef("grid_import_power_w", "Grid Import Power", "W", "power", "measurement", "mdi:transmission-tower-import", suggested_display_precision=0),
    EcoFlowSensorDef("grid_export_power_w", "Grid Export Power", "W", "power", "measurement", "mdi:transmission-tower-export", suggested_display_precision=0),
    # --- SOC ---
    EcoFlowSensorDef("soc_pct", "Battery SOC", "%", "battery", "measurement", "mdi:battery", suggested_display_precision=0),
    # --- Battery Detail ---
    EcoFlowSensorDef("bp_soh_pct", "Battery SOH", "%", None, "measurement", "mdi:battery-heart-variant", suggested_display_precision=0),
    EcoFlowSensorDef("bp_cycles", "Battery Cycles", None, None, "total_increasing", "mdi:battery-sync"),
    EcoFlowSensorDef("bp_remain_watth", "Battery Remaining Capacity", "Wh", "energy_storage", "measurement", "mdi:battery-clock", suggested_display_precision=0),
    # --- Energy Dashboard (total_increasing, kWh) ---
    # All 6 energy sensors available in Standard Mode via Riemann sum integration
    EcoFlowSensorDef("solar_energy_kwh", "Solar Energy", "kWh", "energy", "total_increasing", "mdi:solar-power", suggested_display_precision=2),
    EcoFlowSensorDef("home_energy_kwh", "Home Energy", "kWh", "energy", "total_increasing", "mdi:home-lightning-bolt", suggested_display_precision=2),
    EcoFlowSensorDef("grid_import_energy_kwh", "Grid Import Energy", "kWh", "energy", "total_increasing", "mdi:transmission-tower-import", suggested_display_precision=2),
    EcoFlowSensorDef("grid_export_energy_kwh", "Grid Export Energy", "kWh", "energy", "total_increasing", "mdi:transmission-tower-export", suggested_display_precision=2),
    EcoFlowSensorDef("batt_charge_energy_kwh", "Battery Charge Energy", "kWh", "energy", "total_increasing", "mdi:battery-charging", suggested_display_precision=2),
    EcoFlowSensorDef("batt_discharge_energy_kwh", "Battery Discharge Energy", "kWh", "energy", "total_increasing", "mdi:battery", suggested_display_precision=2),
    # --- Battery Diagnostics ---
    EcoFlowSensorDef("bp_voltage_v", "Battery Voltage", "V", "voltage", "measurement", "mdi:flash-triangle", "diagnostic", suggested_display_precision=1),
    EcoFlowSensorDef("bp_current_a", "Battery Current", "A", "current", "measurement", "mdi:current-dc", "diagnostic", suggested_display_precision=2),
    EcoFlowSensorDef("bp_max_cell_temp_c", "Battery Max Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-chevron-up", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("bp_min_cell_temp_c", "Battery Min Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-chevron-down", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("bp_env_temp_c", "Battery Environment Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("bp_max_mos_temp_c", "Battery Max MOSFET Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-alert", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("bp_cell_max_vol_mv", "Battery Cell Max Voltage", "mV", "voltage", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bp_cell_min_vol_mv", "Battery Cell Min Voltage", "mV", "voltage", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bp_real_soc_pct", "Battery Real SOC", "%", None, "measurement", "mdi:battery", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bp_real_soh_pct", "Battery Real SOH", "%", None, "measurement", "mdi:battery-heart-variant", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bp_down_limit_soc_pct", "Battery Min SOC Limit", "%", None, None, "mdi:battery-low", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bp_up_limit_soc_pct", "Battery Max SOC Limit", "%", None, None, "mdi:battery-high", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    # --- Inverter / PCS Diagnostics ---
    EcoFlowSensorDef("pcs_ac_freq_hz", "Grid Frequency", "Hz", "frequency", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=1),
    # EMS heartbeat only. PowerOcean has no MQTT push in Standard Mode and the
    # HTTP quota carries no NTC reading, so with developer keys this would be
    # a visible sensor that never fills.
    EcoFlowSensorDef("ems_ntc_temp_max_c", "EMS Max Internal Temp", "°C", "temperature", "measurement", "mdi:thermometer-alert", "diagnostic", enhanced_only=True, suggested_display_precision=1),
    EcoFlowSensorDef("ems_bp_alive_num", "Battery Packs Online", None, None, "measurement", "mdi:battery-check", "diagnostic", disabled_by_default=True),
    # PowerGlow heating rod: an optional accessory, so these four are created
    # only after the device has reported them at least once (#7).
    EcoFlowSensorDef("heating_rod_power_w", "Heating Rod Power", "W", "power", "measurement", "mdi:water-boiler", "diagnostic", suggested_display_precision=0, disabled_by_default=True, accessory=True),
    EcoFlowSensorDef("heating_rod_water_temp_c", "Heating Rod Water Temperature", "°C", "temperature", "measurement", "mdi:thermometer-water", "diagnostic", suggested_display_precision=0, disabled_by_default=True, accessory=True),
    EcoFlowSensorDef("heating_rod_target_power_w", "Heating Rod Target Power", "W", "power", "measurement", "mdi:water-boiler", "diagnostic", suggested_display_precision=0, disabled_by_default=True, accessory=True),
    EcoFlowSensorDef("heating_rod_target_temp_c", "Heating Rod Target Temperature", "°C", "temperature", "measurement", "mdi:thermometer-chevron-up", "diagnostic", suggested_display_precision=0, disabled_by_default=True, accessory=True),
    EcoFlowSensorDef("bp_online_sum", "Battery Packs Online (EMS)", None, None, "measurement", "mdi:battery-check", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("mppt_pv1_power_w", "MPPT String 1 Power", "W", "power", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=0),
    EcoFlowSensorDef("mppt_pv1_voltage_v", "MPPT String 1 Voltage", "V", "voltage", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=1),
    EcoFlowSensorDef("mppt_pv1_current_a", "MPPT String 1 Current", "A", "current", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=2),
    EcoFlowSensorDef("mppt_pv2_power_w", "MPPT String 2 Power", "W", "power", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=0),
    EcoFlowSensorDef("mppt_pv2_voltage_v", "MPPT String 2 Voltage", "V", "voltage", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=1),
    EcoFlowSensorDef("mppt_pv2_current_a", "MPPT String 2 Current", "A", "current", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=2),
    # PowerOcean Plus exposes additional live PV inputs. String 3 is confirmed
    # on R374 hardware and string 4 matches the parser ceiling. Both stay
    # disabled by default: ordinary PowerOcean units have two strings, and an
    # entity created for a key the device never sends would sit at "unknown"
    # forever on the majority of installations.
    EcoFlowSensorDef("mppt_pv3_power_w", "MPPT String 3 Power", "W", "power", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("mppt_pv3_voltage_v", "MPPT String 3 Voltage", "V", "voltage", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("mppt_pv3_current_a", "MPPT String 3 Current", "A", "current", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
    EcoFlowSensorDef("mppt_pv4_power_w", "MPPT String 4 Power", "W", "power", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("mppt_pv4_voltage_v", "MPPT String 4 Voltage", "V", "voltage", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("mppt_pv4_current_a", "MPPT String 4 Current", "A", "current", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
    EcoFlowSensorDef("grid_phase_a_voltage_v", "Grid Phase A Voltage", "V", "voltage", "measurement", "mdi:transmission-tower", "diagnostic", suggested_display_precision=1),
    EcoFlowSensorDef("grid_phase_b_voltage_v", "Grid Phase B Voltage", "V", "voltage", "measurement", "mdi:transmission-tower", "diagnostic", suggested_display_precision=1),
    EcoFlowSensorDef("grid_phase_c_voltage_v", "Grid Phase C Voltage", "V", "voltage", "measurement", "mdi:transmission-tower", "diagnostic", suggested_display_precision=1),
    # --- Per-Phase Active Power & Current (3-phase monitoring) ---
    EcoFlowSensorDef("grid_phase_a_active_power_w", "Grid Phase A Active Power", "W", "power", "measurement", "mdi:transmission-tower", "diagnostic", suggested_display_precision=0),
    EcoFlowSensorDef("grid_phase_b_active_power_w", "Grid Phase B Active Power", "W", "power", "measurement", "mdi:transmission-tower", "diagnostic", suggested_display_precision=0),
    EcoFlowSensorDef("grid_phase_c_active_power_w", "Grid Phase C Active Power", "W", "power", "measurement", "mdi:transmission-tower", "diagnostic", suggested_display_precision=0),
    EcoFlowSensorDef("grid_phase_a_current_a", "Grid Phase A Current", "A", "current", "measurement", "mdi:transmission-tower", "diagnostic", suggested_display_precision=2),
    EcoFlowSensorDef("grid_phase_b_current_a", "Grid Phase B Current", "A", "current", "measurement", "mdi:transmission-tower", "diagnostic", suggested_display_precision=2),
    EcoFlowSensorDef("grid_phase_c_current_a", "Grid Phase C Current", "A", "current", "measurement", "mdi:transmission-tower", "diagnostic", suggested_display_precision=2),
    # --- Per-Phase Reactive & Apparent Power (3-phase monitoring) ---
    EcoFlowSensorDef("grid_phase_a_reactive_power_var", "Grid Phase A Reactive Power", "var", "reactive_power", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("grid_phase_b_reactive_power_var", "Grid Phase B Reactive Power", "var", "reactive_power", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("grid_phase_c_reactive_power_var", "Grid Phase C Reactive Power", "var", "reactive_power", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("grid_phase_a_apparent_power_va", "Grid Phase A Apparent Power", "VA", "apparent_power", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("grid_phase_b_apparent_power_va", "Grid Phase B Apparent Power", "VA", "apparent_power", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("grid_phase_c_apparent_power_va", "Grid Phase C Apparent Power", "VA", "apparent_power", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    # --- PV Inverter Link ---
    EcoFlowSensorDef("pv_inverter_power_w", "PV Inverter Power", "W", "power", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    # --- EMS State & Control ---
    EcoFlowSensorDef("ems_feed_mode", "EMS Feed Mode", None, "enum", None, "mdi:cog", "diagnostic", options=["off", "no_limit", "zero", "limit"]),
    EcoFlowSensorDef("ems_work_mode", "EMS Work Mode", None, "enum", None, "mdi:cog", "diagnostic", options=["self_use", "time_of_use", "backup", "debug", "ac_makeup", "drm", "remote_schedule", "standby", "soc_calibration", "timer", "fcr", "third_party", "ai_schedule", "kraken"]),
    EcoFlowSensorDef("pcs_run_state", "PCS Running State", None, "enum", None, "mdi:power", "diagnostic", disabled_by_default=True, options=["standby", "running", "stopped"]),
    EcoFlowSensorDef("grid_status", "Grid Status", None, "enum", None, "mdi:transmission-tower", "diagnostic", options=["not_detected", "ok"]),
    EcoFlowSensorDef("pcs_power_factor", "Power Factor", None, "power_factor", "measurement", "mdi:sine-wave", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("ems_feed_power_limit_w", "Feed Power Limit", "W", "power", "measurement", "mdi:transmission-tower-export", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("ems_feed_ratio_pct", "Feed Ratio", "%", None, "measurement", "mdi:percent", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("batt_charge_discharge_state", "Battery Charge/Discharge State", None, "enum", None, "mdi:battery-sync", "diagnostic", options=["standby", "discharging", "charging"]),
    # --- EMS / System extended sensors (diagnostic, disabled by default) ---
    EcoFlowSensorDef("ems_charge_upper_limit_pct", "EMS Charge Upper Limit", "%", None, "measurement", "mdi:battery-charging-high", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("ems_discharge_lower_limit_pct", "EMS Discharge Lower Limit", "%", None, "measurement", "mdi:battery-alert-variant-outline", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("ems_keep_soc_pct", "EMS Keep SoC", "%", None, "measurement", "mdi:battery-lock", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("ems_backup_ratio_pct", "EMS Backup Ratio", "%", None, "measurement", "mdi:battery-lock-open", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("mppt1_fault_code", "MPPT 1 Fault Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("mppt2_fault_code", "MPPT 2 Fault Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
    # Unlike the fault codes above, the MPPT *warning* codes have no HTTP
    # quota entry - they ride the protobuf EMS messages only.
    EcoFlowSensorDef("mppt1_warning_code", "MPPT 1 Warning Code", None, None, None, "mdi:alert-outline", "diagnostic", enhanced_only=True, disabled_by_default=True),
    EcoFlowSensorDef("mppt2_warning_code", "MPPT 2 Warning Code", None, None, None, "mdi:alert-outline", "diagnostic", enhanced_only=True, disabled_by_default=True),
    EcoFlowSensorDef("pcs_ac_error_code", "PCS AC Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("pcs_dc_error_code", "PCS DC Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("pcs_ac_warning_code", "PCS AC Warning Code", None, None, None, "mdi:alert-outline", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("wifi_status", "WiFi Status", None, "enum", None, "mdi:wifi", "diagnostic", disabled_by_default=True, options=["disconnected", "connected"]),
    EcoFlowSensorDef("ethernet_status", "Ethernet Status", None, "enum", None, "mdi:ethernet", "diagnostic", disabled_by_default=True, options=["disconnected", "connected"]),
    EcoFlowSensorDef("cellular_status", "4G Status", None, "enum", None, "mdi:signal-4g", "diagnostic", disabled_by_default=True, options=["disconnected", "connected"]),
    EcoFlowSensorDef("ems_led_brightness", "EMS LED Brightness", None, None, "measurement", "mdi:brightness-6", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("ems_work_state", "EMS Work State", None, "enum", None, "mdi:cog", "diagnostic", disabled_by_default=True, options=["none", "init", "idle", "startup_ext_bp", "startup_inner_bp", "startup_pv", "startup_grid", "running", "stop", "maintain"]),
    EcoFlowSensorDef("ems_total_battery_capacity_wh", "Total Battery Capacity", "Wh", "energy_storage", "measurement", "mdi:battery", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("pcs_max_output_power_w", "PCS Max Output Power", "W", "power", "measurement", "mdi:flash-triangle", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("pcs_max_input_power_w", "PCS Max Input Power", "W", "power", "measurement", "mdi:flash-triangle", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bp_max_charge_power_w", "Battery Max Charge Power", "W", "power", "measurement", "mdi:battery-charging", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bp_max_discharge_power_w", "Battery Max Discharge Power", "W", "power", "measurement", "mdi:battery", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    # --- Run state and safety diagnostics (EMS change report, cmd_id=17) ---
    # Raw device codes: the value ranges are not documented anywhere we can
    # verify, so they stay numeric instead of being dressed up as enums with
    # invented labels. All disabled by default - they only matter when
    # something is wrong, and then the user enables them deliberately.
    # All of these ride the protobuf EMS messages (cmd 8 and cmd 17), which
    # only exist on the app channel - hence enhanced_only. Disabled by default
    # is not enough on its own: it only delays the empty sensor until somebody
    # enables it, and by then the registry entry is permanent.
    EcoFlowSensorDef("afci_self_test_result", "AFCI Self-Test Result", None, None, None, "mdi:flash-alert", "diagnostic", enhanced_only=True, disabled_by_default=True),
    EcoFlowSensorDef("ems_self_check_state", "EMS Self-Check State", None, None, None, "mdi:clipboard-check-outline", "diagnostic", enhanced_only=True, disabled_by_default=True),
    EcoFlowSensorDef("sys_heat_state", "System Heating State", None, None, None, "mdi:radiator", "diagnostic", enhanced_only=True, disabled_by_default=True),
    EcoFlowSensorDef("sys_calibration_state", "SoC Calibration State", None, None, None, "mdi:tune-variant", "diagnostic", enhanced_only=True, disabled_by_default=True),
    EcoFlowSensorDef("parallel_mode", "Parallel Mode", None, None, None, "mdi:call-split", "diagnostic", enhanced_only=True, disabled_by_default=True),
    EcoFlowSensorDef("battery_limit_reason", "Battery Limit Reason", None, None, None, "mdi:battery-alert", "diagnostic", enhanced_only=True, disabled_by_default=True),
    EcoFlowSensorDef("ems_sg_ready_state", "SG Ready State", None, None, None, "mdi:home-lightning-bolt", "diagnostic", enhanced_only=True, disabled_by_default=True),
]


def _build_po_pack_sensors(pack_num: int) -> list[EcoFlowSensorDef]:
    """Build sensor definitions for a PowerOcean battery pack.

    Pack 1: 7 core sensors enabled, 17 diagnostic disabled.
    Packs 2-5: all 24 sensors disabled by default.
    """
    p = f"pack{pack_num}"
    enabled = pack_num == 1  # Only Pack 1 core sensors enabled by default

    core = [
        EcoFlowSensorDef(f"{p}_soc", f"Pack {pack_num} SoC", "%", None, "measurement", "mdi:battery", suggested_display_precision=0, disabled_by_default=not enabled),
        EcoFlowSensorDef(f"{p}_power_w", f"Pack {pack_num} Power", "W", "power", "measurement", "mdi:flash", suggested_display_precision=0, disabled_by_default=not enabled),
        EcoFlowSensorDef(f"{p}_soh", f"Pack {pack_num} SoH", "%", None, "measurement", "mdi:battery-heart-variant", suggested_display_precision=0, disabled_by_default=not enabled),
        EcoFlowSensorDef(f"{p}_cycles", f"Pack {pack_num} Cycles", None, None, "total_increasing", "mdi:battery-sync", suggested_display_precision=0, disabled_by_default=not enabled),
        EcoFlowSensorDef(f"{p}_voltage_v", f"Pack {pack_num} Voltage", "V", "voltage", "measurement", "mdi:flash-triangle", suggested_display_precision=1, disabled_by_default=not enabled),
        EcoFlowSensorDef(f"{p}_current_a", f"Pack {pack_num} Current", "A", "current", "measurement", "mdi:current-dc", suggested_display_precision=2, disabled_by_default=not enabled),
        EcoFlowSensorDef(f"{p}_remain_watth", f"Pack {pack_num} Remaining Capacity", "Wh", "energy_storage", "measurement", "mdi:battery-clock", suggested_display_precision=0, disabled_by_default=not enabled),
    ]

    diagnostic = [
        EcoFlowSensorDef(f"{p}_max_cell_temp_c", f"Pack {pack_num} Max Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-chevron-up", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_min_cell_temp_c", f"Pack {pack_num} Min Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-chevron-down", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_env_temp_c", f"Pack {pack_num} Environment Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_calendar_soh", f"Pack {pack_num} Calendar SoH", "%", None, "measurement", "mdi:battery-heart-variant", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_cycle_soh", f"Pack {pack_num} Cycle SoH", "%", None, "measurement", "mdi:battery-heart-variant", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_accu_chg_energy_kwh", f"Pack {pack_num} Lifetime Charge Energy", "kWh", "energy", "total_increasing", "mdi:battery-charging", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_accu_dsg_energy_kwh", f"Pack {pack_num} Lifetime Discharge Energy", "kWh", "energy", "total_increasing", "mdi:battery", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_max_mos_temp_c", f"Pack {pack_num} Max MOSFET Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-alert", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_hv_mos_temp_c", f"Pack {pack_num} HV MOSFET Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-alert", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_lv_mos_temp_c", f"Pack {pack_num} LV MOSFET Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-alert", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_bus_voltage_v", f"Pack {pack_num} Bus Voltage", "V", "voltage", "measurement", "mdi:flash-triangle", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_ptc_temp_c", f"Pack {pack_num} PTC Heater Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_cell_max_vol_mv", f"Pack {pack_num} Max Cell Voltage", "mV", "voltage", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_cell_min_vol_mv", f"Pack {pack_num} Min Cell Voltage", "mV", "voltage", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_design_cap_mah", f"Pack {pack_num} Design Capacity", "mAh", None, "measurement", "mdi:battery", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_full_cap_mah", f"Pack {pack_num} Full Capacity", "mAh", None, "measurement", "mdi:battery", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_error_code", f"Pack {pack_num} Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
    ]

    return core + diagnostic


# Append per-pack sensors (5 packs x 24 sensors = 120 sensors)
for _pack_num in range(1, 6):
    POWEROCEAN_SENSORS.extend(_build_po_pack_sensors(_pack_num))

POWEROCEAN_BINARY_SENSORS: list[EcoFlowBinarySensorDef] = [
    # Fault flags from the EMS change report (cmd_id=17). Off is the normal
    # state; each one turns on only when the device reports that condition.
    # That report is protobuf on the app channel, so all five are
    # enhanced_only - with developer keys they would never leave "unknown".
    EcoFlowBinarySensorDef("afci_fault_ch1", "AFCI Fault String 1", "problem", "mdi:flash-alert", "diagnostic", disabled_by_default=True, enhanced_only=True),
    EcoFlowBinarySensorDef("afci_fault_ch2", "AFCI Fault String 2", "problem", "mdi:flash-alert", "diagnostic", disabled_by_default=True, enhanced_only=True),
    EcoFlowBinarySensorDef("battery_line_off", "Battery Line Disconnected", "problem", "mdi:power-plug-off", "diagnostic", disabled_by_default=True, enhanced_only=True),
    EcoFlowBinarySensorDef("battery_relay_fault", "Battery Relay Fault", "problem", "mdi:electric-switch", "diagnostic", disabled_by_default=True, enhanced_only=True),
    EcoFlowBinarySensorDef("ems_sg_ready_enabled", "SG Ready Enabled", None, "mdi:home-lightning-bolt-outline", "diagnostic", disabled_by_default=True, enhanced_only=True),
]

POWEROCEAN_NUMBERS: list[EcoFlowNumberDef] = [
    # Backup-Reserve (App-slider): minimum SoC kept in reserve. Wire field 2
    # in cmd_id=112 SysBatChgDsgSet, sent as a 3-field app-replay payload.
    EcoFlowNumberDef("backup_reserve", "Backup Reserve", "ems_discharge_lower_limit_pct", "%", "mdi:battery-lock", 0, 100, 5, enhanced_only=True),
    # Überschüssige-Solarenergie threshold (App-slider).
    #
    # Source key is `ems_app_surplus_pct` (proto wire field 4 = dev_soc /
    # cloud-quota socDev) - the user-side setting the EcoFlow app reads
    # and writes. The companion `ems_backup_ratio_pct` (wire field 3 =
    # sys_bat_backup_ratio) is a derived EMS status that the device
    # clamps internally; it diverges from dev_soc at edge cases (notably
    # 100%, where the EMS caps at 90). The SET path still writes BOTH
    # fields (handled in async_set_powerocean_soc) so the device EMS
    # follows the user value where it can; the slider sources from the
    # user-side mirror so HA matches what the app shows the user.
    EcoFlowNumberDef("solar_surplus_threshold", "Solar Surplus Threshold", "ems_app_surplus_pct", "%", "mdi:solar-power-variant", 0, 100, 5, enhanced_only=True),
]


POWEROCEAN_SELECTS: list[EcoFlowSelectDef] = [
    # Work mode select. Phase 1 scope: SELFUSE (0) and AI_SCHEDULE (12),
    # which are the modes that work without TouParam/BackupParam sub-data.
    EcoFlowSelectDef(
        "work_mode",
        "Work Mode",
        "ems_work_mode",
        ("self_use", "ai_schedule"),
        icon="mdi:cog-transfer",
        enhanced_only=True,
    ),
]


# =====================================================================
# Delta 2 Max sensor definitions (from ha_delta_discovery.py)
# =====================================================================

DELTA2MAX_SENSORS: list[EcoFlowSensorDef] = [
    # --- Battery / SoC ---
    EcoFlowSensorDef("soc", "SoC", "%", "battery", "measurement", "mdi:battery", suggested_display_precision=0),
    EcoFlowSensorDef("bms_soh_pct", "Battery SoH", "%", None, "measurement", "mdi:battery-heart-variant", suggested_display_precision=0),
    EcoFlowSensorDef("bms_precise_soc", "Precise SoC", "%", None, "measurement", "mdi:battery-sync", suggested_display_precision=0),
    # --- Power (W) ---
    EcoFlowSensorDef("watts_in_sum", "Input Total", "W", "power", "measurement", "mdi:flash", suggested_display_precision=0),
    EcoFlowSensorDef("watts_out_sum", "Output Total", "W", "power", "measurement", "mdi:flash", suggested_display_precision=0),
    EcoFlowSensorDef("ac_in_w", "AC Input", "W", "power", "measurement", "mdi:power-plug", suggested_display_precision=0),
    EcoFlowSensorDef("ac_out_w", "AC Output", "W", "power", "measurement", "mdi:power-plug-outline", suggested_display_precision=0),
    EcoFlowSensorDef("solar_in_w", "Solar Input", "W", "power", "measurement", "mdi:solar-power", suggested_display_precision=0),
    EcoFlowSensorDef("solar2_in_w", "Solar 2 Input", "W", "power", "measurement", "mdi:solar-power", suggested_display_precision=0),
    EcoFlowSensorDef("mppt_out_w", "MPPT Output", "W", "power", "measurement", "mdi:solar-panel-large", suggested_display_precision=0),
    EcoFlowSensorDef("car_12v_out_w", "12V Output", "W", "power", "measurement", "mdi:car-battery", suggested_display_precision=0),
    EcoFlowSensorDef("dcdc_12v_w", "DC-DC 12V", "W", "power", "measurement", "mdi:current-dc", suggested_display_precision=0),
    EcoFlowSensorDef("car_out_w", "Car Output", "W", "power", "measurement", "mdi:car-electric", suggested_display_precision=0),
    EcoFlowSensorDef("usb1_w", "USB 1", "W", "power", "measurement", "mdi:usb", suggested_display_precision=0),
    EcoFlowSensorDef("usb2_w", "USB 2", "W", "power", "measurement", "mdi:usb", suggested_display_precision=0),
    EcoFlowSensorDef("usb_qc1_w", "USB QC 1", "W", "power", "measurement", "mdi:usb", suggested_display_precision=0),
    EcoFlowSensorDef("usb_qc2_w", "USB QC 2", "W", "power", "measurement", "mdi:usb", suggested_display_precision=0),
    EcoFlowSensorDef("typec1_w", "Type-C 1", "W", "power", "measurement", "mdi:usb-c-port", suggested_display_precision=0),
    EcoFlowSensorDef("typec2_w", "Type-C 2", "W", "power", "measurement", "mdi:usb-c-port", suggested_display_precision=0),
    EcoFlowSensorDef("ac_chg_rated_power_w", "AC Charge Rated Power", "W", "power", "measurement", "mdi:lightning-bolt", suggested_display_precision=0),
    # --- Voltage (V) ---
    EcoFlowSensorDef("batt_voltage_v", "Battery Voltage", "V", "voltage", "measurement", "mdi:flash-triangle", suggested_display_precision=1),
    EcoFlowSensorDef("ac_out_vol_v", "AC Output Voltage", "V", "voltage", "measurement", "mdi:sine-wave", suggested_display_precision=1),
    EcoFlowSensorDef("ac_in_vol_v", "AC Input Voltage", "V", "voltage", "measurement", "mdi:sine-wave", suggested_display_precision=1),
    EcoFlowSensorDef("dc_in_vol_v", "DC Input Voltage", "V", "voltage", "measurement", "mdi:current-dc", suggested_display_precision=1),
    EcoFlowSensorDef("dcdc_12v_vol_v", "12V Rail Voltage", "V", "voltage", "measurement", "mdi:car-battery", suggested_display_precision=1),
    # --- Current (A) ---
    EcoFlowSensorDef("batt_current_a", "Battery Current", "A", "current", "measurement", "mdi:current-dc", suggested_display_precision=2),
    EcoFlowSensorDef("ac_out_amp_a", "AC Output Current", "A", "current", "measurement", "mdi:current-ac", suggested_display_precision=2),
    EcoFlowSensorDef("solar_in_amp_a", "Solar Current", "A", "current", "measurement", "mdi:solar-power", suggested_display_precision=2),
    EcoFlowSensorDef("solar2_in_amp_a", "Solar 2 Current", "A", "current", "measurement", "mdi:solar-power", suggested_display_precision=2),
    # --- Temperature ---
    EcoFlowSensorDef("batt_temp_c", "Battery Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer", suggested_display_precision=1),
    EcoFlowSensorDef("inv_out_temp_c", "Inverter Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer", suggested_display_precision=1),
    EcoFlowSensorDef("dc_in_temp_c", "DC Input Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer", suggested_display_precision=1),
    EcoFlowSensorDef("mppt_temp_c", "MPPT Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer", suggested_display_precision=1),
    EcoFlowSensorDef("solar2_mppt_temp_c", "MPPT 2 Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer", suggested_display_precision=1),
    EcoFlowSensorDef("batt_max_cell_temp_c", "Max Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-high", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("batt_min_cell_temp_c", "Min Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-low", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("batt_max_mos_temp_c", "Max MOSFET Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-alert", suggested_display_precision=1, disabled_by_default=True),
    # --- Duration ---
    EcoFlowSensorDef("remain_time_min", "Remaining Time", "min", "duration", "measurement", "mdi:timer-sand", suggested_display_precision=0),
    EcoFlowSensorDef("chg_remain_time_min", "Charge Time Remaining", "min", "duration", "measurement", "mdi:battery-clock", suggested_display_precision=0),
    EcoFlowSensorDef("dsg_remain_time_min", "Discharge Time Remaining", "min", "duration", "measurement", "mdi:battery-clock-outline", suggested_display_precision=0),
    # --- Frequency ---
    EcoFlowSensorDef("ac_out_freq_hz", "AC Output Frequency", "Hz", "frequency", "measurement", "mdi:sine-wave", suggested_display_precision=1),
    EcoFlowSensorDef("ac_in_freq_hz", "AC Input Frequency", "Hz", "frequency", "measurement", "mdi:sine-wave", suggested_display_precision=1),
    # --- Capacity ---
    EcoFlowSensorDef("batt_remain_cap_mah", "Remaining Capacity", "mAh", None, "measurement", "mdi:battery-50", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("batt_full_cap_mah", "Full Capacity", "mAh", None, "measurement", "mdi:battery", suggested_display_precision=0, disabled_by_default=True),
    # --- Counters / State ---
    EcoFlowSensorDef("bms_cycles", "Battery Cycles", None, None, "total_increasing", "mdi:counter"),
    EcoFlowSensorDef("fan_level", "Fan Level", None, None, "measurement", "mdi:fan", disabled_by_default=True),
    EcoFlowSensorDef("chg_dsg_state", "Charge/Discharge State", None, "enum", None, "mdi:battery-charging", disabled_by_default=True, options=["idle", "discharging", "charging"]),
    EcoFlowSensorDef("ems_chg_state", "EMS Charge State", None, "enum", None, "mdi:battery-charging-outline", disabled_by_default=True, options=["idle", "charging", "discharging"]),
    EcoFlowSensorDef("charger_type", "Charger Type", None, "enum", None, "mdi:ev-plug-type2", disabled_by_default=True, options=["none", "ac", "solar", "dc", "unknown"]),
    EcoFlowSensorDef("mppt_chg_state", "MPPT Charge State", None, "enum", None, "mdi:solar-panel", disabled_by_default=True, options=["idle", "charging"]),
    EcoFlowSensorDef("ems_lcd_soc", "LCD SoC", "%", None, "measurement", "mdi:monitor", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("ems_precise_soc", "EMS Precise SoC", "%", None, "measurement", "mdi:monitor", suggested_display_precision=0, disabled_by_default=True),
    # --- Energy Dashboard (total_increasing, kWh) ---
    EcoFlowSensorDef("solar_energy_kwh", "Solar Energy", "kWh", "energy", "total_increasing", "mdi:solar-power", suggested_display_precision=2),
    EcoFlowSensorDef("solar2_energy_kwh", "Solar 2 Energy", "kWh", "energy", "total_increasing", "mdi:solar-power", suggested_display_precision=2),
    EcoFlowSensorDef("ac_in_energy_kwh", "AC Input Energy", "kWh", "energy", "total_increasing", "mdi:power-plug", suggested_display_precision=2),
    EcoFlowSensorDef("ac_out_energy_kwh", "AC Output Energy", "kWh", "energy", "total_increasing", "mdi:power-plug-outline", suggested_display_precision=2),
    # --- Cell voltages (diagnostic) ---
    EcoFlowSensorDef("batt_max_cell_vol_mv", "Max Cell Voltage", "mV", "voltage", "measurement", "mdi:flash-triangle-outline", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("batt_min_cell_vol_mv", "Min Cell Voltage", "mV", "voltage", "measurement", "mdi:flash-triangle-outline", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    # --- Error codes (diagnostic) ---
    EcoFlowSensorDef("pd_err_code", "PD Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("inv_err_code", "Inverter Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("bms_err_code", "BMS Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("mppt_fault_code", "MPPT Fault Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
    # --- Slave Battery Pack 1 (expansion) ---
    # No device_class="battery" on slave SoC: HA picks battery-class
    # entities for the device-card header, which must stay the main SoC.
    EcoFlowSensorDef("slave1_soc", "Slave 1 SoC", "%", None, "measurement", "mdi:battery", disabled_by_default=True),
    EcoFlowSensorDef("slave1_soh", "Slave 1 SoH", "%", None, "measurement", "mdi:battery-heart-variant", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("slave1_voltage_v", "Slave 1 Voltage", "V", "voltage", "measurement", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("slave1_current_a", "Slave 1 Current", "A", "current", "measurement", suggested_display_precision=2, disabled_by_default=True),
    EcoFlowSensorDef("slave1_temp_c", "Slave 1 Temp", "\u00b0C", "temperature", "measurement", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("slave1_cycles", "Slave 1 Cycles", None, None, "total_increasing", "mdi:battery-sync", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("slave1_in_w", "Slave 1 Input", "W", "power", "measurement", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("slave1_out_w", "Slave 1 Output", "W", "power", "measurement", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("slave1_remain_cap_mah", "Slave 1 Remaining Capacity", "mAh", None, "measurement", "mdi:battery-outline", "diagnostic", disabled_by_default=True, suggested_display_precision=0),
    EcoFlowSensorDef("slave1_full_cap_mah", "Slave 1 Full Capacity", "mAh", None, "measurement", "mdi:battery", "diagnostic", disabled_by_default=True, suggested_display_precision=0),
    EcoFlowSensorDef("slave1_max_cell_vol_mv", "Slave 1 Max Cell Voltage", "mV", "voltage", "measurement", "mdi:flash-triangle-outline", "diagnostic", disabled_by_default=True, suggested_display_precision=0),
    EcoFlowSensorDef("slave1_min_cell_vol_mv", "Slave 1 Min Cell Voltage", "mV", "voltage", "measurement", "mdi:flash-triangle-outline", "diagnostic", disabled_by_default=True, suggested_display_precision=0),
    EcoFlowSensorDef("slave1_max_cell_temp_c", "Slave 1 Max Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-high", "diagnostic", disabled_by_default=True, suggested_display_precision=1),
    EcoFlowSensorDef("slave1_min_cell_temp_c", "Slave 1 Min Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-low", "diagnostic", disabled_by_default=True, suggested_display_precision=1),
    EcoFlowSensorDef("slave1_max_mos_temp_c", "Slave 1 Max MOSFET Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-alert", "diagnostic", disabled_by_default=True, suggested_display_precision=1),
    EcoFlowSensorDef("slave1_err_code", "Slave 1 Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
    # --- Slave Battery Pack 2 (expansion) ---
    EcoFlowSensorDef("slave2_soc", "Slave 2 SoC", "%", None, "measurement", "mdi:battery", disabled_by_default=True),
    EcoFlowSensorDef("slave2_soh", "Slave 2 SoH", "%", None, "measurement", "mdi:battery-heart-variant", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("slave2_voltage_v", "Slave 2 Voltage", "V", "voltage", "measurement", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("slave2_current_a", "Slave 2 Current", "A", "current", "measurement", suggested_display_precision=2, disabled_by_default=True),
    EcoFlowSensorDef("slave2_temp_c", "Slave 2 Temp", "\u00b0C", "temperature", "measurement", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("slave2_cycles", "Slave 2 Cycles", None, None, "total_increasing", "mdi:battery-sync", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("slave2_in_w", "Slave 2 Input", "W", "power", "measurement", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("slave2_out_w", "Slave 2 Output", "W", "power", "measurement", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("slave2_remain_cap_mah", "Slave 2 Remaining Capacity", "mAh", None, "measurement", "mdi:battery-outline", "diagnostic", disabled_by_default=True, suggested_display_precision=0),
    EcoFlowSensorDef("slave2_full_cap_mah", "Slave 2 Full Capacity", "mAh", None, "measurement", "mdi:battery", "diagnostic", disabled_by_default=True, suggested_display_precision=0),
    EcoFlowSensorDef("slave2_max_cell_vol_mv", "Slave 2 Max Cell Voltage", "mV", "voltage", "measurement", "mdi:flash-triangle-outline", "diagnostic", disabled_by_default=True, suggested_display_precision=0),
    EcoFlowSensorDef("slave2_min_cell_vol_mv", "Slave 2 Min Cell Voltage", "mV", "voltage", "measurement", "mdi:flash-triangle-outline", "diagnostic", disabled_by_default=True, suggested_display_precision=0),
    EcoFlowSensorDef("slave2_max_cell_temp_c", "Slave 2 Max Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-high", "diagnostic", disabled_by_default=True, suggested_display_precision=1),
    EcoFlowSensorDef("slave2_min_cell_temp_c", "Slave 2 Min Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-low", "diagnostic", disabled_by_default=True, suggested_display_precision=1),
    EcoFlowSensorDef("slave2_max_mos_temp_c", "Slave 2 Max MOSFET Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-alert", "diagnostic", disabled_by_default=True, suggested_display_precision=1),
    EcoFlowSensorDef("slave2_err_code", "Slave 2 Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
]

DELTA2MAX_BINARY_SENSORS: list[EcoFlowBinarySensorDef] = [
    EcoFlowBinarySensorDef("ac_enabled", "AC Enabled", "power", "mdi:power-plug"),
    EcoFlowBinarySensorDef("dc_out_enabled", "DC Output Enabled", "power", "mdi:flash"),
    EcoFlowBinarySensorDef("car_12v_enabled", "12V Enabled", "power", "mdi:car-battery"),
    EcoFlowBinarySensorDef("ups_enabled", "UPS Enabled", "power", "mdi:lightning-bolt", "diagnostic", disabled_by_default=True),
]

DELTA2MAX_SWITCHES: list[EcoFlowSwitchDef] = [
    EcoFlowSwitchDef("ac_switch", "AC Output", "ac_enabled", "mdi:power-plug"),
    EcoFlowSwitchDef("dc_switch", "DC Output", "dc_out_enabled", "mdi:flash"),
    EcoFlowSwitchDef("car_12v_switch", "12V Output", "car_12v_enabled", "mdi:car-battery"),
    EcoFlowSwitchDef("beeper_switch", "Beeper", "beep_enabled", "mdi:volume-high"),
    EcoFlowSwitchDef("xboost_switch", "X-Boost", "ac_xboost", "mdi:lightning-bolt"),
    EcoFlowSwitchDef("ac_auto_on_switch", "AC Auto Restart", "ac_auto_on", "mdi:power-plug"),
    EcoFlowSwitchDef("backup_reserve_switch", "Backup Reserve", "backup_reserve_enabled", "mdi:battery-lock"),
]

DELTA2MAX_NUMBERS: list[EcoFlowNumberDef] = [
    EcoFlowNumberDef("ac_charge_speed", "AC Charge Speed", "ac_slow_chg_watts", "W", "mdi:lightning-bolt", 200, 2400, 100),
    EcoFlowNumberDef("max_charge_soc", "Max Charge SoC", "max_charge_soc", "%", "mdi:battery-charging-100", 50, 100, 1),
    EcoFlowNumberDef("min_discharge_soc", "Min Discharge SoC", "min_discharge_soc", "%", "mdi:battery-alert-variant-outline", 0, 30, 1),
    EcoFlowNumberDef("standby_timeout", "Standby Timeout", "standby_timeout_min", "min", "mdi:timer-off-outline", 0, 720, 1),
    EcoFlowNumberDef("car_standby_timeout", "12V Port Timeout", "car_standby_min", "min", "mdi:timer-outline", 0, 720, 30),
    EcoFlowNumberDef("screen_brightness", "Screen Brightness", "screen_brightness", "%", "mdi:brightness-6", 0, 100, 10),
    EcoFlowNumberDef("screen_timeout", "Screen Timeout", "screen_timeout_sec", "s", "mdi:monitor-off", 0, 1800, 10),
    EcoFlowNumberDef("backup_reserve_soc", "Backup Reserve Level", "backup_reserve_soc", "%", "mdi:battery-lock", 5, 100, 5),
]


# =====================================================================
# Smart Plug sensor definitions
# =====================================================================

SMARTPLUG_SENSORS: list[EcoFlowSensorDef] = [
    EcoFlowSensorDef("power_w", "Power", "W", "power", "measurement", "mdi:flash", suggested_display_precision=0),
    EcoFlowSensorDef("current_a", "Current", "A", "current", "measurement", "mdi:current-ac", suggested_display_precision=2),
    EcoFlowSensorDef("voltage_v", "Voltage", "V", "voltage", "measurement", "mdi:sine-wave", suggested_display_precision=1),
    EcoFlowSensorDef("frequency_hz", "Frequency", "Hz", "frequency", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=1),
    EcoFlowSensorDef("temperature_c", "Temperature", "\u00b0C", "temperature", "measurement", "mdi:thermometer", "diagnostic", suggested_display_precision=1),
    EcoFlowSensorDef("max_power_w", "Max Power Rating", "W", "power", None, "mdi:flash-alert", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("max_current_a", "Max Current Rating", "A", "current", None, "mdi:current-ac", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
    EcoFlowSensorDef("led_brightness", "LED Brightness", "%", None, "measurement", "mdi:brightness-6", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("error_code", "Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("warning_code", "Warning Code", None, None, None, "mdi:alert-outline", "diagnostic", disabled_by_default=True),
    # --- Energy Dashboard (total_increasing, kWh) ---
    EcoFlowSensorDef("energy_kwh", "Energy", "kWh", "energy", "total_increasing", "mdi:flash", suggested_display_precision=2),
]

SMARTPLUG_BINARY_SENSORS: list[EcoFlowBinarySensorDef] = [
    EcoFlowBinarySensorDef("switch_state", "Relay", "power", "mdi:power-plug"),
]

SMARTPLUG_SWITCHES: list[EcoFlowSwitchDef] = [
    EcoFlowSwitchDef("plug_switch", "Plug", "switch_state", "mdi:power-plug"),
]

STREAM_BINARY_SENSORS: list[EcoFlowBinarySensorDef] = [
    EcoFlowBinarySensorDef("ac_outlet_1_enabled", "AC Outlet 1", "power", "mdi:power-socket-eu"),
    EcoFlowBinarySensorDef("ac_outlet_2_enabled", "AC Outlet 2", "power", "mdi:power-socket-eu"),
]

STREAM_SWITCHES: list[EcoFlowSwitchDef] = []

SMARTPLUG_NUMBERS: list[EcoFlowNumberDef] = [
    EcoFlowNumberDef("led_brightness", "LED Brightness", "led_brightness", "%", "mdi:brightness-6", 0, 100, 5),
    EcoFlowNumberDef("max_watts", "Max Power Limit", "max_power_w", "W", "mdi:flash-alert", 0, 2500, 100),
]

STREAM_NUMBERS: list[EcoFlowNumberDef] = [
    EcoFlowNumberDef("backup_reserve", "Backup Reserve", "backup_reserve_pct", "%", "mdi:battery-lock", 3, 95, 1, enhanced_only=True),
]


# =====================================================================
# Stream AC Pro sensor definitions
# =====================================================================

STREAM_SENSORS: list[EcoFlowSensorDef] = [
    EcoFlowSensorDef("soc_pct", "Battery SOC", "%", "battery", "measurement", "mdi:battery", suggested_display_precision=0),
    EcoFlowSensorDef("soc_precise_pct", "Battery SOC (Precise)", "%", None, "measurement", "mdi:battery-sync", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("solar_w", "Solar Power", "W", "power", "measurement", "mdi:solar-power", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    # Per-string PV. Standard mode reports these individually (powGetPv..
    # powGetPv4); the protobuf stream only carries the sum. Strings 3 and 4 are
    # off by default because only the larger units populate them.
    EcoFlowSensorDef("pv1_w", "PV 1 Power", "W", "power", "measurement", "mdi:solar-power-variant", suggested_display_precision=0),
    EcoFlowSensorDef("pv2_w", "PV 2 Power", "W", "power", "measurement", "mdi:solar-power-variant", suggested_display_precision=0),
    EcoFlowSensorDef("pv3_w", "PV 3 Power", "W", "power", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("pv4_w", "PV 4 Power", "W", "power", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    # Per-string PV input voltage and current. The key naming follows the
    # vendor's own asymmetry (plugInInfoPvVol / plugInInfoPv2Vol): the first
    # string has no index, so the existing pv_voltage_v key stays as it is
    # and renaming it would orphan the entity for current users.
    EcoFlowSensorDef("pv_voltage_v", "PV Voltage", "V", "voltage", "measurement", "mdi:flash", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("pv_current_a", "PV Current", "A", "current", "measurement", "mdi:current-dc", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
    EcoFlowSensorDef("pv2_voltage_v", "PV 2 Voltage", "V", "voltage", "measurement", "mdi:flash", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("pv2_current_a", "PV 2 Current", "A", "current", "measurement", "mdi:current-dc", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
    EcoFlowSensorDef("home_from_solar_w", "Home From Solar", "W", "power", "measurement", "mdi:home-lightning-bolt-outline", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("home_w", "Home Power", "W", "power", "measurement", "mdi:home-lightning-bolt", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("grid_w", "Grid Power", "W", "power", "measurement", "mdi:transmission-tower", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("batt_w", "Battery Power", "W", "power", "measurement", "mdi:battery", suggested_display_precision=0),
    EcoFlowSensorDef("batt_charge_power_w", "Battery Charge Power", "W", "power", "measurement", "mdi:battery-charging", suggested_display_precision=0),
    EcoFlowSensorDef("batt_discharge_power_w", "Battery Discharge Power", "W", "power", "measurement", "mdi:battery", suggested_display_precision=0),
    EcoFlowSensorDef("ac_grid_connection_power_w", "AC Grid Connection Power", "W", "power", "measurement", "mdi:transmission-tower", suggested_display_precision=0),
    EcoFlowSensorDef("solar_energy_kwh", "Solar Energy", "kWh", "energy", "total_increasing", "mdi:solar-power", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
    EcoFlowSensorDef("home_energy_kwh", "Home Energy", "kWh", "energy", "total_increasing", "mdi:home-lightning-bolt", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
    # Per-string PV energy, Riemann-integrated from pv1_w..pv4_w. All four are
    # off by default: `solar_energy_kwh` already covers the PV total, and a
    # dashboard that sums the enabled strings would silently under-report on a
    # unit whose higher strings are disabled. Users who want per-string
    # tracking enable exactly the strings their unit has.
    EcoFlowSensorDef("pv1_energy_kwh", "PV 1 Energy", "kWh", "energy", "total_increasing", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
    EcoFlowSensorDef("pv2_energy_kwh", "PV 2 Energy", "kWh", "energy", "total_increasing", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
    EcoFlowSensorDef("pv3_energy_kwh", "PV 3 Energy", "kWh", "energy", "total_increasing", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
    EcoFlowSensorDef("pv4_energy_kwh", "PV 4 Energy", "kWh", "energy", "total_increasing", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
    EcoFlowSensorDef("batt_charge_energy_kwh", "Battery Charge Energy", "kWh", "energy", "total_increasing", "mdi:battery-charging", suggested_display_precision=2),
    EcoFlowSensorDef("batt_discharge_energy_kwh", "Battery Discharge Energy", "kWh", "energy", "total_increasing", "mdi:battery", suggested_display_precision=2),
    EcoFlowSensorDef("home_from_batt_w", "Home From Battery", "W", "power", "measurement", "mdi:home-battery-outline", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("home_from_grid_w", "Home From Grid", "W", "power", "measurement", "mdi:home-import-outline", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("ac_outlet_1_w", "AC Outlet 1 Power", "W", "power", "measurement", "mdi:power-socket-eu", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("ac_outlet_2_w", "AC Outlet 2 Power", "W", "power", "measurement", "mdi:power-socket-eu", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("led_brightness", "LED Brightness", "%", None, "measurement", "mdi:brightness-6", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("grid_connection_power_w", "Grid Connection Power", "W", "power", "measurement", "mdi:transmission-tower", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("sys_grid_connection_power_w", "System Grid Connection Power", "W", "power", "measurement", "mdi:transmission-tower-export", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bms_soh_pct", "Battery SoH", "%", None, "measurement", "mdi:battery-heart-variant", suggested_display_precision=0),
    EcoFlowSensorDef("batt_voltage_v", "Battery Voltage", "V", "voltage", "measurement", "mdi:flash-triangle", suggested_display_precision=1),
    EcoFlowSensorDef("batt_temp_c", "Battery Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer", suggested_display_precision=1),
    EcoFlowSensorDef("batt_design_cap_mah", "Design Capacity", "mAh", None, "measurement", "mdi:battery", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("batt_remain_cap_mah", "Remaining Capacity", "mAh", None, "measurement", "mdi:battery-50", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("batt_full_cap_mah", "Full Capacity", "mAh", None, "measurement", "mdi:battery", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("batt_charge_capacity_ah", "Battery Charge Capacity", "Ah", None, "total_increasing", "mdi:battery-plus", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
    EcoFlowSensorDef("batt_discharge_capacity_ah", "Battery Discharge Capacity", "Ah", None, "total_increasing", "mdi:battery-minus", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
    EcoFlowSensorDef("ac_voltage_v", "AC Voltage", "V", "voltage", "measurement", "mdi:sine-wave", suggested_display_precision=1),
    EcoFlowSensorDef("ac_current_a", "AC Current", "A", "current", "measurement", "mdi:current-ac", suggested_display_precision=2),
    EcoFlowSensorDef("ac_frequency_hz", "AC Frequency", "Hz", "frequency", "measurement", "mdi:sine-wave", suggested_display_precision=2),
    # Grid tie state and the configured feed-in cap. Both are read-only here:
    # the cap is changed in the vendor app, we only report what it is set to.
    EcoFlowSensorDef("grid_connection_state", "Grid Connection State", None, "enum", None, "mdi:transmission-tower", "diagnostic", options=["invalid", "grid_in", "not_online", "feed_grid"]),
    EcoFlowSensorDef("feed_grid_power_limit_w", "Feed-in Power Limit", "W", "power", "measurement", "mdi:transmission-tower-export", "diagnostic", suggested_display_precision=0),
    EcoFlowSensorDef("wifi_rssi_dbm", "WiFi Signal", "dBm", "signal_strength", "measurement", "mdi:wifi", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("batt_max_cell_temp_c", "Max Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-high", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("batt_min_cell_temp_c", "Min Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-low", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("batt_max_mos_temp_c", "Max MOSFET Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-alert", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("batt_max_cell_vol_mv", "Max Cell Voltage", "mV", "voltage", "measurement", "mdi:flash-triangle-outline", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("batt_min_cell_vol_mv", "Min Cell Voltage", "mV", "voltage", "measurement", "mdi:flash-triangle-outline", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("backup_reserve_pct", "Backup Reserve", "%", None, "measurement", "mdi:battery-lock", "diagnostic", suggested_display_precision=0),
    EcoFlowSensorDef("batt_charge_discharge_state", "Battery Charge/Discharge State", None, "enum", None, "mdi:battery-sync", "diagnostic", disabled_by_default=True, options=["standby", "discharging", "charging"]),
]


# =====================================================================
# STREAM AC 5000 (ES22) sensor definitions
# =====================================================================

# The list name must stay a single run of A-Z0-9 before the suffix.
# test_entity_translations.py, test_const.py and _collect_total_increasing_keys
# in mqtt_ingest.py all discover these lists with `[A-Z0-9]+_SENSORS`, which
# cannot span an underscore. Named STREAM_AC5000_SENSORS it would be skipped
# by every one of them, including the monotonic guard, and no test would fail.
STREAMAC5000_SENSORS: list[EcoFlowSensorDef] = [
    EcoFlowSensorDef("soc_pct", "Battery SOC", "%", "battery", "measurement", "mdi:battery", suggested_display_precision=0),
    EcoFlowSensorDef("soc_precise_pct", "Battery SOC (Precise)", "%", None, "measurement", "mdi:battery-sync", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    # The BMS pack reading, about two points above the system SoC above. Keyed
    # apart from the Delta family's `bms_precise_soc`, which is the same
    # reading under a key that predates the `_pct` convention every other
    # percentage here follows.
    EcoFlowSensorDef("bms_soc_precise_pct", "BMS SoC", "%", None, "measurement", "mdi:battery-heart-outline", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("bms_soh_pct", "Battery SoH", "%", None, "measurement", "mdi:battery-heart-variant", suggested_display_precision=0),
    # --- battery power ---
    EcoFlowSensorDef("batt_w", "Battery Power", "W", "power", "measurement", "mdi:battery", suggested_display_precision=0),
    EcoFlowSensorDef("batt_charge_power_w", "Battery Charge Power", "W", "power", "measurement", "mdi:battery-charging", suggested_display_precision=0),
    EcoFlowSensorDef("batt_discharge_power_w", "Battery Discharge Power", "W", "power", "measurement", "mdi:battery", suggested_display_precision=0),
    EcoFlowSensorDef("batt_charge_discharge_state", "Battery Charge/Discharge State", None, "enum", None, "mdi:battery-sync", "diagnostic", disabled_by_default=True, options=["standby", "discharging", "charging"]),
    # --- power flow ---
    EcoFlowSensorDef("home_w", "Home Power", "W", "power", "measurement", "mdi:home-lightning-bolt", suggested_display_precision=0),
    # Signed: positive draws from the grid, negative feeds into it. Present
    # only while a smart meter is linked in the EcoFlow app.
    EcoFlowSensorDef("grid_w", "Grid Power", "W", "power", "measurement", "mdi:transmission-tower", suggested_display_precision=0),
    EcoFlowSensorDef("grid_import_power_w", "Grid Import Power", "W", "power", "measurement", "mdi:transmission-tower-import", suggested_display_precision=0),
    EcoFlowSensorDef("grid_export_power_w", "Grid Export Power", "W", "power", "measurement", "mdi:transmission-tower-export", suggested_display_precision=0),
    EcoFlowSensorDef("home_from_batt_w", "Home From Battery", "W", "power", "measurement", "mdi:home-battery-outline", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("home_from_grid_w", "Home From Grid", "W", "power", "measurement", "mdi:home-import-outline", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    # Solar is accessory-gated rather than listed per prefix: whether a unit
    # has PV on the EcoFlow itself is a wiring choice, not a model difference.
    EcoFlowSensorDef("solar_w", "Solar Power", "W", "power", "measurement", "mdi:solar-power", suggested_display_precision=0, accessory=True),
    EcoFlowSensorDef("home_from_solar_w", "Home From Solar", "W", "power", "measurement", "mdi:home-lightning-bolt-outline", "diagnostic", suggested_display_precision=0, disabled_by_default=True, accessory=True),
    # --- smart meter, EcoFlow P1 variant only ---
    # A Tibber Pulse reports a single total, so these stay absent there.
    EcoFlowSensorDef("grid_phase_a_active_power_w", "Grid Phase A Power", "W", "power", "measurement", "mdi:transmission-tower", "diagnostic", suggested_display_precision=0, disabled_by_default=True, accessory=True),
    EcoFlowSensorDef("grid_phase_b_active_power_w", "Grid Phase B Power", "W", "power", "measurement", "mdi:transmission-tower", "diagnostic", suggested_display_precision=0, disabled_by_default=True, accessory=True),
    EcoFlowSensorDef("grid_phase_c_active_power_w", "Grid Phase C Power", "W", "power", "measurement", "mdi:transmission-tower", "diagnostic", suggested_display_precision=0, disabled_by_default=True, accessory=True),
    EcoFlowSensorDef("grid_phase_a_voltage_v", "Grid Phase A Voltage", "V", "voltage", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=1, disabled_by_default=True, accessory=True),
    EcoFlowSensorDef("grid_phase_b_voltage_v", "Grid Phase B Voltage", "V", "voltage", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=1, disabled_by_default=True, accessory=True),
    EcoFlowSensorDef("grid_phase_c_voltage_v", "Grid Phase C Voltage", "V", "voltage", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=1, disabled_by_default=True, accessory=True),
    EcoFlowSensorDef("grid_phase_a_current_a", "Grid Phase A Current", "A", "current", "measurement", "mdi:current-ac", "diagnostic", suggested_display_precision=2, disabled_by_default=True, accessory=True),
    EcoFlowSensorDef("grid_phase_b_current_a", "Grid Phase B Current", "A", "current", "measurement", "mdi:current-ac", "diagnostic", suggested_display_precision=2, disabled_by_default=True, accessory=True),
    EcoFlowSensorDef("grid_phase_c_current_a", "Grid Phase C Current", "A", "current", "measurement", "mdi:current-ac", "diagnostic", suggested_display_precision=2, disabled_by_default=True, accessory=True),
    EcoFlowSensorDef("ac_frequency_hz", "AC Frequency", "Hz", "frequency", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=2, disabled_by_default=True, accessory=True),
    # --- configuration readback ---
    EcoFlowSensorDef("work_mode", "Work Mode", None, "enum", None, "mdi:cog-outline", "diagnostic", options=["self_powered", "intelligent_plus", "custom"]),
    EcoFlowSensorDef("max_charge_soc_pct", "Charge Limit", "%", None, "measurement", "mdi:battery-charging-high", "diagnostic", suggested_display_precision=0),
    EcoFlowSensorDef("min_discharge_soc_pct", "Discharge Limit", "%", None, "measurement", "mdi:battery-arrow-down", "diagnostic", suggested_display_precision=0),
    EcoFlowSensorDef("backup_reserve_pct", "Backup Reserve", "%", None, "measurement", "mdi:battery-lock", "diagnostic", suggested_display_precision=0),
    # The two power limits, named as the app names them. The output limit is
    # an account-level ceiling and raising it needs a request to EcoFlow; the
    # input limit is an ordinary setting in the app. A task power above either
    # is accepted and then clamped.
    EcoFlowSensorDef("max_grid_output_power_w", "Max Grid-tied Output Power", "W", "power", "measurement", "mdi:transmission-tower-export", "diagnostic", suggested_display_precision=0),
    EcoFlowSensorDef("max_grid_input_power_w", "Max Grid Input Power", "W", "power", "measurement", "mdi:transmission-tower-import", "diagnostic", suggested_display_precision=0),
    EcoFlowSensorDef("scheduled_discharge_power_w", "Scheduled Discharge Power", "W", "power", "measurement", "mdi:calendar-clock", "diagnostic", suggested_display_precision=0),
    EcoFlowSensorDef("scheduled_charge_power_w", "Scheduled Charge Power", "W", "power", "measurement", "mdi:calendar-clock", "diagnostic", suggested_display_precision=0),
    # The charge task's own SoC target, shown in the app as "Charge limit".
    # Exposed because a power write has to preserve it, so it is worth being
    # able to see what will be preserved.
    EcoFlowSensorDef("scheduled_charge_soc_target", "Scheduled Charge Target SoC", "%", None, "measurement", "mdi:battery-clock", "diagnostic", suggested_display_precision=0),
    # --- battery diagnostics ---
    EcoFlowSensorDef("batt_voltage_v", "Battery Voltage", "V", "voltage", "measurement", "mdi:flash-triangle", suggested_display_precision=1),
    EcoFlowSensorDef("bms_current_a", "Battery Current", "A", "current", "measurement", "mdi:current-dc", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
    EcoFlowSensorDef("batt_temp_c", "Battery Temp", "°C", "temperature", "measurement", "mdi:thermometer", suggested_display_precision=1),
    EcoFlowSensorDef("batt_max_cell_temp_c", "Max Cell Temp", "°C", "temperature", "measurement", "mdi:thermometer-high", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("batt_min_cell_temp_c", "Min Cell Temp", "°C", "temperature", "measurement", "mdi:thermometer-low", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("batt_max_mos_temp_c", "Max MOSFET Temp", "°C", "temperature", "measurement", "mdi:thermometer-alert", "diagnostic", suggested_display_precision=1, disabled_by_default=True),
    EcoFlowSensorDef("batt_max_cell_vol_mv", "Max Cell Voltage", "mV", "voltage", "measurement", "mdi:flash-triangle-outline", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("batt_min_cell_vol_mv", "Min Cell Voltage", "mV", "voltage", "measurement", "mdi:flash-triangle-outline", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("batt_design_cap_mah", "Design Capacity", "mAh", None, "measurement", "mdi:battery", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("batt_full_cap_mah", "Full Capacity", "mAh", None, "measurement", "mdi:battery", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("batt_remain_cap_mah", "Remaining Capacity", "mAh", None, "measurement", "mdi:battery-50", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    # --- Energy Dashboard (total_increasing, kWh) ---
    EcoFlowSensorDef("batt_charge_energy_kwh", "Battery Charge Energy", "kWh", "energy", "total_increasing", "mdi:battery-charging", suggested_display_precision=2),
    EcoFlowSensorDef("batt_discharge_energy_kwh", "Battery Discharge Energy", "kWh", "energy", "total_increasing", "mdi:battery", suggested_display_precision=2),
    EcoFlowSensorDef("grid_import_energy_kwh", "Grid Import Energy", "kWh", "energy", "total_increasing", "mdi:transmission-tower-import", suggested_display_precision=2),
    EcoFlowSensorDef("grid_export_energy_kwh", "Grid Export Energy", "kWh", "energy", "total_increasing", "mdi:transmission-tower-export", suggested_display_precision=2),
    EcoFlowSensorDef("home_energy_kwh", "Home Energy", "kWh", "energy", "total_increasing", "mdi:home-lightning-bolt", "diagnostic", suggested_display_precision=2, disabled_by_default=True),
    # No solar energy counter. This device derives its solar figure from the
    # house flows and reports one on a unit with no PV wired to it at all, so
    # integrating it would credit the Energy Dashboard with production that
    # never happened. A total_increasing counter only goes up and cannot be
    # corrected afterwards, while the instantaneous `solar_w` reading carries
    # the same information and can simply be ignored.
]

STREAMAC5000_BINARY_SENSORS: list[EcoFlowBinarySensorDef] = [
    EcoFlowBinarySensorDef("backup_reserve_enabled", "Backup Reserve", None, "mdi:battery-lock", "diagnostic"),
    EcoFlowBinarySensorDef("backup_socket_enabled", "Backup Socket", None, "mdi:power-socket-eu", "diagnostic"),
]


# The Stream Micro (BK01) is a grid-tie PV inverter: two PV strings, one
# single-phase grid connection, no battery, no AC outlets. It speaks the same
# wire format as the rest of the BK series and therefore shares the parser and
# the entity lists, so the keys it never produces are filtered out here rather
# than by forking a second device type.
#
# The list is deliberately generous. Home Assistant keeps an entity in the
# registry after a later fix removes it, so a wrongly created entity is
# permanent for that owner, while a wrongly omitted one is added back in the
# next release without breaking anything.
STREAM_MICRO_EXCLUDED_KEYS: frozenset[str] = frozenset({
    # Battery block: the periodic full telemetry upload carries none of the
    # battery fields the protocol defines in that same message.
    "soc_pct",
    "soc_precise_pct",
    "bms_soh_pct",
    "batt_w",
    "batt_charge_power_w",
    "batt_discharge_power_w",
    "batt_charge_energy_kwh",
    "batt_discharge_energy_kwh",
    "batt_voltage_v",
    "batt_temp_c",
    "batt_design_cap_mah",
    "batt_remain_cap_mah",
    "batt_full_cap_mah",
    "batt_charge_capacity_ah",
    "batt_discharge_capacity_ah",
    "batt_max_cell_vol_mv",
    "batt_min_cell_vol_mv",
    "batt_max_cell_temp_c",
    "batt_min_cell_temp_c",
    "batt_max_mos_temp_c",
    "batt_charge_discharge_state",
    # The SoC limits (fields 270/271) are parsed but have no Stream entity of
    # their own, so there is nothing to exclude for them here.
    # Backup reserve is a battery control: the read-only sensor and the
    # number entity that writes it.
    "backup_reserve_pct",
    "backup_reserve",
    # AC outlets: this unit has no sockets to switch or meter.
    "ac_outlet_1_enabled",
    "ac_outlet_2_enabled",
    "ac_outlet_1_w",
    "ac_outlet_2_w",
    # System load/grid paths. A microinverter reports what it feeds in, not
    # a house energy balance, and none of these appear in its uploads.
    "grid_w",
    "home_w",
    "home_energy_kwh",
    "home_from_batt_w",
    "home_from_grid_w",
    "home_from_solar_w",
    "sys_grid_connection_power_w",
    # The meter-dependent solar total belongs to that same system path and is
    # absent from both full uploads. This unit reports its PV through the two
    # per-string readings instead, which it does keep. Its energy counter is
    # integrated from the power reading, so leaving the counter in would put a
    # lifetime total on the device that can never move off zero.
    "solar_w",
    "solar_energy_kwh",
})

# Port priority exists on part of the Delta 3 family only. The app shows the
# menu entry when the serial starts with D3M or D51 and hides it otherwise, so
# a base DELTA 3 (P231) and a P321 never get it - the entities would be created
# and stay empty forever.
#
# The table below is a denylist, which means a Delta 3 prefix added later gets
# port priority by default - the wrong default for exactly this feature. A test
# pins every non-D3M Delta 3 prefix to this set, so adding a prefix to the
# device-type map without deciding here fails CI rather than shipping entities
# that can never fill.
DELTA3_PORT_PRIORITY_KEYS: frozenset[str] = frozenset(
    {
        "port_priority_ac1_switch",
        "port_priority_ac2_switch",
        "port_priority_dc_switch",
        "port_priority_ac1_soc",
        "port_priority_ac2_soc",
        "port_priority_dc_soc",
        "port_priority_ac1_limited",
        "port_priority_ac2_limited",
        "port_priority_dc_limited",
        "port_priority_ac1_cutoff_soc",
        "port_priority_ac2_cutoff_soc",
        "port_priority_dc_cutoff_soc",
        "port_priority_active",
    }
)

# Serial prefix -> entity keys that variant never produces. A prefix absent
# from this table gets the full entity list of its device type.
_SN_PREFIX_EXCLUDED_KEYS: dict[str, frozenset[str]] = {
    "BK01": STREAM_MICRO_EXCLUDED_KEYS,
    "P321": DELTA3_PORT_PRIORITY_KEYS,
    "P231": DELTA3_PORT_PRIORITY_KEYS,
}


# Any entity definition carrying a ``key`` attribute (sensor, binary sensor,
# number, switch, select).
_DefT = TypeVar("_DefT")


def excluded_keys_for_serial(device_sn: str) -> frozenset[str]:
    """Return the entity keys the device behind ``device_sn`` never produces."""
    if not device_sn:
        return frozenset()
    return _SN_PREFIX_EXCLUDED_KEYS.get(device_sn[:4].upper(), frozenset())


def filter_defs_for_serial(definitions: list[_DefT], device_sn: str) -> list[_DefT]:
    """Drop entity definitions a device variant cannot ever populate.

    Applied by the sensor, binary sensor, number, switch and select platforms
    next to the ``enhanced_only`` filter. Number, switch and select
    definitions read their value from ``state_key``, so both that and ``key``
    are matched against the exclusion set.
    """
    excluded = excluded_keys_for_serial(device_sn)
    if not excluded:
        return list(definitions)
    return [
        definition
        for definition in definitions
        if definition.key not in excluded
        and getattr(definition, "state_key", None) not in excluded
    ]


# =====================================================================
# Delta 3 Max Plus sensor definitions
# =====================================================================
#
# Telemetry arrives via HTTP quota (Standard mode) or protobuf push
# (Enhanced mode); both paths produce identical sensor keys. Switches and
# numbers are defined further below and require Standard mode, because the
# SET commands go through the official HTTP endpoint. The quota exposes no
# native energy counters, so the kWh Energy Dashboard sensors are derived
# from the live power keys via Riemann-sum integration (see
# DELTA3_POWER_TO_ENERGY and the delta3_http.py docstring).

DELTA3_SENSORS: list[EcoFlowSensorDef] = [
    # --- Battery / SoC ---
    EcoFlowSensorDef("cms_batt_soc", "SoC", "%", "battery", "measurement", "mdi:battery", suggested_display_precision=0),
    # --- Power (W) ---
    EcoFlowSensorDef("pow_in_sum_w", "Input Total", "W", "power", "measurement", "mdi:flash", suggested_display_precision=0),
    EcoFlowSensorDef("pow_out_sum_w", "Output Total", "W", "power", "measurement", "mdi:flash", suggested_display_precision=0),
    EcoFlowSensorDef("ac_in_w", "AC Input", "W", "power", "measurement", "mdi:power-plug", suggested_display_precision=0),
    EcoFlowSensorDef("pv1_in_w", "Solar Input 1", "W", "power", "measurement", "mdi:solar-power", suggested_display_precision=0),
    EcoFlowSensorDef("pv2_in_w", "Solar Input 2", "W", "power", "measurement", "mdi:solar-power", suggested_display_precision=0),
    EcoFlowSensorDef("dc_12v_out_w", "12V Output", "W", "power", "measurement", "mdi:car-battery", suggested_display_precision=0),
    EcoFlowSensorDef("anderson_out_w", "Anderson Output", "W", "power", "measurement", "mdi:power-plug-outline", suggested_display_precision=0),
    EcoFlowSensorDef("ac1_out_w", "AC Output 1", "W", "power", "measurement", "mdi:power-plug-outline", suggested_display_precision=0),
    EcoFlowSensorDef("ac2_out_w", "AC Output 2", "W", "power", "measurement", "mdi:power-plug-outline", suggested_display_precision=0),
    EcoFlowSensorDef("typec1_w", "Type-C 1", "W", "power", "measurement", "mdi:usb-c-port", suggested_display_precision=0),
    EcoFlowSensorDef("typec2_w", "Type-C 2", "W", "power", "measurement", "mdi:usb-c-port", suggested_display_precision=0),
    EcoFlowSensorDef("typec3_w", "Type-C 3", "W", "power", "measurement", "mdi:usb-c-port", suggested_display_precision=0),
    EcoFlowSensorDef("usb_qc1_w", "USB QC 1", "W", "power", "measurement", "mdi:usb", suggested_display_precision=0),
    EcoFlowSensorDef("usb_qc2_w", "USB QC 2", "W", "power", "measurement", "mdi:usb", suggested_display_precision=0),
    # --- Energy Dashboard (total_increasing, kWh; derived via Riemann integration) ---
    EcoFlowSensorDef("solar_energy_kwh", "Solar Energy", "kWh", "energy", "total_increasing", "mdi:solar-power", suggested_display_precision=2),
    EcoFlowSensorDef("solar2_energy_kwh", "Solar 2 Energy", "kWh", "energy", "total_increasing", "mdi:solar-power", suggested_display_precision=2),
    EcoFlowSensorDef("ac_in_energy_kwh", "AC Input Energy", "kWh", "energy", "total_increasing", "mdi:power-plug", suggested_display_precision=2),
    EcoFlowSensorDef("out_energy_kwh", "Output Energy", "kWh", "energy", "total_increasing", "mdi:power-plug-outline", suggested_display_precision=2),
    # --- Duration (minutes; can exceed 12000, no cap) ---
    EcoFlowSensorDef("chg_remain_time_min", "Charge Time Remaining", "min", "duration", "measurement", "mdi:battery-clock", suggested_display_precision=0),
    EcoFlowSensorDef("dsg_remain_time_min", "Discharge Time Remaining", "min", "duration", "measurement", "mdi:battery-clock-outline", suggested_display_precision=0),
    # --- State (enum) ---
    EcoFlowSensorDef("chg_dsg_state", "Charge/Discharge State", None, "enum", None, "mdi:battery-charging", options=["idle", "discharging", "charging"]),
    # --- SoC limits / backup reserve (diagnostic) ---
    EcoFlowSensorDef("max_charge_soc_pct", "Charge Limit", "%", None, "measurement", "mdi:battery-charging-100", "diagnostic", suggested_display_precision=0),
    EcoFlowSensorDef("min_discharge_soc_pct", "Discharge Limit", "%", None, "measurement", "mdi:battery-alert-variant-outline", "diagnostic", suggested_display_precision=0),
    # --- AC charge power limit (Enhanced Mode only, #181) ---
    # The value behind the charge speed slider in the app. It travels on the
    # protobuf push path only, so it stays unavailable with developer keys.
    EcoFlowSensorDef("ac_charge_power_limit_w", "AC Charge Power Limit", "W", "power", "measurement", "mdi:lightning-bolt", "diagnostic", enhanced_only=True, suggested_display_precision=0),
    # --- Battery health (BMS heartbeat, Enhanced Mode only) ---
    # The BMS frame is the only source for these; the HTTP quota carries no
    # battery health at all, so with developer keys they never get a value.
    # Hence enhanced_only on every one: without it the entity is created and
    # stays empty forever, and an empty sensor claims a reading is on its way
    # when it is not. Registry entries outlive the fix, so the flag has to be
    # right the first time.
    # Same keys the Delta 2 Max and the Stream already use, on purpose: one
    # translation, one meaning, one entity name across the device families.
    # Those two families keep their own definitions without the flag, because
    # their HTTP parsers do carry battery health.
    EcoFlowSensorDef("bms_soh_pct", "Battery SoH", "%", None, "measurement", "mdi:battery-heart-variant", enhanced_only=True, suggested_display_precision=0),
    EcoFlowSensorDef("bms_cycles", "Battery Cycles", None, None, "total_increasing", "mdi:counter", enhanced_only=True),
    # Lifetime counters read from the BMS, not integrated from power.
    EcoFlowSensorDef("bms_accu_chg_energy_kwh", "Battery Lifetime Charge Energy", "kWh", "energy", "total_increasing", "mdi:battery-charging", enhanced_only=True, suggested_display_precision=2),
    EcoFlowSensorDef("bms_accu_dsg_energy_kwh", "Battery Lifetime Discharge Energy", "kWh", "energy", "total_increasing", "mdi:battery", enhanced_only=True, suggested_display_precision=2),
    EcoFlowSensorDef("bms_voltage_v", "Battery Voltage", "V", "voltage", "measurement", "mdi:flash-triangle", "diagnostic", enhanced_only=True, suggested_display_precision=2),
    EcoFlowSensorDef("bms_current_a", "Battery Current", "A", "current", "measurement", "mdi:current-dc", "diagnostic", enhanced_only=True, suggested_display_precision=2),
    EcoFlowSensorDef("bms_temp_c", "Battery Temp", "°C", "temperature", "measurement", "mdi:thermometer", "diagnostic", enhanced_only=True, suggested_display_precision=0),
    EcoFlowSensorDef("bms_max_cell_temp_c", "Battery Max Cell Temp", "°C", "temperature", "measurement", "mdi:thermometer-chevron-up", "diagnostic", enhanced_only=True, suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bms_min_cell_temp_c", "Battery Min Cell Temp", "°C", "temperature", "measurement", "mdi:thermometer-chevron-down", "diagnostic", enhanced_only=True, suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bms_max_mos_temp_c", "Battery Max MOSFET Temp", "°C", "temperature", "measurement", "mdi:thermometer-alert", "diagnostic", enhanced_only=True, suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bms_min_mos_temp_c", "Battery Min MOSFET Temp", "°C", "temperature", "measurement", "mdi:thermometer-alert", "diagnostic", enhanced_only=True, suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bms_max_cell_vol_mv", "Battery Max Cell Voltage", "mV", "voltage", "measurement", "mdi:sine-wave", "diagnostic", enhanced_only=True, suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bms_min_cell_vol_mv", "Battery Min Cell Voltage", "mV", "voltage", "measurement", "mdi:sine-wave", "diagnostic", enhanced_only=True, suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bms_cell_vol_diff_mv", "Battery Cell Voltage Spread", "mV", "voltage", "measurement", "mdi:arrow-expand-vertical", "diagnostic", enhanced_only=True, suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bms_remain_cap_mah", "Battery Remaining Capacity", "mAh", None, "measurement", "mdi:battery-clock", "diagnostic", enhanced_only=True, suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bms_full_cap_mah", "Battery Full Capacity", "mAh", None, "measurement", "mdi:battery", "diagnostic", enhanced_only=True, suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bms_design_cap_mah", "Battery Design Capacity", "mAh", None, "measurement", "mdi:battery-outline", "diagnostic", enhanced_only=True, suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bms_cell_count", "Battery Cell Count", None, None, None, "mdi:counter", "diagnostic", enhanced_only=True, disabled_by_default=True),
    EcoFlowSensorDef("bms_real_soh_pct", "Battery Real Health", "%", None, "measurement", "mdi:battery-heart-variant", "diagnostic", enhanced_only=True, suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bms_calendar_soh_pct", "Battery Calendar Health", "%", None, "measurement", "mdi:calendar-heart", "diagnostic", enhanced_only=True, suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bms_cycle_soh_pct", "Battery Cycle Health", "%", None, "measurement", "mdi:battery-sync-outline", "diagnostic", enhanced_only=True, suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bms_error_code", "Battery Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", enhanced_only=True, disabled_by_default=True),
]

# The Delta 3 controls read back from the same fields a binary sensor would
# expose, so a read-only twin for every switch would just double the entity
# count. The one entry here is not a twin of anything: it reports whether port
# priority is currently in effect, which the device decides on its own and no
# control can set.
DELTA3_BINARY_SENSORS: list[EcoFlowBinarySensorDef] = [
    # Only true while the unit runs off battery or solar with no AC input and
    # no smart generator attached, so on a grid-connected device this stays
    # off. Diagnostic rather than a headline reading for exactly that reason.
    EcoFlowBinarySensorDef("port_priority_active", "Port Priority Active", None, "mdi:priority-high", "diagnostic", enhanced_only=True),
]

# Controls. Every switch reads back from the same field the read-only entity
# above uses, so the device state stays the single source of truth. The params
# keys and value ranges are vendor-documented, see
# docs/reference/ecoflow-api-delta3-max-plus.md
DELTA3_SWITCHES: list[EcoFlowSwitchDef] = [
    EcoFlowSwitchDef("ac_out_switch", "AC Output", "ac_out_flow", "mdi:power-plug"),
    EcoFlowSwitchDef("ac2_out_switch", "AC Output 2", "ac2_out_flow", "mdi:power-plug"),
    EcoFlowSwitchDef("dc_12v_out_switch", "12V Output", "dc_12v_out_flow", "mdi:car-battery"),
    EcoFlowSwitchDef("energy_backup_switch", "Backup Reserve", "backup_reserve_enabled", "mdi:battery-lock"),
    EcoFlowSwitchDef("xboost_switch", "X-Boost", "xboost_enabled", "mdi:lightning-bolt"),
    EcoFlowSwitchDef("beeper_switch", "Beeper", "beeper_enabled", "mdi:volume-high"),
    EcoFlowSwitchDef("bypass_out_disable_switch", "Bypass Output Disabled", "bypass_out_disabled", "mdi:transmission-tower-off"),
    # Port priority. On means the port is non-essential and gets switched off
    # once the battery falls to its cutoff below; off means essential, which is
    # what the wire calls false. The switch follows the wire rather than the
    # app's wording, because an inverted control here cuts power to the wrong
    # outlets during an outage.
    #
    # Push path only, like the AC charge power: the polled quota carries none
    # of this, so with developer keys the switch could be flipped but never
    # show where the device actually stands.
    EcoFlowSwitchDef("port_priority_ac1_switch", "AC 1 Non-Essential", "port_priority_ac1_limited", "mdi:power-plug-off-outline", enhanced_only=True),
    EcoFlowSwitchDef("port_priority_ac2_switch", "AC 2 Non-Essential", "port_priority_ac2_limited", "mdi:power-plug-off-outline", enhanced_only=True),
    EcoFlowSwitchDef("port_priority_dc_switch", "DC Non-Essential", "port_priority_dc_limited", "mdi:power-plug-off-outline", enhanced_only=True),
]

# Ranges are the vendor's own bounds, not our choice. Backup reserve tops out at
# 50 and the charge limit cannot go below 50.
DELTA3_NUMBERS: list[EcoFlowNumberDef] = [
    EcoFlowNumberDef("backup_reserve_soc", "Backup Reserve Level", "backup_reserve_soc_pct", "%", "mdi:battery-lock", 0, 50, 1),
    EcoFlowNumberDef("max_charge_soc", "Charge Limit", "max_charge_soc_pct", "%", "mdi:battery-charging-100", 50, 100, 1),
    EcoFlowNumberDef("min_discharge_soc", "Discharge Limit", "min_discharge_soc_pct", "%", "mdi:battery-alert-variant-outline", 0, 30, 1),
    # AC charge power. Push path only: the polled quota never carries this
    # field, so it exists on account sign-in and nowhere else. Ships with the
    # charge mode below because the app puts this slider under its custom
    # power mode; a write was accepted and reported back in battery-optimised
    # mode too, so "only works in custom mode" is the app's framing rather
    # than a measured property of the device.
    EcoFlowNumberDef("ac_charge_power_limit", "AC Charge Power", "ac_charge_power_limit_w", "W", "mdi:lightning-bolt", 200, 2400, 100, enhanced_only=True),
    # Port priority cutoffs. The bounds below are the widest the app's own
    # formula can produce; the entity narrows them at runtime from the two
    # battery limits (see `port_priority_soc_bounds`). Push path only, same
    # reason as the switches above.
    EcoFlowNumberDef("port_priority_ac1_soc", "AC 1 Cutoff Level", "port_priority_ac1_cutoff_soc", "%", "mdi:battery-off-outline", 5, 95, 1, enhanced_only=True),
    EcoFlowNumberDef("port_priority_ac2_soc", "AC 2 Cutoff Level", "port_priority_ac2_cutoff_soc", "%", "mdi:battery-off-outline", 5, 95, 1, enhanced_only=True),
    EcoFlowNumberDef("port_priority_dc_soc", "DC Cutoff Level", "port_priority_dc_cutoff_soc", "%", "mdi:battery-off-outline", 5, 95, 1, enhanced_only=True),
]

# LCD screen timeout. The six steps are the app's own list, in the app's order,
# and the wire values behind them were read off a D3M1 rather than guessed.
#
# Zero is "never", not "off". Three other rows on the same app page read "Never"
# and carry 0 on the wire, so a user reaching for a dark panel must not be able
# to pick 0 by mistake - hence it is labelled and sits last, where the app puts
# it. There is no value that switches the screen off outright; the shortest
# timeout is as close as the device gets.
DELTA3_SCREEN_TIMEOUT_VALUES: Mapping[int, str] = MappingProxyType(
    {
        10: "10_seconds",
        30: "30_seconds",
        60: "1_minute",
        300: "5_minutes",
        1800: "30_minutes",
        0: "never",
    }
)
DELTA3_SCREEN_TIMEOUT_KEY = "screen_timeout"
DELTA3_SCREEN_TIMEOUT_STATE_KEY = "screen_off_time_sec"

# The four idle shutdowns from the same app page, in minutes. Every one of them
# offers the identical eight steps, read off all four pages of the app.
#
# The unit is settled rather than inferred: the 12 V DC page had "2 h" ticked
# while its field read 120, and two hours is 120 minutes. Note that it differs
# from the screen timeout above, which is seconds, in the same frame.
#
# These are idle shutdowns, not timers. The app's own description on each page:
# the output switches off when no load is connected and no activity is seen for
# the configured span. A load that keeps drawing keeps its output alive.
DELTA3_IDLE_SHUTDOWN_VALUES: Mapping[int, str] = MappingProxyType(
    {
        30: "30_minutes",
        60: "1_hour",
        120: "2_hours",
        240: "4_hours",
        360: "6_hours",
        720: "12_hours",
        1440: "24_hours",
        0: "never",
    }
)

# key -> (entity name, state key, icon). The names say what powers down and that
# it is idle-triggered; the app calls them "timeout", which describes the
# mechanism and not the effect.
DELTA3_IDLE_SHUTDOWNS: tuple[tuple[str, str, str, str], ...] = (
    ("device_idle_shutdown", "Device Idle Shutdown", "dev_standby_time_min", "mdi:power-off"),
    ("ac1_idle_shutdown", "AC 1 Idle Shutdown", "ac_standby_time_min", "mdi:power-socket-de"),
    ("ac2_idle_shutdown", "AC 2 Idle Shutdown", "ac2_standby_time_min", "mdi:power-socket-de"),
    ("dc_idle_shutdown", "12 V Idle Shutdown", "dc_standby_time_min", "mdi:car-battery"),
)

DELTA3_SELECTS: list[EcoFlowSelectDef] = [
    # Push path only: the polled quota carries no screen or standby field at all,
    # and the official Delta 3 HTTP documentation has none either. Without this
    # flag the entities would exist on developer keys and never read or write.
    EcoFlowSelectDef(
        DELTA3_SCREEN_TIMEOUT_KEY,
        "Screen Timeout",
        DELTA3_SCREEN_TIMEOUT_STATE_KEY,
        tuple(DELTA3_SCREEN_TIMEOUT_VALUES.values()),
        icon="mdi:monitor-off",
        enhanced_only=True,
        value_map=DELTA3_SCREEN_TIMEOUT_VALUES,
    ),
    *(
        EcoFlowSelectDef(
            key,
            name,
            state_key,
            tuple(DELTA3_IDLE_SHUTDOWN_VALUES.values()),
            icon=icon,
            enhanced_only=True,
            value_map=DELTA3_IDLE_SHUTDOWN_VALUES,
        )
        for key, name, state_key, icon in DELTA3_IDLE_SHUTDOWNS
    ),
]

# The charge mode deliberately has no entity. It is part of the same wire
# setting as the power above and travels with every write, but its read-back
# (status frame field 124) only arrives when the mode changes, not on a cycle:
# measured over five minutes on a D3M1 without a single report. A select that
# cannot show what the device is set to fails the read-back gate, so the mode
# is written and never displayed.
AC_CHARGE_POWER_STATE_KEY = "ac_charge_power_limit_w"


# =====================================================================
# Power → Energy mappings (Riemann sum integration per device type)
# =====================================================================

POWEROCEAN_POWER_TO_ENERGY: dict[str, str] = {
    "solar_w": "solar_energy_kwh",
    "home_w": "home_energy_kwh",
    "grid_import_power_w": "grid_import_energy_kwh",
    "grid_export_power_w": "grid_export_energy_kwh",
}

POWEROCEAN_ENERGY_FROM_API: list[tuple[str, str]] = [
    ("batt_charge_power_w", "batt_charge_energy_kwh"),
    ("batt_discharge_power_w", "batt_discharge_energy_kwh"),
]

DELTA_POWER_TO_ENERGY: dict[str, str] = {
    "solar_in_w": "solar_energy_kwh",
    "solar2_in_w": "solar2_energy_kwh",
    "ac_in_w": "ac_in_energy_kwh",
    "ac_out_w": "ac_out_energy_kwh",
}

DELTA_ENERGY_FROM_API: list[tuple[str, str]] = []

# The Delta 3 HTTP quota exposes no native energy counters, so these kWh
# sensors are integrated from the live power keys. pow_out_sum_w is the
# aggregate output (AC + DC + USB), giving one clean "output energy" total.
DELTA3_POWER_TO_ENERGY: dict[str, str] = {
    "pv1_in_w": "solar_energy_kwh",
    "pv2_in_w": "solar2_energy_kwh",
    "ac_in_w": "ac_in_energy_kwh",
    "pow_out_sum_w": "out_energy_kwh",
}

DELTA3_ENERGY_FROM_API: list[tuple[str, str]] = []

SMARTPLUG_POWER_TO_ENERGY: dict[str, str] = {
    "power_w": "energy_kwh",
}

SMARTPLUG_ENERGY_FROM_API: list[tuple[str, str]] = []

# grid_w is deliberately excluded: the Stream reports a signed grid power
# without an import/export split. Feeding abs(grid_w) into a single energy
# counter would conflate consumption and feed-in, making it useless for the
# Energy Dashboard.
STREAM_POWER_TO_ENERGY: dict[str, str] = {
    "solar_w": "solar_energy_kwh",
    "pv1_w": "pv1_energy_kwh",
    "pv2_w": "pv2_energy_kwh",
    "pv3_w": "pv3_energy_kwh",
    "pv4_w": "pv4_energy_kwh",
    "home_w": "home_energy_kwh",
    "batt_charge_power_w": "batt_charge_energy_kwh",
    "batt_discharge_power_w": "batt_discharge_energy_kwh",
}

STREAM_ENERGY_FROM_API: list[tuple[str, str]] = []

STREAMAC5000_POWER_TO_ENERGY: dict[str, str] = {
    # `solar_w` is deliberately absent: the device infers that figure from the
    # house flows and reports it with no PV wired to it, so integrating it
    # would write production that never happened into a counter that only ever
    # counts up. See the note on the sensor list.
    "home_w": "home_energy_kwh",
    "batt_charge_power_w": "batt_charge_energy_kwh",
    "batt_discharge_power_w": "batt_discharge_energy_kwh",
    # This device reports the grid split as separate flow edges, so import and
    # export each get an honest counter instead of one signed total.
    "grid_import_power_w": "grid_import_energy_kwh",
    "grid_export_power_w": "grid_export_energy_kwh",
}

STREAMAC5000_ENERGY_FROM_API: list[tuple[str, str]] = []


# ===========================================================================
# SET Command Templates (per device type)
# ===========================================================================

# Smart Plug SET-command templates (uses cmdCode format)
SMARTPLUG_SWITCH_COMMANDS: dict[str, dict[str, dict[str, Any]]] = {
    "plug_switch": {
        "on": {
            "cmdCode": "WN511_SOCKET_SET_PLUG_SWITCH_MESSAGE",
            "params": {"plugSwitch": 1},
        },
        "off": {
            "cmdCode": "WN511_SOCKET_SET_PLUG_SWITCH_MESSAGE",
            "params": {"plugSwitch": 0},
        },
    },
}

# IoT API SET-command templates for switches (Delta 2 Max R351 profile)
SWITCH_COMMANDS_R351: dict[str, dict[str, dict[str, Any]]] = {
    "ac_switch": {
        "on": {
            "moduleType": 3,
            "operateType": "acOutCfg",
            "params": {"enabled": 1, "out_voltage": 4294967295, "out_freq": 1, "xboost": 1},
        },
        "off": {
            "moduleType": 3,
            "operateType": "acOutCfg",
            "params": {"enabled": 0, "out_voltage": 4294967295, "out_freq": 1, "xboost": 0},
        },
    },
    "dc_switch": {
        "on": {"moduleType": 1, "operateType": "dcOutCfg", "params": {"enabled": 1}},
        "off": {"moduleType": 1, "operateType": "dcOutCfg", "params": {"enabled": 0}},
    },
    "car_12v_switch": {
        "on": {"moduleType": 5, "operateType": "mpptCar", "params": {"enabled": 1}},
        "off": {"moduleType": 5, "operateType": "mpptCar", "params": {"enabled": 0}},
    },
}

# IoT API SET-command templates for switches (Delta 2 Max R331/legacy profile)
SWITCH_COMMANDS_R331: dict[str, dict[str, dict[str, Any]]] = {
    "ac_switch": {
        "on": {
            "moduleType": 5,
            "operateType": "acOutCfg",
            "params": {"enabled": 1, "out_voltage": 4294967295, "out_freq": 255, "xboost": 255},
        },
        "off": {
            "moduleType": 5,
            "operateType": "acOutCfg",
            "params": {"enabled": 0, "out_voltage": 4294967295, "out_freq": 255, "xboost": 255},
        },
    },
    "dc_switch": {
        "on": {"moduleType": 1, "operateType": "dcOutCfg", "params": {"enabled": 1}},
        "off": {"moduleType": 1, "operateType": "dcOutCfg", "params": {"enabled": 0}},
    },
    "car_12v_switch": {
        "on": {"moduleType": 5, "operateType": "mpptCar", "params": {"enabled": 1}},
        "off": {"moduleType": 5, "operateType": "mpptCar", "params": {"enabled": 0}},
    },
    "xboost_switch": {
        "on": {
            "moduleType": 5,
            "operateType": "acOutCfg",
            "params": {"xboost": 1, "enabled": 255, "out_voltage": 4294967295, "out_freq": 255},
        },
        "off": {
            "moduleType": 5,
            "operateType": "acOutCfg",
            "params": {"xboost": 0, "enabled": 255, "out_voltage": 4294967295, "out_freq": 255},
        },
    },
}

# Declarative switch command templates (Delta 2 Max R351)
SWITCH_DECLARATIVE_R351: dict[str, dict[str, Any]] = {
    "beeper_switch": {
        "moduleType": 1,
        "operateType": "quietCfg",
        "param_key": "enabled",
        "invert": True,
    },
    "xboost_switch": {
        "moduleType": 3,
        "operateType": "acOutCfg",
        "param_key": "xboost",
    },
    "ac_auto_on_switch": {
        "moduleType": 1,
        "operateType": "newAcAutoOnCfg",
        "param_key": "enabled",
        "extra_params": {"minAcSoc": 5},
    },
    "backup_reserve_switch": {
        "moduleType": 1,
        "operateType": "watthConfig",
        "param_key": "isConfig",
        "extra_params": {"bpPowerSoc": 50, "minChgSoc": 0, "minDsgSoc": 0},
    },
}

# Declarative switch command templates (Delta 2 Max R331/legacy)
SWITCH_DECLARATIVE_R331: dict[str, dict[str, Any]] = {
    "beeper_switch": {
        "moduleType": 1,
        "operateType": "quietMode",
        "param_key": "enabled",
        "invert": True,
    },
    "ac_auto_on_switch": {
        "moduleType": 1,
        "operateType": "acAutoOutConfig",
        "param_key": "acAutoOutConfig",
        "extra_params": {"minAcOutSoc": 5},
    },
    "backup_reserve_switch": {
        "moduleType": 1,
        "operateType": "watthConfig",
        "param_key": "isConfig",
        "extra_params": {"bpPowerSoc": 50, "minChgSoc": 0, "minDsgSoc": 0},
    },
}

# IoT API SET-command templates for number entities (Delta 2 Max)
NUMBER_COMMANDS: dict[str, dict[str, Any]] = {
    "ac_charge_speed": {
        "moduleType": 3,
        "operateType": "acChgCfg",
        # The effective AC charge speed follows slowChgWatts; fastChgWatts is
        # mirrored to the same value so both charger paths use the user's
        # setting (issue #95: hardcoding slowChgWatts=400 pinned the device
        # at 400 W regardless of the requested value).
        "param_key": "slowChgWatts",
        "value_params": ["fastChgWatts"],
        "extra_params": {"chgPauseFlag": 0},
    },
    "max_charge_soc": {
        "moduleType": 2,
        "operateType": "upsConfig",
        "param_key": "maxChgSoc",
    },
    "min_discharge_soc": {
        "moduleType": 2,
        "operateType": "dsgCfg",
        "param_key": "minDsgSoc",
    },
    "standby_timeout": {
        "moduleType": 1,
        "operateType": "standbyTime",
        "param_key": "standbyMin",
    },
    "car_standby_timeout": {
        "moduleType": 5,
        "operateType": "standbyTime",
        "param_key": "standbyMins",
    },
    "screen_brightness": {
        "moduleType": 1,
        "operateType": "lcdCfg",
        "param_key": "brighLevel",
        "extra_params": {"delayOff": 0},
    },
    "screen_timeout": {
        "moduleType": 1,
        "operateType": "lcdCfg",
        "param_key": "delayOff",
        "extra_params": {"brighLevel": 255},
    },
    "backup_reserve_soc": {
        "moduleType": 1,
        "operateType": "watthConfig",
        "param_key": "bpPowerSoc",
        "extra_params": {"isConfig": 1, "minChgSoc": 0, "minDsgSoc": 0},
    },
}

# Smart Plug SET-command templates for number entities
SMARTPLUG_NUMBER_COMMANDS: dict[str, dict[str, Any]] = {
    "led_brightness": {
        "cmdCode": "WN511_SOCKET_SET_BRIGHTNESS_PACK",
        "param_key": "brightness",
        "scale": 1023.0 / 100.0,
    },
    "max_watts": {
        "cmdCode": "WN511_SOCKET_SET_MAX_WATTS",
        "param_key": "maxWatts",
        "scale": 1,
    },
}
