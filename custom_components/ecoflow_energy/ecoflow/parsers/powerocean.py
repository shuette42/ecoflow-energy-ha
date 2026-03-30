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

from __future__ import annotations

import json
from typing import Any

from . import _safe_float


def parse_powerocean_http_quota(quota_data: dict) -> dict[str, Any]:
    """Parse a PowerOcean GET /quota/all response into flat sensor keys.

    Args:
        quota_data: The "data" dict from the API response.

    Returns:
        Dict mapping sensor keys to values.
    """
    result: dict[str, Any] = {}

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

    # --- Per-pack battery data (all packs → pack{n}_* keys) ---
    result.update(_extract_all_battery_packs(quota_data))

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

    # --- EMS extended fields (from ems_change_report.*) ---
    _extract_ems_extended(quota_data, result)

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
            if isinstance(val, dict) and _is_real_battery_pack(val):
                bp_data = val
                break  # Use first real pack

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


def _is_real_battery_pack(bp_dict: dict) -> bool:
    """Check if a bp_addr dict represents a real battery pack.

    The EcoFlow API may return a phantom/empty entry (e.g. the EMS module)
    before real battery packs.  A real pack always has at least one core
    battery field with a non-None value.
    """
    _BATTERY_INDICATORS = ("bpSoc", "bpPwr", "bpSoh", "bpCycles", "bpVol")
    return any(bp_dict.get(k) is not None for k in _BATTERY_INDICATORS)


def _extract_all_battery_packs(quota_data: dict) -> dict[str, Any]:
    """Extract per-pack battery data from all bp_addr.{SN} keys.

    Maps each pack to pack{n}_* sensor keys (n = 1..5).
    Existing bp_* sensors are NOT affected — this produces separate keys.
    Phantom/empty entries (no core battery fields) are skipped so that
    numbering starts at 1 for the first real battery pack.
    """
    result: dict[str, Any] = {}

    pack_num = 0
    for key, val in quota_data.items():
        if not key.startswith("bp_addr.") or key == "bp_addr.updateTime":
            continue

        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                continue

        if not isinstance(val, dict):
            continue

        # Skip phantom/empty packs (e.g. EMS module entry)
        if not _is_real_battery_pack(val):
            continue

        pack_num += 1
        if pack_num > 5:
            break  # Max 5 packs

        prefix = f"pack{pack_num}"

        # Core + diagnostic fields (no scaling needed — verified from probe data)
        _field_map = {
            "bpSoc": f"{prefix}_soc",
            "bpPwr": f"{prefix}_power_w",
            "bpSoh": f"{prefix}_soh",
            "bpCycles": f"{prefix}_cycles",
            "bpVol": f"{prefix}_voltage_v",
            "bpAmp": f"{prefix}_current_a",
            "bpRemainWatth": f"{prefix}_remain_watth",
            "bpMaxCellTemp": f"{prefix}_max_cell_temp_c",
            "bpMinCellTemp": f"{prefix}_min_cell_temp_c",
            "bpEnvTemp": f"{prefix}_env_temp_c",
            "bpCalendarSoh": f"{prefix}_calendar_soh",
            "bpCycleSoh": f"{prefix}_cycle_soh",
            "bpMaxMosTemp": f"{prefix}_max_mos_temp_c",
            "bpHvMosTemp": f"{prefix}_hv_mos_temp_c",
            "bpLvMosTemp": f"{prefix}_lv_mos_temp_c",
            "bpBusVol": f"{prefix}_bus_voltage_v",
            "bpPtcTemp": f"{prefix}_ptc_temp_c",
            "bpCellMaxVol": f"{prefix}_cell_max_vol_mv",
            "bpCellMinVol": f"{prefix}_cell_min_vol_mv",
            "bpDesignCap": f"{prefix}_design_cap_mah",
            "bpFullCap": f"{prefix}_full_cap_mah",
            "bpErrCode": f"{prefix}_error_code",
        }

        for api_key, sensor_key in _field_map.items():
            val_field = val.get(api_key)
            if val_field is not None:
                fv = _safe_float(val_field)
                if fv is not None:
                    result[sensor_key] = fv

        # Lifetime energy: Wh -> kWh (divide by 1000)
        for api_key, sensor_key in (
            ("bpAccuChgEnergy", f"{prefix}_accu_chg_energy_kwh"),
            ("bpAccuDsgEnergy", f"{prefix}_accu_dsg_energy_kwh"),
        ):
            val_field = val.get(api_key)
            if val_field is not None:
                fv = _safe_float(val_field)
                if fv is not None:
                    result[sensor_key] = fv / 1000.0

    return result


def _extract_ems_extended(quota_data: dict, result: dict) -> None:
    """Extract additional EMS/system fields from ems_change_report.* keys."""
    ems_prefix = "ems_change_report."

    _ems_extended = {
        "sysBatChgUpLimit": "ems_charge_upper_limit_pct",
        "sysBatDsgDownLimit": "ems_discharge_lower_limit_pct",
        "emsKeepSoc": "ems_keep_soc_pct",
        "sysBatBackupRatio": "ems_backup_ratio_pct",
        "mppt1FaultCode": "mppt1_fault_code",
        "mppt2FaultCode": "mppt2_fault_code",
        "pcsAcErrCode": "pcs_ac_error_code",
        "pcsDcErrCode": "pcs_dc_error_code",
        "pcsAcWarningCode": "pcs_ac_warning_code",
        "wifiStaStat": "wifi_status",
        "ethWanStat": "ethernet_status",
        "iot4gSta": "cellular_status",
        "emsCtrlLedBright": "ems_led_brightness",
        "emsWorkState": "ems_work_state",
    }
    for api_key, sensor_key in _ems_extended.items():
        full_key = ems_prefix + api_key
        if full_key in quota_data:
            fv = _safe_float(quota_data[full_key])
            if fv is not None:
                result[sensor_key] = fv

    # Nested poAiSchedule fields (system capabilities)
    _ai_schedule = {
        "poAiSchedule.bpFullCap": "ems_total_battery_capacity_wh",
        "poAiSchedule.pcsMaxOutPwr": "pcs_max_output_power_w",
        "poAiSchedule.pcsMaxInPwr": "pcs_max_input_power_w",
        "poAiSchedule.bpChgPwrMax": "bp_max_charge_power_w",
        "poAiSchedule.bpDsgPwrMax": "bp_max_discharge_power_w",
    }
    for api_key, sensor_key in _ai_schedule.items():
        full_key = ems_prefix + api_key
        if full_key in quota_data:
            fv = _safe_float(quota_data[full_key])
            if fv is not None:
                result[sensor_key] = fv
