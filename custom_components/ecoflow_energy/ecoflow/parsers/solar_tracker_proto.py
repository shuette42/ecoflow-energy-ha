"""Protobuf telemetry parser for the EcoFlow Single Axis Solar Tracker.

Covers both account serial prefixes the device family ships under, `HZ31`
and `S02F`: same product id, same message, same field numbers.

Derived from a 264-frame reporter capture and two diagnostics downloads
(issue #339, gabbo99g-creator, 2026-09-02). Every frame decodes as a plain
EcoFlow header, `cmd_func=32, cmd_id=1, product_id=7937`, with no
`enc_type` - unlike the BK-series devices, this message is never XOR
masked. The report pack carries 27 fields, every one of them optional and present on
every frame observed; six are mapped here and the rest are named and left out,
because no observation settles what they mean.

Reused from `stream_proto.py`, unchanged: the header decode
(`decode_header_message`), the plain/XOR payload candidates
(`_pdata_candidates`; every tracker frame today takes the plain branch
since `enc_type` is absent), the one-level field walk (`_iter_fields`) and
the scalar decoder (`_decode_scalar`). Own to this parser: the field map,
the `+10` offset on the three angle fields, the sentinel on the optimal
angle, and the two-state tracking-mode enum.

Field notes:

- `angle` (9), `angle_manual` (10) and `angle_target` (11) all publish the
  wire value plus 10 degrees. The offset is not frame-derivable: it rests
  on the reporter's own cross-check against the app at raw 0, 10 and 75 on
  both units. The frames do show `angle == angle_target` at rest in manual
  mode on both units (10/10, 75/75, 71/71), which is consistent with all
  three fields sharing one encoding, but only the reporter's app check
  confirms the offset itself.
- `angle_manual` (field 10, published as `optimal_angle_deg`) carries
  `0xFFFFFFFF` in most frames (76 percent of the reporter's dataset) and a
  real value only for a short window around a manual command. The sentinel
  becomes an explicit `None` - the key stays in the result - rather than
  being dropped, so the entity reads unknown instead of holding a stale
  recommendation for the rest of the day. The proto names this field
  `angle_manual`; "optimal angle" is the reporter's app-verified label, and
  the two names do not agree. The label is kept because the frames do not
  contradict it (the field is populated only around manual commands and
  never carries a value the device was told to move to) and the vendor
  name would read as a plain repeat of the tilt angle otherwise.
- `angle_target` (field 11, published as `target_angle_deg`) is the manual
  setpoint and is published exactly as read in every mode, auto included.
  In auto mode it holds the last manual value rather than where the
  tracker is currently headed; the frames confirm this over more than
  three hours (field 9 sweeping 70-74 while field 11 sits at the last
  manual setpoint the whole time). Deriving it to `None` in auto would be
  a claim the frames do not support, and round 2's control entity needs a
  state in every mode.
- `mode` (field 3, published as `tracking_mode`) reads back exactly two
  values across the whole capture, 0 and 1, mapped to `manual` and `auto`.
  Any other value publishes an explicit `None` rather than raising: no
  third state was ever measured, and a device that later reports one
  should not crash the coordinator over it.
- `lux` (field 4, published as `light_level`) is published unscaled and
  without a unit. The wire maximum in the reporter's capture (1 439 885)
  is roughly ten times the brightest physical sunlight, so the number is
  scaled by some factor the proto does not name, and no factor is
  verified.
- `battery_percent` (field 14, published as `battery_pct`) is published
  unscaled. The vendor names the field, the wire carries 96..100 across
  both units over the whole capture, and a percent has one possible scale.

Deliberately not mapped, each for the reason PLAN-119 records: `angle`'s
sibling `battery_temperature` (15, scale unverified against any app
reading), `track_num` (24, a counter, not a reading), `word` (2, follows
motion with no label), the charge/config/version/counter fields (12, 13,
16-23, 25-27), and `scenes` (1). Nothing in that group is a reading an
owner watches, and none has a vendor name plus a frame plus an app check
behind it the way the six shipped fields do.
"""

from __future__ import annotations

from typing import Any

from ..proto.decoder import decode_header_message
from .stream_proto import (
    _TYPE_INT,
    _decode_scalar,
    _iter_fields,
    _pdata_candidates,
)

# The device never masks this message: `_pdata_candidates` returns the
# plain bytes whenever `enc_type != 1`, which every captured frame is.

# The device's one telemetry message, cmd_func 32 / cmd_id 1.
_SOLAR_TRACKER_FIELD_MAP: dict[tuple[int, int], dict[int, tuple[str, str]]] = {
    (32, 1): {
        3: ("_tracking_mode_raw", _TYPE_INT),
        4: ("light_level", _TYPE_INT),
        9: ("_tilt_angle_raw", _TYPE_INT),
        10: ("_optimal_angle_raw", _TYPE_INT),
        11: ("_target_angle_raw", _TYPE_INT),
        14: ("battery_pct", _TYPE_INT),
    },
}

# The angle fields share one encoding: wire value plus this offset, in
# degrees. Confirmed against the app at raw 0, 10 and 75 on both units
# (reporter cross-check, not frame-derivable).
_ANGLE_OFFSET_DEG = 10

# `angle_manual` (field 10) at rest. Becomes an explicit `None`, not an
# omitted key, so a stale recommendation does not stand for hours.
_OPTIMAL_ANGLE_SENTINEL = 0xFFFFFFFF

# `mode` (field 3). No third value was measured; anything outside this map
# reads as an explicit `None` rather than raising.
_TRACKING_MODE = {
    0: "manual",
    1: "auto",
}


def _decode_mapped_fields(
    pdata: bytes,
    field_map: dict[int, tuple[str, str]],
) -> dict[str, Any]:
    """Decode the mapped scalars of one report pack."""
    result: dict[str, Any] = {}

    for field_num, wire_type, raw in _iter_fields(pdata):
        mapping = field_map.get(field_num)
        if mapping is None:
            continue

        sensor_key, scalar_type = mapping
        value = _decode_scalar(wire_type, raw, scalar_type)
        if value is not None:
            result[sensor_key] = value

    return result


def _finalize(parsed: dict[str, Any]) -> dict[str, Any]:
    """Apply the angle offset, the sentinel and the tracking-mode enum."""
    result = dict(parsed)

    tilt_raw = result.pop("_tilt_angle_raw", None)
    if isinstance(tilt_raw, int):
        result["tilt_angle_deg"] = tilt_raw + _ANGLE_OFFSET_DEG

    target_raw = result.pop("_target_angle_raw", None)
    if isinstance(target_raw, int):
        result["target_angle_deg"] = target_raw + _ANGLE_OFFSET_DEG

    optimal_raw = result.pop("_optimal_angle_raw", None)
    if isinstance(optimal_raw, int):
        if optimal_raw == _OPTIMAL_ANGLE_SENTINEL:
            result["optimal_angle_deg"] = None
        else:
            result["optimal_angle_deg"] = optimal_raw + _ANGLE_OFFSET_DEG

    mode_raw = result.pop("_tracking_mode_raw", None)
    if isinstance(mode_raw, int):
        result["tracking_mode"] = _TRACKING_MODE.get(mode_raw)

    return result


def parse_solar_tracker_message(payload: bytes) -> dict[str, Any] | None:
    """Parse a Solar Tracker protobuf frame into flat sensor keys."""
    try:
        headers, _ = decode_header_message(payload)
        if not headers:
            return None

        merged: dict[str, Any] = {}
        for header in headers:
            cmd_key = (int(header.get("cmd_func", -1)), int(header.get("cmd_id", -1)))
            field_map = _SOLAR_TRACKER_FIELD_MAP.get(cmd_key)
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
