"""Constants for the EcoFlow Energy integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.const import Platform

from .ecoflow.const import (  # noqa: E402
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_UNKNOWN,
    get_device_type,
)

DOMAIN = "ecoflow_energy"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
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

# Auth methods
AUTH_METHOD_DEVELOPER = "developer"
AUTH_METHOD_APP = "app"

# Device modes
MODE_STANDARD = "standard"
MODE_ENHANCED = "enhanced"

# Coordinator
STALE_THRESHOLD_S = 35.0  # MQTT data older than this → HTTP fallback
HTTP_FALLBACK_INTERVAL_S = 30
HTTP_SUPPLEMENT_INTERVAL_S = 60  # Enhanced Mode: HTTP supplement poll for detail sensors
ENERGY_STREAM_KEEPALIVE_S = 20  # Re-send EnergyStreamSwitch every 20s
QUOTAS_KEEPALIVE_S = 30  # latestQuotas poll interval (app-level keepalive)
PING_KEEPALIVE_S = 60  # MQTT ping heartbeat interval

DEVICE_TYPE_DISPLAY_NAMES: dict[str, str] = {
    DEVICE_TYPE_POWEROCEAN: "PowerOcean",
    DEVICE_TYPE_DELTA: "Delta 2 Max",
    DEVICE_TYPE_SMARTPLUG: "Smart Plug",
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


@dataclass(frozen=True)
class EcoFlowBinarySensorDef:
    key: str
    name: str
    device_class: str | None = None
    icon: str | None = None
    entity_category: str | None = None
    disabled_by_default: bool = False


@dataclass(frozen=True)
class EcoFlowSwitchDef:
    key: str
    name: str
    state_key: str
    icon: str | None = None


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
    EcoFlowSensorDef("bp_real_soc_pct", "Battery Real SOC", "%", "battery", "measurement", "mdi:battery", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bp_real_soh_pct", "Battery Real SOH", "%", None, "measurement", "mdi:battery-heart-variant", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bp_down_limit_soc_pct", "Battery Min SOC Limit", "%", None, None, "mdi:battery-low", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bp_up_limit_soc_pct", "Battery Max SOC Limit", "%", None, None, "mdi:battery-high", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    # --- Inverter / PCS Diagnostics ---
    EcoFlowSensorDef("pcs_ac_freq_hz", "Grid Frequency", "Hz", "frequency", "measurement", "mdi:sine-wave", "diagnostic", suggested_display_precision=1),
    EcoFlowSensorDef("ems_bp_alive_num", "Battery Packs Online", None, None, "measurement", "mdi:battery-check", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("bp_online_sum", "Battery Packs Online (EMS)", None, None, "measurement", "mdi:battery-check", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("mppt_pv1_power_w", "MPPT String 1 Power", "W", "power", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=0),
    EcoFlowSensorDef("mppt_pv1_voltage_v", "MPPT String 1 Voltage", "V", "voltage", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=1),
    EcoFlowSensorDef("mppt_pv1_current_a", "MPPT String 1 Current", "A", "current", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=2),
    EcoFlowSensorDef("mppt_pv2_power_w", "MPPT String 2 Power", "W", "power", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=0),
    EcoFlowSensorDef("mppt_pv2_voltage_v", "MPPT String 2 Voltage", "V", "voltage", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=1),
    EcoFlowSensorDef("mppt_pv2_current_a", "MPPT String 2 Current", "A", "current", "measurement", "mdi:solar-power-variant", "diagnostic", suggested_display_precision=2),
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
    EcoFlowSensorDef("ems_feed_mode", "EMS Feed Mode", None, None, None, "mdi:cog", "diagnostic"),
    EcoFlowSensorDef("ems_work_mode", "EMS Work Mode", None, None, None, "mdi:cog", "diagnostic"),
    EcoFlowSensorDef("pcs_run_state", "PCS Running State", None, None, None, "mdi:power", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("grid_status", "Grid Status", None, None, None, "mdi:transmission-tower", "diagnostic"),
    EcoFlowSensorDef("pcs_power_factor", "Power Factor", None, "power_factor", "measurement", "mdi:sine-wave", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("ems_feed_power_limit_w", "Feed Power Limit", "W", "power", "measurement", "mdi:transmission-tower-export", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("ems_feed_ratio_pct", "Feed Ratio", "%", None, "measurement", "mdi:percent", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("batt_charge_discharge_state", "Battery Charge/Discharge State", None, None, None, "mdi:battery-sync", "diagnostic"),
    # --- EMS / System extended sensors (diagnostic, disabled by default) ---
    EcoFlowSensorDef("ems_charge_upper_limit_pct", "EMS Charge Upper Limit", "%", None, "measurement", "mdi:battery-charging-high", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("ems_discharge_lower_limit_pct", "EMS Discharge Lower Limit", "%", None, "measurement", "mdi:battery-alert-variant-outline", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("ems_keep_soc_pct", "EMS Keep SoC", "%", None, "measurement", "mdi:battery-lock", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("ems_backup_ratio_pct", "EMS Backup Ratio", "%", None, "measurement", "mdi:battery-lock-open", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("mppt1_fault_code", "MPPT 1 Fault Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("mppt2_fault_code", "MPPT 2 Fault Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("pcs_ac_error_code", "PCS AC Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("pcs_dc_error_code", "PCS DC Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("pcs_ac_warning_code", "PCS AC Warning Code", None, None, None, "mdi:alert-outline", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("wifi_status", "WiFi Status", None, None, None, "mdi:wifi", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("ethernet_status", "Ethernet Status", None, None, None, "mdi:ethernet", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("cellular_status", "4G Status", None, None, None, "mdi:signal-4g", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("ems_led_brightness", "EMS LED Brightness", None, None, "measurement", "mdi:brightness-6", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("ems_work_state", "EMS Work State", None, None, None, "mdi:cog", "diagnostic", disabled_by_default=True),
    EcoFlowSensorDef("ems_total_battery_capacity_wh", "Total Battery Capacity", "Wh", "energy_storage", "measurement", "mdi:battery", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("pcs_max_output_power_w", "PCS Max Output Power", "W", "power", "measurement", "mdi:flash-triangle", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("pcs_max_input_power_w", "PCS Max Input Power", "W", "power", "measurement", "mdi:flash-triangle", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bp_max_charge_power_w", "Battery Max Charge Power", "W", "power", "measurement", "mdi:battery-charging", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("bp_max_discharge_power_w", "Battery Max Discharge Power", "W", "power", "measurement", "mdi:battery", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
]


def _build_po_pack_sensors(pack_num: int) -> list[EcoFlowSensorDef]:
    """Build sensor definitions for a PowerOcean battery pack.

    Pack 1: 7 core sensors enabled, 17 diagnostic disabled.
    Packs 2-5: all 24 sensors disabled by default.
    """
    p = f"pack{pack_num}"
    enabled = pack_num == 1  # Only Pack 1 core sensors enabled by default

    core = [
        EcoFlowSensorDef(f"{p}_soc", f"Pack {pack_num} SoC", "%", "battery", "measurement", "mdi:battery", suggested_display_precision=0, disabled_by_default=not enabled),
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
        EcoFlowSensorDef(f"{p}_accu_chg_energy_kwh", f"Pack {pack_num} Lifetime Charge Energy", "kWh", "energy", "total_increasing", "mdi:battery-charging", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
        EcoFlowSensorDef(f"{p}_accu_dsg_energy_kwh", f"Pack {pack_num} Lifetime Discharge Energy", "kWh", "energy", "total_increasing", "mdi:battery", "diagnostic", suggested_display_precision=0, disabled_by_default=True),
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
]

POWEROCEAN_NUMBERS: list[EcoFlowNumberDef] = [
    EcoFlowNumberDef("min_discharge_soc", "Min Discharge SoC", "ems_discharge_lower_limit_pct", "%", "mdi:battery-alert-variant-outline", 0, 30, 5, enhanced_only=True),
]


# =====================================================================
# Delta 2 Max sensor definitions (from ha_delta_discovery.py)
# =====================================================================

DELTA2MAX_SENSORS: list[EcoFlowSensorDef] = [
    # --- Battery / SoC ---
    EcoFlowSensorDef("soc", "SoC", "%", "battery", "measurement", "mdi:battery", suggested_display_precision=0),
    EcoFlowSensorDef("bms_soh_pct", "Battery SoH", "%", "battery", "measurement", "mdi:battery-heart-variant", suggested_display_precision=0),
    EcoFlowSensorDef("bms_precise_soc", "Precise SoC", "%", "battery", "measurement", "mdi:battery-sync", suggested_display_precision=0),
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
    EcoFlowSensorDef("chg_dsg_state", "Charge/Discharge State", None, None, None, "mdi:battery-charging", disabled_by_default=True),
    EcoFlowSensorDef("ems_chg_state", "EMS Charge State", None, None, None, "mdi:battery-charging-outline", disabled_by_default=True),
    EcoFlowSensorDef("charger_type", "Charger Type", None, None, None, "mdi:ev-plug-type2", disabled_by_default=True),
    EcoFlowSensorDef("mppt_chg_state", "MPPT Charge State", None, None, None, "mdi:solar-panel", disabled_by_default=True),
    EcoFlowSensorDef("ems_lcd_soc", "LCD SoC", "%", "battery", "measurement", "mdi:monitor", suggested_display_precision=0, disabled_by_default=True),
    EcoFlowSensorDef("ems_precise_soc", "EMS Precise SoC", "%", "battery", "measurement", "mdi:monitor", suggested_display_precision=0, disabled_by_default=True),
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
    EcoFlowSensorDef("slave1_soc", "Slave 1 SoC", "%", "battery", "measurement", "mdi:battery", disabled_by_default=True),
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
    EcoFlowSensorDef("slave2_soc", "Slave 2 SoC", "%", "battery", "measurement", "mdi:battery", disabled_by_default=True),
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
    EcoFlowNumberDef("ac_charge_speed", "AC Charge Speed", "ac_chg_rated_power_w", "W", "mdi:lightning-bolt", 200, 2400, 100),
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

SMARTPLUG_NUMBERS: list[EcoFlowNumberDef] = [
    EcoFlowNumberDef("led_brightness", "LED Brightness", "led_brightness", "%", "mdi:brightness-6", 0, 100, 5),
    EcoFlowNumberDef("max_watts", "Max Power Limit", "max_power_w", "W", "mdi:flash-alert", 0, 2500, 100),
]


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

SMARTPLUG_POWER_TO_ENERGY: dict[str, str] = {
    "power_w": "energy_kwh",
}

SMARTPLUG_ENERGY_FROM_API: list[tuple[str, str]] = []
