"""Constants for the EcoFlow Energy integration."""

from __future__ import annotations

from dataclasses import dataclass

DOMAIN = "ecoflow_energy"

from homeassistant.const import Platform

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

# Device types (from IoT API productName / productType)
DEVICE_TYPE_POWEROCEAN = "powerocean"
DEVICE_TYPE_DELTA = "delta"
DEVICE_TYPE_SMARTPLUG = "smartplug"
DEVICE_TYPE_UNKNOWN = "unknown"

# Keywords used to classify devices from productName strings
_POWEROCEAN_KEYWORDS = ("powerocean", "power ocean")
_DELTA_KEYWORDS = ("delta",)
_SMARTPLUG_KEYWORDS = ("smart plug", "smartplug")


def get_device_type(product_name: str) -> str:
    """Classify a device based on its productName string.

    Returns DEVICE_TYPE_POWEROCEAN, DEVICE_TYPE_DELTA, DEVICE_TYPE_SMARTPLUG,
    or DEVICE_TYPE_UNKNOWN.
    """
    name = product_name.lower()
    for kw in _POWEROCEAN_KEYWORDS:
        if kw in name:
            return DEVICE_TYPE_POWEROCEAN
    for kw in _DELTA_KEYWORDS:
        if kw in name:
            return DEVICE_TYPE_DELTA
    for kw in _SMARTPLUG_KEYWORDS:
        if kw in name:
            return DEVICE_TYPE_SMARTPLUG
    return DEVICE_TYPE_UNKNOWN


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


@dataclass(frozen=True)
class EcoFlowBinarySensorDef:
    key: str
    name: str
    device_class: str | None = None
    icon: str | None = None
    entity_category: str | None = None


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


# =====================================================================
# PowerOcean sensor definitions (from ha_discovery.py)
# =====================================================================

POWEROCEAN_SENSORS: list[EcoFlowSensorDef] = [
    # --- Core Power (measurement) ---
    EcoFlowSensorDef("solar_w", "Solar Power", "W", "power", "measurement", "mdi:solar-power"),
    EcoFlowSensorDef("home_w", "Home Power", "W", "power", "measurement", "mdi:home-lightning-bolt"),
    EcoFlowSensorDef("grid_w", "Grid Power", "W", "power", "measurement", "mdi:transmission-tower"),
    EcoFlowSensorDef("batt_w", "Battery Power", "W", "power", "measurement", "mdi:battery"),
    EcoFlowSensorDef("batt_charge_power_w", "Battery Charge Power", "W", "power", "measurement", "mdi:battery-charging"),
    EcoFlowSensorDef("batt_discharge_power_w", "Battery Discharge Power", "W", "power", "measurement", "mdi:battery"),
    EcoFlowSensorDef("grid_import_power_w", "Grid Import Power", "W", "power", "measurement", "mdi:transmission-tower-import"),
    EcoFlowSensorDef("grid_export_power_w", "Grid Export Power", "W", "power", "measurement", "mdi:transmission-tower-export"),
    # --- SOC ---
    EcoFlowSensorDef("soc_pct", "Battery SOC", "%", "battery", "measurement", "mdi:battery"),
    # --- Battery Detail ---
    EcoFlowSensorDef("bp_soh_pct", "Battery SOH", "%", None, "measurement", "mdi:battery-heart-variant"),
    EcoFlowSensorDef("bp_cycles", "Battery Cycles", None, None, "total_increasing", "mdi:battery-sync"),
    EcoFlowSensorDef("bp_remain_watth", "Battery Remaining Capacity", "Wh", "energy_storage", "measurement", "mdi:battery-clock"),
    # --- Energy Dashboard (total_increasing, kWh) ---
    # All 6 energy sensors available in Standard Mode via Riemann sum integration
    EcoFlowSensorDef("solar_energy_kwh", "Solar Energy", "kWh", "energy", "total_increasing", "mdi:solar-power"),
    EcoFlowSensorDef("home_energy_kwh", "Home Energy", "kWh", "energy", "total_increasing", "mdi:home-lightning-bolt"),
    EcoFlowSensorDef("grid_import_energy_kwh", "Grid Import Energy", "kWh", "energy", "total_increasing", "mdi:transmission-tower-import"),
    EcoFlowSensorDef("grid_export_energy_kwh", "Grid Export Energy", "kWh", "energy", "total_increasing", "mdi:transmission-tower-export"),
    EcoFlowSensorDef("batt_charge_energy_kwh", "Battery Charge Energy", "kWh", "energy", "total_increasing", "mdi:battery-charging"),
    EcoFlowSensorDef("batt_discharge_energy_kwh", "Battery Discharge Energy", "kWh", "energy", "total_increasing", "mdi:battery"),
    # --- Battery Diagnostics ---
    EcoFlowSensorDef("bp_voltage_v", "Battery Voltage", "V", "voltage", "measurement", "mdi:flash-triangle", "diagnostic"),
    EcoFlowSensorDef("bp_current_a", "Battery Current", "A", "current", "measurement", "mdi:current-dc", "diagnostic"),
    EcoFlowSensorDef("bp_max_cell_temp_c", "Battery Max Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-chevron-up", "diagnostic"),
    EcoFlowSensorDef("bp_min_cell_temp_c", "Battery Min Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-chevron-down", "diagnostic"),
    EcoFlowSensorDef("bp_env_temp_c", "Battery Environment Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer", "diagnostic"),
    EcoFlowSensorDef("bp_max_mos_temp_c", "Battery Max MOSFET Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-alert", "diagnostic"),
    EcoFlowSensorDef("bp_cell_max_vol_mv", "Battery Cell Max Voltage", "mV", "voltage", "measurement", "mdi:sine-wave", "diagnostic"),
    EcoFlowSensorDef("bp_cell_min_vol_mv", "Battery Cell Min Voltage", "mV", "voltage", "measurement", "mdi:sine-wave", "diagnostic"),
    EcoFlowSensorDef("bp_real_soc_pct", "Battery Real SOC", "%", "battery", "measurement", "mdi:battery", "diagnostic"),
    EcoFlowSensorDef("bp_real_soh_pct", "Battery Real SOH", "%", None, "measurement", "mdi:battery-heart-variant", "diagnostic"),
    EcoFlowSensorDef("bp_down_limit_soc_pct", "Battery Min SOC Limit", "%", None, None, "mdi:battery-low", "diagnostic"),
    EcoFlowSensorDef("bp_up_limit_soc_pct", "Battery Max SOC Limit", "%", None, None, "mdi:battery-high", "diagnostic"),
    # --- Inverter / PCS Diagnostics ---
    EcoFlowSensorDef("pcs_ac_freq_hz", "Grid Frequency", "Hz", "frequency", "measurement", "mdi:sine-wave", "diagnostic"),
    EcoFlowSensorDef("ems_bp_alive_num", "Battery Packs Online", None, None, "measurement", "mdi:battery-check", "diagnostic"),
    EcoFlowSensorDef("bp_online_sum", "Battery Packs Online (EMS)", None, None, "measurement", "mdi:battery-check", "diagnostic"),
    EcoFlowSensorDef("mppt_pv1_power_w", "MPPT String 1 Power", "W", "power", "measurement", "mdi:solar-power-variant", "diagnostic"),
    EcoFlowSensorDef("mppt_pv1_voltage_v", "MPPT String 1 Voltage", "V", "voltage", "measurement", "mdi:solar-power-variant", "diagnostic"),
    EcoFlowSensorDef("mppt_pv1_current_a", "MPPT String 1 Current", "A", "current", "measurement", "mdi:solar-power-variant", "diagnostic"),
    EcoFlowSensorDef("mppt_pv2_power_w", "MPPT String 2 Power", "W", "power", "measurement", "mdi:solar-power-variant", "diagnostic"),
    EcoFlowSensorDef("mppt_pv2_voltage_v", "MPPT String 2 Voltage", "V", "voltage", "measurement", "mdi:solar-power-variant", "diagnostic"),
    EcoFlowSensorDef("mppt_pv2_current_a", "MPPT String 2 Current", "A", "current", "measurement", "mdi:solar-power-variant", "diagnostic"),
    EcoFlowSensorDef("grid_phase_a_voltage_v", "Grid Phase A Voltage", "V", "voltage", "measurement", "mdi:transmission-tower", "diagnostic"),
    EcoFlowSensorDef("grid_phase_b_voltage_v", "Grid Phase B Voltage", "V", "voltage", "measurement", "mdi:transmission-tower", "diagnostic"),
    EcoFlowSensorDef("grid_phase_c_voltage_v", "Grid Phase C Voltage", "V", "voltage", "measurement", "mdi:transmission-tower", "diagnostic"),
    # --- Per-Phase Active Power & Current (3-phase monitoring) ---
    EcoFlowSensorDef("grid_phase_a_active_power_w", "Grid Phase A Active Power", "W", "power", "measurement", "mdi:transmission-tower", "diagnostic"),
    EcoFlowSensorDef("grid_phase_b_active_power_w", "Grid Phase B Active Power", "W", "power", "measurement", "mdi:transmission-tower", "diagnostic"),
    EcoFlowSensorDef("grid_phase_c_active_power_w", "Grid Phase C Active Power", "W", "power", "measurement", "mdi:transmission-tower", "diagnostic"),
    EcoFlowSensorDef("grid_phase_a_current_a", "Grid Phase A Current", "A", "current", "measurement", "mdi:transmission-tower", "diagnostic"),
    EcoFlowSensorDef("grid_phase_b_current_a", "Grid Phase B Current", "A", "current", "measurement", "mdi:transmission-tower", "diagnostic"),
    EcoFlowSensorDef("grid_phase_c_current_a", "Grid Phase C Current", "A", "current", "measurement", "mdi:transmission-tower", "diagnostic"),
    # --- Per-Phase Reactive & Apparent Power (3-phase monitoring) ---
    EcoFlowSensorDef("grid_phase_a_reactive_power_var", "Grid Phase A Reactive Power", "var", "reactive_power", "measurement", "mdi:sine-wave", "diagnostic"),
    EcoFlowSensorDef("grid_phase_b_reactive_power_var", "Grid Phase B Reactive Power", "var", "reactive_power", "measurement", "mdi:sine-wave", "diagnostic"),
    EcoFlowSensorDef("grid_phase_c_reactive_power_var", "Grid Phase C Reactive Power", "var", "reactive_power", "measurement", "mdi:sine-wave", "diagnostic"),
    EcoFlowSensorDef("grid_phase_a_apparent_power_va", "Grid Phase A Apparent Power", "VA", "apparent_power", "measurement", "mdi:sine-wave", "diagnostic"),
    EcoFlowSensorDef("grid_phase_b_apparent_power_va", "Grid Phase B Apparent Power", "VA", "apparent_power", "measurement", "mdi:sine-wave", "diagnostic"),
    EcoFlowSensorDef("grid_phase_c_apparent_power_va", "Grid Phase C Apparent Power", "VA", "apparent_power", "measurement", "mdi:sine-wave", "diagnostic"),
    # --- PV Inverter Link ---
    EcoFlowSensorDef("pv_inverter_power_w", "PV Inverter Power", "W", "power", "measurement", "mdi:solar-power-variant", "diagnostic"),
    # --- EMS State & Control ---
    EcoFlowSensorDef("ems_feed_mode", "EMS Feed Mode", None, None, None, "mdi:cog", "diagnostic"),
    EcoFlowSensorDef("ems_work_mode", "EMS Work Mode", None, None, None, "mdi:cog", "diagnostic"),
    EcoFlowSensorDef("pcs_run_state", "PCS Running State", None, None, None, "mdi:power", "diagnostic"),
    EcoFlowSensorDef("grid_status", "Grid Status", None, None, None, "mdi:transmission-tower", "diagnostic"),
    EcoFlowSensorDef("pcs_power_factor", "Power Factor", None, "power_factor", "measurement", "mdi:sine-wave", "diagnostic"),
    EcoFlowSensorDef("ems_feed_power_limit_w", "Feed Power Limit", "W", "power", "measurement", "mdi:transmission-tower-export", "diagnostic"),
    EcoFlowSensorDef("ems_feed_ratio_pct", "Feed Ratio", "%", None, "measurement", "mdi:percent", "diagnostic"),
    EcoFlowSensorDef("batt_charge_discharge_state", "Battery Charge/Discharge State", None, None, None, "mdi:battery-sync", "diagnostic"),
]

POWEROCEAN_BINARY_SENSORS: list[EcoFlowBinarySensorDef] = [
]


# =====================================================================
# Delta 2 Max sensor definitions (from ha_delta_discovery.py)
# =====================================================================

DELTA2MAX_SENSORS: list[EcoFlowSensorDef] = [
    # --- Battery / SoC ---
    EcoFlowSensorDef("soc", "SoC", "%", "battery", "measurement", "mdi:battery"),
    EcoFlowSensorDef("bms_soh_pct", "Battery SoH", "%", "battery", "measurement", "mdi:battery-heart-variant"),
    EcoFlowSensorDef("bms_precise_soc", "Precise SoC", "%", "battery", "measurement", "mdi:battery-sync"),
    # --- Power (W) ---
    EcoFlowSensorDef("watts_in_sum", "Input Total", "W", "power", "measurement", "mdi:flash"),
    EcoFlowSensorDef("watts_out_sum", "Output Total", "W", "power", "measurement", "mdi:flash"),
    EcoFlowSensorDef("ac_in_w", "AC Input", "W", "power", "measurement", "mdi:power-plug"),
    EcoFlowSensorDef("ac_out_w", "AC Output", "W", "power", "measurement", "mdi:power-plug-outline"),
    EcoFlowSensorDef("solar_in_w", "Solar Input", "W", "power", "measurement", "mdi:solar-power"),
    EcoFlowSensorDef("solar2_in_w", "Solar 2 Input", "W", "power", "measurement", "mdi:solar-power"),
    EcoFlowSensorDef("mppt_out_w", "MPPT Output", "W", "power", "measurement", "mdi:solar-panel-large"),
    EcoFlowSensorDef("car_12v_out_w", "12V Output", "W", "power", "measurement", "mdi:car-battery"),
    EcoFlowSensorDef("dcdc_12v_w", "DC-DC 12V", "W", "power", "measurement", "mdi:current-dc"),
    EcoFlowSensorDef("car_out_w", "Car Output", "W", "power", "measurement", "mdi:car-electric"),
    EcoFlowSensorDef("usb1_w", "USB 1", "W", "power", "measurement", "mdi:usb"),
    EcoFlowSensorDef("usb2_w", "USB 2", "W", "power", "measurement", "mdi:usb"),
    EcoFlowSensorDef("usb_qc1_w", "USB QC 1", "W", "power", "measurement", "mdi:usb"),
    EcoFlowSensorDef("usb_qc2_w", "USB QC 2", "W", "power", "measurement", "mdi:usb"),
    EcoFlowSensorDef("typec1_w", "Type-C 1", "W", "power", "measurement", "mdi:usb-c-port"),
    EcoFlowSensorDef("typec2_w", "Type-C 2", "W", "power", "measurement", "mdi:usb-c-port"),
    EcoFlowSensorDef("ac_chg_rated_power_w", "AC Charge Rated Power", "W", "power", "measurement", "mdi:lightning-bolt"),
    # --- Voltage (V) ---
    EcoFlowSensorDef("batt_voltage_v", "Battery Voltage", "V", "voltage", "measurement", "mdi:flash-triangle"),
    EcoFlowSensorDef("ac_out_vol_v", "AC Output Voltage", "V", "voltage", "measurement", "mdi:sine-wave"),
    EcoFlowSensorDef("ac_in_vol_v", "AC Input Voltage", "V", "voltage", "measurement", "mdi:sine-wave"),
    EcoFlowSensorDef("dc_in_vol_v", "DC Input Voltage", "V", "voltage", "measurement", "mdi:current-dc"),
    EcoFlowSensorDef("dcdc_12v_vol_v", "12V Rail Voltage", "V", "voltage", "measurement", "mdi:car-battery"),
    # --- Current (A) ---
    EcoFlowSensorDef("batt_current_a", "Battery Current", "A", "current", "measurement", "mdi:current-dc"),
    EcoFlowSensorDef("ac_out_amp_a", "AC Output Current", "A", "current", "measurement", "mdi:current-ac"),
    EcoFlowSensorDef("solar_in_amp_a", "Solar Current", "A", "current", "measurement", "mdi:solar-power"),
    EcoFlowSensorDef("solar2_in_amp_a", "Solar 2 Current", "A", "current", "measurement", "mdi:solar-power"),
    # --- Temperature ---
    EcoFlowSensorDef("batt_temp_c", "Battery Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer"),
    EcoFlowSensorDef("inv_out_temp_c", "Inverter Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer"),
    EcoFlowSensorDef("dc_in_temp_c", "DC Input Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer"),
    EcoFlowSensorDef("mppt_temp_c", "MPPT Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer"),
    EcoFlowSensorDef("solar2_mppt_temp_c", "MPPT 2 Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer"),
    EcoFlowSensorDef("batt_max_cell_temp_c", "Max Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-high"),
    EcoFlowSensorDef("batt_min_cell_temp_c", "Min Cell Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-low"),
    EcoFlowSensorDef("batt_max_mos_temp_c", "Max MOSFET Temp", "\u00b0C", "temperature", "measurement", "mdi:thermometer-alert"),
    # --- Duration ---
    EcoFlowSensorDef("remain_time_min", "Remaining Time", "min", "duration", "measurement", "mdi:timer-sand"),
    EcoFlowSensorDef("chg_remain_time_min", "Charge Time Remaining", "min", "duration", "measurement", "mdi:battery-clock"),
    EcoFlowSensorDef("dsg_remain_time_min", "Discharge Time Remaining", "min", "duration", "measurement", "mdi:battery-clock-outline"),
    # --- Frequency ---
    EcoFlowSensorDef("ac_out_freq_hz", "AC Output Frequency", "Hz", "frequency", "measurement", "mdi:sine-wave"),
    EcoFlowSensorDef("ac_in_freq_hz", "AC Input Frequency", "Hz", "frequency", "measurement", "mdi:sine-wave"),
    # --- Capacity ---
    EcoFlowSensorDef("batt_remain_cap_mah", "Remaining Capacity", "mAh", None, "measurement", "mdi:battery-50"),
    EcoFlowSensorDef("batt_full_cap_mah", "Full Capacity", "mAh", None, "measurement", "mdi:battery"),
    # --- Counters / State ---
    EcoFlowSensorDef("bms_cycles", "Battery Cycles", None, None, "total_increasing", "mdi:counter"),
    EcoFlowSensorDef("fan_level", "Fan Level", None, None, "measurement", "mdi:fan"),
    EcoFlowSensorDef("chg_dsg_state", "Charge/Discharge State", None, None, None, "mdi:battery-charging"),
    EcoFlowSensorDef("ems_chg_state", "EMS Charge State", None, None, None, "mdi:battery-charging-outline"),
    EcoFlowSensorDef("charger_type", "Charger Type", None, None, None, "mdi:ev-plug-type2"),
    EcoFlowSensorDef("mppt_chg_state", "MPPT Charge State", None, None, None, "mdi:solar-panel"),
    EcoFlowSensorDef("ems_lcd_soc", "LCD SoC", "%", "battery", "measurement", "mdi:monitor"),
    EcoFlowSensorDef("ems_precise_soc", "EMS Precise SoC", "%", "battery", "measurement", "mdi:monitor"),
    # --- Energy Dashboard (total_increasing, kWh) ---
    EcoFlowSensorDef("solar_energy_kwh", "Solar Energy", "kWh", "energy", "total_increasing", "mdi:solar-power"),
    EcoFlowSensorDef("solar2_energy_kwh", "Solar 2 Energy", "kWh", "energy", "total_increasing", "mdi:solar-power"),
    EcoFlowSensorDef("ac_in_energy_kwh", "AC Input Energy", "kWh", "energy", "total_increasing", "mdi:power-plug"),
    EcoFlowSensorDef("ac_out_energy_kwh", "AC Output Energy", "kWh", "energy", "total_increasing", "mdi:power-plug-outline"),
    # --- Cell voltages (diagnostic) ---
    EcoFlowSensorDef("batt_max_cell_vol_mv", "Max Cell Voltage", "mV", "voltage", "measurement", "mdi:flash-triangle-outline", "diagnostic"),
    EcoFlowSensorDef("batt_min_cell_vol_mv", "Min Cell Voltage", "mV", "voltage", "measurement", "mdi:flash-triangle-outline", "diagnostic"),
    # --- Error codes (diagnostic) ---
    EcoFlowSensorDef("pd_err_code", "PD Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic"),
    EcoFlowSensorDef("inv_err_code", "Inverter Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic"),
    EcoFlowSensorDef("bms_err_code", "BMS Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic"),
    EcoFlowSensorDef("mppt_fault_code", "MPPT Fault Code", None, None, None, "mdi:alert-circle-outline", "diagnostic"),
]

DELTA2MAX_BINARY_SENSORS: list[EcoFlowBinarySensorDef] = [
    EcoFlowBinarySensorDef("ac_enabled", "AC Enabled", "power", "mdi:power-plug"),
    EcoFlowBinarySensorDef("dc_out_enabled", "DC Output Enabled", "power", "mdi:flash"),
    EcoFlowBinarySensorDef("car_12v_enabled", "12V Enabled", "power", "mdi:car-battery"),
    EcoFlowBinarySensorDef("ups_enabled", "UPS Enabled", "power", "mdi:lightning-bolt"),
    EcoFlowBinarySensorDef("ac_xboost", "AC X-Boost", "power", "mdi:flash-auto"),
]

DELTA2MAX_SWITCHES: list[EcoFlowSwitchDef] = [
    EcoFlowSwitchDef("ac_switch", "AC Output", "ac_enabled", "mdi:power-plug"),
    EcoFlowSwitchDef("dc_switch", "DC Output", "dc_out_enabled", "mdi:flash"),
    EcoFlowSwitchDef("car_12v_switch", "12V Output", "car_12v_enabled", "mdi:car-battery"),
]

DELTA2MAX_NUMBERS: list[EcoFlowNumberDef] = [
    EcoFlowNumberDef("ac_charge_speed", "AC Charge Speed", "ac_chg_rated_power_w", "W", "mdi:lightning-bolt", 200, 2400, 100),
    EcoFlowNumberDef("max_charge_soc", "Max Charge SoC", "max_charge_soc", "%", "mdi:battery-charging-100", 50, 100, 1),
    EcoFlowNumberDef("min_discharge_soc", "Min Discharge SoC", "min_discharge_soc", "%", "mdi:battery-alert-variant-outline", 0, 30, 1),
    EcoFlowNumberDef("standby_timeout", "Standby Timeout", "standby_timeout_min", "min", "mdi:timer-off-outline", 0, 720, 1),
]


# =====================================================================
# Smart Plug sensor definitions
# =====================================================================

SMARTPLUG_SENSORS: list[EcoFlowSensorDef] = [
    EcoFlowSensorDef("power_w", "Power", "W", "power", "measurement", "mdi:flash"),
    EcoFlowSensorDef("current_a", "Current", "A", "current", "measurement", "mdi:current-ac"),
    EcoFlowSensorDef("voltage_v", "Voltage", "V", "voltage", "measurement", "mdi:sine-wave"),
    EcoFlowSensorDef("frequency_hz", "Frequency", "Hz", "frequency", "measurement", "mdi:sine-wave", "diagnostic"),
    EcoFlowSensorDef("temperature_c", "Temperature", "\u00b0C", "temperature", "measurement", "mdi:thermometer", "diagnostic"),
    EcoFlowSensorDef("max_power_w", "Max Power Rating", "W", "power", None, "mdi:flash-alert", "diagnostic"),
    EcoFlowSensorDef("max_current_a", "Max Current Rating", "A", "current", None, "mdi:current-ac", "diagnostic"),
    EcoFlowSensorDef("led_brightness", "LED Brightness", None, None, "measurement", "mdi:brightness-6", "diagnostic"),
    EcoFlowSensorDef("error_code", "Error Code", None, None, None, "mdi:alert-circle-outline", "diagnostic"),
    EcoFlowSensorDef("warning_code", "Warning Code", None, None, None, "mdi:alert-outline", "diagnostic"),
    # --- Energy Dashboard (total_increasing, kWh) ---
    EcoFlowSensorDef("energy_kwh", "Energy", "kWh", "energy", "total_increasing", "mdi:flash"),
]

SMARTPLUG_BINARY_SENSORS: list[EcoFlowBinarySensorDef] = [
    EcoFlowBinarySensorDef("switch_state", "Relay", "power", "mdi:power-plug"),
]

SMARTPLUG_SWITCHES: list[EcoFlowSwitchDef] = [
    EcoFlowSwitchDef("plug_switch", "Plug", "switch_state", "mdi:power-plug"),
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
