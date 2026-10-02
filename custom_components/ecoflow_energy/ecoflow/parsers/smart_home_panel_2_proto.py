"""Protobuf telemetry parser for the EcoFlow Smart Home Panel 2 (`HD31`).

Source: issue #464, jrbeir's two diagnostics downloads of 2026-10-02, 51
frames from one installation (a panel with a 3-pack DELTA Pro Ultra
attached to its first storage channel).

The panel pushes two messages on cmd_func 12, both from src 11 and both
unmasked (no `enc_type`):

- cmd_id 1 (`ProtoTime`): a snapshot roughly every 30 s with the grid and
  home power, the 12 circuit powers and currents, the two grid-leg currents
  and the backup runtime. Its sub-messages are present-if-changed.
- cmd_id 32 (`ProtoPushAndSet`): the settings and battery block. A
  `get_reply` bundle carries the full state (2 kB), a property push is a
  delta: 14 of the 24 pushes carry the grid voltage alone, one carries the
  battery block alone.

Mapping (paths are `cmd_id.field.subfield`):

- 12.1 field 4 `grid_power_w` (4.1), `load_power_w` (4.21, the home total)
- 12.1 field 2 packed float arrays: circuit power (2.1) and circuit current
  (2.2), element `n - 1` for circuit `n`
- 12.1 4.30 packed float pair: grid leg L1/L2 current
- 12.1 3.3 `backup_runtime_min`
- 12.32 80.3/80.2/80.4 battery SOC, full capacity and remaining energy
- 12.32 80.(79+n).8 `storage_ch{n}_soc_pct`, gated (see below)
- 12.32 82.3 `grid_voltage_v`
- 12.32 81.1.(29+n).4 the circuit label, as `circuit_{n}_name`

The packed arrays are length-delimited runs of little-endian float32, not
varints, so they are collected as raw bytes through the walker's `repeated`
map and unpacked here. Each such path is declared by name: a generic reading
of a length-delimited field misread the grid-leg pair in 4 of 22 frames.

Merge rule: a frame returns only the keys it carries. Sub-messages come and
go between frames and a delta push is a few bytes, so a key that is absent is
unchanged, never zero; the coordinator's own `_device_data.update()` is the
one merge that keeps the rest. The exception is a retraction, sent as `None`.
A storage channel's SOC is only read from a channel that reports itself ready
(`80.(59+n).1`) or connected (`80.(79+n).3`) in the same frame: an unused
channel is sent as an all-zero block and would otherwise publish a 0 % battery
that does not exist. A channel that reports both flags as 0 retracts its SOC,
so an unplugged battery does not keep its last level. The system battery block
(80.2, 80.3, 80.4) follows the channels: published next to a ready or
connected channel, retracted when the frame carries channel flags and none is
set, and left out when it carries no channel flags at all.

Never read, never published: 12.32 field 17 (`area`, the installation
address), 12.1 1.4 (`timezone_id`) and every serial number. They are not in
the field map, so the walker skips them by length.

Verification against the 51 frames: the 12 circuit powers sum to the home
total within 1.5 W on every 12.1 frame at 1233-1600 W; the two grid-leg
currents match the currents of the circuits on each leg; the battery SOC,
capacity and remaining energy agree (18432 Wh x 93 % = 17141.76 Wh) and match
the attached battery's own SOC. The storage-channel power fields (12.1 4.11,
12.32 80.80.9) were 0 in every frame, so their sign was never seen nonzero
and they are not published.
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
    _iter_fields,
    _walk,
)
from .stream_proto import _pdata_candidates

_CMD_FUNC = 12
_CMD_ID_TIME = 1
_CMD_ID_PUSH = 32
#: The panel itself; a relayed header from another source is not read.
_SRC_PANEL = 11

#: Circuits the panel carries, and the storage channels it can attach.
CIRCUIT_COUNT = 12
STORAGE_CHANNEL_COUNT = 3

# --- 12.1 `ProtoTime` -------------------------------------------------------

_TIME_FIELD_MAP: dict[str, tuple[str, str, float]] = {
    "4.1": ("grid_power_w", _TYPE_FLOAT, 1),
    "4.21": ("load_power_w", _TYPE_FLOAT, 1),
    "3.3": ("backup_runtime_min", _TYPE_INT, 1),
}

#: Packed float32 arrays in 12.1: path -> the key `_walk` collects them under.
_TIME_PACKED: dict[str, str] = {
    "2.1": "_circuit_power",
    "2.2": "_circuit_current",
    "4.30": "_grid_current",
}

#: Decimals for the scalar floats `_walk` returns unrounded.
_DECIMALS: dict[str, int] = {
    "grid_power_w": 1,
    "load_power_w": 1,
    "battery_remaining_energy_wh": 1,
}


def _declare_groups(tree: dict[int, Any], paths: dict[str, str]) -> None:
    """Declare each packed path as an (empty) group so `_walk` collects it raw.

    `_walk` only diverts a path named in `repeated` when the tree holds a
    group there; a leaf would decode it as a scalar instead, and a packed
    array is not one.
    """
    for path in paths:
        *parents, last = (int(part) for part in path.split("."))
        node = tree
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = {}


_TIME_TREE: dict[int, Any] = _compile(_TIME_FIELD_MAP)
_declare_groups(_TIME_TREE, _TIME_PACKED)

# --- 12.32 `ProtoPushAndSet` ------------------------------------------------

_PUSH_FIELD_MAP: dict[str, tuple[str, str, float]] = {
    "80.3": ("battery_soc_pct", _TYPE_INT, 1),
    "80.2": ("battery_full_capacity_wh", _TYPE_INT, 1),
    "80.4": ("battery_remaining_energy_wh", _TYPE_FLOAT, 1),
    "82.3": ("grid_voltage_v", _TYPE_INT, 1),
}


def _ready_key(n: int) -> str:
    return f"_storage_ch{n}_ready"


def _connected_key(n: int) -> str:
    return f"_storage_ch{n}_connected"


for _n in range(1, STORAGE_CHANNEL_COUNT + 1):
    # The channel's own block sits on 80.(79+n), its readiness flag on the
    # separate 80.(59+n) block.
    _PUSH_FIELD_MAP[f"80.{79 + _n}.8"] = (f"storage_ch{_n}_soc_pct", _TYPE_INT, 1)
    _PUSH_FIELD_MAP[f"80.{79 + _n}.3"] = (_connected_key(_n), _TYPE_INT, 1)
    _PUSH_FIELD_MAP[f"80.{59 + _n}.1"] = (_ready_key(_n), _TYPE_INT, 1)
del _n

_PUSH_TREE: dict[int, Any] = _compile(_PUSH_FIELD_MAP)

#: The system battery block, published only next to a ready or connected
#: storage channel (see `_parse_push`).
_SYSTEM_BATTERY_KEYS = (
    "battery_soc_pct",
    "battery_full_capacity_wh",
    "battery_remaining_energy_wh",
)

#: `81.1.(29+n).4`: the owner's label of circuit n.
_HALL_FIELD = 81
_HALL_CIRCUITS_FIELD = 1
_CIRCUIT_INFO_BASE_FIELD = 29
_CIRCUIT_NAME_FIELD = 4


def _clean(value: float, decimals: int) -> float | None:
    """Round a reading, drop a non-finite one, and turn -0.0 into 0.0."""
    if not isfinite(value):
        return None
    rounded = round(value, decimals)
    return 0.0 if rounded == 0 else rounded


def _unpack_floats(chunks: list[bytes]) -> list[float]:
    """Unpack the little-endian float32 elements of a packed array.

    A short array yields the indices it has. A chunk whose length is not a
    multiple of 4 is not a run of float32 and is skipped whole: joined with
    the chunks after it, it would shift every element behind it by the bytes
    it carries and publish a value at the wrong circuit.
    """
    raw = b"".join(chunk for chunk in chunks if len(chunk) % 4 == 0)
    count = len(raw) // 4
    return list(struct.unpack(f"<{count}f", raw[: count * 4]))


def _parse_time(pdata: bytes) -> dict[str, Any]:
    """Decode one 12.1 payload into the keys it carries."""
    walked: dict[str, Any] = {}
    _walk(pdata, _TIME_TREE, walked, set(), repeated=_TIME_PACKED)

    out: dict[str, Any] = {}
    for key, value in walked.items():
        if key.startswith("_"):
            continue
        if isinstance(value, float):
            cleaned = _clean(value, _DECIMALS.get(key, 1))
            if cleaned is not None:
                out[key] = cleaned
        else:
            out[key] = value

    for index, value in enumerate(_unpack_floats(walked.get("_circuit_power", []))):
        if index < CIRCUIT_COUNT and (cleaned := _clean(value, 1)) is not None:
            out[f"circuit_{index + 1}_power_w"] = cleaned
    for index, value in enumerate(_unpack_floats(walked.get("_circuit_current", []))):
        if index < CIRCUIT_COUNT and (cleaned := _clean(value, 2)) is not None:
            out[f"circuit_{index + 1}_current_a"] = cleaned
    for index, value in enumerate(_unpack_floats(walked.get("_grid_current", []))):
        if index < 2 and (cleaned := _clean(value, 2)) is not None:
            out[f"grid_l{index + 1}_current_a"] = cleaned
    return out


def _circuit_names(pdata: bytes) -> dict[str, Any]:
    """Read the circuit labels (81.1.30 ... 81.1.41, sub-field 4).

    Same convention as the Smart Panel 40: a circuit block with no name
    publishes `""`, and an undecodable byte becomes U+FFFD instead of
    dropping the label.
    """
    out: dict[str, Any] = {}
    for field_num, wire_type, raw in _iter_fields(pdata):
        if field_num != _HALL_FIELD or wire_type != 2:
            continue
        for hall_num, hall_wire, hall_raw in _iter_fields(raw):
            if hall_num != _HALL_CIRCUITS_FIELD or hall_wire != 2:
                continue
            for info_num, info_wire, info_raw in _iter_fields(hall_raw):
                n = info_num - _CIRCUIT_INFO_BASE_FIELD
                if info_wire != 2 or not 1 <= n <= CIRCUIT_COUNT:
                    continue
                # Every circuit block that is present publishes a name, empty
                # when the owner left it blank: the circuit's entities wait
                # for this key, so one without a name would never appear.
                name = ""
                for sub_num, sub_wire, sub_raw in _iter_fields(info_raw):
                    if sub_num == _CIRCUIT_NAME_FIELD and sub_wire == 2:
                        name = sub_raw.decode("utf-8", errors="replace").strip()
                out[f"circuit_{n}_name"] = name
    return out


def _parse_push(pdata: bytes) -> dict[str, Any]:
    """Decode one 12.32 payload into the keys it carries."""
    walked: dict[str, Any] = {}
    _walk(pdata, _PUSH_TREE, walked, set())

    out: dict[str, Any] = {}
    for key, value in walked.items():
        if key.startswith("_") or key.startswith("storage_ch"):
            continue
        if key in _SYSTEM_BATTERY_KEYS:
            continue
        if isinstance(value, float):
            cleaned = _clean(value, _DECIMALS.get(key, 1))
            if cleaned is not None:
                out[key] = cleaned
        else:
            out[key] = value

    # Channel by channel: a channel that says it is ready or connected
    # publishes its SOC, a channel that reports both flags as 0 retracts it
    # (an unplugged battery must not keep its last level), and a frame that
    # carries neither verdict leaves the key alone.
    any_active = False
    any_gate_seen = False
    for n in range(1, STORAGE_CHANNEL_COUNT + 1):
        ready = walked.get(_ready_key(n))
        connected = walked.get(_connected_key(n))
        any_gate_seen = any_gate_seen or ready is not None or connected is not None
        key = f"storage_ch{n}_soc_pct"
        if ready == 1 or connected == 1:
            any_active = True
            if key in walked:
                out[key] = walked[key]
        elif ready == 0 and connected == 0:
            out[key] = None

    # The system battery block (80.2, 80.3, 80.4) has the same trap one level
    # up: on a panel with no storage it is sent as zeros. It is published only
    # next to a channel that is ready or connected, retracted when the frame
    # says no channel is, and left alone when the frame has no channel flags.
    if any_active:
        for key in _SYSTEM_BATTERY_KEYS:
            if key in walked:
                value = walked[key]
                if isinstance(value, float):
                    value = _clean(value, _DECIMALS.get(key, 1))
                if value is not None:
                    out[key] = value
    elif any_gate_seen:
        for key in _SYSTEM_BATTERY_KEYS:
            if key in walked:
                out[key] = None

    out.update(_circuit_names(pdata))
    return out


def parse_smart_home_panel_2_message(payload: bytes) -> dict[str, Any] | None:
    """Parse one HD31 frame into the keys it carries.

    Only the keys present in the frame are returned; the coordinator's
    `_device_data.update(parsed)` keeps everything else. A frame with
    nothing this parser maps (254.21, 254.32, ...) returns None. A
    `get_reply` bundle carries both messages, and both are combined into one
    result in header order.
    """
    try:
        headers, _ = decode_header_message(payload)
    except (IndexError, OverflowError, ValueError, struct.error):
        return None
    if not headers:
        return None

    result: dict[str, Any] = {}
    for header in headers:
        if header.get("cmd_func") != _CMD_FUNC or header.get("src") != _SRC_PANEL:
            continue
        cmd_id = header.get("cmd_id")
        if cmd_id not in (_CMD_ID_TIME, _CMD_ID_PUSH):
            continue
        for pdata in _pdata_candidates(header):
            try:
                decoded = (
                    _parse_time(pdata) if cmd_id == _CMD_ID_TIME else _parse_push(pdata)
                )
            except (IndexError, OverflowError, ValueError, struct.error):
                # Not valid protobuf: try the next candidate. A clean decode
                # ends the attempt whether it produced fields or not.
                continue
            result.update(decoded)
            break

    return result or None
