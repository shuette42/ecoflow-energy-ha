"""Protobuf telemetry parser for the EcoFlow DELTA Pro Ultra (`Y711`).

Source: the reporter's two diagnostics downloads from issue #464 (2026-10-02,
integration v1.23.0, Enhanced Mode), 66 frames after de-duplication, read
against the EcoFlow app's message layout for this device family.

The device pushes its telemetry on `cmd_func` 2, from `src` 2 (the main unit):

| cmd (func.id) | Message                    | Read here                           |
|---------------|----------------------------|-------------------------------------|
| 2.1           | `AppShowHeartbeatReport`   | SOC, remaining time, power block    |
| 2.2           | `BackendRecordHeartbeatReport` | battery voltage/power, temperatures |
| 2.3           | `APPParaHeartbeatReport`   | settings: backup reserve level      |
| 2.4           | `BpInfoReport` (repeated `BPInfo`) | per-pack SOC and temperature |

Every other header is ignored: 3.28 and 50.2 come from a second module
(`src` 6, the battery management unit), 254.21 is a cross-family display
property message and 1.88 is a header-only keepalive. A `src` other than 2 is
never read, whatever its cmd pair.

Frames are **incremental**. A property push on 2.1 is often a single field
(`remain_time`, 5 of the 9 property frames in the capture) against the full
58-field state a `get_reply` bundle carries, and a field missing from a push
is unchanged, not zero. `parse_delta_pro_ultra_message` returns only the keys
a frame actually carries; the coordinator's own `_device_data.update()` is the
one merge that keeps the rest, as for every other device. A reading of
`-0.0` is published as `0.0`.

Payload masking follows the Stream rule shared through `_pdata_candidates`:
a header with `enc_type == 1` has its payload XOR-masked with the low byte of
its own sequence number, per header. The headers inside a `get_reply` bundle
carry no `enc_type` and arrive unmasked, so one decoder reads both shapes.

Two integer fields are signed 32-bit values written as a varint
(`pd_temp_c`, `bp{n}_temp_c`); a negative reading is the 64-bit two's
complement of the value, not a zigzag encoding. `_decode_scalar` already
returns that as a negative integer, so they need no kind of their own.

Scaling: the SOC and temperature rows are cross-checked against the second
module's own reading of the same pack (SOC 99/98/82-83, temperature 32/30/29),
and the pack voltage against its millivolt figure on that module. The power
fields (2.1 field 41-58, 2.2 field 63/64) are **LIKELY**, not verified: the
unit was idle in the capture, so no frame shows the port sums balancing
against the battery. They are floats in watts by the message layout; the
release gate for calling them verified is one capture under load.

Not parsed: the per-port voltage/current block, power factors, USB/Type-C
powers, frequencies, enums (work state, pcs type), the 4G fields, time tasks,
and the per-pack power/remain-time fields (`bp_pwr` only ever read -0.0).
"""

from __future__ import annotations

import struct
from math import isfinite
from typing import Any

from ..proto.decoder import decode_header_message
from .stream_proto import _TYPE_INT, _decode_scalar, _iter_fields, _pdata_candidates

#: Only the main unit's frames are read (see the module docstring).
_SRC_MAIN_UNIT = 2

_CMD_FUNC = 2
_CMD_ID_SHOW = 1
_CMD_ID_RECORD = 2
_CMD_ID_PARA = 3
_CMD_ID_BP_INFO = 4

# Field kinds in the maps below.
_INT = "int"  # varint; a negative value arrives as 64-bit two's complement
_FLOAT = "float"  # little-endian float32, rounded to `decimals`

# field number -> (key, kind, decimals)
_SHOW_FIELDS: dict[int, tuple[str, str, int]] = {
    21: ("soc", _INT, 0),
    26: ("remain_time_min", _INT, 0),
    41: ("watts_in_sum", _FLOAT, 1),
    42: ("watts_out_sum", _FLOAT, 1),
    48: ("ac_out_l1_1_w", _FLOAT, 1),
    49: ("ac_out_l1_2_w", _FLOAT, 1),
    50: ("ac_out_l2_1_w", _FLOAT, 1),
    51: ("ac_out_l2_2_w", _FLOAT, 1),
    52: ("ac_out_tt30_w", _FLOAT, 1),
    53: ("ac_out_l14_w", _FLOAT, 1),
    54: ("power_io_out_w", _FLOAT, 1),
    55: ("power_io_in_w", _FLOAT, 1),
    56: ("ac_in_w", _FLOAT, 1),
    57: ("solar_lv_in_w", _FLOAT, 1),
    58: ("solar_hv_in_w", _FLOAT, 1),
}

_RECORD_FIELDS: dict[int, tuple[str, str, int]] = {
    61: ("batt_voltage_v", _FLOAT, 2),
    63: ("batt_charge_power_w", _FLOAT, 1),
    64: ("batt_discharge_power_w", _FLOAT, 1),
    98: ("inv_ac_temp_c", _FLOAT, 1),
    101: ("pd_temp_c", _INT, 0),
}

_PARA_FIELDS: dict[int, tuple[str, str, int]] = {
    3: ("backup_reserve_pct", _INT, 0),
}

_FIELDS_BY_CMD_ID: dict[int, dict[int, tuple[str, str, int]]] = {
    _CMD_ID_SHOW: _SHOW_FIELDS,
    _CMD_ID_RECORD: _RECORD_FIELDS,
    _CMD_ID_PARA: _PARA_FIELDS,
}

# `BpInfoReport`: repeated field 1 holds one `BPInfo` per pack. Inside it,
# field 1 is the pack number, 3 the SOC and 11 the temperature.
_BP_ENTRY_FIELD = 1
_BP_NO_FIELD = 1
_BP_SOC_FIELD = 3
_BP_TEMP_FIELD = 11
#: Highest pack number read. The capture shows packs 1-3; a number outside
#: 1..MAX_PACKS is treated as not a pack. Also sizes the per-pack entities.
MAX_PACKS = 5

_WIRE_VARINT = 0
_WIRE_FIXED32 = 5


def _decode_field(wire_type: int, raw: bytes, kind: str, decimals: int) -> Any | None:
    """Decode one mapped field, or None when its wire type or value is wrong."""
    if kind == _FLOAT:
        if wire_type != _WIRE_FIXED32 or len(raw) != 4:
            return None
        value = struct.unpack("<f", raw)[0]
        if not isfinite(value):
            return None
        # `+ 0.0` turns a rounded -0.0 into 0.0 and leaves every other value.
        return round(value, decimals) + 0.0
    if wire_type != _WIRE_VARINT:
        return None
    decoded = _decode_scalar(wire_type, raw, _TYPE_INT)
    if not isinstance(decoded, int):
        return None
    return decoded


def _parse_fields(
    pdata: bytes, fields: dict[int, tuple[str, str, int]]
) -> dict[str, Any]:
    """Decode the mapped fields of one 2.1 / 2.2 / 2.3 payload."""
    out: dict[str, Any] = {}
    for field_num, wire_type, raw in _iter_fields(pdata):
        spec = fields.get(field_num)
        if spec is None:
            continue
        key, kind, decimals = spec
        value = _decode_field(wire_type, raw, kind, decimals)
        if value is not None:
            out[key] = value
    return out


def _parse_bp_info(pdata: bytes) -> dict[str, Any]:
    """Decode one 2.4 payload: SOC and temperature for each pack it lists."""
    out: dict[str, Any] = {}
    for field_num, wire_type, raw in _iter_fields(pdata):
        if field_num != _BP_ENTRY_FIELD or wire_type != 2:
            continue
        bp_no: int | None = None
        soc: int | None = None
        temp: int | None = None
        for sub_num, sub_wire, sub_raw in _iter_fields(raw):
            if sub_num == _BP_NO_FIELD:
                bp_no = _decode_field(sub_wire, sub_raw, _INT, 0)
            elif sub_num == _BP_SOC_FIELD:
                soc = _decode_field(sub_wire, sub_raw, _INT, 0)
            elif sub_num == _BP_TEMP_FIELD:
                temp = _decode_field(sub_wire, sub_raw, _INT, 0)
        if bp_no is None or not 1 <= bp_no <= MAX_PACKS:
            continue
        if soc is not None:
            out[f"bp{bp_no}_soc_pct"] = soc
        if temp is not None:
            out[f"bp{bp_no}_temp_c"] = temp
    return out


def _parse_payload(cmd_id: int, pdata: bytes) -> dict[str, Any]:
    if cmd_id == _CMD_ID_BP_INFO:
        return _parse_bp_info(pdata)
    return _parse_fields(pdata, _FIELDS_BY_CMD_ID[cmd_id])


def parse_delta_pro_ultra_message(payload: bytes) -> dict[str, Any] | None:
    """Parse one DELTA Pro Ultra frame into the keys it carries.

    The frame is incremental, so the result holds only what it carries; the
    coordinator's `_device_data.update(parsed)` keeps the rest. A frame with
    nothing this parser reads (3.28, 254.21, 50.2, 1.88, or a `src` other
    than 2) returns None, the "nothing to merge" signal the sibling parsers
    give.

    A `get_reply` bundle carries the 2.1, 2.2, 2.3 and 2.4 headers in one
    envelope; all of them are combined into one result, in header order.
    """
    try:
        headers, _ = decode_header_message(payload)
    except (IndexError, ValueError, struct.error):
        return None
    if not headers:
        return None

    result: dict[str, Any] = {}
    for header in headers:
        if header.get("src") != _SRC_MAIN_UNIT:
            continue
        if header.get("cmd_func") != _CMD_FUNC:
            continue
        cmd_id = header.get("cmd_id")
        if cmd_id not in (
            _CMD_ID_SHOW,
            _CMD_ID_RECORD,
            _CMD_ID_PARA,
            _CMD_ID_BP_INFO,
        ):
            continue
        for pdata in _pdata_candidates(header):
            try:
                decoded = _parse_payload(cmd_id, pdata)
            except (IndexError, ValueError, struct.error):
                # Only a payload that is not valid protobuf falls through to
                # the next candidate; a clean decode ends the attempt whether
                # it produced fields or not.
                continue
            result.update(decoded)
            break

    return result or None
