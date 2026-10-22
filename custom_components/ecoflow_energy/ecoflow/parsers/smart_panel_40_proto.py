"""Protobuf telemetry parser for the EcoFlow OCEAN Smart Electrical Panel 40 (`HR61`).

Two frames on cmd_func 254: the property push on cmd_id 21
(`DisplayPropertyUpload`) carries grid/load/PV/battery totals, the grid L1/L2
breakdown and the 40 circuit blocks; the safety push on cmd_id 25
(`SafetyParamSet`) carries the grid code and the nominal voltage/frequency the
panel was commissioned with.

Unlike the Ocean 2 parser (`ocean2_proto.py`), a `254/21` push from this
device is **incremental**: a live push changes 52-75 of the roughly 550
declared paths against the ~3 kB full state a `get_reply` bundle carries, and
a field missing from a push is unchanged, not zero. `parse_hr61_proto_message`
returns only what a frame carries; the coordinator's own
`_device_data.update()` is the one merge that keeps the rest, as for every
other device.

Decoding reuses the declared-path walker (`_compile`/`_walk`) that
`stream_ac5000_proto` implements, the way `ocean2_proto` already does for the
same shape, rather than a second copy of it. The circuit power/voltage
readings (fields 1015-1054, one message per circuit, sub `.1` voltage, `.2`
power, `.3` current) sit flat inside that tree like any other block. The
circuit on/name/link block (`LoadChSta`, fields 794-805 for circuits 1-12 and
920-947 for circuits 13-40) carries a name string in its own sub-field, which
the walker's scalar decoder cannot hold - so, like Ocean 2's PV string and
inverter phase blocks, it is read directly with `_iter_fields` instead.

The circuit message's schema marks all three sub-fields with explicit
presence tracking, so an absent field is not implied by a default-value
convention - and yet a 46-frame capture (issue #434) shows the panel omitting
`.2` (power) whenever a circuit's reading is exactly 0 W: 280 of 431 circuit
messages seen, 38 of them a circuit that had reported a nonzero power moments
before and, unfixed, would keep showing it. `.3` (current) does the same and
was already read that way. `.1` (voltage) carries the identical presence
tracking but was never once absent from a present circuit message in that
capture - an energized panel leg does not read 0 V - so it keeps the plain
rule instead: absent means unchanged, not zero. See `_CIRCUIT_ZERO_FILL`.

The same distinction applies to any field outside the circuit blocks: a field
this device tracks presence on but never omits at zero (every one of
515/516/517/518/956/957/962-967 checked against all 9 full-state frames in
the capture) needs no fill and keeps the incremental merge below. Absence can
only be read as zero on a full-state `get_reply` frame that would otherwise
have to carry the field, never on an incremental push, where a field's
absence never means anything but "unchanged since the last frame that
carried it".

Field assignments come from a 46-frame capture of a live installation
(issue #434): grid/PV/battery close
the load balance exactly on multiple frames (`load = grid + pv - battery`),
grid L1 and L2 power sum to the grid total within the meter's own instant-lag
(observed up to ~6 W apart), and the circuit sign was confirmed against the
two circuits labelled "OCEAN Pro" in the capture - a generation source, whose
raw power is positive while every consuming circuit's raw power is negative.

Circuits are created on report, never pre-declared: fields 1055-1062 (which
would be circuits 41-48) are schema-present but this unit never sends them,
so they are not in the field map at all, and a circuit only gets keys once
its message is actually seen on the wire.

Not parsed: 960/961 (frequency, encoding unresolved), 1005 (a second battery
source), 254/22, 254/23, 240/x and the kWh counters - none of those are
settled well enough yet to publish.
"""

from __future__ import annotations

import struct
from math import isfinite
from typing import Any

from ..proto.decoder import decode_header_message
from .stream_ac5000_proto import (
    _TYPE_FLOAT,
    _TYPE_INT,
    _compile,
    _decode_scalar,
    _iter_fields,
    _walk,
)
from .stream_proto import _pdata_candidates

# The two frames this device sends on cmd_func 254.
_CMD_FUNC = 254
_CMD_ID_PROPERTY = 21
_CMD_ID_SAFETY = 25

#: mA -> A. `956`/`957` (voltage) and `962`-`967` (power/reactive/apparent) are
#: already in their published unit; only the L1/L2 current pair is scaled -
#: `958`/`959` exist too but round to whole amps and are deliberately not
#: read here.
_MILLIAMP_TO_AMP = 0.001

_HR61_FIELD_MAP: dict[str, tuple[str, str, float]] = {
    # --- system totals, positive = import/charging ---
    "515": ("grid_power_w", _TYPE_FLOAT, 1),
    "516": ("load_power_w", _TYPE_FLOAT, 1),
    "517": ("pv_power_w", _TYPE_FLOAT, 1),
    "518": ("battery_power_w", _TYPE_FLOAT, 1),
    "262": ("battery_soc_pct", _TYPE_FLOAT, 1),
    # Full-state constant on every frame seen (50); still read live rather
    # than hardcoded, the same way the ES22 grid ceiling is read live.
    "461": ("backup_reserve_pct", _TYPE_INT, 1),
    # --- grid L1/L2 breakdown ---
    "956": ("grid_l1_voltage_v", _TYPE_FLOAT, 1),
    "957": ("grid_l2_voltage_v", _TYPE_FLOAT, 1),
    "1485": ("grid_l1_current_a", _TYPE_INT, _MILLIAMP_TO_AMP),
    "1486": ("grid_l2_current_a", _TYPE_INT, _MILLIAMP_TO_AMP),
    "962": ("grid_l1_power_w", _TYPE_FLOAT, 1),
    "963": ("grid_l2_power_w", _TYPE_FLOAT, 1),
    "964": ("grid_l1_reactive_power_var", _TYPE_FLOAT, 1),
    "965": ("grid_l2_reactive_power_var", _TYPE_FLOAT, 1),
    "966": ("grid_l1_apparent_power_va", _TYPE_FLOAT, 1),
    "967": ("grid_l2_apparent_power_va", _TYPE_FLOAT, 1),
}

#: Circuit count this device schema carries (1055-1062 would be circuits
#: 41-48 and are never sent by this unit, so they stay out of the map - see
#: the module docstring).
CIRCUIT_COUNT = 40
#: Circuit `n`'s power/voltage/current message sits on field `1014 + n`.
_CIRCUIT_POWER_BASE_FIELD = 1014

for _n in range(1, CIRCUIT_COUNT + 1):
    _field = _CIRCUIT_POWER_BASE_FIELD + _n
    _HR61_FIELD_MAP[f"{_field}.1"] = (f"circuit_{_n}_voltage_v", _TYPE_FLOAT, 1)
    # Raw negative is the device's own "consumption" sign; the app and every
    # entity in this integration expect the opposite: positive draws power
    # from the panel, negative feeds it (the two "OCEAN Pro" circuits in the
    # capture, a generation source, report positive raw and so end up
    # negative here).
    _HR61_FIELD_MAP[f"{_field}.2"] = (f"circuit_{_n}_power_w", _TYPE_FLOAT, -1)
    _HR61_FIELD_MAP[f"{_field}.3"] = (f"circuit_{_n}_current_a", _TYPE_INT, 1)
del _n, _field

_HR61_TREE: dict[int, Any] = _compile(_HR61_FIELD_MAP)

#: group path ("1015", ..., "1054") -> ((key, zero value), ...) for the two
#: sub-fields (`.2` power, `.3` current) whose own absence inside a present
#: circuit message decodes as zero, the same reasoning `_ZERO_FILL_PATHS` in
#: `stream_ac5000_proto` applies to its own fields - see the module docstring
#: for the capture evidence. Voltage (`.1`) is deliberately not here: it
#: keeps the plain "message present, field absent -> state unchanged" merge.
#:
#: It only fires when the group itself was seen: a circuit whose whole
#: message is absent from this push reports nothing, so the coordinator merge
#: (or `state` here) keeps whatever it read last. And it creates the key on
#: the circuit's first sighting the same way as any later one - a circuit
#: that only ever reports 0 W (circuit 1 in the fixture) still gets a
#: `circuit_1_power_w` key, rather than never getting one at all.
_CIRCUIT_ZERO_FILL: dict[str, tuple[tuple[str, int | float], ...]] = {
    str(_CIRCUIT_POWER_BASE_FIELD + n): (
        (f"circuit_{n}_power_w", 0.0),
        (f"circuit_{n}_current_a", 0),
    )
    for n in range(1, CIRCUIT_COUNT + 1)
}

_SAFETY_FIELD_MAP: dict[str, tuple[str, str, float]] = {
    "112": ("grid_code", _TYPE_INT, 1),
    "113": ("grid_nominal_voltage_v", _TYPE_FLOAT, 1),
    "114": ("grid_nominal_frequency_hz", _TYPE_FLOAT, 1),
}
_SAFETY_TREE: dict[int, Any] = _compile(_SAFETY_FIELD_MAP)

# `LoadChSta`: one message per circuit, at a field number that does not
# follow the power block's `1014 + n` numbering. `.1` is the on/off state,
# `.5` the owner's app label, and `.2.2` the two-pole partner channel for a
# circuit wired across two breaker slots - both legs still report their own
# power/voltage/current, this is only the cross-reference.
_LOAD_CH_STA_ON_FIELD = 1
_LOAD_CH_STA_SPLITPHASE_FIELD = 2
_LOAD_CH_STA_LINK_FIELD = 2
_LOAD_CH_STA_NAME_FIELD = 5


def _load_ch_sta_circuit(field: int) -> int | None:
    """Circuit number for a `LoadChSta` field, or None outside the two ranges."""
    if 794 <= field <= 805:
        return field - 793
    if 920 <= field <= 947:
        return field - 907
    return None


def _decode_circuit_states(payload: bytes) -> dict[str, Any]:
    """Decode every `LoadChSta` message present in `payload`.

    Read with `_iter_fields` rather than through the declared-path tree: the
    name is a string, which `_decode_scalar` has no type for, the same reason
    Ocean 2's PV string and inverter phase blocks are read this way instead
    of being force-fit into the tree.
    """
    out: dict[str, Any] = {}
    for field_num, wire_type, raw in _iter_fields(payload):
        if wire_type != 2:
            continue
        n = _load_ch_sta_circuit(field_num)
        if n is None:
            continue
        on_raw: int | None = None
        name: str | None = None
        link: int | None = None
        for sub_num, sub_wire, sub_raw in _iter_fields(raw):
            if sub_num == _LOAD_CH_STA_ON_FIELD and sub_wire == 0:
                decoded = _decode_scalar(0, sub_raw, _TYPE_INT)
                if isinstance(decoded, int):
                    on_raw = decoded
            elif sub_num == _LOAD_CH_STA_NAME_FIELD and sub_wire == 2:
                try:
                    name = sub_raw.decode("utf-8")
                except UnicodeDecodeError:
                    name = None
            elif sub_num == _LOAD_CH_STA_SPLITPHASE_FIELD and sub_wire == 2:
                for ssub_num, ssub_wire, ssub_raw in _iter_fields(sub_raw):
                    if ssub_num == _LOAD_CH_STA_LINK_FIELD and ssub_wire == 0:
                        decoded = _decode_scalar(0, ssub_raw, _TYPE_INT)
                        if isinstance(decoded, int):
                            link = decoded
        # The panel omits zero values, so a present message without the
        # state field is an open breaker (0), not "unchanged". The name is
        # always published, empty when the owner left it blank, so an entity
        # that waits for it is never held back by a circuit without one.
        out[f"circuit_{n}_on"] = on_raw == 1
        out[f"circuit_{n}_name"] = (name or "").strip()
        if link is not None:
            out[f"circuit_{n}_link"] = link
    return out


def _parse_property(pdata: bytes) -> dict[str, Any]:
    """Decode one `254/21` payload into flat sensor keys.

    Returns only what this frame carries - the incremental case leaves most
    of the ~550 declared paths out entirely, and that absence is the caller's
    signal to keep whatever `state` already holds for those keys.
    """
    walked: dict[str, Any] = {}
    seen: set[str] = set()
    _walk(pdata, _HR61_TREE, walked, seen)

    # A NaN or an infinity reaching a sensor raises inside Home Assistant's
    # rounding and aborts the rest of that update; `_walk` itself has no
    # notion of this, the same guard every sibling parser applies.
    not_finite = [
        key
        for key, value in walked.items()
        if isinstance(value, float) and not isfinite(value)
    ]
    for key in not_finite:
        del walked[key]

    # A field the panel sent as NaN was sent, just unreadable: it keeps its
    # last value rather than being taken for an omitted zero.
    for group, fills in _CIRCUIT_ZERO_FILL.items():
        if group not in seen:
            continue
        for key, zero in fills:
            if key not in walked and key not in not_finite:
                walked[key] = zero

    walked.update(_decode_circuit_states(pdata))
    return walked


def _parse_safety(pdata: bytes) -> dict[str, Any]:
    """Decode one `254/25` payload into flat sensor keys."""
    walked: dict[str, Any] = {}
    _walk(pdata, _SAFETY_TREE, walked, set())
    return walked


def parse_hr61_proto_message(payload: bytes) -> dict[str, Any] | None:
    """Parse one HR61 frame into the keys it carries.

    A property push is incremental, so the result holds only what this frame
    carries; the coordinator's `_device_data.update(parsed)` keeps the rest.
    A frame with nothing this parser recognises (only `240/x`, `254/22`,
    `254/23`, ...) returns None, the same "nothing to merge" signal
    `ocean2_proto` gives its caller.

    A `get_reply` bundle carries several headers in one envelope - `254/21`
    (the full state), `254/25` (the grid-code block) and others this parser
    does not read - and both of the ones it does read are combined into one
    result, in header order.
    """
    try:
        headers, _ = decode_header_message(payload)
    except (IndexError, ValueError, struct.error):
        return None
    if not headers:
        return None

    result: dict[str, Any] = {}
    updated = False
    for header in headers:
        if header.get("cmd_func") != _CMD_FUNC:
            continue
        cmd_id = header.get("cmd_id")
        if cmd_id not in (_CMD_ID_PROPERTY, _CMD_ID_SAFETY):
            continue
        for pdata in _pdata_candidates(header):
            try:
                decoded = (
                    _parse_property(pdata)
                    if cmd_id == _CMD_ID_PROPERTY
                    else _parse_safety(pdata)
                )
            except (IndexError, ValueError, struct.error):
                # Only a payload that is not valid protobuf falls through to
                # the next candidate; a clean decode ends the attempt whether
                # it produced fields or not.
                continue
            if decoded:
                result.update(decoded)
                updated = True
            break

    return result if updated else None
