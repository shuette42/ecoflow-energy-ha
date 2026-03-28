"""PowerOcean HTTP Quota API response parser.

Parses the response from GET /iot-open/sign/device/quota/all into
flat sensor keys matching POWEROCEAN_SENSORS in const.py.

The /quota/all response contains 300+ keys including:
- Top-level power values: mpptPwr, sysLoadPwr, bpPwr, sysGridPwr, bpSoc
- Nested battery packs: bp_addr.{sn}.bpCycles, bp_addr.{sn}.bpSoh, ...
- EMS data: emsBpAliveNum, ems_change_report.*, energy_stream.*
- PV data: mpptHeartBeat[].mpptPv[]

Reference: EcoFlow IoT Developer Platform — PowerOcean section.
"""

import json
from typing import Any, Dict, Optional


def _safe_float(val: Any) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def parse_powerocean_http_quota(quota_data: dict) -> Dict[str, Any]:
    """Parse a PowerOcean GET /quota/all response into flat sensor keys.

    Args:
        quota_data: The "data" dict from the API response.

    Returns:
        Dict mapping sensor keys to values.
    """
    result: Dict[str, Any] = {}

    # --- Top-level power values ---
    _simple = {
        "mpptPwr": "solar_w",
        "sysLoadPwr": "home_w",
        "sysGridPwr": "grid_w",
        "bpPwr": "batt_w",
        "bpSoc": "soc_pct",
    }
    for http_key, sensor_key in _simple.items():
        if http_key in quota_data:
            v = _safe_float(quota_data[http_key])
            if v is not None:
                result[sensor_key] = v

    # --- Derived power values ---
    grid_w = result.get("grid_w")
    if grid_w is not None:
        result["grid_import_power_w"] = grid_w if grid_w > 0.0 else 0.0
        result["grid_export_power_w"] = abs(grid_w) if grid_w < 0.0 else 0.0

    batt_w = result.get("batt_w")
    if batt_w is not None:
        result["batt_charge_power_w"] = batt_w if batt_w > 0.0 else 0.0
        result["batt_discharge_power_w"] = abs(batt_w) if batt_w < 0.0 else 0.0

    # --- EMS data ---
    if "emsBpAliveNum" in quota_data:
        v = _safe_float(quota_data["emsBpAliveNum"])
        if v is not None:
            result["ems_bp_alive_num"] = v

    # --- Battery pack data (first pack found in bp_addr.*) ---
    _extract_battery_pack(quota_data, result)

    # --- MPPT per-string (mpptHeartBeat[0].mpptPv[0|1]) ---
    mppt_hb = quota_data.get("mpptHeartBeat")
    if isinstance(mppt_hb, list) and mppt_hb:
        pvs = mppt_hb[0].get("mpptPv") if isinstance(mppt_hb[0], dict) else None
        if isinstance(pvs, list):
            for i, pv in enumerate(pvs[:2], start=1):
                if isinstance(pv, dict):
                    for field, suffix in [("pwr", "power_w"), ("vol", "voltage_v"), ("amp", "current_a")]:
                        if field in pv:
                            v = _safe_float(pv[field])
                            if v is not None:
                                result[f"mppt_pv{i}_{suffix}"] = v

    # --- Grid phase data (flat keys from GET /quota/all) ---
    _phase_fields = {
        "vol": "voltage_v",
        "actPwr": "active_power_w",
        "amp": "current_a",
        "reactPwr": "reactive_power_var",
        "apparentPwr": "apparent_power_va",
    }
    for phase_key, phase_label in [("pcsAPhase", "a"), ("pcsBPhase", "b"), ("pcsCPhase", "c")]:
        for api_field, sensor_suffix in _phase_fields.items():
            flat_key = f"{phase_key}.{api_field}"
            if flat_key in quota_data:
                v = _safe_float(quota_data[flat_key])
                if v is not None:
                    result[f"grid_phase_{phase_label}_{sensor_suffix}"] = v
            else:
                # Fallback: nested dict (from POST /quota or MQTT)
                phase = quota_data.get(phase_key)
                if isinstance(phase, dict) and api_field in phase:
                    v = _safe_float(phase[api_field])
                    if v is not None:
                        result[f"grid_phase_{phase_label}_{sensor_suffix}"] = v

    # --- PV Inverter Power (MPPT → PCS link) ---
    if "pvInvPwr" in quota_data:
        v = _safe_float(quota_data["pvInvPwr"])
        if v is not None:
            result["pv_inverter_power_w"] = v

    # --- Energy stream data (from ems_change_report or energy_stream) ---
    _extract_energy_stream(quota_data, result)

    return result


def _extract_battery_pack(quota_data: dict, result: dict) -> None:
    """Extract battery pack data from bp_addr.{sn} nested objects.

    Takes the first battery pack found and maps to sensor keys.
    If multiple packs exist, aggregates SOC/SOH from the top level.
    """
    bp_data = None
    for key, val in quota_data.items():
        if key.startswith("bp_addr.") and key != "bp_addr.updateTime":
            # API returns bp_addr.{sn} as JSON string, not dict
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    continue
            if isinstance(val, dict):
                bp_data = val
                break  # Use first pack

    if bp_data is None:
        return

    _bp_map = {
        "bpSoh": "bp_soh_pct",
        "bpCycles": "bp_cycles",
        "bpRemainWatth": "bp_remain_watth",
        "bpVol": "bp_voltage_v",
        "bpAmp": "bp_current_a",
        "bpMaxCellTemp": "bp_max_cell_temp_c",
        "bpMinCellTemp": "bp_min_cell_temp_c",
        "bpEnvTemp": "bp_env_temp_c",
        "bpMaxMosTemp": "bp_max_mos_temp_c",
        "bpCellMaxVol": "bp_cell_max_vol_mv",
        "bpCellMinVol": "bp_cell_min_vol_mv",
        "bpRealSoc": "bp_real_soc_pct",
        "bpRealSoh": "bp_real_soh_pct",
        "bpDownLimitSoc": "bp_down_limit_soc_pct",
        "bpUpLimitSoc": "bp_up_limit_soc_pct",
    }

    for http_key, sensor_key in _bp_map.items():
        if http_key in bp_data:
            v = _safe_float(bp_data[http_key])
            if v is not None:
                result[sensor_key] = v


def _extract_energy_stream(quota_data: dict, result: dict) -> None:
    """Extract energy stream / EMS change report data."""
    # Try ems_change_report prefix
    ems_prefix = "ems_change_report."
    # Numeric EMS fields
    _ems_numeric = {
        "emsFeedMode": "ems_feed_mode",
        "bpOnlineSum": "bp_online_sum",
        "pcsPfValue": "pcs_power_factor",
        "emsFeedPwr": "ems_feed_power_limit_w",
        "emsFeedRatio": "ems_feed_ratio_pct",
        "sysGridSta": "grid_status",
        "bpChgDsgSta": "batt_charge_discharge_state",
    }
    for http_key, sensor_key in _ems_numeric.items():
        full_key = ems_prefix + http_key
        if full_key in quota_data:
            v = quota_data[full_key]
            if isinstance(v, (int, float)):
                result[sensor_key] = v
            else:
                fv = _safe_float(v)
                if fv is not None:
                    result[sensor_key] = fv

    # String EMS fields (work mode, run state)
    _ems_string = {
        "emsWordMode": "ems_work_mode",
        "pcsRunSta": "pcs_run_state",
    }
    for http_key, sensor_key in _ems_string.items():
        full_key = ems_prefix + http_key
        if full_key in quota_data:
            result[sensor_key] = quota_data[full_key]

    # Energy totals from energy_stream if available
    _energy_keys = {
        "energy_stream.solarTotalEnergy": "solar_energy_kwh",
        "energy_stream.homeTotalEnergy": "home_energy_kwh",
        "energy_stream.gridInTotalEnergy": "grid_import_energy_kwh",
        "energy_stream.gridOutTotalEnergy": "grid_export_energy_kwh",
        "energy_stream.bpChgTotalEnergy": "batt_charge_energy_kwh",
        "energy_stream.bpDsgTotalEnergy": "batt_discharge_energy_kwh",
    }
    for http_key, sensor_key in _energy_keys.items():
        if http_key in quota_data:
            v = _safe_float(quota_data[http_key])
            if v is not None:
                # API returns Wh, sensors expect kWh
                result[sensor_key] = v / 1000.0

    # Also check top-level energy totals (from POST /quota response)
    _top_energy = {
        "mpptTotalEnergy": "solar_energy_kwh",
        "sysTotalLoadEnergy": "home_energy_kwh",
        "bpTotalChgEnergy": "batt_charge_energy_kwh",
        "bpTotalDsgEnergy": "batt_discharge_energy_kwh",
        "sysTotalGridEnergy": "grid_import_energy_kwh",
    }
    for http_key, sensor_key in _top_energy.items():
        if http_key in quota_data and sensor_key not in result:
            v = _safe_float(quota_data[http_key])
            if v is not None:
                result[sensor_key] = v / 1000.0

    # ems_change_report energy totals (GET /quota/all flattened keys)
    _ems_energy = {
        "ems_change_report.bpTotalChgEnergy": "batt_charge_energy_kwh",
        "ems_change_report.bpTotalDsgEnergy": "batt_discharge_energy_kwh",
    }
    for http_key, sensor_key in _ems_energy.items():
        if http_key in quota_data and sensor_key not in result:
            v = _safe_float(quota_data[http_key])
            if v is not None:
                result[sensor_key] = v / 1000.0

    # Grid frequency from PCS
    if "pcs_change_report.gridFreq" in quota_data:
        v = _safe_float(quota_data["pcs_change_report.gridFreq"])
        if v is not None:
            result["pcs_ac_freq_hz"] = v
