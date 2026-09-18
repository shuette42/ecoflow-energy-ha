"""Protobuf telemetry parser for the EcoFlow Ocean 2 (`RE11`, `RE17`).

Two frames on cmd_func 254: the main telemetry on cmd_id 39, and the
per-module battery frame on cmd_id 46. They are shaped differently and are
read by separate functions - telemetry carries every reading in one payload,
while a module frame carries exactly one module and a frame bundles one
header per module.

Unlike the Stream and Delta 3 frames, an Ocean 2 telemetry payload is
**nested**: the readings sit inside length-delimited sub-messages (65, 7/87,
4) rather than at the top level. A flat field map like `_STREAM_FIELD_MAP`
cannot express that, which is why decoding here reuses the declared-path
walker (`_compile`/`_walk`) that `stream_ac5000_proto` already implements for
the same shape, instead of a second copy of it.

That walker writes one dict key per declared path and has no notion of one
path superseding another, so the four power paths that exist in more than one
block (65.4/7.3/87.3 for solar, 7.2/87.2/4.13 for grid, 65.20/7.4/87.4 for
battery, 7.1/87.1 for the home load) are each declared under a distinct,
block-prefixed key, and the precedence between them - block 87 over block 7
over the summary/inverter fallback - is resolved afterwards in
`_parse_telemetry`, not by the walker.

Field assignments below were verified against live hardware in Enhanced mode
by cross-checking every value against the EcoFlow app over several days,
including a 3.6 kW wallbox charge that moved all four power paths at once,
and confirmed afterwards against a seven hour recording from a second,
unrelated installation: the numbers line up frame for frame, the load-bearing
readings and the module block included.

They come from https://github.com/jensfr1/ha-ecoflow-ocean2, a standalone
Ocean 2 integration that decodes the full frame set, including the
per-module battery data this parser reads below.

Standard Mode is not an option for this device: the HTTP quota call answers
error 1006 ("current device is not allowed to get device info"), the same
situation as the J32D/J32E PowerOcean variants (#89, #145).
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
_CMD_ID_TELEMETRY = 39
_CMD_ID_MODULE = 46

# Declared paths -> (temporary key, scalar type, scale). Every power path
# that exists in more than one block gets its own block-prefixed key here;
# `_parse_telemetry` picks between them afterwards, the walker does not.
_OCEAN2_FIELD_MAP: dict[str, tuple[str, str, float]] = {
    # Block 65 - system summary. 65.6/65.7 look like grid readings and are
    # not: 65.7 is the configured feed-in limit (constant 10000 on a unit
    # capped at 10 kW, constant 0 on a zero-export unit), indistinguishable
    # from a real meter at rest for a long time. 65.4 is read as a fallback
    # for solar - the flow blocks below win when present.
    "65.4": ("_summary_solar_w", _TYPE_FLOAT, 1),
    "65.15": ("batt_remaining_wh", _TYPE_FLOAT, 1),
    "65.17": ("soc_pct", _TYPE_FLOAT, 1),
    "65.20": ("_summary_batt_w_signed", _TYPE_FLOAT, 1),
    # Blocks 7 and 87 - the energy flow summary as the app shows it: home
    # load, grid power, PV power, battery power (signed), all four sharing
    # one measurement instant and balancing within it. Both can appear in
    # the same frame and then differ slightly - block 7 trails by roughly
    # one measurement cycle and drops fields more often.
    "7.1": ("_flow7_home_w", _TYPE_FLOAT, 1),
    "7.2": ("_flow7_grid_w", _TYPE_FLOAT, 1),
    "7.3": ("_flow7_solar_w", _TYPE_FLOAT, 1),
    "7.4": ("_flow7_batt_w", _TYPE_FLOAT, 1),
    "87.1": ("_flow87_home_w", _TYPE_FLOAT, 1),
    "87.2": ("_flow87_grid_w", _TYPE_FLOAT, 1),
    "87.3": ("_flow87_solar_w", _TYPE_FLOAT, 1),
    "87.4": ("_flow87_batt_w", _TYPE_FLOAT, 1),
    # Block 4 - inverter. 4.13 is the grid meter at its own instant, positive
    # = import. 4.14 (PV strings) is not declared here: it repeats per
    # string, which one flat key cannot hold, so it is read separately below.
    "4.13": ("_inverter_grid_w", _TYPE_FLOAT, 1),
}
_OCEAN2_TREE: dict[int, Any] = _compile(_OCEAN2_FIELD_MAP)
# `_walk` only consults `repeated` for a path whose tree node is a dict, so
# 4.14 needs a (content-free) dict node here purely to make it eligible for
# that check - an undeclared field is otherwise skipped silently, never
# raised, so this is not a workaround for an error path. `_pv_strings` reads
# the collected raw bytes below, not through this tree.
_OCEAN2_TREE[4][14] = {}
_PV_STRINGS_GROUP = "4.14"
_PV_STRINGS_KEY = "_pv_string_blocks"


# Block 5 of the `254/46` frame - one battery module per header, with the
# module number in 5.15.
#
# A frame bundles several of these headers, and the bundle size is not the
# module count: a 98-record capture holds bundles of thirteen and fourteen
# while carrying only two distinct module numbers, with field 37 - a
# timestamp - stepping 3 to 7 seconds between the records inside one bundle.
# It is a backlog of per-module heartbeats, not a snapshot of the pack. The
# entities are still created on report rather than declared for a fixed
# count: the module count is an installation choice either way, and keying
# on the number actually seen is what makes the bundle case work at all.
#
# Three of these were corrected against the running unit on 2026-07-31, and
# each correction was the wrong quantity rather than the wrong scale:
#
#   54  is the energy left in the module, not its capacity. Measured 4114 Wh
#       at 81.5 % and 4137 Wh at 82.0 %, which puts the full pack near
#       5046 Wh. Read as a capacity it falls while the module discharges -
#       harmless on a desk, permanently misleading in service.
#   39  is the state of health, not the state of charge: constant 100.0
#       across a whole measurement while 38 moved. The pair 38/39 mirrors
#       the 2/3 of the older generation. Read as a float; field 3 carries
#       the same number as a varint.
#    6  is a cell voltage in millivolts, not the pack voltage - it follows
#       the load.
#
# The pack voltage does exist, in field 9. It was overlooked because 16.5 V
# reads as implausibly low for a home battery - but the modules are wired 5S:
# 16.46 V over 3.311 V per cell is exactly five cells in series. Confirmed
# through the power balance, field 9 times field 10 hitting field 1 within
# 1 % on both modules independently.
_MODULE_FIELD_MAP: dict[str, tuple[str, str, float]] = {
    "5.15": ("_index", _TYPE_INT, 1),
    "5.1": ("power_w", _TYPE_FLOAT, 1),
    # 39, not 3. Both read a constant 100 on a healthy pack, so the values
    # cannot tell them apart - the wire type can: field 3 arrives as a varint
    # and 39 as a float, checked on raw frames from an RE11. The pair 38/39
    # mirrors the 2/3 of the older generation, 38 being the state of charge
    # that actually moves. Declaring 3 as a float meant the walker rejected
    # it on type and the entity stayed empty; no test caught that, because
    # the tests built field 3 as a float themselves.
    "5.39": ("soh_pct", _TYPE_FLOAT, 1),
    "5.38": ("soc_pct", _TYPE_FLOAT, 1),
    "5.54": ("remaining_wh", _TYPE_FLOAT, 1),
    "5.17": ("cycles", _TYPE_INT, 1),
    "5.9": ("voltage_v", _TYPE_FLOAT, 1),
    "5.10": ("current_a", _TYPE_FLOAT, 1),
    # Millivolts, like the PowerOcean cell-voltage entities - 3437 on a real
    # frame. Published as mV rather than scaled to volts: 5 x 3.437 V is
    # 17.19 V against 17.11 V in field 9, which is the 5S wiring and the
    # cross-check that the field is a cell rather than the pack.
    "5.6": ("cell_voltage_mv", _TYPE_FLOAT, 1),
    # Temperatures, separated on 2026-08-01 by a load test rather than by
    # their averages: 45 minutes of wallbox charging, up to 3.6 kW per
    # module. What tells them apart is how they move, not how warm they are.
    # 21, 30 and 31 hold the order 31 <= 21 <= 30 in every frame and rise
    # together and slowly - cell minimum, average and maximum. 23/24/32/33
    # follow the load within a minute, swinging 11 to 17 K at up to 7 K per
    # minute, which is power electronics rather than cells.
    "5.21": ("cell_temp_c", _TYPE_FLOAT, 1),
    "5.30": ("cell_temp_max_c", _TYPE_FLOAT, 1),
    "5.31": ("cell_temp_min_c", _TYPE_FLOAT, 1),
    "5.23": ("_mos_a_c", _TYPE_FLOAT, 1),
    "5.24": ("_mos_b_c", _TYPE_FLOAT, 1),
    "5.32": ("_mos_c_c", _TYPE_FLOAT, 1),
    "5.33": ("_mos_d_c", _TYPE_FLOAT, 1),
}
_MODULE_TREE: dict[int, Any] = _compile(_MODULE_FIELD_MAP)

#: Highest module number that gets entity definitions. The reporter's unit
#: has two, recordings from other installations carry fourteen; sixteen
#: leaves headroom without costing anything, because every module entity is
#: created on report rather than up front.
MAX_MODULES = 16

#: The four power-electronics readings are published as one maximum. They sit
#: on different parts of the same board and the hottest is what matters; four
#: near-identical entities per module would be noise at fourteen modules.
_MOS_KEYS = ("_mos_a_c", "_mos_b_c", "_mos_c_c", "_mos_d_c")


def _parse_module(pdata: bytes) -> dict[str, Any]:
    """Decode one `cmd_id 46` payload into keys for a single module.

    Returns an empty dict when the payload carries no usable module number:
    without it there is nothing to attach the readings to, and guessing a
    position would put one module's cell voltages under another's name.
    """
    walked: dict[str, Any] = {}
    _walk(pdata, _MODULE_TREE, walked, set())

    # Same guard `_parse_telemetry` applies to its own readings: a NaN or an
    # infinity reaching a sensor raises inside Home Assistant's rounding and
    # aborts the rest of that update, and `_walk` itself has no notion of this.
    for key in [
        key
        for key, value in walked.items()
        if isinstance(value, float) and not isfinite(value)
    ]:
        del walked[key]

    index = walked.pop("_index", None)
    if (
        not isinstance(index, (int, float))
        or not isfinite(index)
        or not 1 <= int(index) <= MAX_MODULES
    ):
        return {}

    mos = [walked.pop(key) for key in _MOS_KEYS if key in walked]
    if mos:
        walked["mos_temp_c"] = max(mos)

    prefix = f"module{int(index)}_"
    return {prefix + key: value for key, value in walked.items()}


def _pick(values: dict[str, Any], *keys: str) -> float | None:
    """Return the first present value among `keys`, in priority order."""
    for key in keys:
        if key in values:
            return values[key]  # type: ignore[no-any-return]
    return None


def _pv_strings(blocks: list[Any]) -> dict[str, float]:
    """Decode the raw `4.14` occurrences collected by the walker.

    Each occurrence holds one repeated field 1 per PV string, string number
    in its own field 1 and power in field 4. One flat result can only hold
    the last occurrence of a declared path, which is exactly what a repeated
    group is for - see `_walk`'s docstring - so this reads the raw bytes
    directly with `_iter_fields` rather than through the tree.
    """
    out: dict[str, float] = {}
    for block in blocks:
        if not isinstance(block, bytes):
            continue
        for field_num, wire_type, raw in _iter_fields(block):
            if field_num != 1 or wire_type != 2:
                continue
            index: float | None = None
            power: float | None = None
            for sub_num, sub_wire, sub_raw in _iter_fields(raw):
                if sub_num == 1:
                    index = _decode_scalar(sub_wire, sub_raw, _TYPE_FLOAT)
                elif sub_num == 4:
                    power = _decode_scalar(sub_wire, sub_raw, _TYPE_FLOAT)
            if (
                index is None
                or power is None
                or not isfinite(index)
                or not isfinite(power)
                or not 1 <= index <= 4
            ):
                continue
            out[f"pv{int(index)}_w"] = power
    return out


def _parse_telemetry(pdata: bytes) -> dict[str, Any]:
    """Decode one cmd_id 39 payload into flat sensor keys.

    `_walk`/`_read_field` raise `ValueError` on a wire type they do not know
    (3/4, the deprecated proto2 group markers), which the caller in
    `parse_ocean2_proto_message` catches around this whole call and drops the
    candidate - the entire frame, not just the field that follows the bad
    tag. Proto3 never emits those wire types, so this only fires on garbage,
    and garbage is safer dropped whole than partially decoded. Accepted:
    `stream_ac5000_proto.parse_stream_ac5000_message` takes the same trade.
    """
    walked: dict[str, Any] = {}
    seen: set[str] = set()
    repeated = {_PV_STRINGS_GROUP: _PV_STRINGS_KEY}
    _walk(pdata, _OCEAN2_TREE, walked, seen, repeated=repeated)

    # A NaN or an infinity reaching a sensor raises inside Home Assistant's
    # rounding and aborts the rest of that update, so it is dropped here -
    # the same place the sibling parsers drop theirs. `_walk` itself has no
    # notion of this; every numeric leaf declared above passes through it.
    for key in [
        key
        for key, value in walked.items()
        if isinstance(value, float) and not isfinite(value)
    ]:
        del walked[key]

    out: dict[str, Any] = {}

    soc = walked.get("soc_pct")
    if soc is not None:
        out["soc_pct"] = soc
    remaining = walked.get("batt_remaining_wh")
    if remaining is not None:
        out["batt_remaining_wh"] = remaining

    # Home load: only the flow blocks carry it, 87 over 7 - block 7 trails
    # block 87 by roughly one measurement cycle and drops fields more often.
    # Observed 2026-07-28: block 7 reported 550 W while block 87 reported
    # 560 W in the same frame, and the app showed 560 W.
    home_w = _pick(walked, "_flow87_home_w", "_flow7_home_w")
    if home_w is not None:
        out["home_w"] = home_w

    # Grid power: the flow blocks read it at the same instant as the other
    # three readings below and win; 4.13 is a finer-grained meter but reads
    # its own instant (up to 6 W apart in the fixture) and is the fallback
    # for a frame that lacks a flow block.
    grid_w = _pick(walked, "_flow87_grid_w", "_flow7_grid_w", "_inverter_grid_w")
    if grid_w is not None:
        out["grid_w"] = grid_w

    # Solar power: same reasoning - the flow blocks win (up to 17 W apart
    # from 65.4 in the fixture), the system summary is the fallback.
    solar_w = _pick(walked, "_flow87_solar_w", "_flow7_solar_w", "_summary_solar_w")
    if solar_w is not None:
        out["solar_w"] = solar_w

    # Battery power: the flow blocks win here too. 65.20, read only as a
    # fallback, is signed with the opposite convention - negative while the
    # pack charges. Measured on an RE11 over 17 consecutive frames during a
    # charge: 65.20 ran -871, -862, -852 W against +880, +830, +850 W in
    # block 87.4, the magnitudes tracking within the usual inter-block lag
    # and the sign inverted. That matches the 13 non-zero frames in the
    # second-installation recording where 65.20 equalled -(87.4) exactly.
    batt_w = _pick(walked, "_flow87_batt_w", "_flow7_batt_w")
    if batt_w is not None:
        out["batt_w"] = batt_w
    elif "_summary_batt_w_signed" in walked:
        out["batt_w"] = -walked["_summary_batt_w_signed"]

    # 4.14 holds one sub-message per PV string, reported only when a string
    # changes, so a frame may carry any subset - including none.
    out.update(_pv_strings(walked.get(_PV_STRINGS_KEY, [])))

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
        cmd_id = header.get("cmd_id")
        if cmd_id not in (_CMD_ID_TELEMETRY, _CMD_ID_MODULE):
            continue
        for pdata in _pdata_candidates(header):
            try:
                decoded = (
                    _parse_telemetry(pdata)
                    if cmd_id == _CMD_ID_TELEMETRY
                    else _parse_module(pdata)
                )
            except (IndexError, ValueError, struct.error):
                # Only a payload that is not valid protobuf falls through to
                # the next candidate; a clean decode ends the attempt whether
                # it produced fields or not.
                continue
            merged.update(decoded)
            break

    return _finalize(merged) if merged else None
