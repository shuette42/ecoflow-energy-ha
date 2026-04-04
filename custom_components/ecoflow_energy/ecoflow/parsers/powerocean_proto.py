"""PowerOcean protobuf key remapping for Enhanced Mode.

Converts raw protobuf decoder output (EnergyStream, EMS heartbeat,
battery heartbeat, EMS change report) into sensor-compatible keys
used by the coordinator and entity platforms.

No Home Assistant dependencies - stdlib only.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# EnergyStream (fast ~3s updates): proto key -> sensor key
PROTO_TO_SENSOR: dict[str, str] = {
    "solar": "solar_w",
    "home_direct": "home_w",
    "batt_pb": "batt_w",
    "grid_raw_f2": "grid_w",
    "soc": "soc_pct",
}

# Heartbeat (cmd_id=1) key -> sensor key mapping
HEARTBEAT_TO_SENSOR: dict[str, str] = {
    "pcs_ac_freq": "pcs_ac_freq_hz",
    "ems_bp_alive_num": "ems_bp_alive_num",
    "ems_pv_inv_pwr": "pv_inverter_power_w",
    "ems_work_mode": "ems_work_mode",
}

# Battery heartbeat (cmd_id=7) key -> sensor key mapping
BP_TO_SENSOR: dict[str, str] = {
    "bp_soh": "bp_soh_pct",
    "bp_cycles": "bp_cycles",
    # bp_remain_watth intentionally excluded - computed as sum of all packs
    "bp_vol": "bp_voltage_v",
    "bp_amp": "bp_current_a",
    "bp_max_cell_temp": "bp_max_cell_temp_c",
    "bp_min_cell_temp": "bp_min_cell_temp_c",
    "bp_env_temp": "bp_env_temp_c",
    "bp_max_mos_temp": "bp_max_mos_temp_c",
    "bp_cell_max_vol": "bp_cell_max_vol_mv",
    "bp_cell_min_vol": "bp_cell_min_vol_mv",
    "bp_real_soc": "bp_real_soc_pct",
    "bp_real_soh": "bp_real_soh_pct",
    "bp_down_limit_soc": "bp_down_limit_soc_pct",
    "bp_up_limit_soc": "bp_up_limit_soc_pct",
}

# EMS change report (cmd_id=8) key -> sensor key mapping
EMS_CHANGE_TO_SENSOR: dict[str, str] = {
    "bp_online_sum": "bp_online_sum",
    "ems_feed_mode": "ems_feed_mode",
    "ems_feed_ratio": "ems_feed_ratio_pct",
    "ems_feed_pwr": "ems_feed_power_limit_w",
    "sys_grid_sta": "grid_status",
    "bp_chg_dsg_sta": "batt_charge_discharge_state",
    "pcs_run_sta": "pcs_run_state",
    "ems_work_mode": "ems_work_mode",
    "pcs_pf_value": "pcs_power_factor",
    "bp_total_chg_energy": "batt_charge_energy_kwh",
    "bp_total_dsg_energy": "batt_discharge_energy_kwh",
    "sys_bat_chg_up_limit": "ems_charge_upper_limit_pct",
    "sys_bat_dsg_down_limit": "ems_discharge_lower_limit_pct",
    "ems_keep_soc": "ems_keep_soc_pct",
    "sys_bat_backup_ratio": "ems_backup_ratio_pct",
}

# Battery pack proto key suffix -> sensor key suffix (for multi-pack extraction)
BP_PACK_SENSOR_MAP: dict[str, str] = {
    "bp_soc": "soc",
    "bp_pwr": "power_w",
    "bp_soh": "soh",
    "bp_cycles": "cycles",
    "bp_vol": "voltage_v",
    "bp_amp": "current_a",
    "bp_remain_watth": "remain_watth",
    "bp_max_cell_temp": "max_cell_temp_c",
    "bp_min_cell_temp": "min_cell_temp_c",
    "bp_env_temp": "env_temp_c",
    "bp_calendar_soh": "calendar_soh",
    "bp_cycle_soh": "cycle_soh",
    "bp_max_mos_temp": "max_mos_temp_c",
    "bp_hv_mos_temp": "hv_mos_temp_c",
    "bp_lv_mos_temp": "lv_mos_temp_c",
    "bp_bus_vol": "bus_voltage_v",
    "bp_ptc_temp": "ptc_temp_c",
    "bp_cell_max_vol": "cell_max_vol_mv",
    "bp_cell_min_vol": "cell_min_vol_mv",
    "bp_design_cap": "design_cap_mah",
    "bp_full_cap": "full_cap_mah",
    "bp_err_code": "error_code",
}

# Core battery identity keys: if ANY of these are present in a proto pack
# dict, the pack is real.  Proto3 MessageToDict omits zero-valued fields,
# but a real battery always has bp_design_cap/bp_full_cap > 0 and bp_sn
# non-empty, so at least one key will be present.  An EMS module placeholder
# produces {} (no battery fields at all).
BP_IDENTITY_KEYS: frozenset[str] = frozenset({
    "bp_soc", "bp_pwr", "bp_soh", "bp_vol", "bp_cycles",
    "bp_design_cap", "bp_full_cap", "bp_sn",
})


def remap_proto_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """Remap protobuf decoder keys to sensor keys.

    Protobuf outputs: solar, home_direct, batt_pb, grid_raw_f2, soc
    Sensors expect:   solar_w, home_w, batt_w, grid_w, soc_pct

    Also computes derived power splits (grid import/export, batt charge/discharge)
    to match the HTTP parser output format.
    """
    result: dict[str, Any] = {}
    for proto_key, value in raw.items():
        sensor_key = PROTO_TO_SENSOR.get(proto_key, proto_key)
        result[sensor_key] = value

    # Derived power splits (same logic as HTTP parser)
    grid_w = result.get("grid_w")
    if grid_w is not None:
        result["grid_import_power_w"] = grid_w if grid_w > 0.0 else 0.0
        result["grid_export_power_w"] = abs(grid_w) if grid_w < 0.0 else 0.0

    batt_w = result.get("batt_w")
    if batt_w is not None:
        result["batt_charge_power_w"] = batt_w if batt_w > 0.0 else 0.0
        result["batt_discharge_power_w"] = abs(batt_w) if batt_w < 0.0 else 0.0

    return result


def flatten_heartbeat(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract nested messages from EMS heartbeat (cmd_id=1).

    Extracts: MPPT per-string, grid phase data, and scalar diagnostics.
    Mirrors the main EcoFlow service's mqtt_primary_pipeline extraction.
    """
    result: dict[str, Any] = {}

    # Scalar fields -> sensor keys
    for proto_key, sensor_key in HEARTBEAT_TO_SENSOR.items():
        val = raw.get(proto_key)
        if val is not None:
            result[sensor_key] = float(val) if isinstance(val, (int, float)) else val

    # MPPT per-string (nested in mppt_heart_beat[0].mppt_pv[])
    mppt_hb = raw.get("mppt_heart_beat")
    if isinstance(mppt_hb, list) and mppt_hb:
        mppt_data = mppt_hb[0] if isinstance(mppt_hb[0], dict) else {}
        pv_arr = mppt_data.get("mppt_pv", [])
        for idx, pv in enumerate(pv_arr[:2]):
            if isinstance(pv, dict):
                prefix = f"mppt_pv{idx + 1}"
                for field, suffix in (("pwr", "power_w"), ("vol", "voltage_v"), ("amp", "current_a")):
                    val = pv.get(field)
                    if val is not None:
                        result[f"{prefix}_{suffix}"] = float(val)

    # Grid phase data (nested in pcs_load_info[] or pcs_a/b/c_phase)
    load_info = raw.get("pcs_load_info")
    if isinstance(load_info, list):
        phase_names = ("a", "b", "c")
        for idx, phase in enumerate(load_info[:3]):
            if isinstance(phase, dict):
                label = phase_names[idx]
                for field, suffix in (("vol", "voltage_v"), ("amp", "current_a"), ("pwr", "active_power_w")):
                    val = phase.get(field)
                    if val is not None:
                        result[f"grid_phase_{label}_{suffix}"] = float(val)

    # Fallback: pcs_a/b/c_phase (JTS1PhaseInfo nested messages)
    for phase_key, label in (("pcs_a_phase", "a"), ("pcs_b_phase", "b"), ("pcs_c_phase", "c")):
        phase = raw.get(phase_key)
        if isinstance(phase, dict):
            for field, suffix in (("vol", "voltage_v"), ("amp", "current_a"), ("act_pwr", "active_power_w")):
                if f"grid_phase_{label}_{suffix}" not in result:
                    val = phase.get(field)
                    if val is not None:
                        result[f"grid_phase_{label}_{suffix}"] = float(val)

    return result


def remap_bp_keys(
    raw: dict[str, Any],
    bp_sn_to_index: dict[str, int],
    device_sn: str,
) -> dict[str, Any]:
    """Remap battery heartbeat (cmd_id=7) and EMS change (cmd_id=8) keys to sensor keys.

    Args:
        raw: Raw protobuf-decoded dict (mutated: all_packs is popped).
        bp_sn_to_index: Mutable SN-to-pack-index mapping (updated in place).
        device_sn: Device serial number for debug logging.
    """
    result: dict[str, Any] = {}

    # Multi-pack extraction from proto heartbeat (cmd_id=7)
    # Filter out phantom/empty packs using key-presence check:
    # A real battery pack always has at least one core identity key
    # (bp_design_cap, bp_full_cap, bp_sn are always >0/non-empty for real packs).
    # Proto3 MessageToDict omits zero-valued fields, so an EMS module
    # placeholder or wire-default entry produces {} (no identity keys).
    # This replaces the previous numeric non-zero filter that falsely
    # rejected idle packs whose power/SoC happened to be zero (#10).
    all_packs = raw.pop("all_packs", [])
    real_packs = [
        p for p in all_packs
        if isinstance(p, dict) and any(k in p for k in BP_IDENTITY_KEYS)
    ]
    _LOGGER.debug(
        "BP heartbeat for %s: %d pack(s) in message, %d real",
        device_sn, len(all_packs), len(real_packs),
    )
    for pos, pack_data in enumerate(real_packs[:5], 1):
        # Stable pack numbering via SN: the device sends one pack per
        # heartbeat, so positional indexing would assign every pack to
        # pack1.  Using bp_sn as key gives each physical battery a
        # consistent pack number across messages.
        sn = pack_data.get("bp_sn", "")
        if sn:
            if sn not in bp_sn_to_index:
                bp_sn_to_index[sn] = len(bp_sn_to_index) + 1
            idx = bp_sn_to_index[sn]
        else:
            # No SN available - fall back to positional index
            idx = pos
        if idx > 5:
            continue
        prefix = f"pack{idx}"
        for proto_key, sensor_suffix in BP_PACK_SENSOR_MAP.items():
            val = pack_data.get(proto_key)
            if val is not None:
                result[f"{prefix}_{sensor_suffix}"] = float(val)
        # Lifetime energy Wh -> kWh
        for proto_key, sensor_suffix in (
            ("bp_accu_chg_energy", "accu_chg_energy_kwh"),
            ("bp_accu_dsg_energy", "accu_dsg_energy_kwh"),
        ):
            val = pack_data.get(proto_key)
            if val is not None:
                result[f"{prefix}_{sensor_suffix}"] = float(val) / 1000.0

    # Try battery key mapping first, then EMS change mapping
    for proto_key, value in raw.items():
        sensor_key = (
            BP_TO_SENSOR.get(proto_key)
            or EMS_CHANGE_TO_SENSOR.get(proto_key)
        )
        if sensor_key:
            # Energy totals from EMS change report: Wh -> kWh
            if sensor_key in ("batt_charge_energy_kwh", "batt_discharge_energy_kwh"):
                if isinstance(value, (int, float)):
                    result[sensor_key] = float(value) / 1000.0
            else:
                result[sensor_key] = float(value) if isinstance(value, (int, float)) else value

    return result
