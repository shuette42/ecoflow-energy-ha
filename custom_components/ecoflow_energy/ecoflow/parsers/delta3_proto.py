"""Delta 3 Max Plus protobuf telemetry parser (Enhanced Mode).

The Delta 3 generation never answers the JSON quota request on the
Enhanced Mode connection - it publishes protobuf frames only. Two of
those frames carry values we surface:

    cmd_func=254, cmd_id=21   main status frame (all contract fields)
    cmd_func=32,  cmd_id=2    battery heartbeat (SoC and SoC limits)

Both use the same field names as the HTTP quota response, just in
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


def parse_delta3_display_property(fields: dict[str, Any]) -> dict[str, Any]:
    """Parse a decoded Delta 3 status frame into flat sensor keys."""
    return parse_delta3_http_quota(_translate_display_property(fields))


def parse_delta3_cms_heartbeat(fields: dict[str, Any]) -> dict[str, Any]:
    """Parse a decoded Delta 3 battery heartbeat into flat sensor keys."""
    return parse_delta3_http_quota(_translate_cms_heartbeat(fields))
