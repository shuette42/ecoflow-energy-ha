"""Protobuf telemetry parser for EcoFlow Stream devices.

Current scope targets BkSeries stream devices in app-auth MQTT mode.
The parser was derived from the BK31 Stream AC Pro capture and is
intentionally conservative: only fields observed repeatedly in live
captures are mapped. The same parser is reused for other BK-series
device prefixes until hardware-specific differences are identified.

TODO: verify BK11 / BK41 / BK51 / BK61 field layouts against real
hardware; the current implementation assumes the same protobuf frames
and field numbers as BK31.

Current field notes from dump analysis:
- `grid_w` tracks the summed AC grid intake seen in the app "grid
  connection" view and matches `batt_w + home_from_grid_w` during
  charging cycles.
- `ac_grid_connection_power_w` is derived from the signed system grid
  connection path and matches the app "grid connection" / "Netz-Anschluss"
  value. It includes battery charging plus AC outlet pass-through load:
  negative = input from grid, positive = output/feed-in.
- `batt_w` is the signed battery power path. Most charging/discharging
  frames line up with real battery behavior, while AC outlet activity is
  exposed separately via `ac_outlet_1_w` / `ac_outlet_2_w`.
- `home_w` behaves like the overall active load path. In zero-export
  discharge captures, `home_w` stays aligned with `abs(batt_w)` while
  `ac_outlet_1_w` remains a diagnostic breakdown of that total rather
  than an extra load to add on top.
- `solar_w` appears to be Smart-Meter/App energy-flow data rather than
  direct PV production on the AC-coupled battery. Without a paired meter
  it may be absent or misleading, and in zero-export setups it can remain
  zero until surplus power would be exported.
- LED brightness is confirmed separately from battery health:
  `254/21 field 994` carries the live brightness percentage and is the
  only field mapped to `led_brightness`. `254/18 field 384` carries the
  set/ack (slider target) value and is intentionally not mapped, so the
  live value never flaps to the slider target on a SET acknowledgement.
  `32/50 field 15` stayed at `100` throughout the dedicated LED capture
  and does not track brightness changes.
"""

from __future__ import annotations

import struct
from math import isfinite
from typing import Any

from ..proto.decoder import decode_header_message

_TYPE_INT = "int"
_TYPE_FLOAT = "float"
_FLOAT_ZERO_EPS = 1e-6

# cmd_func/cmd_id -> field_number -> (sensor_key, scalar_type)
_STREAM_FIELD_MAP: dict[tuple[int, int], dict[int, tuple[str, str]]] = {
    # Main status frame (observed every few seconds)
    (254, 21): {
        242: ("soc_pct", _TYPE_INT),
        262: ("soc_pct", _TYPE_INT),  # mirrored SoC
        270: ("max_charge_soc_pct", _TYPE_INT),
        271: ("min_discharge_soc_pct", _TYPE_INT),
        380: ("ac_outlet_1_enabled", _TYPE_INT),
        381: ("ac_outlet_2_enabled", _TYPE_INT),
        461: ("backup_reserve_pct", _TYPE_INT),
        # Summed AC intake from grid. Charge-dump correlation:
        # grid_w ~= batt_w + home_from_grid_w.
        515: ("grid_w", _TYPE_FLOAT),
        # Overall active load path. Outlet power appears to be included in
        # this total and is published separately for diagnostics.
        516: ("home_w", _TYPE_FLOAT),
        517: ("solar_w", _TYPE_FLOAT),
        # Signed battery power path: positive = charging, negative = discharging.
        518: ("batt_w", _TYPE_FLOAT),
        602: ("_batt_w_fallback", _TYPE_FLOAT),
        613: ("ac_voltage_v", _TYPE_FLOAT),
        615: ("ac_frequency_hz", _TYPE_FLOAT),
        616: ("grid_connection_power_w", _TYPE_FLOAT),
        # Mirror fields for the AC outlet enable flags. They carry the same
        # on/off semantics as 380/381; whichever appears last in the frame
        # wins, which is harmless because both encode the same boolean state.
        980: ("ac_outlet_1_enabled", _TYPE_INT),
        982: ("ac_outlet_2_enabled", _TYPE_INT),
        992: ("sys_grid_connection_power_w", _TYPE_FLOAT),
        # Live LED brightness percentage. The set/ack value (field 384) is
        # intentionally NOT mapped to the same key: it carries the slider
        # target rather than the live value and would otherwise flap with 994.
        994: ("led_brightness", _TYPE_INT),
        1003: ("home_from_batt_w", _TYPE_FLOAT),
        # House/system load supplied from grid, excluding battery path.
        1004: ("home_from_grid_w", _TYPE_FLOAT),
        # Confirmed by live load dumps on AC1/AC2 outlets. These values act
        # as a diagnostic split of the total load path, not an extra load
        # that should be summed on top of `home_w`.
        1210: ("ac_outlet_1_w", _TYPE_FLOAT),
        1211: ("ac_outlet_2_w", _TYPE_FLOAT),
    },
    # Auxiliary status/config frame with a more precise SoC value
    (32, 50): {
        7: ("_batt_voltage_mv", _TYPE_INT),
        9: ("batt_temp_c", _TYPE_INT),
        11: ("batt_design_cap_mah", _TYPE_INT),
        12: ("batt_remain_cap_mah", _TYPE_INT),
        13: ("batt_full_cap_mah", _TYPE_INT),
        # Stable 100 even during a dedicated LED brightness sweep.
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
    },
    # SET acknowledgement path for backup reserve slider
    (254, 18): {
        380: ("ac_outlet_1_enabled", _TYPE_INT),
        381: ("ac_outlet_2_enabled", _TYPE_INT),
        102: ("backup_reserve_pct", _TYPE_INT),
    },
}


def _read_varint(mv: memoryview, pos: int) -> tuple[int, int]:
    """Decode one protobuf varint from ``mv`` starting at ``pos``.

    Raises ``ValueError`` on an oversized (>64-bit) varint and ``IndexError``
    on truncated input; both are caught by the outer parse guard.
    """
    shift = 0
    value = 0
    while True:
        byte = mv[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("oversized varint")


def _decode_scalar(wire_type: int, raw: bytes, scalar_type: str) -> float | int | None:
    """Decode one mapped scalar field into the requested target type."""
    if wire_type == 0:
        value, _ = _read_varint(memoryview(raw), 0)
        # Negative int32/int64 values arrive as 64-bit two's complement
        # (e.g. signed power/temperature paths below zero).
        if value >= 1 << 63:
            value -= 1 << 64
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

    for key, value in list(result.items()):
        if isinstance(value, float) and isfinite(value) and abs(value) < _FLOAT_ZERO_EPS:
            result[key] = 0.0

    if "soc_pct" not in result and "soc_precise_pct" in result:
        result["soc_pct"] = result["soc_precise_pct"]

    if "batt_w" not in result and "_batt_w_fallback" in result:
        result["batt_w"] = float(result["_batt_w_fallback"])

    result.pop("_batt_w_fallback", None)
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

    grid_connection = result.get("sys_grid_connection_power_w")
    if not isinstance(grid_connection, (int, float)):
        grid_connection = result.get("grid_connection_power_w")
    if isinstance(grid_connection, (int, float)):
        result["ac_grid_connection_power_w"] = float(grid_connection)

    batt_w = result.get("batt_w")
    if isinstance(batt_w, (int, float)):
        result["batt_charge_power_w"] = float(batt_w) if batt_w > 0 else 0.0
        result["batt_discharge_power_w"] = abs(float(batt_w)) if batt_w < 0 else 0.0
        # batt_charge_discharge_state is intentionally NOT set here. The
        # coordinator pops any parser-provided value and derives the state
        # from a hysteresis window over batt_w (see _derive_battery_state,
        # issue #50). Setting it from instantaneous sign would only be used
        # by unit tests and never reaches the live entity.

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
