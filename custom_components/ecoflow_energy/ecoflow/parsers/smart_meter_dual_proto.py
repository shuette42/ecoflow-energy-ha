"""Read-only Smart Meter Dual CT (ES81) plaintext 254/21 telemetry.

Native powers, voltages, signed currents and Wh counters use the observed
float32 fields. Directional counters sum phase import/export before netting;
they do not express billing-meter equivalence. Missing fields stay absent.
No BK21 omitted-export zero policy or power integration is inherited.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from math import isfinite
from typing import Any

from ..proto.decoder import decode_header_message
from .stream_proto import _read_varint

_SCALAR_FIELDS = {
    515: "grid_w",
    962: "grid_l1_w",
    963: "grid_l2_w",
    772: "grid_l3_w",
    956: "grid_l1_voltage_v",
    957: "grid_l2_voltage_v",
    771: "grid_l3_voltage_v",
    958: "grid_l1_current_a",
    959: "grid_l2_current_a",
    784: "grid_l3_current_a",
}
_ENERGY_FIELDS = {
    1: "grid_l1_net_energy_wh",
    2: "grid_l2_net_energy_wh",
    3: "grid_l3_net_energy_wh",
    4: "grid_import_energy_wh",
    6: "grid_export_energy_wh",
    7: "grid_net_energy_wh",
}
_PAYLOAD_LIMIT = 8192


def _fields(payload: bytes | memoryview) -> Iterator[tuple[int, int, memoryview]]:
    """Walk a complete protobuf level and reject incomplete trailing data."""
    view = memoryview(payload)
    position = 0
    while position < len(view):
        tag, position = _read_varint(view, position)
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError("zero_field")
        if wire == 0:
            start = position
            _, position = _read_varint(view, position)
            yield number, wire, view[start:position]
            continue
        if wire == 2:
            length, position = _read_varint(view, position)
        elif wire in (1, 5):
            length = 8 if wire == 1 else 4
        else:
            raise ValueError("unsupported_wire")
        end = position + length
        if end > len(view):
            raise ValueError("truncated_field")
        yield number, wire, view[position:end]
        position = end


def _readings(
    payload: bytes | memoryview,
    field_map: dict[int, str],
    *,
    energy_record: bool = False,
) -> dict[str, float]:
    """Decode present scalars and, once only, the observed native773 record."""
    result: dict[str, float] = {}
    seen: set[int] = set()
    for number, wire, raw in _fields(payload):
        mapped = number in field_map or energy_record and number == 773
        if mapped and number in seen:
            raise ValueError("duplicate_mapped_field")
        seen.add(number)
        if energy_record and number == 773 and wire == 2:
            result.update(_readings(raw, _ENERGY_FIELDS))
        elif number in field_map and wire == 5:
            value = struct.unpack("<f", raw)[0]
            if isfinite(value):
                result[field_map[number]] = value
    return result


def _reported_wifi_signal(payload: bytes) -> dict[str, float]:
    """Omit an ambiguous diagnostic without rejecting valid measurements."""
    signal: tuple[int, memoryview] | None = None
    for number, wire, raw in _fields(payload):
        if number != 602:
            continue
        if signal is not None:
            return {}
        signal = (wire, raw)
    if signal is not None and signal[0] == 5:
        value = struct.unpack("<f", signal[1])[0]
        if isfinite(value):
            return {"wifi_signal_reported": value}
    return {}


def parse_smart_meter_dual_message(payload: bytes) -> dict[str, Any] | None:
    """Decode ES81 plain telemetry only, preserving incremental field presence."""
    if not payload or len(payload) > _PAYLOAD_LIMIT:
        return None
    try:
        # The shared envelope decoder is permissive about truncated tails.
        # Validate each actual header first, without changing other devices.
        critical = {1, 6, 8, 9, 10, 12, 15, 16, 17}
        for number, wire, raw in _fields(payload):
            if number != 1:
                continue
            if wire != 2:
                raise ValueError("header_wrong_wire")
            seen: set[int] = set()
            for field, field_wire, _ in _fields(raw):
                if field in critical and field_wire != (2 if field == 1 else 0):
                    raise ValueError("header_field_wrong_wire")
                if field in critical and field in seen:
                    raise ValueError("duplicate_header_field")
                seen.add(field)
        headers, _ = decode_header_message(payload)
        result: dict[str, Any] = {}
        for header in headers:
            if (header.get("cmd_func"), header.get("cmd_id")) != (254, 21):
                continue
            if header.get("enc_type", 0) != 0 or header.get("is_ack", 0) != 0:
                continue
            # Actual get_reply bundles omit product/version metadata; the
            # property packets supply these three values. Both are captured.
            if any(
                key in header and header[key] != value
                for key, value in (
                    ("product_id", 42241),
                    ("version", 3),
                    ("payload_ver", 1),
                )
            ):
                continue
            data = bytes.fromhex(header.get("pdata", ""))
            if not data or header.get("data_len", len(data)) != len(data):
                continue
            result.update(_readings(data, _SCALAR_FIELDS, energy_record=True))
            result.update(_reported_wifi_signal(data))
    except (ValueError, TypeError, IndexError, struct.error):
        return None
    return result or None
