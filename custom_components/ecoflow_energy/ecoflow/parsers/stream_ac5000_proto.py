"""Protobuf telemetry parser for the EcoFlow STREAM AC 5000 (ES22).

Derived from a 1239-frame capture of a live ES22 in app-auth MQTT mode
(2026-08-03) plus the reporter diagnostics on issue #177. Every field is
checked against the frames themselves or against the EcoFlow app, with
one exception that is marked where it stands: `12.8` was never observed
and its position is inferred from the edges around it, so it would create
an entity from a guess on the first unit that sends it. The fixtures come
from a unit with no PV wired to the EcoFlow, so a field being absent from
them is not evidence that it is never sent.

An ES21 capture matches this field layout exactly: the same four
`(cmd_func, cmd_id)` pairs, the same header shape, and the same sub-field
numbers throughout, including the BMS heartbeat and the SoC-limit pair. The
two models differ in whether solar is physically wired to the unit (ES21
has native PV input, ES22 does not), which `solar_w`'s accessory gating
already handles without a model-specific branch. ES21 and ES22 are still
two distinct device types (`DEVICE_TYPE_STREAM_5000` and
`DEVICE_TYPE_STREAM_AC5000` in `ecoflow/const.py`), because "AC 5000"
specifically names the AC-only variant and would be a wrong name for a unit
with DC/PV input - both types are routed to this same
`parse_stream_ac5000_message` entry point, and every caller that
dispatches on the device type has to check for both.

Despite the product name this cannot share `stream_proto.py`: an ES22
sends no `254/21` frame, its telemetry is `254/39` and `254/40`, and it
nests power readings where the BK series uses flat scalars. Only `32/50`
is common to both, and its key names are kept identical here.

Data shape:

- ``f11`` node totals in half-watt units, except `f11.9` (solar) which is
  already watts. `f11.2` and `f11.4`/`f11.6` are unsigned magnitudes and
  are not mapped; the signed sources below are unambiguous.
- ``f12`` flow matrix, one field per source-to-destination edge, watts.
  House consumption equals the sum of the edges into home within 2 W over
  651 frames.
- ``f15`` Tibber Pulse meter, signed total in `.3`, positive on import
  (seen down to -419 W). ``f16`` is the EcoFlow P1 variant with per-phase
  readings and the net in `.16`. A unit reports one or the other.
- Battery power is derived from the ``f12`` edges, positive is charge.
- ``f40.1.9.1`` discharge task setpoint. It followed every value written
  during a control test (600, 500, 0, 200, 700, 900, 300, 600 W), so it
  is a readback and not a static config echo.
- ``f40.1.8.3.3`` charge task setpoint, a readback on the same evidence:
  it carried 691 W and then 598 W within two seconds of each being
  written, on a whole-day window ending at minute 1439, which only this
  integration writes.

Not mapped: ``f50.1.4`` latches at rest (see the field map); ``f38.1`` and
``f44`` repeat pack readings `32/50` already carries, and mapping both
makes the keys flap; ``f38.1.3``/``f44.2`` look like a cycle count but
read 497, 499 and 1311 within minutes; ``f33.9`` sat at 600 throughout and
is not the scheduled charge power, which reads back on ``f40.1.8.3.3``;
``50/2`` thresholds do not track the app limits and ``53/77`` is a
constant.
"""

from __future__ import annotations

import struct
from math import isfinite
from typing import Any

from ..proto.decoder import decode_header_message

_TYPE_INT = "int"
_TYPE_FLOAT = "float"
# A varint carried inside a length-delimited field rather than as a scalar.
# The scheduled-task window is the only field that arrives this way.
_TYPE_PACKED = "packed"
_FLOAT_ZERO_EPS = 1e-6

# f11 node totals arrive in half-watt units.
_HALF_WATT = 0.5

# cmd_func/cmd_id -> dotted field path -> (sensor_key, scalar_type, scale)
#
# A path is followed only where this map declares it, which keeps the
# walker off length-delimited fields that are not submessages: `f23.3`
# holds "Europe/Amsterdam", `f15.2` a meter UUID, `32/50 f33` a packed
# cell-voltage array. Descending blindly into that array invented a field
# `33.405` holding 4.43e-185.
_ES22_FIELD_MAP: dict[tuple[int, int], dict[str, tuple[str, str, float]]] = {
    (254, 39): {
        # --- node totals (half-watt) ---
        "11.1": ("home_w", _TYPE_FLOAT, _HALF_WATT),
        "11.5": ("soc_pct", _TYPE_INT, 1),
        # Watts, not half-watts. Absent on a unit with no PV wired to the
        # EcoFlow, apart from rare frames where it attributes part of a
        # house export to a solar node.
        "11.9": ("solar_w", _TYPE_FLOAT, 1),
        # --- flow matrix edges (watts) ---
        "12.4": ("home_from_batt_w", _TYPE_FLOAT, 1),
        "12.5": ("_batt_to_grid_w", _TYPE_FLOAT, 1),
        "12.6": ("home_from_grid_w", _TYPE_FLOAT, 1),
        "12.7": ("_grid_to_batt_w", _TYPE_FLOAT, 1),
        # Field 8 would be solar to home by position, and it appears in none
        # of the 1239 captured frames. It is not mapped: an inferred position
        # reaching an accessory entity is a wrong reading Home Assistant keeps
        # forever, and the reporter with PV on issue #177 is the one whose
        # capture can settle it. Map it when that capture shows it.
        # Confirmed on PV hardware in issue #177, three readings: 1552 against
        # `f11.4` = 3104 halved, 1482, and 46 in the 12:19 frame where the
        # battery took 47 W out of 2.86 kW of solar. Absent from the fixtures
        # because that unit has no PV. Internal, not published: publishing it
        # would create a solar entity on every frame and defeat the accessory
        # gating. It feeds the battery term in `_finalize`.
        "12.9": ("_solar_to_batt_w", _TYPE_FLOAT, 1),
        "12.10": ("_solar_to_grid_w", _TYPE_FLOAT, 1),
        # --- meter block, Tibber Pulse variant (source id 4) ---
        "15.3": ("_meter_net_w", _TYPE_FLOAT, 1),
        # --- meter block, EcoFlow P1 variant (source id 8) ---
        # From the #177 reporter's diagnostics, where the net equals the sum
        # of the three phases in every frame. Our unit sends this empty.
        "16.4": ("grid_phase_a_active_power_w", _TYPE_FLOAT, 1),
        "16.5": ("grid_phase_b_active_power_w", _TYPE_FLOAT, 1),
        "16.6": ("grid_phase_c_active_power_w", _TYPE_FLOAT, 1),
        "16.7": ("grid_phase_a_voltage_v", _TYPE_FLOAT, 1),
        "16.8": ("grid_phase_b_voltage_v", _TYPE_FLOAT, 1),
        "16.9": ("grid_phase_c_voltage_v", _TYPE_FLOAT, 1),
        "16.10": ("grid_phase_a_current_a", _TYPE_FLOAT, 1),
        "16.11": ("grid_phase_b_current_a", _TYPE_FLOAT, 1),
        "16.12": ("grid_phase_c_current_a", _TYPE_FLOAT, 1),
        "16.15": ("ac_frequency_hz", _TYPE_FLOAT, 1),
        "16.16": ("_meter_net_w", _TYPE_FLOAT, 1),
        # --- configuration readback ---
        # The two power limits the app calls "Max grid-tied output power" and
        # "Max grid input power". `.5` and `.6` hold values that look like
        # these and are not, for different reasons: `.6` behaves like a
        # ceiling, having moved once across four days of captures, when the
        # account limit went from 600 to 2500, and through neither user
        # change. `.5` is simply unexplained, 600 at that ceiling and 800
        # after it rose, matching no setting visible in the app. Both stay
        # unmapped.
        "10.1": ("max_grid_output_power_w", _TYPE_INT, 1),
        "10.2": ("max_grid_input_power_w", _TYPE_INT, 1),
        "25": ("_work_mode_raw", _TYPE_INT, 1),
        # The app's backup socket control, written on config field 19.
        "19.1": ("_backup_socket_enabled_raw", _TYPE_INT, 1),
        "30.1": ("_backup_reserve_enabled_raw", _TYPE_INT, 1),
        "30.2": ("backup_reserve_pct", _TYPE_INT, 1),
        "33.6": ("soc_precise_pct", _TYPE_FLOAT, 1),
        # `f33.7` and `f33.8` are a third copy of the SoC limits, reading 90
        # and 20 in the same get-all that carries them on `32/2` and on `f29`.
        # Deliberately unmapped: `32/2` is the source, and this file has
        # already had the limits taken from the wrong one of three.
        # --- scheduled task readback ---
        # Tasks are written on config field 39 and read back here on `f40`.
        # The grammar is identical on both sides, only the top-level number
        # differs, and it is the one config field that changes number between
        # write and readback.
        # One task per frame, and the device rotates through them, so these
        # land as per-kind keys that merge across frames rather than as a list
        # that each frame would overwrite. `.1` echoes the last operation and
        # `.4` flips every few seconds with no change to the task, so neither
        # is mapped.
        "40.1.2": ("_task_kind_raw", _TYPE_INT, 1),
        "40.1.3": ("_task_enabled_raw", _TYPE_INT, 1),
        "40.1.7": ("_task_window_raw", _TYPE_PACKED, 1),
        # `.2` is the task's target SoC, shown in the app as "Charge limit".
        # A power write has to carry it back unchanged or it would reset the
        # task to charge to 100%.
        "40.1.8.3.2": ("_task_charge_soc_target", _TYPE_INT, 1),
        "40.1.8.3.3": ("_task_charge_power_w", _TYPE_INT, 1),
        "40.1.9.1": ("_task_discharge_power_w", _TYPE_INT, 1),
        # `f50.1.4` (signed battery power) is not mapped: the whole `f50` block
        # stops being sent when the unit idles, so it latches at its last
        # active value. `_finalize` derives the battery term from the edges.
        #
        # `f29.1`/`f29.2` (the SoC limits) are not mapped either: this block
        # rides only in the full-state frame, while `32/2` below carries the
        # same pair in every frame it sends.
    },
    # `254/40 f22` looks like a pair of power limits in milliwatts (600000 and
    # 1200000) and is deliberately not mapped: both stayed exactly there while
    # the account limit was raised to 2500 W, while the output limit was set
    # to 2400 W, and while the discharge task ran at 1400 W. They are neither
    # the limits nor the task powers, so their meaning is unknown.
    #
    # `32/2 f1.7` and `f1.21` carry the same field numbers as the Delta 3 CMS
    # heartbeat, where that pair stays unmapped because its meaning was never
    # seen away from the default. On an ES22 both follow the app, so here they
    # are the SoC limits.
    (32, 2): {
        "1.7": ("max_charge_soc_pct", _TYPE_INT, 1),
        "1.21": ("min_discharge_soc_pct", _TYPE_INT, 1),
    },
    # Field numbers and key names match the (32, 50) block in
    # `stream_proto.py`: it is the same BMS heartbeat.
    (32, 50): {
        "7": ("_batt_voltage_mv", _TYPE_INT, 1),
        "8": ("_bms_current_ma", _TYPE_INT, 1),
        "9": ("batt_temp_c", _TYPE_INT, 1),
        "11": ("batt_design_cap_mah", _TYPE_INT, 1),
        "12": ("batt_remain_cap_mah", _TYPE_INT, 1),
        "13": ("batt_full_cap_mah", _TYPE_INT, 1),
        "15": ("bms_soh_pct", _TYPE_INT, 1),
        "16": ("batt_max_cell_vol_mv", _TYPE_INT, 1),
        "17": ("batt_min_cell_vol_mv", _TYPE_INT, 1),
        "18": ("batt_max_cell_temp_c", _TYPE_INT, 1),
        "19": ("batt_min_cell_temp_c", _TYPE_INT, 1),
        "20": ("batt_max_mos_temp_c", _TYPE_INT, 1),
        # Not soc_precise_pct: this is the BMS pack SoC and runs about two
        # points above the system SoC the app shows (20.93 vs 18.63 in the
        # same second).
        "25": ("bms_soc_precise_pct", _TYPE_FLOAT, 1),
    },
}

# Paths whose absence inside a present `f12` decodes as zero.
#
# This is reading proto3, not inventing a value: an omitted scalar in a
# message that was sent is zero by definition, and none of 1024 observed
# `f12` subfields was ever an explicit zero. Without it an idle battery edge
# would keep reporting its last power forever. It applies only when the group
# is present; an absent `f12`, which is 116 of 355 pushes, means unchanged.
#
# `11.9` is filled for the same reason and needs one more step. It is the
# solar node total, and holding it out of the fill was how the solar entity
# was kept off units without PV. But the fill is also the only thing that
# turns an omitted scalar into a real zero, and nothing deletes a key that
# stops arriving, so on a unit with PV the reading held its last daylight
# value all night. It is filled here, and `solar_w` carries
# `accessory_needs_nonzero` in const.py so the entity still waits for a
# reading that is actually solar.
_ZERO_FILL_PATHS: dict[tuple[int, int], tuple[str, ...]] = {
    # `40.1.3` is the task's enabled flag, and disabling a task in the app made
    # it disappear rather than read 0, so its absence inside a present task is
    # what "disabled" looks like on the wire.
    (254, 39): ("11.9", "12.4", "12.5", "12.6", "12.7", "40.1.3"),
}

# (parent group, the child that carries its content, marker key) per command.
# A declared group arriving with no bytes is the device stating the collection
# is empty, which a group that never arrives does not: most 254/39 frames are
# deltas that carry no `f40` at all even while tasks exist (20 task blocks in
# 748 payloads across the ramp capture). Only the empty container may clear a
# readback; keying off absence would flap on nearly every frame.
_EMPTY_GROUP_CLEARS: dict[tuple[int, int], tuple[tuple[str, str, str], ...]] = {
    (254, 39): (("40", "40.1", "_task_list_empty"),),
}

# Work mode as written and read back on config field 25. Values outside this
# table decode to None so an unknown mode can never reach an enum sensor as a
# raw integer.
_WORK_MODE = {
    0: "self_powered",
    1: "intelligent_plus",
    2: "custom",
}


def _compile(field_map: dict[str, tuple[str, str, float]]) -> dict[int, Any]:
    """Turn dotted paths into a nested lookup tree keyed by field number."""
    root: dict[int, Any] = {}
    for path, mapping in field_map.items():
        parts = [int(part) for part in path.split(".")]
        node = root
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = mapping
    return root


_ES22_TREE: dict[tuple[int, int], dict[int, Any]] = {
    cmd: _compile(field_map) for cmd, field_map in _ES22_FIELD_MAP.items()
}

# group path -> (sensor key, zero value) once that group is seen. The zero
# keeps the field's own type: an int field filled with 0.0 would read as a
# float everywhere downstream.
_ZERO_FILL_KEYS: dict[tuple[int, int], dict[str, tuple[tuple[str, Any], ...]]] = {
    cmd: {
        group: tuple(
            (
                _ES22_FIELD_MAP[cmd][path][0],
                0 if _ES22_FIELD_MAP[cmd][path][1] == _TYPE_INT else 0.0,
            )
            for path in paths
            if path.rsplit(".", 1)[0] == group
        )
        for group in {path.rsplit(".", 1)[0] for path in paths}
    }
    for cmd, paths in _ZERO_FILL_PATHS.items()
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
    """Decode one scalar field value, or None if the wire type cannot hold one."""
    if wire_type == 0:
        value = 0
        shift = 0
        for byte in raw:
            value |= (byte & 0x7F) << shift
            shift += 7
        # Protobuf stores a negative varint as its 64-bit two's complement
        # (e.g. the signed BMS current below zero).
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

    if wire_type == 2 and scalar_type == _TYPE_PACKED:
        if not raw:
            return None
        try:
            value, pos = _read_varint(memoryview(raw), 0)
        except (IndexError, ValueError):
            return None
        return value if pos == len(raw) else None

    return None


def _read_field(mv: memoryview, pos: int, wire_type: int) -> tuple[bytes, int]:
    """Return the raw bytes of one field and the position just past it."""
    if wire_type == 0:
        start = pos
        _, pos = _read_varint(mv, pos)
        return mv[start:pos].tobytes(), pos
    if wire_type == 1:
        return mv[pos:pos + 8].tobytes(), pos + 8
    if wire_type == 2:
        length, pos = _read_varint(mv, pos)
        return mv[pos:pos + length].tobytes(), pos + length
    if wire_type == 5:
        return mv[pos:pos + 4].tobytes(), pos + 4
    raise ValueError(f"unsupported wire type {wire_type}")


def _walk(
    payload: bytes,
    node: dict[int, Any],
    result: dict[str, Any],
    seen_groups: set[str],
    prefix: str = "",
) -> None:
    """Decode the declared paths of ``payload`` into ``result``.

    Undeclared fields are skipped by length and never entered, so a string
    or a packed array cannot be mistaken for a submessage.
    """
    mv = memoryview(payload)
    pos = 0
    while pos < len(mv):
        tag, pos = _read_varint(mv, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        raw, pos = _read_field(mv, pos, wire_type)

        child = node.get(field_num)
        if child is None:
            continue

        if isinstance(child, dict):
            # Declared as a group but arriving as a scalar: a layout
            # difference, not something to guess at.
            if wire_type != 2:
                continue
            seen_groups.add(f"{prefix}{field_num}")
            _walk(raw, child, result, seen_groups, f"{prefix}{field_num}.")
            continue

        sensor_key, scalar_type, scale = child
        value = _decode_scalar(wire_type, raw, scalar_type)
        if value is None:
            continue
        result[sensor_key] = value * scale if scale != 1 else value


# Every key one task readback can produce, in the order the two kinds are
# built above, so an empty task list can clear all of them.
_TASK_KEYS: tuple[str, ...] = (
    "scheduled_charge_power_w",
    "scheduled_charge_soc_target",
    "scheduled_charge_enabled",
    "scheduled_charge_start_min",
    "scheduled_charge_end_min",
    "scheduled_discharge_power_w",
    "scheduled_discharge_enabled",
    "scheduled_discharge_start_min",
    "scheduled_discharge_end_min",
)


def _finalize_task(result: dict[str, Any]) -> None:
    """Turn one task readback into the keys the control side writes back.

    A frame carries a single task and the device cycles through them, so each
    kind gets its own keys and the coordinator's own merge keeps the other
    kind's values. The start, end and enabled keys have no entity: they exist
    so a power write can rebuild the task it is changing instead of inventing
    a window.

    That merge is also why an empty task list has to clear every key at once:
    deleting the last task in the app stops the readback rather than zeroing
    it, so the setpoint entities would otherwise keep reporting a task the
    device no longer has.
    """
    task_list_empty = result.pop("_task_list_empty", False)
    kind_raw = result.pop("_task_kind_raw", None)
    enabled_raw = result.pop("_task_enabled_raw", None)
    window_raw = result.pop("_task_window_raw", None)
    charge_power = result.pop("_task_charge_power_w", None)
    charge_soc_target = result.pop("_task_charge_soc_target", None)
    discharge_power = result.pop("_task_discharge_power_w", None)

    kind = {1: "charge", 2: "discharge"}.get(kind_raw) if isinstance(kind_raw, int) else None
    if kind is None:
        if task_list_empty:
            # None is an explicit clear: both platforms show it as unknown and
            # stop falling back to the value they restored at startup.
            for key in _TASK_KEYS:
                result[key] = None
        return

    power = charge_power if kind == "charge" else discharge_power
    if isinstance(power, int):
        result[f"scheduled_{kind}_power_w"] = power
    if kind == "charge" and isinstance(charge_soc_target, int):
        result["scheduled_charge_soc_target"] = charge_soc_target
    if isinstance(enabled_raw, int):
        result[f"scheduled_{kind}_enabled"] = bool(enabled_raw)
    if isinstance(window_raw, int):
        # One varint: start in the low 16 bits, end in the high 16, both
        # minutes since midnight.
        result[f"scheduled_{kind}_start_min"] = window_raw & 0xFFFF
        result[f"scheduled_{kind}_end_min"] = (window_raw >> 16) & 0xFFFF


def _finalize(parsed: dict[str, Any]) -> dict[str, Any]:
    """Normalize units and derive the convenience values entities read."""
    result = dict(parsed)

    for key, value in list(result.items()):
        if isinstance(value, float) and isfinite(value) and abs(value) < _FLOAT_ZERO_EPS:
            result[key] = 0.0

    work_mode_raw = result.pop("_work_mode_raw", None)
    if isinstance(work_mode_raw, int):
        result["work_mode"] = _WORK_MODE.get(work_mode_raw)

    backup_raw = result.pop("_backup_reserve_enabled_raw", None)
    if isinstance(backup_raw, int):
        result["backup_reserve_enabled"] = bool(backup_raw)

    socket_raw = result.pop("_backup_socket_enabled_raw", None)
    if isinstance(socket_raw, int):
        result["backup_socket_enabled"] = bool(socket_raw)

    batt_voltage_mv = result.pop("_batt_voltage_mv", None)
    if isinstance(batt_voltage_mv, (int, float)):
        result["batt_voltage_v"] = float(batt_voltage_mv) / 1000.0

    bms_current_ma = result.pop("_bms_current_ma", None)
    if isinstance(bms_current_ma, (int, float)):
        result["bms_current_a"] = float(bms_current_ma) / 1000.0

    # The meter block is the only signed grid reading this device sends, so
    # a unit with no meter linked reports no grid value at all.
    meter_net = result.pop("_meter_net_w", None)
    if isinstance(meter_net, (int, float)):
        result["grid_w"] = float(meter_net)

    # Import and export come from the flow edges, not the meter, so both are
    # structurally non-negative as the Energy Dashboard needs. Zero-fill puts
    # all four grid edges in together, so one of them can stand in for "the
    # group was seen".
    batt_to_grid = result.pop("_batt_to_grid_w", None)
    grid_to_batt = result.pop("_grid_to_batt_w", None)
    solar_to_grid = result.pop("_solar_to_grid_w", None)
    solar_to_batt = result.pop("_solar_to_batt_w", None)
    home_from_grid = result.get("home_from_grid_w")
    home_from_batt = result.get("home_from_batt_w")

    if isinstance(grid_to_batt, (int, float)) and isinstance(home_from_grid, (int, float)):
        result["grid_import_power_w"] = float(home_from_grid) + float(grid_to_batt)
    if isinstance(batt_to_grid, (int, float)):
        # A solar-to-grid edge only exists on a unit with PV attached; its
        # absence here means no such contribution, not an unknown one.
        extra = float(solar_to_grid) if isinstance(solar_to_grid, (int, float)) else 0.0
        result["grid_export_power_w"] = float(batt_to_grid) + extra

    # Signed battery power, positive is charge. All three zero-filled edges
    # being numbers means this frame carried `f12`; an absent group leaves
    # `batt_w` out so the coordinator keeps the last value.
    if all(isinstance(v, (int, float)) for v in (home_from_batt, batt_to_grid, grid_to_batt)):
        # `f12.9` is the solar-to-battery edge and belongs in this sum. It is
        # absent from the fixtures only because the unit they came from has no
        # PV wired to the EcoFlow; on issue #177 it is confirmed three times on
        # a unit that has: 1552 against `f11.4` = 3104 halved, 1482, and the
        # 12:19 frame reading 46 while the battery took 47 W out of 2.86 kW of
        # solar, where the opposite assignment would have required 2860.
        # Leaving it out costs a PV owner the whole solar charge: battery power
        # would read zero while the pack fills.
        into = float(grid_to_batt)
        if isinstance(solar_to_batt, (int, float)):
            into += float(solar_to_batt)
        result["batt_w"] = into - (float(home_from_batt) + float(batt_to_grid))

    _finalize_task(result)

    batt_w = result.get("batt_w")
    if isinstance(batt_w, (int, float)):
        result["batt_charge_power_w"] = float(batt_w) if batt_w > 0 else 0.0
        result["batt_discharge_power_w"] = abs(float(batt_w)) if batt_w < 0 else 0.0
        # batt_charge_discharge_state is intentionally NOT set here: the
        # coordinator pops any parser-provided value and derives the state
        # from a hysteresis window over batt_w.

    return result


def _pdata_candidates(header: dict[str, Any]) -> list[bytes]:
    """Return the payload bytes to try for one header, most likely first.

    No ES22 frame in any capture set ``enc_type``, so the masked branch is
    carried over from `stream_proto.py` unexercised on real hardware. It
    costs nothing to keep and covers a firmware that starts setting it.
    """
    pdata_hex = header.get("pdata")
    if not isinstance(pdata_hex, str) or not pdata_hex:
        return []
    try:
        pdata = bytes.fromhex(pdata_hex)
    except ValueError:
        return []
    if not pdata:
        return []

    if header.get("enc_type") != 1:
        return [pdata]

    seq = header.get("seq")
    if not isinstance(seq, int) or not seq & 0xFF:
        return [pdata]

    xor_key = seq & 0xFF
    return [bytes(value ^ xor_key for value in pdata), pdata]


def parse_stream_ac5000_message(payload: bytes) -> dict[str, Any] | None:
    """Parse a STREAM AC 5000 protobuf frame into flat sensor keys."""
    try:
        headers, _ = decode_header_message(payload)
        if not headers:
            return None

        merged: dict[str, Any] = {}
        for header in headers:
            cmd_key = (int(header.get("cmd_func", -1)), int(header.get("cmd_id", -1)))
            tree = _ES22_TREE.get(cmd_key)
            if tree is None:
                continue
            for pdata in _pdata_candidates(header):
                decoded: dict[str, Any] = {}
                seen_groups: set[str] = set()
                try:
                    _walk(pdata, tree, decoded, seen_groups)
                except (IndexError, ValueError):
                    # Only a payload that is not valid protobuf falls through
                    # to the next candidate. A clean decode ends the attempt,
                    # empty or not.
                    continue
                for group, defaults in _ZERO_FILL_KEYS.get(cmd_key, {}).items():
                    if group not in seen_groups:
                        continue
                    for key, zero in defaults:
                        decoded.setdefault(key, zero)
                for parent, child, marker in _EMPTY_GROUP_CLEARS.get(cmd_key, ()):
                    if parent in seen_groups and child not in seen_groups:
                        decoded[marker] = True
                merged.update(decoded)
                break
            # A decode error is contained to the message that caused it: the
            # remaining headers of a bundle still contribute.
    except Exception:
        return None
    if not merged:
        return None

    finalized = _finalize(merged)
    return finalized or None
