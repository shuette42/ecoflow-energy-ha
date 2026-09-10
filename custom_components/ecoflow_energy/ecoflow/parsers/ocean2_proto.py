"""Protobuf telemetry parser for the EcoFlow Ocean 2 (RE11/RE17) home battery.

Derived from a reporter's diagnostics download of a live RE11 (PLAN-135,
issue #145): 69 frames, 06:46 to 13:49 UTC. No RE11 header carries `enc_type`
(verified over all 69 frames, 232 headers), so every payload is plaintext
protobuf and no XOR unmask key is ever needed - `_pdata_candidates` is still
used for both message types below so a header that ever does set `enc_type`
would not silently corrupt a reading (the same defence `powerpulse_proto.py`
applies for the same reason).

Two message types carry readings, both under cmd_func 254:

- `39` display upload. Top-level fields are nested messages: block `65`
  (system: solar, battery, SoC), blocks `7` and `87` (two snapshots of the
  same four quantities - home, grid, solar, battery power - both quantised
  to 10 W by the device; `7` is always present when either is, `87`
  sometimes, and wins field-by-field when both arrive), and block `4`
  (inverter: AC output, three-phase records, PV strings, grid-port phases).
  A full upload (52 top-level fields) arrives only as a `get_reply`, in a
  5-header bundle (`254/40, 53/113, 254/44, 254/39, 254/46`); a `property`
  push is an incremental carrying a subset of blocks, sometimes only
  `7.1`/`7.4`. This parser returns only the keys the message actually
  carries; the coordinator merges across messages.
- `46` battery module report. **Only when its payload uses field `5`** (one
  module record per header, see below) - the `254/46` header embedded in a
  `get_reply` bundle uses a completely different, much richer payload under
  fields `11` and `20` instead (dozens of unmapped fields, no field `5` at
  all). That richer form is out of scope for this parser and is not read:
  every `get_reply` bundle in the capture therefore contributes system and
  inverter data but never pack data, and every pack reading in the capture
  comes from a `property`-topic module-only bundle. A bundle can carry up to
  14 separate `254/46` *headers* that are a backlog of heartbeats from the
  same two modules, distinguished by field `5.37` (a unix timestamp): key
  modules on `5.15` (module index, 1..N) and keep only the record with the
  highest `5.37` per module - never count records as modules, or 13-14
  headers would be read as 13-14 modules where the device has two. `5.16`
  (module serial) is the sanitizer's `XXXXXXXXXXXXXXXX` placeholder in every
  captured record; it is never keyed on and never published.

Everything else (`254/40`, `254/42`, `254/44`, `53/113`, `96/53`, `2/245`)
returns nothing from this parser.

Wire types: every power, voltage, current and temperature value is a
fixed32 IEEE-754 float. The only varints among the mapped paths are `65.15`,
`65.17`, the module-index and phase-index subfields, and module fields `15`
and `17`. No mapped path carries a deci-unit scale; the one path not in
plain units is module field `6` (max cell voltage), which is millivolts on
the wire and is published as-is (`pack{n}_cell_max_vol_mv`, matching the
PowerOcean key of the same name) - dividing it by anything would turn a
LiFePO4 cell voltage into nonsense.

Not mapped, on purpose:

- `65.7` - a setting (the feed-in ceiling), not a reading (ADR-028
  decision 5).
- `65.18` - byte-identical duplicate of `65.15` in every captured frame.
- module field `3` - a varint duplicate of field `39` (SoH).
- module field `15` - used only to key a record to a module, never
  published as its own sensor.
- module field `16` - the serial placeholder; see above.
- module field `37` - used only to pick the newest record per module.

Battery power sign - a correction against the field map as it was first
read from the capture, which had "`65.20` positive = charging, `87.4`
positive = discharging", inherited from the PowerOcean convention without
being checked against this device's own data. Checked here against three
independent signals in the fixture, all of which agree with each other and
disagree with that reading:

1. **Remaining energy (`65.15`, Wh).** Frame 28->34 (11:54:41->12:22:37,
   28 min): 10047 -> 7934 Wh, a clear loss of stored energy (discharging).
   `65.20` is +4283/+4287 W in that window. Seven seconds later, frame
   35->36 (12:22:40->12:22:47): remaining energy rises by 9 Wh in the same
   breath that `65.20` flips to -4999 W. Positive `65.20` tracks energy
   *leaving* the battery in every one of 14 checked samples spanning the
   whole capture.
2. **SoC (`65.17`, %).** Same frames: 99% -> 78% while `65.20` is positive,
   78% -> 81% while `65.20` goes negative, 81% -> 56% while `65.20` is
   positive again (frames 40/46/48/50/54), 56% -> 58% while `65.20` is
   negative again (frames 55/57/59/61). No exception in the capture.
3. **The load balance itself.** `home ~= solar + grid_import + raw(65.20)`
   closes to within 1-5% across all ten full-upload frames with `65.20`
   read as-is and ADDED (frame 46: 6687 + (-8.16) + 4328 = 11006.8 against
   an actual home of 11000, 6.8 W off). Reading `65.20` as charge-positive
   and subtracting it, as that first reading would, misses by up to
   8650 W on the same frames (frame 46: 6687 - 4328 - 8.16 = 2350.8 against
   11000, off by 8649 W) - not a rounding difference, a wrong sign.

So `65.20` is **discharge-positive** on the wire (opposite of the PowerOcean
field it shares a number with), and since `87.4`/`7.4` are proven opposite in
sign to `65.20` in every frame carrying both, they are **charge-positive** -
also opposite of the first reading's label, but consistent with its
"opposite signs" measurement, which was correct.

The desired output convention (`batt_w > 0` is charging, matching
`parsers/powerocean_proto.py`) is therefore reached by using `87.4`/`7.4`
as-is and negating `65.20` where it is the only source. Proof frames: 32 vs
34/35/36 and 46 vs 40 above; the module-level check below confirms it a
second, independent way.

Precedence between the blocks - the snapshot wins, coherence over
resolution. Blocks 7 and 87 carry home, grid, solar and battery power as one
snapshot, quantised to 10 W; block 65 and block 4 carry solar (`65.4`),
battery (`65.20`) and grid (`4.13`) to full float resolution but from their
own instants. Measured over the ten full-upload frames, the load balance
`home = solar + grid + battery` closes with a mean residual of 29 W when
every term comes from block 87 and of 55 W when grid comes from `4.13`
(capture 35: 17 W against 305 W). A dashboard adds these four up, so the
four are published from one snapshot: block 87 first, block 7 where 87 is
absent, and the block 65 / block 4 figures only where neither snapshot block
carries the reading (a small incremental with block 65 alone). The 10 W
quantisation is below the display precision of every entity built on them.

Module power sign (`5.1`) - checked directly within single bundles, no
cross-frame timing assumption needed. Capture 32 (12:12:02), module 1's six
consecutive heartbeats (three seconds apart) show `5.1` steady around
-2250 W while `5.54` (that module's own remaining energy) falls
monotonically, 4423.8 -> 4386.4 Wh: negative raw power while the module's
own stored energy drops, i.e. **discharging**. Capture 65 (13:49:38),
module 1's six heartbeats show `5.1` positive (2500 W falling to 1362 W)
while `5.54` rises monotonically, 3363.7 -> 3386.3 Wh: positive raw power
while stored energy rises, i.e. **charging**. So `5.1` is already
charge-positive on the wire and needs no negation to match the `batt_w`
convention - unlike `65.20`, which shares the same physical quantity at the
system level but the opposite sign.

Grid sign (`4.13`, `87.2`, `7.2`) - confirmed import-positive as-is (no
flip), via the same load-balance check. Frame 28 is a frame with a clear
grid reading (-200.75 W on `4.13`, -170 W on `87.2`) and no battery flow, so
it separates the two hypotheses cleanly: reading it as import-positive gives
a balance error of +35.75 W (1635 + (-200.75) + 0 = 1434.25 against a home
of 1470); reading it as export-positive (negating it) gives +365.75 W (25%
off). Every other full-upload frame agrees with the import-positive reading
to within a few percent.

Block 4.4 - the grid-port phase records - publishes only the phase voltage
and the shared frequency. Their subfield 2 sits at 0.84 to 0.90 A and
subfield 4 within 6 W of zero in every frame on file while the grid power
swings from -374 W to +42 W and the inverter phases carry up to 3.5 kW, so
whatever those two are, they are not the grid current and the grid power,
and they stay unmapped until a frame says what they measure.

Module fields 6, 21 and 54 (cell voltage, temperature, remaining energy)
are floats on the wire and are published as whole numbers: the entities
built on them display without decimals, and a millivolt, a degree and a
watt hour are the resolution the vendor app shows.
"""

from __future__ import annotations

from math import isfinite
from typing import Any

from ..proto.decoder import decode_header_message
from .stream_proto import (
    _FLOAT_ZERO_EPS,
    _TYPE_FLOAT,
    _TYPE_INT,
    _decode_scalar,
    _iter_fields,
    _pdata_candidates,
)

_DISPLAY_CMD = (254, 39)
_MODULE_CMD = (254, 46)

# Two modules exist on the reference hardware; the entity layer defines six
# packs (const.py, OCEAN2_SENSORS), so a record beyond that has no entity
# and is dropped here rather than published under a key nothing reads.
_MAX_MODULES = 6

# Block 65 (system), nested under top-level field 65. 65.7 (feed-in ceiling)
# and 65.18 (duplicate of 65.15) are deliberately absent - see module
# docstring. 65.20 is decoded under its own raw key and negated separately
# (see _decode_display_message), so it is not in this map.
_SYSTEM_FIELD_MAP: dict[int, tuple[str, str]] = {
    4: ("solar_w", _TYPE_FLOAT),
    15: ("bp_remain_watth", _TYPE_INT),
    17: ("soc_pct", _TYPE_INT),
}
_SYSTEM_BATT_RAW_FIELD = 20

# Blocks 7 and 87 share this layout: two snapshots of home/grid/solar/
# battery power, both quantised to 10 W. No sign flips here - the module
# docstring's correction established that these two raw fields are already
# charge-positive / import-positive, matching the output convention as-is.
_SNAPSHOT_FIELD_MAP: dict[int, tuple[str, str]] = {
    1: ("home_w", _TYPE_FLOAT),
    2: ("grid_w", _TYPE_FLOAT),
    3: ("solar_w", _TYPE_FLOAT),
    4: ("batt_w", _TYPE_FLOAT),
}

# Block 4 (inverter) top-level scalars.
_INVERTER_FIELD_MAP: dict[int, tuple[str, str]] = {
    1: ("pcs_ac_power_w", _TYPE_FLOAT),
    13: ("grid_w", _TYPE_FLOAT),
}

_PHASE_INDEX_LABELS: dict[int, str] = {1: "a", 2: "b", 3: "c"}
_PV_STRING_INDEX_LABELS: dict[int, str] = {1: "1", 2: "2", 3: "3"}

# Inverter phase records live at block4.3, one repeated field 1 per phase.
_INV_PHASE_CONTAINER_FIELD = 3
_INV_PHASE_RECORD_FIELD = 1
_INV_PHASE_INDEX_FIELD = 6
_INV_PHASE_VALUE_FIELDS: dict[int, tuple[str, str]] = {
    1: ("voltage_v", _TYPE_FLOAT),
    2: ("current_a", _TYPE_FLOAT),
    3: ("active_power_w", _TYPE_FLOAT),
    4: ("reactive_power_var", _TYPE_FLOAT),
    5: ("apparent_power_va", _TYPE_FLOAT),
}

# Grid-port phase records live at block4.4, one repeated field 1 per phase.
# Subfield 3 (frequency) is the same across every record and is not phase
# indexed - it becomes pcs_ac_freq_hz, read once from whichever record has
# it first. Subfields 2 and 4 are deliberately absent: see the module
# docstring, they do not track the grid current or power.
_GRID_PHASE_CONTAINER_FIELD = 4
_GRID_PHASE_RECORD_FIELD = 1
_GRID_PHASE_INDEX_FIELD = 5
_GRID_PHASE_FREQ_FIELD = 3
_GRID_PHASE_VALUE_FIELDS: dict[int, tuple[str, str]] = {
    1: ("voltage_v", _TYPE_FLOAT),
}

# PV string records live at block4.14, one repeated field 1 per string.
_PV_STRING_CONTAINER_FIELD = 14
_PV_STRING_RECORD_FIELD = 1
_PV_STRING_INDEX_FIELD = 1
_PV_STRING_VALUE_FIELDS: dict[int, tuple[str, str]] = {
    2: ("voltage_v", _TYPE_FLOAT),
    3: ("current_a", _TYPE_FLOAT),
    4: ("power_w", _TYPE_FLOAT),
}

# Battery module record (254/46, field 5), one record per header. 5.1 is
# already charge-positive on the wire (see module docstring) and needs no
# sign flip, unlike its system-level counterpart 65.20. 5.3 (varint SoH
# copy), 5.15 (module index - used only for keying), 5.16 (serial
# placeholder) and 5.37 (heartbeat timestamp - used only to pick the
# newest record per module) are deliberately absent from this map.
_MODULE_RECORD_FIELD_MAP: dict[int, tuple[str, str]] = {
    1: ("power_w", _TYPE_FLOAT),
    6: ("cell_max_vol_mv", _TYPE_INT),
    17: ("cycles", _TYPE_INT),
    21: ("max_cell_temp_c", _TYPE_INT),
    38: ("soc", _TYPE_FLOAT),
    39: ("soh", _TYPE_FLOAT),
    54: ("remain_watth", _TYPE_INT),
}
_MODULE_INDEX_FIELD = 15
_MODULE_TIMESTAMP_FIELD = 37


def _decode_mapped(
    fields: list[tuple[int, int, bytes]], field_map: dict[int, tuple[str, str]]
) -> dict[str, Any]:
    """Decode every field in ``field_map`` found at one nesting level."""
    result: dict[str, Any] = {}
    for field_num, wire_type, raw in fields:
        mapping = field_map.get(field_num)
        if mapping is None:
            continue
        key, scalar_type = mapping
        value = _decode_scalar(wire_type, raw, scalar_type)
        if value is not None:
            result[key] = value
    return result


def _first_container(fields: list[tuple[int, int, bytes]], num: int) -> bytes | None:
    """Return the raw bytes of the first length-delimited field ``num``."""
    for field_num, wire_type, raw in fields:
        if field_num == num and wire_type == 2:
            return raw
    return None


def _first_scalar(
    fields: list[tuple[int, int, bytes]], num: int, scalar_type: str
) -> float | int | None:
    for field_num, wire_type, raw in fields:
        if field_num == num:
            return _decode_scalar(wire_type, raw, scalar_type)
    return None


def _phase_label(
    record_fields: list[tuple[int, int, bytes]], index_field: int
) -> str | None:
    index = _first_scalar(record_fields, index_field, _TYPE_INT)
    if index is None:
        return None
    return _PHASE_INDEX_LABELS.get(int(index))


def _decode_inv_phases(container_raw: bytes) -> dict[str, Any]:
    """Decode block4.3's repeated inverter phase records."""
    result: dict[str, Any] = {}
    for field_num, wire_type, raw in _iter_fields(container_raw):
        if field_num != _INV_PHASE_RECORD_FIELD or wire_type != 2:
            continue
        record = _iter_fields(raw)
        label = _phase_label(record, _INV_PHASE_INDEX_FIELD)
        if label is None:
            continue
        for sub_num, sub_wire, sub_raw in record:
            mapping = _INV_PHASE_VALUE_FIELDS.get(sub_num)
            if mapping is None:
                continue
            suffix, scalar_type = mapping
            value = _decode_scalar(sub_wire, sub_raw, scalar_type)
            if value is not None:
                result[f"inv_phase_{label}_{suffix}"] = value
    return result


def _decode_grid_phases(container_raw: bytes) -> dict[str, Any]:
    """Decode block4.4's repeated grid-port phase records, plus the shared
    grid frequency subfield that rides along on every record."""
    result: dict[str, Any] = {}
    freq: float | None = None
    for field_num, wire_type, raw in _iter_fields(container_raw):
        if field_num != _GRID_PHASE_RECORD_FIELD or wire_type != 2:
            continue
        record = _iter_fields(raw)
        if freq is None:
            freq = _first_scalar(record, _GRID_PHASE_FREQ_FIELD, _TYPE_FLOAT)
        label = _phase_label(record, _GRID_PHASE_INDEX_FIELD)
        if label is None:
            continue
        for sub_num, sub_wire, sub_raw in record:
            mapping = _GRID_PHASE_VALUE_FIELDS.get(sub_num)
            if mapping is None:
                continue
            suffix, scalar_type = mapping
            value = _decode_scalar(sub_wire, sub_raw, scalar_type)
            if value is not None:
                result[f"grid_phase_{label}_{suffix}"] = value
    if freq is not None:
        result["pcs_ac_freq_hz"] = freq
    return result


def _decode_pv_strings(container_raw: bytes) -> dict[str, Any]:
    """Decode block4.14's repeated PV string records."""
    result: dict[str, Any] = {}
    for field_num, wire_type, raw in _iter_fields(container_raw):
        if field_num != _PV_STRING_RECORD_FIELD or wire_type != 2:
            continue
        record = _iter_fields(raw)
        index = _first_scalar(record, _PV_STRING_INDEX_FIELD, _TYPE_INT)
        if index is None:
            continue
        label = _PV_STRING_INDEX_LABELS.get(int(index))
        if label is None:
            continue
        for sub_num, sub_wire, sub_raw in record:
            mapping = _PV_STRING_VALUE_FIELDS.get(sub_num)
            if mapping is None:
                continue
            suffix, scalar_type = mapping
            value = _decode_scalar(sub_wire, sub_raw, scalar_type)
            if value is not None:
                result[f"mppt_pv{label}_{suffix}"] = value
    return result


def _decode_inverter_block(raw: bytes) -> dict[str, Any]:
    """Decode block 4: AC output, the unrounded grid_w, phases, PV."""
    result: dict[str, Any] = {}
    fields = _iter_fields(raw)
    result.update(_decode_mapped(fields, _INVERTER_FIELD_MAP))

    for field_num, wire_type, sub_raw in fields:
        if wire_type != 2:
            continue
        if field_num == _INV_PHASE_CONTAINER_FIELD:
            result.update(_decode_inv_phases(sub_raw))
        elif field_num == _GRID_PHASE_CONTAINER_FIELD:
            result.update(_decode_grid_phases(sub_raw))
        elif field_num == _PV_STRING_CONTAINER_FIELD:
            result.update(_decode_pv_strings(sub_raw))

    return result


def _decode_display_message(pdata: bytes) -> dict[str, Any]:
    """Decode one display upload (254/39), full or incremental.

    Precedence, weakest to strongest: block 4 (grid_w from 4.13, plus the
    AC/phase/PV-string keys only it carries), then block 65 (solar_w and
    batt_w from 65.4/65.20, plus soc_pct and bp_remain_watth only it
    carries), then block 7, then block 87 overriding 7 field by field. The
    four power readings therefore come from one snapshot whenever a snapshot
    block is present - see the module docstring on coherence. Precedence is
    applied explicitly here rather than by relying on the device's top-level
    field order, which is not part of the wire contract.
    """
    top = _iter_fields(pdata)
    result: dict[str, Any] = {}

    block7 = _first_container(top, 7)
    block87 = _first_container(top, 87)
    block65 = _first_container(top, 65)
    block4 = _first_container(top, 4)

    if block4 is not None:
        result.update(_decode_inverter_block(block4))

    if block65 is not None:
        system_fields = _iter_fields(block65)
        result.update(_decode_mapped(system_fields, _SYSTEM_FIELD_MAP))
        batt_raw = _first_scalar(system_fields, _SYSTEM_BATT_RAW_FIELD, _TYPE_FLOAT)
        if batt_raw is not None:
            # 65.20 is discharge-positive on the wire, opposite of the
            # charge-positive output convention - see module docstring.
            result["batt_w"] = -batt_raw

    if block7 is not None:
        result.update(_decode_mapped(_iter_fields(block7), _SNAPSHOT_FIELD_MAP))
    if block87 is not None:
        result.update(_decode_mapped(_iter_fields(block87), _SNAPSHOT_FIELD_MAP))

    return result


def _decode_module_header(pdata: bytes) -> tuple[int, int, dict[str, Any]] | None:
    """Decode one 254/46 header's field-5 module record.

    Returns ``None`` when the payload has no field 5 - the shape the
    get_reply bundle's 254/46 header uses instead (module docstring).
    """
    for field_num, wire_type, raw in _iter_fields(pdata):
        if field_num != 5 or wire_type != 2:
            continue
        record_fields = _iter_fields(raw)
        index = _first_scalar(record_fields, _MODULE_INDEX_FIELD, _TYPE_INT)
        if index is None:
            return None
        timestamp = _first_scalar(record_fields, _MODULE_TIMESTAMP_FIELD, _TYPE_INT)
        values = _decode_mapped(record_fields, _MODULE_RECORD_FIELD_MAP)
        return int(index), int(timestamp) if timestamp is not None else -1, values
    return None


def _finalize(parsed: dict[str, Any]) -> dict[str, Any]:
    """Zero out float noise and derive the charge/discharge and grid splits."""
    result = dict(parsed)

    for key, value in list(result.items()):
        if not isinstance(value, float):
            continue
        if not isfinite(value):
            # A NaN or an infinity is not a reading; publishing it would
            # put "unknown" on the entity and a NaN into the recorder.
            del result[key]
            continue
        if abs(value) < _FLOAT_ZERO_EPS:
            result[key] = 0.0

    grid_w = result.get("grid_w")
    if isinstance(grid_w, (int, float)):
        result["grid_import_power_w"] = grid_w if grid_w > 0.0 else 0.0
        result["grid_export_power_w"] = abs(grid_w) if grid_w < 0.0 else 0.0

    batt_w = result.get("batt_w")
    if isinstance(batt_w, (int, float)):
        result["batt_charge_power_w"] = batt_w if batt_w > 0.0 else 0.0
        result["batt_discharge_power_w"] = abs(batt_w) if batt_w < 0.0 else 0.0

    return result


def parse_ocean2_message(payload: bytes) -> dict[str, Any] | None:
    """Parse an Ocean 2 (RE11/RE17) protobuf frame into flat sensor keys."""
    try:
        headers, _ = decode_header_message(payload)
        if not headers:
            return None

        merged: dict[str, Any] = {}
        # module index -> (newest timestamp seen, its decoded record)
        module_latest: dict[int, tuple[int, dict[str, Any]]] = {}

        for header in headers:
            cmd_key = (int(header.get("cmd_func", -1)), int(header.get("cmd_id", -1)))

            if cmd_key == _DISPLAY_CMD:
                for pdata in _pdata_candidates(header):
                    try:
                        decoded = _decode_display_message(pdata)
                    except (IndexError, ValueError):
                        # Only a payload that is not valid protobuf falls
                        # through to the next candidate (see
                        # _pdata_candidates); a clean decode ends the
                        # attempt, empty or not.
                        continue
                    merged.update(decoded)
                    break
            elif cmd_key == _MODULE_CMD:
                for pdata in _pdata_candidates(header):
                    try:
                        parsed = _decode_module_header(pdata)
                    except (IndexError, ValueError):
                        continue
                    if parsed is not None:
                        index, timestamp, record = parsed
                        current = module_latest.get(index)
                        if current is None or timestamp >= current[0]:
                            module_latest[index] = (timestamp, record)
                    break
            # 254/40, 254/42, 254/44, 53/113, 96/53 and 2/245 carry
            # configuration or unrelated data this parser does not read
            # (module docstring).
    except Exception:
        return None

    for module_index, (_, record) in module_latest.items():
        if not 1 <= module_index <= _MAX_MODULES:
            continue
        for suffix, value in record.items():
            merged[f"pack{module_index}_{suffix}"] = value

    if not merged:
        return None

    finalized = _finalize(merged)
    return finalized or None
