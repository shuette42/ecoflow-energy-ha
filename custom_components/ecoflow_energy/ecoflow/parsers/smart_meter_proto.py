"""Protobuf telemetry parser for the EcoFlow Smart Meter (BK21, EF-EM-P3-120).

Derived from a 17-minute capture of a live BK21 in app-auth MQTT mode
(2026-08-31, issue #331). The meter measures and reports and does nothing
else: there is no battery, no PV, no outlet and no write path worth having
(its config message covers the timezone, the upload periods and a factory
reset), so it gets its own device type and its own map rather than a branch
inside the Stream parser.

The envelope is the BK-series one, so everything that unwraps a frame is
imported from `stream_proto.py`: the header decode, the per-header XOR mask
keyed on the low byte of the sequence number, the one-level field walk and
the scalar decoder. Only the map lives here, because this message nests its
energy counters where the Stream frames use flat scalars, so the walk is
run a second time on the nested record.

Two message types arrive, both under cmd_func 254:

- `21` DisplayPropertyUpload, the telemetry frame. Three shapes were
  observed: a 24-byte incremental (aggregate power plus the three phase
  powers), a 47-byte one that adds the energy record, and a 146-byte full
  upload that adds voltages, currents, the phase flags and the grid state.
- `22` RuntimePropertyUpload, the upload periods only. Nothing there is a
  reading, so no field of it is mapped; it is left out of the map entirely
  rather than mapped and discarded.

The full upload also arrives bundled with a `22` in a `get_reply` frame,
unmasked. Both candidates from `_pdata_candidates` are tried, so the masked
`property` frames and the plain `get_reply` bundles decode through the same
path.

Field notes:

- `515` pow_get_sys_grid is the aggregate the app shows as the current
  power. `616` grid_connection_power exists in the message definition and
  was never sent, so the aggregate is read from 515 alone.
- The per-phase power, voltage and current fields do not multiply out to
  each other: 240 V and 2.107 A against 318 W on L2. That is the meter
  reporting apparent and active power separately, not a scaling error;
  no factor is applied to any of them. The app shows the same numbers.
- `773` is the only nested field. Its `.4` (today) and `.7` (lifetime)
  counters are equal in every frame of the capture because the meter was
  installed that day, so which is which cannot be told from these bytes.
  `.7` is named lifetime here because the message definition says so; the
  split stays unconfirmed until a capture spanning midnight exists.
- `773.1` (`today_active_L1`) is the one mapped field the capture never
  carried: L1 drew nothing all day, and its power, voltage and current
  fields are all present and all zero. Its two siblings `.2` and `.3` are
  on the wire in the same record, so the record shape is shown rather than
  assumed and only the idle phase is missing.

Deliberately not mapped: `133`/`134`/`135` (timezone), `627` (error code
list), `728`/`729`/`732`/`733` (country, town, factory and debug mode),
`984` (unidentified constant), and the `254/22` upload periods. Not on the
wire in this capture and therefore absent from the map: `616`, `617`
(reactive power), `601`/`602` (WiFi), `484` (ambient temperature),
`773.5`/`773.6` (reactive energy). The message definition has no export
counter at all - export shows only as a negative sign on the power fields.

Values are published unrounded, as in `stream_proto.py`. Display precision
belongs to the sensor definitions, and rounding the current to one decimal
here would make this integration less precise than the app, which shows
2.14 A.
"""

from __future__ import annotations

from math import isfinite
from typing import Any

from ..proto.decoder import decode_header_message
from .stream_proto import (
    _FLOAT_ZERO_EPS,
    _GRID_CONNECTION_STATE,
    _TYPE_FLOAT,
    _TYPE_INT,
    _decode_scalar,
    _iter_fields,
    _pdata_candidates,
)

# The nested record holding the energy counters. It is the only
# length-delimited field this parser descends into: everything else the
# message carries in a length-delimited field is a string or an error list.
_ENERGY_RECORD_FIELD = 773

# cmd_func/cmd_id -> field_number -> (sensor_key, scalar_type)
_SMART_METER_FIELD_MAP: dict[tuple[int, int], dict[int, tuple[str, str]]] = {
    (254, 21): {
        # Aggregate grid power, the app's "current power".
        515: ("grid_w", _TYPE_FLOAT),
        # Per-phase active power. L1 reads 0.0 throughout the capture; that
        # is an idle phase on the reporter's installation, not an absent
        # field - it is sent explicitly in every frame.
        962: ("grid_l1_w", _TYPE_FLOAT),
        963: ("grid_l2_w", _TYPE_FLOAT),
        772: ("grid_l3_w", _TYPE_FLOAT),
        # Per-phase voltage and current, full uploads only.
        956: ("grid_l1_voltage_v", _TYPE_FLOAT),
        957: ("grid_l2_voltage_v", _TYPE_FLOAT),
        771: ("grid_l3_voltage_v", _TYPE_FLOAT),
        958: ("grid_l1_current_a", _TYPE_FLOAT),
        959: ("grid_l2_current_a", _TYPE_FLOAT),
        784: ("grid_l3_current_a", _TYPE_FLOAT),
        # Present in every full upload and 0.0 in all of them.
        618: ("grid_power_factor", _TYPE_FLOAT),
        # Grid connection state, mapped to its enum labels in `_finalize`.
        619: ("_grid_connection_state_raw", _TYPE_INT),
        # Per-phase connection flags, booleans on the wire.
        762: ("_grid_l1_connected_raw", _TYPE_INT),
        763: ("_grid_l2_connected_raw", _TYPE_INT),
        764: ("_grid_l3_connected_raw", _TYPE_INT),
    },
}

# Subfields of `_ENERGY_RECORD_FIELD`, all watt-hours.
_ENERGY_RECORD_MAP: dict[int, tuple[str, str]] = {
    1: ("grid_l1_energy_today_wh", _TYPE_FLOAT),
    2: ("grid_l2_energy_today_wh", _TYPE_FLOAT),
    3: ("grid_l3_energy_today_wh", _TYPE_FLOAT),
    4: ("grid_energy_today_wh", _TYPE_FLOAT),
    7: ("grid_energy_total_wh", _TYPE_FLOAT),
}

# Keys that carry a lifetime counter. Such a counter only ever stands still
# or moves, so a zero on one is a glitch rather than a reading, and a glitch
# published to the Energy Dashboard sensor would show as the house giving
# back everything it ever drew. It is dropped rather than published. The
# daily counters are the opposite case: their midnight zero is the reading.
_LIFETIME_KEYS = frozenset({"grid_energy_total_wh"})

_BOOL_KEYS = {
    "_grid_l1_connected_raw": "grid_l1_connected",
    "_grid_l2_connected_raw": "grid_l2_connected",
    "_grid_l3_connected_raw": "grid_l3_connected",
}


def _decode_mapped_fields(
    pdata: bytes,
    field_map: dict[int, tuple[str, str]],
) -> dict[str, Any]:
    """Decode the mapped scalars plus the nested energy record."""
    result: dict[str, Any] = {}

    for field_num, wire_type, raw in _iter_fields(pdata):
        if field_num == _ENERGY_RECORD_FIELD and wire_type == 2:
            for sub_num, sub_wire, sub_raw in _iter_fields(raw):
                mapping = _ENERGY_RECORD_MAP.get(sub_num)
                if mapping is None:
                    continue
                sensor_key, scalar_type = mapping
                value = _decode_scalar(sub_wire, sub_raw, scalar_type)
                if value is not None:
                    result[sensor_key] = value
            continue

        mapping = field_map.get(field_num)
        if mapping is None:
            continue

        sensor_key, scalar_type = mapping
        value = _decode_scalar(wire_type, raw, scalar_type)
        if value is not None:
            result[sensor_key] = value

    return result


def _finalize(parsed: dict[str, Any]) -> dict[str, Any]:
    """Normalize near-zero floats and resolve the enum and boolean fields."""
    result = dict(parsed)

    for key, value in list(result.items()):
        if isinstance(value, float) and isfinite(value) and abs(value) < _FLOAT_ZERO_EPS:
            result[key] = 0.0

    grid_state_raw = result.pop("_grid_connection_state_raw", None)
    if isinstance(grid_state_raw, int):
        result["grid_connection_state"] = _GRID_CONNECTION_STATE.get(grid_state_raw)

    for raw_key, sensor_key in _BOOL_KEYS.items():
        raw_value = result.pop(raw_key, None)
        if isinstance(raw_value, int):
            result[sensor_key] = bool(raw_value)

    for key in _LIFETIME_KEYS:
        value = result.get(key)
        if isinstance(value, (int, float)) and not value:
            del result[key]

    return result


def parse_smart_meter_message(payload: bytes) -> dict[str, Any] | None:
    """Parse a Smart Meter protobuf frame into flat sensor keys."""
    try:
        headers, _ = decode_header_message(payload)
        if not headers:
            return None

        merged: dict[str, Any] = {}
        for header in headers:
            cmd_key = (int(header.get("cmd_func", -1)), int(header.get("cmd_id", -1)))
            field_map = _SMART_METER_FIELD_MAP.get(cmd_key)
            if field_map is None:
                continue
            for pdata in _pdata_candidates(header):
                try:
                    decoded = _decode_mapped_fields(pdata, field_map)
                except (IndexError, ValueError):
                    # Only a payload that is not valid protobuf falls through
                    # to the next candidate; a clean decode ends the attempt,
                    # empty or not (see `_pdata_candidates`).
                    continue
                merged.update(decoded)
                break
    except Exception:
        return None
    if not merged:
        return None

    finalized = _finalize(merged)
    return finalized or None
