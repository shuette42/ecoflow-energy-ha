"""Protobuf telemetry parser for the EcoFlow STREAM AC 5000 (ES22).

Derived from a 1239-frame capture of a live ES22 in app-auth MQTT mode
(2026-08-03) plus the reporter diagnostics on issue #177. Every field is
checked against the frames themselves or against the EcoFlow app, with
one exception that is marked where it stands: `12.8` was never observed
and its position is inferred from the edges around it, so it would create
an entity from a guess on the first unit that sends it. The fixtures come
from a unit with no PV wired to the EcoFlow, so a field being absent from
them is not evidence that it is never sent.

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
- ``f40`` the device's whole scheduled-task list, one ``f40.1`` per task.
  A frame that carries it carries every task, so it repeats, and each
  task is decoded on its own: see `_REPEATED_GROUPS`.
- ``f40.1.9.1`` discharge task setpoint. It followed every value written
  during a control test (600, 500, 0, 200, 700, 900, 300, 600 W), so it
  is a readback and not a static config echo.
- ``f40.1.8.3.3`` charge task setpoint, a readback on the same evidence:
  it carried 691 W and then 598 W within two seconds of each being
  written, on a whole-day window ending at minute 1439, which only this
  integration writes. It is absent when the task is set to 0 W, which is
  a real setpoint on this device rather than an absence: see the task
  zero fill.
- What a task does is read from which of those two containers it carries,
  never from ``f40.1.2``: see `_TASK_BLOCKS`.

Not mapped: ``f50.1.4`` latches at rest (see the field map); ``f38.1`` and
``f44`` repeat pack readings `32/50` already carries, and mapping both
makes the keys flap; ``f38.1.3``/``f44.2`` look like a cycle count but
read 497, 499 and 1311 within minutes; ``f33.9`` sat at 600 throughout and
is not the scheduled charge power, which reads back on ``f40.1.8.3.3``;
``50/2`` thresholds do not track the app limits and ``53/77`` is a
constant.
"""

from __future__ import annotations

import logging
import struct
from math import isfinite
from typing import Any

from ..proto.decoder import decode_header_message

_LOGGER = logging.getLogger(__name__)

_TYPE_INT = "int"
_TYPE_FLOAT = "float"
# A varint carried inside a length-delimited field rather than as a scalar.
# The scheduled-task window is the only field that arrives this way.
_TYPE_PACKED = "packed"
_FLOAT_ZERO_EPS = 1e-6

# The telemetry command that carries the per-unit block below.
_CMD_TELEMETRY = (254, 39)
# `f54` holds one `.1` entry per linked unit: `.1` the unit serial, `.2` its
# state of charge in percent, `.4` its battery power in half-watts.
_UNIT_BLOCK_FIELD = 54
_UNIT_ENTRY_FIELD = 1
_UNIT_SERIAL_FIELD = 1
_UNIT_POWER_FIELD = 4
_UNIT_POWER_SCALE = 0.5
# Key the coordinator consumes and removes: the serial decides which unit a
# reading belongs to, and only the coordinator knows which serial it is.
UNIT_POWER_BY_SN_KEY = "_unit_batt_w_by_sn"

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
        # The MPPT-to-battery edge, and the only `f12` field that belongs
        # to the string block rather than to the third-party solar node.
        # It exists only on a unit with PV wired to the EcoFlow: field 2
        # appears in no `f12` of the four ES22 captures, while `.9` and
        # `.10` carry the third-party node on both models. On the ES21
        # capture the battery node closes on it in all 5 frames that have
        # it, `.2` plus `.9` plus `.7` less `.4` and `.5` against `f11.4`
        # halved: 203+81+1 = 285 = 570/2, 173+78+1 = 252 = 504/2, and
        # 308+130 = 438 = 876/2. Left out, a PV owner loses the whole
        # solar charge from the battery reading: two of those frames read
        # 34 W and 28 W into the pack and reported 0.
        # `f12.1` also appears on that unit and stays unmapped: it does
        # not close any node balance in the frames available.
        "12.2": ("_mppt_to_batt_w", _TYPE_FLOAT, 1),
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
        # --- PV strings, direct MPPT reading (`f50.1`) ---
        # A different quantity from `solar_w` (`f11.9`), not a second source
        # for it: the EcoFlow app shows the unit's own strings and a separate
        # third-party figure side by side and adds them for its solar total.
        # In the 07:21:30 frame of the ES21 capture the strings total 195.91 W
        # while `f11.9` reads 81, against an app showing 34, 50 and 102 for
        # the strings and 80 for the third-party figure, totalling 265. Key
        # and display names follow the BK series (Stream Ultra / Ultra X):
        # `pvN_w` / "PV N Power".
        #
        # From the ES21 capture on issue #231, stored as
        # `docs/captures/es21-20260816T072152.json`. `.3` equals the sum of
        # `.9` through `.12` exactly in all 7 of its frames that carry `.3`,
        # totals 28.52 W to 310.96 W. The reporter noted his app at the end of
        # that capture and the two frames either side of the moment bracket
        # every value he read: strings 2, 3 and 4 at 36.57 / 55.32 / 104.02
        # and at 27.39 / 49.02 / 98.07 against 34 / 50 / 102 in the app.
        #
        # `.9` is not confirmed by a reading of its own. It is absent from
        # every frame of that capture, where `.10` through `.12` alone already
        # equal `.3` exactly, and the app showed string 1 at 0 W throughout.
        # Its position is read from the order of the fields and nothing more,
        # which is why it is stated here rather than left implied.
        #
        # `.1` is the device serial; `.2`, `.4`, `.5`, `.6` and `.7` are
        # unmapped. `.4` is worth a note: it equals `.3` minus `.7` in all 6
        # frames that carry it, including the 00:27 night frame where it
        # reads 1999 against `.7` = -1999 with `.3` absent. That is the
        # arithmetic saying an absent `.3` is zero rather than unknown, which
        # is what `_ZERO_FILL_PATHS` below acts on. The meaning of `.7` is
        # still open.
        #
        # `f11.3` is the same MPPT total in half-watts (203 / 173 / 34 / 28 /
        # 308 against 195.91 / 174.48 / 34.24 / 28.52 / 310.96 in the same
        # frames) and stays unmapped for that reason: two wire fields feeding
        # one key makes it flap, the way `f38.1` and `f44` do against 32/50.
        "50.1.3": ("pv_total_w", _TYPE_FLOAT, 1),
        "50.1.9": ("pv1_w", _TYPE_FLOAT, 1),
        "50.1.10": ("pv2_w", _TYPE_FLOAT, 1),
        "50.1.11": ("pv3_w", _TYPE_FLOAT, 1),
        "50.1.12": ("pv4_w", _TYPE_FLOAT, 1),
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
        # `f40` is the list and `40.1` is one task in it, so `40.1` repeats and
        # is decoded a task at a time. `.1` echoes the last operation and `.4`
        # flips every few seconds with no change to the task, so neither is
        # mapped.
        #
        # `.2` is the task's number in the app's own list on the evidence there
        # is, not what the task does. Nothing here interprets it. It is carried
        # through to a removal or an update unchanged, because those frames have
        # to name the task the device knows rather than one numbered from the
        # kind. See `_TASK_BLOCKS` for the frames that settled it.
        "40.1.2": ("_task_slot_raw", _TYPE_INT, 1),
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
    # `254/40 f60`, `f61` and `f62` are not mapped, on either model. An earlier
    # version of this comment called `f60` and `f62` ES21-only, on the strength
    # of the one ES21 frame available and no check against what was already
    # here. The ES22 captures carry 30 frames of `254/40` holding all three,
    # with values that move across two decades, so the difference was never
    # real.
    #
    # Movement is not the missing piece either. Correlating those 30 frames
    # against every reading this parser already produces yields only artefacts:
    # `f60.34` through `f60.41` sit at a constant 1584, and a constant divided
    # by a near-constant battery voltage returns a stable ratio that means
    # nothing. What is missing is an anchor - a value read off the app or an
    # independent meter at a known moment - which is how the flow model in this
    # file was settled in the first place.
    #
    # The ES21 reporter expected the solar strings among them. They are not
    # here: they sit on `254/39 f50.1` and are mapped above, found by asking
    # that reporter for a capture from a unit with PV wired directly to it
    # and an app reading to anchor it against. What is left in these three
    # blocks is still unidentified.
    #
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
    # `40.1.3` is deliberately absent here. It is the task's enabled flag, and
    # it used to be filled at frame level, back when a frame carried one task.
    # With `40.1` collected as repeated blocks the fill can no longer reach the
    # task it was written for: `_walk` marks the group seen before the
    # repeated-collect branch, so a frame-level entry would deposit
    # `_task_enabled_raw` in the output instead, and nothing pops it again. The
    # reading itself is unchanged and now runs per task, in
    # `_TASK_ZERO_FILL_PATHS`, where an absent flag inside a present task is
    # still what "disabled" looks like on the wire.
    (254, 39): (
        "11.9",
        "12.2",
        "12.4",
        "12.5",
        "12.6",
        "12.7",
        # The `f50.1` group arrives on every unit, with or without PV, so
        # filling on it would hand a PV-less ES22 five keys reading 0 W.
        # That is what `accessory_needs_nonzero` on all five definitions
        # is for: the keys exist, the entities do not, until a string has
        # actually produced. Filling on the group rather than on `.3` is
        # what closes the night: at 22:15 and again at 00:27 the capture
        # carries `f50.1` with neither `.3` nor a string in it, in a full
        # get-all as well as in a delta, so keying the fill on `.3` would
        # leave all five holding their last daylight reading until dawn.
        "50.1.3",
        "50.1.9",
        "50.1.10",
        "50.1.11",
        "50.1.12",
    ),
}

# The same rule inside one task, applied per task rather than per frame: a
# container in one task says nothing about the next.
#
# `40.1.3` is the task's enabled flag, and disabling a task in the app made it
# disappear rather than read 0. The two power fields behave the same way and
# matter more. A charge task set to 0 W sends its `.8` container with the
# serial and the target SoC in it and no `.3.3` at all, seen between 19:52 and
# 19:55 on 2026-08-08 with the device sitting idle under exactly that task.
# Reading that as "no power reported" is what hid a parked charge task: the
# write path removes the opposite task only when its power is a known number,
# so an unseen task was never removed and every later write landed on top of
# it. The discharge side has been seen sending its zero explicitly, so there
# the fill is a no-op, because an explicit value wins over a `setdefault`.
_TASK_ZERO_FILL_PATHS: tuple[str, ...] = ("40.1.3", "40.1.8.3.3", "40.1.9.1")

# Declared groups that can arrive more than once in one message, and the key
# each occurrence's raw bytes are collected under.
#
# Decoded flat, two tasks leave only the last: the frame of 2026-08-03 22:21:59
# carried an 1800 W charge task at 03:00-17:00 and a 1400 W discharge task at
# 18:00-02:00, and the parser reported the discharge task alone for the four
# minutes both stood. A task the parser cannot see is a task the write path
# never removes, and two whole-day tasks are the overlap this device answers by
# doing nothing at all.
#
# Collecting the bytes rather than splitting the decode inline also gives each
# task its own set of seen groups, which the kind rule below depends on.
_REPEATED_GROUPS: dict[tuple[int, int], dict[str, str]] = {
    (254, 39): {"40.1": "_task_blocks"},
}

# kind -> the container that carries that kind's power. What a task does is
# read from which container it carries, never from `40.1.2`.
#
# `40.1.2` does not say: on 2026-08-03 the app held a charge task numbered 1
# and a discharge task numbered 2, and on 2026-08-08 at 16:01 one app frame
# removed the charge task numbered 1 and added a discharge task numbered 1 in
# the same breath. Read as a kind, that second one filed an 800 W discharge
# task under charge, found no charge power in it and published no power at all,
# while the charge setpoint went on reporting a task that no longer existed.
# Across the captures `.2` reads 1 on 75 charge tasks and on 5 discharge ones.
#
# It is the container and not the watts inside it because a task at 0 W omits
# the watts: see `_TASK_ZERO_FILL_PATHS`.
_TASK_BLOCKS: tuple[tuple[str, str], ...] = (
    ("charge", "40.1.8"),
    ("discharge", "40.1.9"),
)

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

def _zero_fill_keys(
    cmd: tuple[int, int], paths: tuple[str, ...]
) -> dict[str, tuple[tuple[str, Any], ...]]:
    """group path -> (sensor key, zero value) for every filled path under it.

    The zero keeps the field's own type: an int field filled with 0.0 would
    read as a float everywhere downstream.
    """
    return {
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


_ZERO_FILL_KEYS: dict[tuple[int, int], dict[str, tuple[tuple[str, Any], ...]]] = {
    cmd: _zero_fill_keys(cmd, paths) for cmd, paths in _ZERO_FILL_PATHS.items()
}
_TASK_ZERO_FILL_KEYS = _zero_fill_keys((254, 39), _TASK_ZERO_FILL_PATHS)

# The subtree one task block is decoded against, and the prefix its paths keep,
# so a path inside a task reads the same here as in the field map. Navigated
# out of the compiled tree rather than compiled again, so it cannot drift.
_TASK_TREE: dict[int, Any] = _ES22_TREE[(254, 39)][40][1]
_TASK_PREFIX = "40.1."


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
    repeated: dict[str, str] | None = None,
) -> None:
    """Decode the declared paths of ``payload`` into ``result``.

    Undeclared fields are skipped by length and never entered, so a string
    or a packed array cannot be mistaken for a submessage.

    A group named in ``repeated`` is collected as raw bytes rather than
    decoded here. One flat result can only hold the last occurrence of a
    path, and one shared set of seen groups cannot say which occurrence
    carried which container, so a group that repeats is decoded one
    occurrence at a time by the caller.
    """
    repeated = repeated or {}
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
            path = f"{prefix}{field_num}"
            seen_groups.add(path)
            collect = repeated.get(path)
            if collect is not None:
                result.setdefault(collect, []).append(raw)
                continue
            _walk(raw, child, result, seen_groups, f"{path}.", repeated)
            continue

        sensor_key, scalar_type, scale = child
        value = _decode_scalar(wire_type, raw, scalar_type)
        if value is None:
            continue
        result[sensor_key] = value * scale if scale != 1 else value


def _decode_task(block: bytes) -> dict[str, Any]:
    """Decode one `40.1` task block on its own.

    Its own result and its own set of seen groups: a power container in one
    task says nothing about the next, and sharing either is what made a frame
    carrying two tasks report one. The prefix is kept so the paths read the
    same as everywhere else in this file.

    The seen set starts holding `40.1` because that block is what is being
    decoded, which is what lets the enabled flag fill.

    A block that will not decode costs its own task and no more, the same way
    a bad header does not take the rest of a bundle with it.
    """
    task: dict[str, Any] = {}
    seen: set[str] = {"40.1"}
    try:
        _walk(block, _TASK_TREE, task, seen, _TASK_PREFIX)
    except (IndexError, ValueError):
        return {}
    for group, defaults in _TASK_ZERO_FILL_KEYS.items():
        if group not in seen:
            continue
        for key, zero in defaults:
            task.setdefault(key, zero)
    task["_task_kinds"] = tuple(kind for kind, group in _TASK_BLOCKS if group in seen)
    return task


# Every key one task list readback can produce, in the order the two kinds are
# built below, so an empty task list can clear all of them.
_TASK_KEYS: tuple[str, ...] = (
    "scheduled_charge_power_w",
    "scheduled_charge_soc_target",
    "scheduled_charge_enabled",
    "scheduled_charge_start_min",
    "scheduled_charge_end_min",
    "scheduled_charge_task_slot",
    "scheduled_discharge_power_w",
    "scheduled_discharge_enabled",
    "scheduled_discharge_start_min",
    "scheduled_discharge_end_min",
    "scheduled_discharge_task_slot",
)


def _finalize_task(result: dict[str, Any]) -> None:
    """Turn one task list readback into the keys the control side writes back.

    `f40` is the device's task list and each `40.1` inside it is one task. The
    blocks are decoded one at a time because two flattened into one result
    leave only the last, and because the containers inside them are how a
    task's kind is read.

    That kind comes from which power container the task carries, never from
    `40.1.2`. See `_TASK_BLOCKS` for what `.2` turned out to be.

    A task at 0 W omits its watts and sends the container alone, so the
    container is also what the zero fill keys off. Publishing nothing there is
    not a safe default on this device: the write path removes the opposite task
    only when its power is a known number, so a power that goes missing is a
    task that is never removed and then written on top of.

    Each kind gets its own keys and the coordinator's own merge keeps the other
    kind's values across the frames that carry no `f40` at all, which is most
    of them. That merge is why an empty task list has to clear every key at
    once: deleting the last task in the app stops the readback rather than
    zeroing it, so the setpoint entities would otherwise keep reporting a task
    the device no longer has.

    The start, end, enabled and slot keys have no entity. The slot is read so a
    removal or an update can name the task the device knows; the other three
    are reported for a diagnostics download and nothing reads them back.
    """
    task_list_empty = result.pop("_task_list_empty", False)
    blocks = result.pop("_task_blocks", None) or ()

    published: set[str] = set()
    for task in (_decode_task(block) for block in blocks):
        for kind in task.get("_task_kinds", ()):
            if kind in published:
                # The device can hold more than one task of a kind and these
                # keys hold one, so the last in the list is what is reported.
                # The write path removes one task per kind too, so a second one
                # would survive a setpoint write. Worth seeing in a log until
                # there is a reason to model it.
                _LOGGER.debug(
                    "ES22 task list carries more than one %s task, "
                    "reporting the last of them",
                    kind,
                )
            published.add(kind)

            power = task.get(f"_task_{kind}_power_w")
            if isinstance(power, int):
                result[f"scheduled_{kind}_power_w"] = power
            if kind == "charge":
                soc_target = task.get("_task_charge_soc_target")
                if isinstance(soc_target, int):
                    result["scheduled_charge_soc_target"] = soc_target
            slot_raw = task.get("_task_slot_raw")
            if isinstance(slot_raw, int):
                result[f"scheduled_{kind}_task_slot"] = slot_raw
            enabled_raw = task.get("_task_enabled_raw")
            if isinstance(enabled_raw, int):
                result[f"scheduled_{kind}_enabled"] = bool(enabled_raw)
            window_raw = task.get("_task_window_raw")
            if isinstance(window_raw, int):
                # One varint: start in the low 16 bits, end in the high 16,
                # both minutes since midnight.
                result[f"scheduled_{kind}_start_min"] = window_raw & 0xFFFF
                result[f"scheduled_{kind}_end_min"] = (window_raw >> 16) & 0xFFFF

    if published or not task_list_empty:
        return
    # None is an explicit clear: both platforms show it as unknown and stop
    # falling back to the value they restored at startup.
    for key in _TASK_KEYS:
        result[key] = None


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
    mppt_to_batt = result.pop("_mppt_to_batt_w", None)
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
        # `f12.2` is the same edge for the unit's own strings, and it is zero
        # on a unit that has none, so adding it costs the ES22 nothing.
        if isinstance(mppt_to_batt, (int, float)):
            into += float(mppt_to_batt)
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


def _iter_fields(payload: bytes) -> list[tuple[int, int, bytes]]:
    """Return one entry per top-level field: (number, wire type, raw bytes).

    Repeats are kept. `_walk` cannot be used for the per-unit block because
    it writes each declared path into one dict key, so a repeated block
    would leave only whichever entry came last.
    """
    fields: list[tuple[int, int, bytes]] = []
    mv = memoryview(payload)
    pos = 0
    while pos < len(mv):
        tag, pos = _read_varint(mv, pos)
        wire_type = tag & 0x07
        raw, pos = _read_field(mv, pos, wire_type)
        fields.append((tag >> 3, wire_type, raw))
    return fields


def _read_unit_entries(payload: bytes) -> dict[str, float]:
    """Return battery power in watts per unit serial, from the `f54` block.

    Linked STREAM units report one entry per unit here, each stamped with
    that unit's serial. On a single-unit installation the block holds one
    entry whose value is the system reading, which is what all three ES22
    captures from single-unit accounts show.

    `f54.1.4` is in half-watts, the same scale as the system field `11.4`,
    and the two agree: across the six frames of the two-unit capture the
    entries sum to that field within 34 W, and the `f54.1.2` states of
    charge average to `11.5` exactly. `f50.1` mirrors both as floats and is
    not read here, for the reason the field map already gives - that block
    stops being sent when a unit idles and latches at its last active
    value, while `f54` was observed going to 0 and staying there.

    An entry is taken only when it carries both a serial-shaped string and
    a power value. A frame that omits the power leaves the previous reading
    standing, which is how every incremental field in this container works.
    """
    entries: dict[str, float] = {}
    for number, wire_type, raw in _iter_fields(payload):
        if number != _UNIT_BLOCK_FIELD or wire_type != 2:
            continue
        for entry_num, entry_wire, entry_raw in _iter_fields(raw):
            if entry_num != _UNIT_ENTRY_FIELD or entry_wire != 2:
                continue
            serial: str | None = None
            power: int | None = None
            for num, wire, value in _iter_fields(entry_raw):
                if num == _UNIT_SERIAL_FIELD and wire == 2:
                    text = value.decode("ascii", "ignore")
                    if len(text) == len(value) and text.isalnum() and text.isupper():
                        serial = text
                elif num == _UNIT_POWER_FIELD and wire == 0:
                    decoded = _decode_scalar(0, value, _TYPE_INT)
                    if isinstance(decoded, int):
                        power = decoded
            if serial and power is not None:
                entries[serial] = float(power) * _UNIT_POWER_SCALE
    return entries


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
                    _walk(
                        pdata,
                        tree,
                        decoded,
                        seen_groups,
                        repeated=_REPEATED_GROUPS.get(cmd_key),
                    )
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
                if cmd_key == _CMD_TELEMETRY:
                    try:
                        unit_entries = _read_unit_entries(pdata)
                    except (IndexError, ValueError):
                        # A malformed per-unit block costs its own reading,
                        # never the telemetry that already decoded cleanly.
                        unit_entries = {}
                    if unit_entries:
                        decoded[UNIT_POWER_BY_SN_KEY] = unit_entries
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
