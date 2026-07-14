"""Delta 3 Max Plus HTTP Quota API response parser.

Parses the response from GET /iot-open/sign/device/quota/all into
flat sensor keys.

The Delta 3 generation uses a flat camelCase quota namespace (like
PowerOcean) that is completely different from the Delta 2 Max
("pd.*"/"inv.*"/"mppt.*"), so it gets its own parser module.

Scaling across the Delta 3 generation is direct (multiplier 1) for W, %
and min - there is no deciwatt anywhere in this generation. Values are
rounded to clean integers (the HTTP quota subset carries no voltage,
current or temperature fields that would need decimals).

No native energy (Wh/kWh) counters exist in the HTTP quota, so Energy
Dashboard integration is deferred until the power keys are validated on
real hardware.
"""

from __future__ import annotations

from typing import Any

from . import _safe_float

# Charge/discharge state enum: raw int -> option label. Unknown values are
# dropped (not emitted) so the enum sensor never receives an out-of-options
# value.
_DELTA3_CHG_DSG_MAP: dict[int, str] = {
    0: "idle",
    1: "discharging",
    2: "charging",
}

# Output flow states are NOT booleans: "on" means value != 4 (per the
# official docs, value 4 = no flow). Emit a derived 0/1 boolean per flow.
_DELTA3_FLOW_FIELD_MAP: dict[str, str] = {
    "flowInfoAcOut": "ac_out_flow",
    "flowInfoAc2Out": "ac2_out_flow",
    "flowInfo12v": "dc_12v_out_flow",
}

# Mapping: flat HTTP quota key -> sensor key. All values are direct-scaled
# (W, %, min) and rounded to clean integers. Enum, flow and the nested
# AC-outlet array are handled separately below.
DELTA3_HTTP_FIELD_MAP: dict[str, str] = {
    # --- Battery / SoC ---
    "cmsBattSoc": "cms_batt_soc",
    "bmsBattSoc": "bms_batt_soc",
    # --- Remaining time (minutes; can exceed 12000, never clamp) ---
    "cmsChgRemTime": "chg_remain_time_min",
    "cmsDsgRemTime": "dsg_remain_time_min",
    # --- Power (W, direct) ---
    "powInSumW": "pow_in_sum_w",
    "powOutSumW": "pow_out_sum_w",
    "powGetAcIn": "ac_in_w",
    "powGetPv": "pv1_in_w",
    "powGetPv2": "pv2_in_w",
    "powGet12v": "dc_12v_out_w",
    "powGetTypec1": "typec1_w",
    "powGetTypec2": "typec2_w",
    "powGetTypec3": "typec3_w",
    "powGetQcusb1": "usb_qc1_w",
    "powGetQcusb2": "usb_qc2_w",
    # --- SoC limits / backup reserve (%, do NOT clamp reads) ---
    "cmsMaxChgSoc": "max_charge_soc_pct",
    "cmsMinDsgSoc": "min_discharge_soc_pct",
    "backupReverseSoc": "backup_reserve_soc_pct",
    # --- Boolean flags (0/1, consumed by binary sensors) ---
    "xboostEn": "xboost_enabled",
    "enBeep": "beeper_enabled",
    "energyBackupEn": "backup_reserve_enabled",
    "bypassOutDisable": "bypass_out_disabled",
}


def _extract_ac_out_list(quota_data: dict) -> list[Any] | None:
    """Return the powGetAcOutItem array from either nested or dotted form."""
    nested = quota_data.get("powGetAcOutList")
    if isinstance(nested, dict):
        items = nested.get("powGetAcOutItem")
        if isinstance(items, list):
            return items
    dotted = quota_data.get("powGetAcOutList.powGetAcOutItem")
    if isinstance(dotted, list):
        return dotted
    return None


def parse_delta3_http_quota(quota_data: dict) -> dict[str, Any]:
    """Parse a Delta 3 Max Plus GET /quota/all response into flat sensor keys.

    Maps the flat quota keys via DELTA3_HTTP_FIELD_MAP (rounded integers),
    derives the output-flow booleans (value != 4), decodes the
    charge/discharge enum, and extracts per-outlet AC power from the nested
    array. Unmapped keys are ignored so they never leak into the device data
    store; the raw snapshot is exposed via diagnostics instead.
    """
    result: dict[str, Any] = {}

    # Flat direct-scaled keys, rounded to clean integers.
    for http_key, sensor_key in DELTA3_HTTP_FIELD_MAP.items():
        if http_key in quota_data:
            v = _safe_float(quota_data[http_key])
            if v is not None:
                result[sensor_key] = int(round(v))

    # Charge/discharge state: int -> option label, drop unknown values.
    if "cmsChgDsgState" in quota_data:
        raw = _safe_float(quota_data["cmsChgDsgState"])
        if raw is not None and int(raw) in _DELTA3_CHG_DSG_MAP:
            result["chg_dsg_state"] = _DELTA3_CHG_DSG_MAP[int(raw)]

    # Output flow states: "on" = value != 4 (NOT a plain boolean).
    for http_key, sensor_key in _DELTA3_FLOW_FIELD_MAP.items():
        if http_key in quota_data:
            v = _safe_float(quota_data[http_key])
            if v is not None:
                result[sensor_key] = 0 if int(v) == 4 else 1

    # Per-outlet AC power: nested signed array, item[0]=AC1, item[2]=AC2.
    ac_out = _extract_ac_out_list(quota_data)
    if ac_out is not None:
        if len(ac_out) >= 1:
            v = _safe_float(ac_out[0])
            if v is not None:
                result["ac1_out_w"] = int(round(abs(v)))
        if len(ac_out) >= 3:
            v = _safe_float(ac_out[2])
            if v is not None:
                result["ac2_out_w"] = int(round(abs(v)))

    return result
