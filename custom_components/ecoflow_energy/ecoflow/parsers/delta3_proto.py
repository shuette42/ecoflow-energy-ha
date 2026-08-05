"""Delta 3 Max Plus protobuf telemetry parser (Enhanced Mode).

The Delta 3 generation never answers the JSON quota request on the
Enhanced Mode connection - it publishes protobuf frames only. Two of
those frames carry values we surface:

    cmd_func=254, cmd_id=21   main status frame (all contract fields)
    cmd_func=32,  cmd_id=2    battery heartbeat (SoC and SoC limits)
    cmd_func=32,  cmd_id=50   BMS heartbeat (battery health, see below)

The first two use the same field names as the HTTP quota response, just in
snake_case instead of camelCase. This module therefore does not
re-implement any parsing logic: it translates the decoded protobuf field
names back to their HTTP quota spelling and hands the result to
`parse_delta3_http_quota`. That keeps Enhanced Mode and Standard Mode on
one single code path, so both produce byte-identical sensor keys and the
entity IDs survive a mode switch. It also means the remaining-time
placeholder handling, the flow-state decoding (4 = inactive) and the
integer rounding exist exactly once.
"""

from __future__ import annotations

from typing import Any

from .delta3_http import parse_delta3_http_quota

# Power scaling for the protobuf transport. The protobuf field names are
# identical to the HTTP quota field names, and the HTTP path was verified
# on a DELTA 3 Max Plus to be direct watts (no deciwatt anywhere in this
# generation), so the same factor applies here. Kept as a single named
# constant: if a future measurement shows the protobuf transport scales
# differently, this line is the only thing that changes.
DELTA3_PROTO_POWER_SCALE = 1.0

# Protobuf field name -> HTTP quota key. Power fields (scaled by
# DELTA3_PROTO_POWER_SCALE) are listed separately from the plain
# pass-through fields.
_PROTO_POWER_FIELDS: dict[str, str] = {
    "pow_in_sum_w": "powInSumW",
    "pow_out_sum_w": "powOutSumW",
    "pow_get_ac_in": "powGetAcIn",
    "pow_get_pv": "powGetPv",
    "pow_get_pv2": "powGetPv2",
    "pow_get_12v": "powGet12v",
    "pow_get_typec1": "powGetTypec1",
    "pow_get_typec2": "powGetTypec2",
    "pow_get_typec3": "powGetTypec3",
    "pow_get_qcusb1": "powGetQcusb1",
    "pow_get_qcusb2": "powGetQcusb2",
}

# Percent, minute, enum and boolean fields - no scaling.
_PROTO_PLAIN_FIELDS: dict[str, str] = {
    "cms_batt_soc": "cmsBattSoc",
    "cms_chg_dsg_state": "cmsChgDsgState",
    "cms_dsg_rem_time": "cmsDsgRemTime",
    "cms_chg_rem_time": "cmsChgRemTime",
    "cms_max_chg_soc": "cmsMaxChgSoc",
    "cms_min_dsg_soc": "cmsMinDsgSoc",
    "backup_reverse_soc": "backupReverseSoc",
    "flow_info_ac_out": "flowInfoAcOut",
    "flow_info_ac2_out": "flowInfoAc2Out",
    "flow_info_12v": "flowInfo12v",
    "xboost_en": "xboostEn",
    "en_beep": "enBeep",
    "energy_backup_en": "energyBackupEn",
    "bypass_out_disable": "bypassOutDisable",
}

# Nested per-outlet arrays: protobuf submessage name -> (HTTP outer key,
# protobuf item name, HTTP item key).
_PROTO_LIST_FIELDS: dict[str, tuple[str, str, str]] = {
    "pow_get_ac_out_list": ("powGetAcOutList", "pow_get_ac_out_item", "powGetAcOutItem"),
    "pow_get_12v_list": ("powGet12vList", "pow_get_12v_item", "powGet12vItem"),
}

# Battery heartbeat (cmd_id=2), inner pack `v1p0`. Only fields whose
# meaning is identical to the HTTP quota are forwarded. The float SoC
# wins over the integer one when both are present.
#
# The pack also carries `max_charge_soc` (f7) and `min_dsg_soc` (f21),
# which look like the status-frame SoC limits (fields 270/271). They are
# deliberately NOT forwarded. A live capture over ten minutes on a
# DELTA 3 Max Plus produced 43 heartbeats and 4 full status frames, and
# both sources reported 100 / 0 throughout - agreement, but only at the
# extremes of the value range, where a differing semantic would look
# exactly the same. Since the heartbeat arrives every 10 s and the number
# entities are user-writable, forwarding an unproven field would make a
# limit flap at heartbeat rate. The status frame delivers both limits in
# full every 120 s and incrementally about 2 s after any change, so there
# is nothing to gain. Re-add only with a capture at a non-default limit
# (e.g. max 80 / min 20) that shows both sources agreeing.
_HEARTBEAT_SOC_FIELDS: tuple[str, ...] = ("lcd_show_soc", "f32_lcd_show_soc")

# Status-frame fields that exist on the push path only. They bypass the HTTP
# quota spelling on purpose: the polled quota never carries them, and putting
# them in the shared field map would claim a reach they do not have (#181).
_PROTO_ONLY_FIELDS: dict[str, str] = {
    "ac_in_chg_pow_max": "ac_charge_power_limit_w",
    # LCD backlight timeout in seconds. Reported raw rather than as the app's
    # label, so a diagnostics download shows what the device actually said even
    # when the value is not one of the six steps the app offers. The entity does
    # the labelling.
    "screen_off_time": "screen_off_time_sec",
    # The four idle shutdowns, in minutes - a different unit from the screen
    # timeout above, inside the same frame. Same reasoning for keeping them raw.
    "dev_standby_time": "dev_standby_time_min",
    "ac_standby_time": "ac_standby_time_min",
    "ac2_standby_time": "ac2_standby_time_min",
    "dc_standby_time": "dc_standby_time_min",
}

# AC charge mode, same push-only reach as the fields above. The wire value is
# the AC_IN_CHG_MODE enum; it is translated here rather than in the entity so
# the sensor key carries a label instead of a number that means nothing on its
# own. An unlisted value is dropped: a select showing an option the device did
# not report is worse than one showing nothing.
_AC_CHARGE_MODE_KEY = "ac_charge_mode"
_AC_CHARGE_MODES: dict[int, str] = {
    0: "self_def_pow",
    1: "bat_optimal_pow",
    2: "silence",
}

# Port priority, also push-only. `power_outage_port_type` -> sensor key stem.
# The names come from the app's own port list, which hard-codes type 2 as the
# first AC outlet and type 3 as the second.
PORT_PRIORITY_TYPES: dict[int, str] = {
    1: "dc",
    2: "ac1",
    3: "ac2",
}
_PORT_PRIORITY_ACTIVE_KEY = "port_priority_active"

# The device reports port priority as being in effect with exactly this value;
# every other value means it is not. Observed in both directions on a D3M1 on
# 2026-08-04 by cutting mains ahead of the unit: the flag went to 1 in the same
# frame that reported the AC input at 0 W, and back to 2 when mains returned.
# The state of charge never came near any port's cutoff during that run, which
# settles what "active" means - the feature engages when there is no AC input,
# not when a port is actually dropped.
_PORT_PRIORITY_ACTIVE_VALUE = 1


def port_priority_keys(stem: str) -> tuple[str, str]:
    """Return the (limited, cutoff) sensor keys for one port stem."""
    return f"port_priority_{stem}_limited", f"port_priority_{stem}_cutoff_soc"


def _port_priority_values(fields: dict[str, Any]) -> dict[str, Any]:
    """Flatten the port priority list into per-port keys.

    The read-back carries all three ports on every push, so each item is
    written out in full. `power_outage_port_enable` is proto3-default false
    and therefore absent whenever a port is essential, which is the common
    case - reading it with a default rather than a presence check is what
    keeps an essential port from holding a stale True.
    """
    result: dict[str, Any] = {}

    flag = fields.get("power_outages_active_flag")
    if isinstance(flag, int) and not isinstance(flag, bool):
        result[_PORT_PRIORITY_ACTIVE_KEY] = flag == _PORT_PRIORITY_ACTIVE_VALUE

    nested = fields.get("power_outages_list")
    if not isinstance(nested, dict):
        return result

    items = nested.get("power_outage_item")
    if not isinstance(items, list):
        return result

    for item in items:
        if not isinstance(item, dict):
            continue
        port_type = item.get("power_outage_port_type")
        stem = PORT_PRIORITY_TYPES.get(port_type) if isinstance(port_type, int) else None
        if stem is None:
            # Type 0 is the enum's null member. The app skips those items too.
            continue
        limited_key, cutoff_key = port_priority_keys(stem)
        result[limited_key] = bool(item.get("power_outage_port_enable", False))
        cutoff = item.get("power_outage_min_soc")
        if isinstance(cutoff, int) and not isinstance(cutoff, bool):
            result[cutoff_key] = cutoff

    return result


def _translate_display_property(fields: dict[str, Any]) -> dict[str, Any]:
    """Map decoded status-frame fields onto their HTTP quota spelling."""
    quota: dict[str, Any] = {}

    for proto_key, http_key in _PROTO_POWER_FIELDS.items():
        value = fields.get(proto_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            quota[http_key] = value * DELTA3_PROTO_POWER_SCALE

    for proto_key, http_key in _PROTO_PLAIN_FIELDS.items():
        if proto_key in fields:
            quota[http_key] = fields[proto_key]

    for proto_key, (outer, item_proto, item_http) in _PROTO_LIST_FIELDS.items():
        nested = fields.get(proto_key)
        if not isinstance(nested, dict):
            continue
        items = nested.get(item_proto)
        if isinstance(items, list):
            scaled = [
                v * DELTA3_PROTO_POWER_SCALE
                if isinstance(v, (int, float)) and not isinstance(v, bool)
                else v
                for v in items
            ]
            quota[outer] = {item_http: scaled}

    return quota


def _translate_cms_heartbeat(fields: dict[str, Any]) -> dict[str, Any]:
    """Map the battery heartbeat onto its HTTP quota spelling.

    Only the state of charge is forwarded. The pack also carries
    charge/discharge remaining times, but the matching direction flag in
    this frame uses a different enum than the status frame, so emitting
    them here could park a wrong runtime on the entity. The status frame
    already delivers both remaining times together with the state they
    belong to, and the same reasoning applies to the SoC limits (see the
    note on `_HEARTBEAT_SOC_FIELDS`).
    """
    pack = fields.get("v1p0")
    if not isinstance(pack, dict):
        return {}

    quota: dict[str, Any] = {}

    for proto_key in _HEARTBEAT_SOC_FIELDS:
        value = pack.get(proto_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            quota["cmsBattSoc"] = value

    return quota


def _push_only_values(fields: dict[str, Any]) -> dict[str, Any]:
    """Return the status-frame values that never travel over the quota."""
    result: dict[str, Any] = {}
    for proto_key, sensor_key in _PROTO_ONLY_FIELDS.items():
        value = fields.get(proto_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[sensor_key] = round(float(value))

    mode = fields.get("ac_in_chg_mode")
    if isinstance(mode, int) and not isinstance(mode, bool):
        label = _AC_CHARGE_MODES.get(mode)
        if label is not None:
            result[_AC_CHARGE_MODE_KEY] = label

    result.update(_port_priority_values(fields))
    return result


def parse_delta3_display_property(fields: dict[str, Any]) -> dict[str, Any]:
    """Parse a decoded Delta 3 status frame into flat sensor keys."""
    result = parse_delta3_http_quota(_translate_display_property(fields))
    result.update(_push_only_values(fields))
    return result


def parse_delta3_cms_heartbeat(fields: dict[str, Any]) -> dict[str, Any]:
    """Parse a decoded Delta 3 battery heartbeat into flat sensor keys."""
    return parse_delta3_http_quota(_translate_cms_heartbeat(fields))


# BMS heartbeat (cmd_func=32, cmd_id=50). This frame is the one exception to
# the rule above: it has no HTTP quota counterpart at all, so there is no
# quota spelling to translate into and the fields map straight onto sensor
# keys. Every key is prefixed `bms_` to keep that difference visible - these
# sensors exist on the protobuf path only and stay unavailable in Standard
# Mode.
#
# proto field -> (sensor key, divisor). A divisor of 1 passes the value
# through. The unit of every scaled field was confirmed against the same
# capture: 53070 mV at 16 cells of ~3327 mV, and -70 mA against 3 W of load
# at 53 V.
_BMS_SCALED_FIELDS: dict[str, tuple[str, float]] = {
    "vol": ("bms_voltage_v", 1000.0),
    "amp": ("bms_current_a", 1000.0),
    "temp": ("bms_temp_c", 1.0),
    "soh": ("bms_soh_pct", 1.0),
    "cycles": ("bms_cycles", 1.0),
    "real_soh": ("bms_real_soh_pct", 1.0),
    "calendar_soh": ("bms_calendar_soh_pct", 1.0),
    "cycle_soh": ("bms_cycle_soh_pct", 1.0),
    "max_cell_vol": ("bms_max_cell_vol_mv", 1.0),
    "min_cell_vol": ("bms_min_cell_vol_mv", 1.0),
    "max_vol_diff": ("bms_cell_vol_diff_mv", 1.0),
    "max_cell_temp": ("bms_max_cell_temp_c", 1.0),
    "min_cell_temp": ("bms_min_cell_temp_c", 1.0),
    "max_mos_temp": ("bms_max_mos_temp_c", 1.0),
    "min_mos_temp": ("bms_min_mos_temp_c", 1.0),
    "remain_cap": ("bms_remain_cap_mah", 1.0),
    "full_cap": ("bms_full_cap_mah", 1.0),
    "design_cap": ("bms_design_cap_mah", 1.0),
    "cell_series_num": ("bms_cell_count", 1.0),
    "all_err_code": ("bms_error_code", 1.0),
}

# Lifetime counters the BMS keeps itself, in Wh. Unlike every other kWh
# sensor on this device these are not integrated from power - they are read.
# A zero is "nothing to report", not a reading, and publishing it on a
# total_increasing sensor would make Home Assistant book a meter reset.
_BMS_ENERGY_FIELDS: dict[str, str] = {
    "accu_chg_energy": "bms_accu_chg_energy_kwh",
    "accu_dsg_energy": "bms_accu_dsg_energy_kwh",
}

# Environment-temperature fields are deliberately absent: the DELTA 3 Max
# Plus reports -127 for both, the standard "no sensor fitted" sentinel, and
# publishing it would put a plausible-looking -127 °C on a temperature
# entity.
#
# The frame also carries `soc` and `f32_show_soc`. Neither is forwarded:
# `cms_batt_soc` already owns the state of charge, and a second source
# updating the same entity from a different frame is exactly the split
# ownership this integration avoids. The two differ by more than a rounding
# step in the capture (99 against the display value 98.8), so they are not
# even interchangeable.


def parse_delta3_bms_heartbeat(fields: dict[str, Any]) -> dict[str, Any]:
    """Parse a decoded Delta 3 BMS heartbeat into flat sensor keys."""
    result: dict[str, Any] = {}

    for proto_key, (sensor_key, divisor) in _BMS_SCALED_FIELDS.items():
        value = fields.get(proto_key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        result[sensor_key] = float(value) / divisor if divisor != 1.0 else float(value)

    for proto_key, sensor_key in _BMS_ENERGY_FIELDS.items():
        value = fields.get(proto_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            result[sensor_key] = float(value) / 1000.0

    return result
