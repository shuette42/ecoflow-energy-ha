"""PowerOcean protobuf key remapping for Enhanced Mode.

Converts raw protobuf decoder output (EnergyStream, EMS heartbeat,
battery heartbeat, EMS change report) into sensor-compatible keys
used by the coordinator and entity platforms.

No Home Assistant dependencies - stdlib only.
"""

from __future__ import annotations

import logging
from typing import Any

from .powerocean import (
    _CHG_DSG_STATE_MAP,
    _FEED_MODE_MAP,
    _GRID_STATUS_MAP,
    _PCS_RUN_STATE_INT_MAP,
    _PCS_RUN_STATE_MAP,
    _WORK_MODE_INT_MAP,
    _WORK_MODE_MAP,
    _WORK_STATE_MAP,
    drop_invalid_percentages,
)

_LOGGER = logging.getLogger(__name__)

# Proto enum sensor keys present in EMS Change Report (cmd_id=8).
# With oneof wrappers in the proto, zero-values are preserved.
_PROTO_ENUM_INT: dict[str, dict[int, str]] = {
    "grid_status": _GRID_STATUS_MAP,
    "batt_charge_discharge_state": _CHG_DSG_STATE_MAP,
    "ems_feed_mode": _FEED_MODE_MAP,
    "ems_work_mode": _WORK_MODE_INT_MAP,
    "pcs_run_state": _PCS_RUN_STATE_INT_MAP,
}

# String enum maps (HTTP path sends "WORKMODE_SELFUSE" etc.)
_PROTO_ENUM_STR: dict[str, dict[str, str]] = {
    "pcs_run_state": _PCS_RUN_STATE_MAP,
    "ems_work_mode": _WORK_MODE_MAP,
}

# Work state enum (separate from work mode). Present in HTTP
# (ems_change_report.emsWorkState) and proto EMS Change Report (field 205).
_WORK_STATE_ENUM_INT: dict[str, dict[int, str]] = {
    "ems_work_state": _WORK_STATE_MAP,
}

_CONNECTIVITY_KEYS: frozenset[str] = frozenset(
    {"wifi_status", "ethernet_status", "cellular_status"}
)

# WiFi/Ethernet: 0 = connected, non-zero = error/disconnected.
# 4G (cellular): 1 = connected per observed portal behavior.
_WIFI_ETH_KEYS: frozenset[str] = frozenset({"wifi_status", "ethernet_status"})


def _apply_enum_mappings(result: dict[str, Any]) -> None:
    """Apply all enum and connectivity mappings to sensor-keyed result in place.

    Unknown integer values (e.g. firmware adding new states) are dropped
    rather than passed through, because HA's enum sensors raise
    ``ValueError: state value 'N' not in options`` for any value not in
    the declared options list.
    """
    for sensor_key, mapping in _PROTO_ENUM_INT.items():
        if sensor_key in result:
            value = result[sensor_key]
            if isinstance(value, str) and sensor_key in _PROTO_ENUM_STR:
                # String value (HTTP-style enum): leave it for the
                # string-map loop below instead of dropping it here.
                continue
            iv = int(value) if isinstance(value, (int, float)) else None
            if iv is not None and iv in mapping:
                result[sensor_key] = mapping[iv]
            else:
                # Drop unknown enum value - keeping the raw int crashes the
                # HA sensor with "not in list of options".
                _LOGGER.debug(
                    "Unknown enum value for %s: %r (dropped)",
                    sensor_key,
                    result[sensor_key],
                )
                result.pop(sensor_key, None)

    for sensor_key, mapping in _WORK_STATE_ENUM_INT.items():
        if sensor_key in result:
            iv = (
                int(result[sensor_key])
                if isinstance(result[sensor_key], (int, float))
                else None
            )
            if iv is not None and iv in mapping:
                result[sensor_key] = mapping[iv]
            else:
                _LOGGER.debug(
                    "Unknown work-state value: %r (dropped)",
                    result[sensor_key],
                )
                result.pop(sensor_key, None)

    for sensor_key in _CONNECTIVITY_KEYS:
        if sensor_key in result:
            iv = (
                int(result[sensor_key])
                if isinstance(result[sensor_key], (int, float))
                else 0
            )
            if sensor_key in _WIFI_ETH_KEYS:
                result[sensor_key] = "connected" if iv == 0 else "disconnected"
            else:
                result[sensor_key] = "connected" if iv == 1 else "disconnected"

    for sensor_key, mapping in _PROTO_ENUM_STR.items():
        if sensor_key in result:
            raw_val = str(result[sensor_key])
            result[sensor_key] = mapping.get(raw_val, raw_val)

    # grid_is_energized (bool, field 752) overrides sys_grid_sta when present.
    # The EcoFlow app uses gridIsEnergized for the main grid display.
    if "grid_is_energized" in result:
        result["grid_status"] = (
            "ok" if result.pop("grid_is_energized") else "not_detected"
        )


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
    # Hottest of the EMS-internal NTC probes. Already decoded from the
    # heartbeat (field 81), it simply had no entity.
    "ems_ntc_temp_max": "ems_ntc_temp_max_c",
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
    "wifi_sta_stat": "wifi_status",
    "eth_wan_stat": "ethernet_status",
    "iot_4g_sta": "cellular_status",
    "grid_is_energized": "grid_is_energized",
    "ems_work_state": "ems_work_state",
    # Previously undecoded, both cmd_id=8 fields.
    "ems_sg_ready_en": "ems_sg_ready_enabled",
    "ems_sg_run_stat": "ems_sg_ready_state",
    "battery_limit_reason": "battery_limit_reason",
}

# EMS state report (cmd_id=17) key -> sensor key mapping.
#
# The device sends this through the same message type as cmd_id=8 but with a
# completely disjoint field set: a PowerOcean Plus Get-All bundle contains
# both, 27 fields in cmd 8 and 71 in cmd 17, with zero overlap. It gets its
# own table rather than sharing the one above, because sharing would silently
# hand cmd 17 the right to write keys whose cmd-17 meaning is not established.
#
# That is not hypothetical. In the R374 bundle the phase containers report
# 237 V on all three phases and -3725 W of export while cmd 17 reports
# `sys_grid_sta = 0` and `bp_chg_dsg_sta = 2` - "grid not detected" and
# "discharging" on a grid-exporting unit whose battery power is zero. Under
# the cmd-8 table those two values would land on `grid_status` and
# `batt_charge_discharge_state` and be visibly wrong. The same reasoning
# excludes `sys_work_sta`, `ems_work_state`, `bp_soc`, `bp_online_sum` and
# the two lifetime energy counters (0 in every observed frame).
#
# Those keys keep their existing owner. Re-add one here only with a capture
# that shows the cmd-17 value tracking the device state it claims to
# describe.
EMS_STATE_TO_SENSOR: dict[str, str] = {
    # Error and warning codes. The five PCS and MPPT *fault* codes below also
    # come out of the HTTP quota, so those entities exist in both modes and
    # this message is what finally fills them on account sign-in. The two MPPT
    # *warning* codes are not in the quota at all - they are marked
    # enhanced_only in const.py, because an entity created in Standard Mode
    # would have nothing to read.
    "pcs_ac_err_code": "pcs_ac_error_code",
    "pcs_dc_err_code": "pcs_dc_error_code",
    "pcs_ac_warning_code": "pcs_ac_warning_code",
    "mppt1_fault_code": "mppt1_fault_code",
    "mppt2_fault_code": "mppt2_fault_code",
    "mppt1_warning_code": "mppt1_warning_code",
    "mppt2_warning_code": "mppt2_warning_code",
    # Arc-fault detector and hardware fault flags.
    "afci_sellf_test_result": "afci_self_test_result",
    "afci_fault_flag_ch1": "afci_fault_ch1",
    "afci_fault_flag_ch2": "afci_fault_ch2",
    "bp_line_off_flag": "battery_line_off",
    "bat_relay_close_fail_flag": "battery_relay_fault",
    # Self-check, maintenance and topology states.
    "sys_heat_stat": "sys_heat_state",
    "sys_cal_stat": "sys_calibration_state",
    "parallel_type": "parallel_mode",
    "ems_sys_self_check_stat": "ems_self_check_state",
    # Run state and connectivity. Kept because the same bundle corroborates
    # them: the inverter is running, and the unit is reachable over WiFi.
    "pcs_run_sta": "pcs_run_state",
    "wifi_sta_stat": "wifi_status",
    "eth_wan_stat": "ethernet_status",
    "iot_4g_sta": "cellular_status",
}

# Lifetime energy totals. A zero here is never a reading: the counters only
# ever grow, so the device reports 0 when it has nothing to report. Feeding
# it into a total_increasing sensor makes Home Assistant read a meter reset
# and book the whole standing total a second time. The PowerOcean Plus sends
# exactly this - its cmd_id=17 carries both counters at 0 on every frame.
_LIFETIME_ENERGY_SENSORS: frozenset[str] = frozenset(
    {"batt_charge_energy_kwh", "batt_discharge_energy_kwh"}
)

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
# dict, the pack is real. Proto3 MessageToDict omits zero-valued fields,
# but a real battery always has bp_design_cap/bp_full_cap > 0 and bp_sn
# non-empty, so at least one key will be present. An EMS module placeholder
# produces {} (no battery fields at all).
BP_IDENTITY_KEYS: frozenset[str] = frozenset(
    {
        "bp_soc",
        "bp_pwr",
        "bp_soh",
        "bp_vol",
        "bp_cycles",
        "bp_design_cap",
        "bp_full_cap",
        "bp_sn",
    }
)


def remap_proto_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """Remap protobuf decoder keys to sensor keys.

    Protobuf outputs: solar, home_direct, batt_pb, grid_raw_f2, soc
    Sensors expect:   solar_w, home_w, batt_w, grid_w, soc_pct

    Also computes derived power splits (same logic as HTTP parser output).
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

    drop_invalid_percentages(result)

    return result


def flatten_heartbeat(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract nested messages from an EMS heartbeat (cmd_id=1).

    All MPPT heartbeat containers are flattened in order, exposing up to four
    physical PV inputs. For nested messages that exist, missing proto3 scalar
    values are treated as zero because a heartbeat is a full snapshot; this
    prevents a previous non-zero power/voltage/current value from staying stale
    when the device sends an explicit zero that MessageToDict omits.

    The grid phases are the one exception: two containers describe them and a
    scalar is only zeroed when neither reported it. See the comment there.
    """
    result: dict[str, Any] = {}

    # Scalar fields -> sensor keys
    for proto_key, sensor_key in HEARTBEAT_TO_SENSOR.items():
        val = raw.get(proto_key)
        if val is not None:
            result[sensor_key] = (
                float(val) if isinstance(val, (int, float)) else val
            )

    _apply_enum_mappings(result)

    # MPPT per-string. Captured PowerOcean Plus traffic carries one
    # mppt_heart_beat container holding three mppt_pv entries, so the previous
    # two-entry cap was what hid the third string. Several containers are still
    # walked in order because nothing guarantees a single one.
    #
    # pv_index is positional across containers: entry order decides which
    # physical string becomes mppt_pv1..4. A device that changes entry count or
    # container order between messages would silently remap the sensors. The
    # captured frames keep a stable order, and the entries carry no identifier
    # to key on, so position is the only option available.
    mppt_hb = raw.get("mppt_heart_beat")
    pv_index = 1
    if isinstance(mppt_hb, list):
        for mppt_data in mppt_hb:
            if not isinstance(mppt_data, dict):
                continue
            pv_arr = mppt_data.get("mppt_pv", [])
            if not isinstance(pv_arr, list):
                continue
            for pv in pv_arr:
                if pv_index > 4:
                    break
                if not isinstance(pv, dict):
                    continue
                prefix = f"mppt_pv{pv_index}"
                for field, suffix in (
                    ("pwr", "power_w"),
                    ("vol", "voltage_v"),
                    ("amp", "current_a"),
                ):
                    result[f"{prefix}_{suffix}"] = float(pv.get(field, 0.0))
                pv_index += 1
            if pv_index > 4:
                break

    # Grid phase data. Two containers describe the same three phases and both
    # can appear in one message, each carrying a different subset:
    #
    #   pcs_load_info[]   voltage and frequency
    #   pcs_a/b/c_phase   voltage, current, active, reactive and apparent power
    #
    # A value the device actually sent must never be replaced by a fabricated
    # zero. Both containers are therefore collected first and a scalar is only
    # zero-filled when no container reported it, which keeps a phase that drops
    # to zero from holding a stale reading. On an HJ31 pcs_load_info carries no
    # current and no power at all, so filling those from it reported 0 W while
    # the device was exporting over 200 W on a phase. Both fields exist in the
    # schema, so an omitted one is indistinguishable from a transmitted zero,
    # which is why absence must not be read as a measurement.
    #
    # On a conflict pcs_a/b/c_phase wins, because it is collected second. That
    # container is the one that models a phase electrically, so it is the
    # authoritative source for these sensors. Pinned by
    # test_phase_container_wins_over_load_info_on_conflict.
    #
    # The positional mapping of pcs_load_info entries to A, B and C is an
    # assumption. The schema carries no phase identifier there. It holds on the
    # observed hardware and is currently unobservable anyway, because the phase
    # container overwrites every key the load container can contribute.
    reported_vols: list[float] = []
    collected: dict[str, dict[str, float]] = {}

    def _collect(
        label: str,
        phase: dict[str, Any],
        fields: tuple[tuple[str, str], ...],
    ) -> None:
        vol = phase.get("vol")
        if isinstance(vol, (int, float)) and not isinstance(vol, bool):
            reported_vols.append(float(vol))
        # Created unconditionally, before any field is read. A container that
        # arrives with every scalar at its proto3 default carries no fields at
        # all, and that is exactly the dead-grid case whose zeros must still be
        # written. Moving this into the loop below would silently reintroduce
        # stale readings.
        target = collected.setdefault(label, {})
        for field, suffix in fields:
            value = phase.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                target[suffix] = float(value)

    load_info = raw.get("pcs_load_info")
    if isinstance(load_info, list):
        phase_names = ("a", "b", "c")
        for idx, phase in enumerate(load_info[:3]):
            if isinstance(phase, dict):
                _collect(
                    phase_names[idx],
                    phase,
                    (
                        ("vol", "voltage_v"),
                        ("amp", "current_a"),
                        ("pwr", "active_power_w"),
                    ),
                )

    for phase_key, label in (
        ("pcs_a_phase", "a"),
        ("pcs_b_phase", "b"),
        ("pcs_c_phase", "c"),
    ):
        phase = raw.get(phase_key)
        if isinstance(phase, dict):
            _collect(
                label,
                phase,
                (
                    ("vol", "voltage_v"),
                    ("amp", "current_a"),
                    ("act_pwr", "active_power_w"),
                    ("react_pwr", "reactive_power_var"),
                    ("apparent_pwr", "apparent_power_va"),
                ),
            )

    # Zero-fill only what no container reported. Reactive and apparent power
    # exist solely in pcs_a/b/c_phase, so they are filled only for a phase that
    # container described.
    for label, values in collected.items():
        for suffix in ("voltage_v", "current_a", "active_power_w"):
            result[f"grid_phase_{label}_{suffix}"] = values.get(suffix, 0.0)
        if isinstance(raw.get(f"pcs_{label}_phase"), dict):
            for suffix in ("reactive_power_var", "apparent_power_va"):
                result[f"grid_phase_{label}_{suffix}"] = values.get(suffix, 0.0)

    # Derive grid_status from phase voltage when not set by grid_is_energized.
    # sys_grid_sta is unreliable (always 0). The EcoFlow app uses gridIsEnergized
    # which is computed app-side, not sent by the device. We replicate that logic:
    # if any phase voltage > 50V, the grid is energized.
    #
    # Only reported voltages count. The zero-fill above writes 0.0 for every
    # omitted scalar, so a phase message carrying just power would otherwise
    # report a live grid as "not_detected".
    if "grid_status" not in result and reported_vols:
        result["grid_status"] = (
            "ok" if any(value > 50.0 for value in reported_vols) else "not_detected"
        )

    return result


def remap_bp_keys(
    raw: dict[str, Any],
    bp_sn_to_index: dict[str, int],
    device_sn: str,
) -> dict[str, Any]:
    """Remap battery heartbeat and EMS change keys to sensor keys.

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
        pack
        for pack in all_packs
        if isinstance(pack, dict) and any(key in pack for key in BP_IDENTITY_KEYS)
    ]
    _LOGGER.debug(
        # Four characters, like every other log line here. This one printed
        # the whole serial, and a reporter's 15 minute debug log carried it
        # 2714 times into a public issue (#219).
        "BP heartbeat for %s: %d pack(s) in message, %d real",
        device_sn[:4],
        len(all_packs),
        len(real_packs),
    )
    for pos, pack_data in enumerate(real_packs[:5], 1):
        # Stable pack numbering via SN. Mid-session the device sends one
        # pack per heartbeat, so positional indexing would assign every pack
        # to pack1; using bp_sn as key gives each physical battery a
        # consistent number across messages.
        #
        # The get-all bundle at connect is the exception and reads the other
        # way: one heartbeat there carries every pack at once, measured on
        # live hardware as "2 pack(s) in message, 2 real". That is also the
        # only moment the map is empty, so on a healthy connect the numbering
        # is settled by this one message rather than accumulated over
        # minutes. Do not read the mid-session behaviour as the general rule -
        # PLAN-076 built a whole mechanism on that misreading.
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
        sensor_key = BP_TO_SENSOR.get(proto_key) or EMS_CHANGE_TO_SENSOR.get(
            proto_key
        )
        if sensor_key:
            # Energy totals from EMS change report: Wh -> kWh
            if sensor_key in _LIFETIME_ENERGY_SENSORS:
                if isinstance(value, (int, float)) and value > 0:
                    result[sensor_key] = float(value) / 1000.0
            else:
                result[sensor_key] = (
                    float(value) if isinstance(value, (int, float)) else value
                )

    # Apply enum mappings to remapped sensor keys.
    # Map values when present; never inject zero-defaults.
    # EMS change reports only include fields that actually changed,
    # so most updates contain only bp_soc or are empty.
    # Injecting defaults would overwrite correct values from HTTP.
    _apply_enum_mappings(result)

    drop_invalid_percentages(result)

    return result


# --- PowerPulse wallbox (cmd_func 209) ---

# Only fields with both a name from the message definition and a check against
# the reporter's own session are listed. `switch_bits`, `work_mode` and the
# undeclared field numbers above 30 stay unread on purpose: they move, but
# nothing observed says what they mean, and a sensor named after a guess is
# worse than no sensor. See PLAN-079.
HEATING_ROD_TO_SENSOR: dict[str, str] = {
    "hr_temp": "heating_rod_water_temp_c",
    "hr_heating_power": "heating_rod_power_w",
    "hr_target_temp": "heating_rod_target_temp_c",
    "hr_target_power": "heating_rod_target_power_w",
}


def remap_heating_rod_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """Remap a PowerGlow report (cmd_func 212, cmd_id 8).

    The same four readings the Standard Mode quota supplies, from the report
    the PowerOcean forwards on its real-time stream. The powers are integer
    watts and the temperatures are floats in degrees Celsius, both as sent -
    a reporter capture carries 2159 W drawn against a 2160 W target at 69 of a
    requested 80 degrees, and no scaling reconciles those with the app.

    An idle rod reports its power as a real zero rather than omitting it, which
    the oneof wrapper on the message preserves. Nothing here is a meter, so no
    key reaches a total_increasing sensor.
    """
    result: dict[str, Any] = {}

    for proto_key, value in raw.items():
        sensor_key = HEATING_ROD_TO_SENSOR.get(proto_key)
        if sensor_key is None or not isinstance(value, (int, float)):
            continue
        result[sensor_key] = float(value)

    return result


EV_CHARGING_TO_SENSOR: dict[str, str] = {
    "ev_pwr": "ev_charge_power_w",
    "ev_charging_energy": "ev_session_energy_wh",
    "order_time": "ev_session_duration_s",
    "charging_status": "ev_charge_status",
    "charge_vehicle_id": "ev_vehicle_id",
}

# The charging status arrives as the enum's own name.
_EV_CHARGE_STATUS_MAP: dict[str, str] = {
    "EV_CHG_STS_NONE": "none",
    "EV_CHG_STS_AVAILABLE": "available",
    "EV_CHG_STS_PREPARING": "preparing",
    "EV_CHG_STS_CHARGING": "charging",
    "EV_CHG_STS_SUSPENDED_EVSE": "suspended_charger",
    "EV_CHG_STS_SUSPENDED_EV": "suspended_vehicle",
    "EV_CHG_STS_FINISHING": "finishing",
    "EV_CHG_STS_FAULTED": "faulted",
}

# What the charger reports before a car has been recognized. It is a string
# field, so the placeholder arrives as text rather than as an absent field.
_EV_NO_VEHICLE = "-1"


def remap_ev_charging_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """Remap a PowerPulse charging report (cmd_func 209, cmd_id 8).

    The frame describes one charging session, not a lifetime total: when a new
    session opens, power, energy, duration and the timestamps reset together.
    Nothing here is therefore a meter, and no key is fed to a total_increasing
    sensor.
    """
    result: dict[str, Any] = {}

    for proto_key, value in raw.items():
        sensor_key = EV_CHARGING_TO_SENSOR.get(proto_key)
        if sensor_key is None:
            continue
        if sensor_key == "ev_charge_status":
            mapped = _EV_CHARGE_STATUS_MAP.get(str(value))
            # An unknown state would crash the enum sensor with "not in list
            # of options", so it is dropped rather than passed through.
            if mapped is not None:
                result[sensor_key] = mapped
            continue
        if sensor_key == "ev_vehicle_id":
            text = str(value)
            result[sensor_key] = None if text == _EV_NO_VEHICLE else text
            continue
        result[sensor_key] = (
            float(value) if isinstance(value, (int, float)) else value
        )

    return result


def remap_ems_state_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """Remap an EMS state report (cmd_id=17) to sensor keys.

    Deliberately narrower than `remap_bp_keys`: only the fields listed in
    `EMS_STATE_TO_SENSOR` are surfaced, everything else in the frame is
    dropped. See the note on that table for why.
    """
    result: dict[str, Any] = {}

    for proto_key, value in raw.items():
        sensor_key = EMS_STATE_TO_SENSOR.get(proto_key)
        if sensor_key is None:
            continue
        result[sensor_key] = (
            float(value) if isinstance(value, (int, float)) else value
        )

    _apply_enum_mappings(result)

    return result
