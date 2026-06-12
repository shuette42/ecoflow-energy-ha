"""Protobuf telemetry parser for EcoFlow Stream devices.

Current scope targets BK31 (Stream AC Pro) in app-auth MQTT mode.
Only fields that have been observed repeatedly in live captures are
mapped here. The parser is intentionally conservative so we can extend
it safely as more captures become available.
"""

from __future__ import annotations

import struct
from typing import Any

from ..proto.decoder import decode_header_message

_TYPE_INT = "int"
_TYPE_FLOAT = "float"

# cmd_func/cmd_id -> field_number -> (sensor_key, scalar_type)
_STREAM_FIELD_MAP: dict[tuple[int, int], dict[int, tuple[str, str]]] = {
    # Main status frame (observed every few seconds)
    (254, 21): {
        242: ("soc_pct", _TYPE_INT),
        262: ("soc_pct", _TYPE_INT),  # mirrored SoC
        270: ("max_charge_soc_pct", _TYPE_INT),
        271: ("min_discharge_soc_pct", _TYPE_INT),
        461: ("backup_reserve_pct", _TYPE_INT),
        515: ("grid_w", _TYPE_FLOAT),
        516: ("home_w", _TYPE_FLOAT),
        517: ("solar_w", _TYPE_FLOAT),
        518: ("batt_w", _TYPE_FLOAT),
        613: ("ac_voltage_v", _TYPE_FLOAT),
        615: ("ac_frequency_hz", _TYPE_FLOAT),
        616: ("_batt_w_inverted", _TYPE_FLOAT),
        992: ("_batt_w_negated", _TYPE_FLOAT),
    },
    # Auxiliary status/config frame with a more precise SoC value
    (32, 50): {
        7: ("_batt_voltage_mv", _TYPE_INT),
        9: ("batt_temp_c", _TYPE_INT),
        11: ("batt_design_cap_mah", _TYPE_INT),
        12: ("batt_remain_cap_mah", _TYPE_INT),
        13: ("batt_full_cap_mah", _TYPE_INT),
        15: ("bms_soh_pct", _TYPE_INT),
        16: ("batt_max_cell_vol_mv", _TYPE_INT),
        17: ("batt_min_cell_vol_mv", _TYPE_INT),
        18: ("batt_max_cell_temp_c", _TYPE_INT),
        19: ("batt_min_cell_temp_c", _TYPE_INT),
        20: ("batt_max_mos_temp_c", _TYPE_INT),
        25: ("soc_precise_pct", _TYPE_FLOAT),
        32: ("_batt_charge_capacity_ah_rounded", _TYPE_INT),
        50: ("_batt_charge_capacity_mah_total", _TYPE_INT),
        51: ("_batt_discharge_capacity_mah_total", _TYPE_INT),
        79: ("batt_charge_energy_wh", _TYPE_INT),
        80: ("batt_discharge_energy_wh", _TYPE_INT),
    },
    # SET acknowledgement path for backup reserve slider
    (254, 18): {
        102: ("backup_reserve_pct", _TYPE_INT),
    },
}


def _read_varint(mv: memoryview, pos: int) -> tuple[int, int]:
    """Decode one protobuf varint from ``mv`` starting at ``pos``."""
    shift = 0
    value = 0
    while True:
        byte = mv[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _decode_scalar(wire_type: int, raw: bytes, scalar_type: str) -> float | int | None:
    """Decode one mapped scalar field into the requested target type."""
    if wire_type == 0:
        value, _ = _read_varint(memoryview(raw), 0)
        return float(value) if scalar_type == _TYPE_FLOAT else int(value)

    if wire_type == 5:
        if len(raw) != 4:
            return None
        fval = struct.unpack("<f", raw)[0]
        return fval if scalar_type == _TYPE_FLOAT else int(round(fval))

    if wire_type == 1:
        if len(raw) != 8:
            return None
        dval = struct.unpack("<d", raw)[0]
        return dval if scalar_type == _TYPE_FLOAT else int(round(dval))

    return None


def _decode_mapped_fields(
    pdata: bytes,
    field_map: dict[int, tuple[str, str]],
) -> dict[str, Any]:
    """Decode only the mapped fields from a protobuf payload."""
    result: dict[str, Any] = {}
    mv = memoryview(pdata)
    pos = 0

    while pos < len(mv):
        tag, pos = _read_varint(mv, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:
            start = pos
            _, pos = _read_varint(mv, pos)
            raw = mv[start:pos].tobytes()
        elif wire_type == 1:
            raw = mv[pos:pos + 8].tobytes()
            pos += 8
        elif wire_type == 2:
            length, pos = _read_varint(mv, pos)
            raw = mv[pos:pos + length].tobytes()
            pos += length
        elif wire_type == 5:
            raw = mv[pos:pos + 4].tobytes()
            pos += 4
        else:
            break

        mapping = field_map.get(field_num)
        if mapping is None:
            continue

        sensor_key, scalar_type = mapping
        value = _decode_scalar(wire_type, raw, scalar_type)
        if value is not None:
            result[sensor_key] = value

    return result


def _finalize_stream_state(parsed: dict[str, Any]) -> dict[str, Any]:
    """Normalize mirrored fields and derive convenience sensor values."""
    result = dict(parsed)

    if "soc_pct" not in result and "soc_precise_pct" in result:
        result["soc_pct"] = result["soc_precise_pct"]

    if "batt_w" not in result and "_batt_w_negated" in result:
        result["batt_w"] = -float(result["_batt_w_negated"])
    if "batt_w" not in result and "_batt_w_inverted" in result:
        result["batt_w"] = -float(result["_batt_w_inverted"])

    result.pop("_batt_w_negated", None)
    result.pop("_batt_w_inverted", None)
    batt_voltage_mv = result.pop("_batt_voltage_mv", None)

    if isinstance(batt_voltage_mv, (int, float)):
        result["batt_voltage_v"] = float(batt_voltage_mv) / 1000.0

    charge_capacity_mah = result.pop("_batt_charge_capacity_mah_total", None)
    discharge_capacity_mah = result.pop("_batt_discharge_capacity_mah_total", None)
    charge_capacity_rounded = result.pop("_batt_charge_capacity_ah_rounded", None)

    if isinstance(charge_capacity_mah, (int, float)):
        result["batt_charge_capacity_ah"] = float(charge_capacity_mah) / 1000.0
    elif isinstance(charge_capacity_rounded, (int, float)):
        result["batt_charge_capacity_ah"] = float(charge_capacity_rounded)

    if isinstance(discharge_capacity_mah, (int, float)):
        result["batt_discharge_capacity_ah"] = float(discharge_capacity_mah) / 1000.0

    batt_w = result.get("batt_w")
    if isinstance(batt_w, (int, float)):
        result["batt_charge_power_w"] = float(batt_w) if batt_w > 0 else 0.0
        result["batt_discharge_power_w"] = abs(float(batt_w)) if batt_w < 0 else 0.0

    return result


def parse_stream_proto_message(payload: bytes) -> dict[str, Any] | None:
    """Parse a Stream protobuf frame into flat sensor keys."""
    try:
        headers, _ = decode_header_message(payload)
        if not headers:
            return None

        merged: dict[str, Any] = {}
        for header in headers:
            cmd_key = (int(header.get("cmd_func", -1)), int(header.get("cmd_id", -1)))
            field_map = _STREAM_FIELD_MAP.get(cmd_key)
            pdata_hex = header.get("pdata")
            if field_map is None or not isinstance(pdata_hex, str) or not pdata_hex:
                continue
            try:
                pdata = bytes.fromhex(pdata_hex)
            except ValueError:
                continue
            merged.update(_decode_mapped_fields(pdata, field_map))
    except Exception:
        return None
    if not merged:
        return None

    finalized = _finalize_stream_state(merged)
    return finalized or None
