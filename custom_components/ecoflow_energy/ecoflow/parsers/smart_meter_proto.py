"""Protobuf telemetry parser for the EcoFlow Smart Meter (BK21, EF-EM-P3-120).

Derived from two captures of a live BK21 in app-auth MQTT mode (issue #331):
a 17-minute one taken the day the meter was installed (2026-08-31), and an
18 h 40 min one spanning local midnight (2026-09-03/04, @wildnet), plus a
few-minutes export test he ran on the same house. The meter measures and
reports and does nothing else: there is no battery, no PV, no outlet and no
write path worth having (its config message covers the timezone, the upload
periods and a factory reset), so it gets its own device type and its own
map rather than a branch inside the Stream parser.

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
- `773` is the only nested field, and it holds six lifetime counters, not a
  lifetime pair and four daily ones - direction decides which counter a
  subfield is, not a reset period. `.4` is cumulative import, `.6`
  cumulative export, `.7` cumulative net import (`.4 - .6`), `.1/.2/.3`
  cumulative net per phase (sum to `.7`, not to `.4`). Nothing on the wire
  resets at midnight: the five retained `773` records of the midnight
  capture (one before local midnight, four after) hold `.1 + .2 + .3 = .7`
  and `.4 - .6 = .7` exactly on both sides, and none of the six counters
  falls there. The vendor schema names `.4` `today_active`, `.7`
  `total_active_energy`, `.1/.2/.3` `today_active_L1..L3`, and lists `.5`
  and `.6` as reactive energy; every one of those names is wrong for this
  device. A definition says what a field is called; the numbers say what
  the device puts in it.
- The encoder omits a subfield at zero from the record; absence inside a
  present record is the zero, not a missing reading. `.1` was absent from
  every frame of the first capture with phase A idle at 0 W and is present
  at 3 Wh in every record of the midnight capture; `.6` was absent from the
  first capture, taken before the house had ever exported, and is present
  at 28 Wh in every midnight record, after a few minutes of export on
  2026-09-03 moved it from 25 to 28. `_decode_mapped_fields` leans on this
  once, for `.6` alone: a present record without it publishes
  `grid_export_energy_wh = 0.0`, because export's zero is the one steady
  state among the six (import and the phase nets leave zero within hours of
  commissioning, so their absence is never seen again). `.4` is never
  filled this way - a missing `.4` stays missing, or a house that has
  always exported and never imported would get a fabricated import reading.
- `773.5` is in no record of either capture. The vendor schema calls it
  reactive energy too - the same label it wrongly gives `.6` - and it stays
  unmapped until a frame carries it and its own reading can be checked
  against something.

Deliberately not mapped: `133`/`134`/`135` (timezone), `627` (error code
list), `728`/`729`/`732`/`733` (country, town, factory and debug mode),
`984` (unidentified constant), and the `254/22` upload periods. Not on the
wire in either capture and therefore absent from the map: `616`, `617`
(reactive power), `601`/`602` (WiFi), `484` (ambient temperature), and
`773.5` (see above).

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

# Subfields of `_ENERGY_RECORD_FIELD`, all watt-hours, all lifetime (ADR-018,
# PLAN-123): the midnight capture proves none of the six resets, so the key
# names the direction the meter counts in, never a period.
_ENERGY_RECORD_MAP: dict[int, tuple[str, str]] = {
    1: ("grid_l1_net_energy_wh", _TYPE_FLOAT),
    2: ("grid_l2_net_energy_wh", _TYPE_FLOAT),
    3: ("grid_l3_net_energy_wh", _TYPE_FLOAT),
    4: ("grid_import_energy_wh", _TYPE_FLOAT),
    6: ("grid_export_energy_wh", _TYPE_FLOAT),
    7: ("grid_net_energy_wh", _TYPE_FLOAT),
}

# The one subfield whose absence inside a present record is filled rather
# than left missing (see the module docstring, "encoder behaviour"): its
# zero is the only steady state among the six.
_ENERGY_EXPORT_FIELD = 6

# The key that carries the import counter. It only ever stands still or
# rises, so an explicit zero on it is a glitch rather than a reading - this
# encoder omits zeros, so an explicit one was written some other way - and a
# glitch published to the Energy Dashboard sensor would show as the house
# giving back everything it ever drew. It is dropped rather than published.
# Export's explicit zero is the fill above, and a zero on a net figure
# (total or per phase) is a reading a net exporter passes through, so
# neither belongs on this list.
_LIFETIME_KEYS = frozenset({"grid_import_energy_wh"})

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
            # The fill below rests on absence, so presence has to be read
            # from the wire rather than from a successful decode. A field
            # that is there and does not decode says nothing, and turning
            # that into a zero would write a reading nobody measured onto a
            # counter that only rises, which Home Assistant takes for a
            # meter change (`never-publish-zero-total-increasing`).
            export_present = False
            counters_decoded = 0
            for sub_num, sub_wire, sub_raw in _iter_fields(raw):
                if sub_num == _ENERGY_EXPORT_FIELD:
                    export_present = True
                mapping = _ENERGY_RECORD_MAP.get(sub_num)
                if mapping is None:
                    continue
                sensor_key, scalar_type = mapping
                value = _decode_scalar(sub_wire, sub_raw, scalar_type)
                if value is not None:
                    result[sensor_key] = value
                    counters_decoded += 1
            # And a record that carried no counter we read is not a reading
            # at all: filling it would return a lone export of zero and make
            # the whole message look like it had data.
            if not export_present and counters_decoded:
                export_key, _ = _ENERGY_RECORD_MAP[_ENERGY_EXPORT_FIELD]
                result[export_key] = 0.0
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
        if (
            isinstance(value, float)
            and isfinite(value)
            and abs(value) < _FLOAT_ZERO_EPS
        ):
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

    # The meter sends the power factor on every complete upload and reads
    # 0.0 in all of them, on two separate installations, with power and
    # current both non-zero. A power factor of zero while 319 W flows is not
    # physically possible, so the field is empty rather than measured, and
    # an impossible number is worse than none. Reported on #331; the sensor
    # stays and reports nothing instead. A zero on an idle meter is left
    # alone, because then there is nothing it contradicts.
    power = result.get("grid_w")
    factor = result.get("grid_power_factor")
    if (
        isinstance(factor, (int, float))
        and not factor
        and isinstance(power, (int, float))
        and power
    ):
        # An explicit None rather than dropping the key: device data is
        # merged, not replaced, and a sensor only falls back to its restored
        # value when the key is missing from that merged state. Dropping it
        # would leave whatever was stored last in place, so a meter that
        # reported zero once while idle would keep showing that zero for
        # good. `None` is what the sensor reads as a clear.
        result["grid_power_factor"] = None

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
