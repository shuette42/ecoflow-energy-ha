"""Protobuf telemetry parser for the EcoFlow Ocean 2 (`RE11`, `RE17`).

Scope is deliberately narrow: the main telemetry frame (cmd_func 254 /
cmd_id 39) and nothing else. The unit also emits a per-module battery frame
on cmd_id 46 carrying cell voltages, temperatures and cycle counts; that is
a separate map with its own entities and is left for a follow-up so this
addition stays reviewable.

Unlike the Stream and Delta 3 frames, an Ocean 2 telemetry payload is
**nested**: the readings sit inside length-delimited sub-messages (65, 7/87,
4) rather than at the top level. A flat field map like `_STREAM_FIELD_MAP`
cannot express that, which is why this module carries its own small generic
decoder instead of reusing `_decode_mapped_fields`.

Field assignments below were verified against live hardware in Enhanced mode
by cross-checking every value against the EcoFlow app over several days,
including a 3.6 kW wallbox charge that moved all four power paths at once,
and confirmed afterwards against a seven hour recording from a second,
unrelated installation: the numbers line up frame for frame, the load-bearing
readings and the module block included.

They come from https://github.com/jensfr1/ha-ecoflow-ocean2, a standalone
Ocean 2 integration that decodes the full frame set - including the
per-module battery data this parser leaves for a later change.

Standard Mode is not an option for this device: the HTTP quota call answers
error 1006 ("current device is not allowed to get device info"), the same
situation as the J32D/J32E PowerOcean variants (#89, #145).
"""

from __future__ import annotations

import struct
from math import isfinite
from typing import Any

from ..proto.decoder import decode_header_message
from .stream_proto import _pdata_candidates, _read_varint

# The two frames this device sends on cmd_func 254. Only 39 is mapped here.
_CMD_FUNC = 254
_CMD_ID_TELEMETRY = 39

# Top-level sub-message numbers inside the telemetry payload.
_BLOCK_SUMMARY = 65
_BLOCK_FLOW = (7, 87)
_BLOCK_INVERTER = 4


def _decode_fields(raw: bytes) -> dict[int, list[Any]]:
    """Decode one protobuf message into {field number: [values]}.

    Values are floats for 32-bit fields, ints for varints and raw bytes for
    length-delimited fields, which the callers decode recursively. Repeated
    fields keep every occurrence: the PV string list relies on it.
    """
    out: dict[int, list[Any]] = {}
    mv = memoryview(raw)
    pos = 0
    while pos < len(mv):
        tag, pos = _read_varint(mv, pos)
        field_num, wire_type = tag >> 3, tag & 0x07
        # Annotated because the wire type decides what comes out: a varint, a
        # float, or the raw bytes of a nested submessage.
        value: Any
        if wire_type == 0:
            value, pos = _read_varint(mv, pos)
        elif wire_type == 1:
            if pos + 8 > len(mv):
                break
            value = struct.unpack_from("<d", mv, pos)[0]
            pos += 8
        elif wire_type == 2:
            length, pos = _read_varint(mv, pos)
            if pos + length > len(mv):
                break
            value = mv[pos : pos + length].tobytes()
            pos += length
        elif wire_type == 5:
            if pos + 4 > len(mv):
                break
            value = struct.unpack_from("<f", mv, pos)[0]
            pos += 4
        else:
            break
        out.setdefault(field_num, []).append(value)
    return out


def _num(fields: dict[int, list[Any]], key: int) -> float | None:
    """Return a numeric field, or None when the message did not carry it.

    The distinction matters more here than in a flat map. Ocean 2 telemetry
    is partial by design - a single frame carries whatever changed - so a
    missing field must never be written out as 0, which would show up as a
    real reading of zero watts on a device that simply stayed silent.
    """
    values = fields.get(key)
    if not values or isinstance(values[0], bytes):
        return None
    value = float(values[0])
    # A NaN or an infinity reaching a sensor raises inside Home Assistant's
    # rounding and aborts the rest of that update, so it is dropped here -
    # the same place the sibling parsers drop theirs.
    if not isfinite(value):
        return None
    return value


def _sub(fields: dict[int, list[Any]], key: int) -> dict[int, list[Any]] | None:
    """Decode a nested sub-message, or None when it is absent."""
    values = fields.get(key)
    if not values or not isinstance(values[0], bytes):
        return None
    try:
        return _decode_fields(values[0])
    except (IndexError, ValueError):
        return None


def _parse_telemetry(pdata: bytes) -> dict[str, Any]:
    """Decode one cmd_id 39 payload into flat sensor keys."""
    fields = _decode_fields(pdata)
    out: dict[str, Any] = {}

    # Block 65 - system summary.
    #   4  PV power          15  remaining battery energy (Wh)
    #   17 system SoC        20  battery power, signed, negative = charging
    #
    # 65.6 and 65.7 look like grid readings and are not. 65.7 is the
    # configured feed-in limit: it reads a constant 10000 on a unit capped at
    # 10 kW and a constant 0 on a zero-export unit, which is why it can pass
    # for a plausible meter reading for a long time. The real grid power is
    # 4.13 below.
    summary = _sub(fields, _BLOCK_SUMMARY)
    if summary:
        pv_w = _num(summary, 4)
        if pv_w is not None:
            out["solar_w"] = pv_w
        soc = _num(summary, 17)
        if soc is not None:
            out["soc_pct"] = soc
        remaining = _num(summary, 15)
        if remaining is not None:
            out["batt_remaining_wh"] = remaining

    # Blocks 7 and 87 - the energy flow summary as the app shows it.
    #   1 home load   2 grid power   3 PV power   4 battery power, signed
    #
    # This block balances within itself: PV minus battery minus grid equals
    # the home load exactly, and all four values share one measurement
    # instant. Block 4 further down updates field by field instead.
    #
    # Both blocks can appear in the same frame and then differ slightly -
    # block 7 trails by roughly one measurement cycle and drops fields more
    # often - so they are merged field by field with 87 winning. Observed
    # 2026-07-28: block 7 reported 550 W of home load while block 87 reported
    # 560 W in the same frame, and the app showed 560 W.
    for block in _BLOCK_FLOW:
        flow = _sub(fields, block)
        if not flow:
            continue
        home_w = _num(flow, 1)
        if home_w is not None:
            out["home_w"] = home_w
        # Provisional: 4.13 below takes precedence.
        grid_w = _num(flow, 2)
        if grid_w is not None:
            out["grid_w"] = grid_w
        # Only as a fallback - block 65 is the better source for PV.
        if "solar_w" not in out:
            pv_w = _num(flow, 3)
            if pv_w is not None:
                out["solar_w"] = pv_w
        batt_w = _num(flow, 4)
        if batt_w is not None:
            out["batt_w"] = batt_w

    # Block 4 - inverter.
    #   1 total AC power   13 grid power   14.1 PV strings
    inverter = _sub(fields, _BLOCK_INVERTER)
    if inverter:
        # 4.13 is the grid meter: positive = import, negative = export.
        # Shown 2026-07-27 during a charge from the grid - 1719 W here while
        # the inverter (4.1) drew -1530 W and the house needed about 190 W,
        # and the sum adds up. It dropped to 0 within seconds of the charge
        # ending.
        grid_w = _num(inverter, 13)
        if grid_w is not None:
            out["grid_w"] = grid_w

        # 4.14 holds one sub-message per PV string, the string number in
        # field 1 and its power in field 4. Strings are reported only when
        # they change, so a frame may carry any subset - including none.
        for entry in inverter.get(14, []):
            if not isinstance(entry, bytes):
                continue
            try:
                string = _decode_fields(entry)
            except (IndexError, ValueError):
                continue
            for pv_raw in string.get(1, []):
                if not isinstance(pv_raw, bytes):
                    continue
                try:
                    pv = _decode_fields(pv_raw)
                except (IndexError, ValueError):
                    continue
                index = _num(pv, 1)
                power = _num(pv, 4)
                if index is None or power is None or not 1 <= index <= 4:
                    continue
                out[f"pv{int(index)}_w"] = power

    # Battery power when the flow block is absent.
    #
    # 65.20 was read as an absolute value, and it is not: it is signed with the
    # opposite convention, negative while the pack charges. Measured on an RE11
    # over 17 consecutive frames during a charge, where 65.20 ran -871, -862,
    # -852 W against +880, +830, +850 W in block 87.4 - the magnitudes track
    # each other within the usual inter-block lag, the sign is inverted. That
    # matches the 13 non-zero frames in the second-installation recording where
    # 65.20 equalled -(87.4) exactly.
    #
    # Reading it costs nothing and fixes a real gap: in a frame carrying only
    # block 65 the old code left batt_w at its last flow value, so a pack
    # charging at 5 kW kept reporting whatever it did when the last flow block
    # arrived.
    if "batt_w" not in out and summary:
        batt_summary = _num(summary, 20)
        if batt_summary is not None:
            out["batt_w"] = -batt_summary

    return out


def _finalize(parsed: dict[str, Any]) -> dict[str, Any]:
    """Split the signed power paths into the directional keys HA needs.

    The device reports one signed value per path. The energy dashboard wants
    two one-directional counters per path instead, because a Riemann sum over
    a signed value cancels itself out.

    Both halves are written whenever the signed source is present, including
    the zero: leaving the idle direction out would freeze its sensor at the
    last non-zero reading, and its energy counter would keep integrating a
    flow that stopped.
    """
    result = dict(parsed)

    batt_w = result.get("batt_w")
    if isinstance(batt_w, (int, float)):
        result["batt_charge_power_w"] = max(0.0, float(batt_w))
        result["batt_discharge_power_w"] = max(0.0, -float(batt_w))

    grid_w = result.get("grid_w")
    if isinstance(grid_w, (int, float)):
        result["grid_import_power_w"] = max(0.0, float(grid_w))
        result["grid_export_power_w"] = max(0.0, -float(grid_w))

    return result


def parse_ocean2_proto_message(payload: bytes) -> dict[str, Any] | None:
    """Parse an Ocean 2 protobuf frame into flat sensor keys."""
    try:
        headers, _ = decode_header_message(payload)
    except (IndexError, ValueError, struct.error):
        return None
    if not headers:
        return None

    merged: dict[str, Any] = {}
    for header in headers:
        if header.get("cmd_func") != _CMD_FUNC:
            continue
        if header.get("cmd_id") != _CMD_ID_TELEMETRY:
            continue
        for pdata in _pdata_candidates(header):
            try:
                decoded = _parse_telemetry(pdata)
            except (IndexError, ValueError, struct.error):
                # Only a payload that is not valid protobuf falls through to
                # the next candidate; a clean decode ends the attempt whether
                # it produced fields or not.
                continue
            merged.update(decoded)
            break

    return _finalize(merged) if merged else None
