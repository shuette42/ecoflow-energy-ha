"""Protobuf telemetry parser for the EcoFlow PowerPulse 2 wallbox (C376).

Derived from an 11-frame capture of a live PowerPulse 2 charging session
(PLAN-132, issue #7), spanning an idle plug, a single-phase charge, a
three-phase charge and a finished session. The device is a standalone
wallbox with its own protocol envelope, not the PowerOcean-forwarded
accessory the `ev_*` keys in `const.py` were first built for.

The envelope is the same BK-series one every other parser in this package
uses, so the unwrap is imported from `stream_proto.py`: the header decode,
the per-header XOR mask keyed on the low byte of the sequence number, and
the one-level field walk plus scalar decoder.

Two message types carry readings, both under cmd_func 2:

- `33` HeartBeat, the periodic telemetry frame. Every captured frame carries
  one, either alone (`property` topic, XOR-masked, `enc_type == 1`) or as
  one header inside a 15-header `get_reply` bundle (unmasked).
- `34` ParamReport, seen only inside the three `get_reply` bundles. Of its
  fields only `19` (the cable-lock toggle) is mapped; the rest of its
  message is unrelated configuration this integration does not read yet.
- `241/44` EDevRunDataSync, on the wallbox's own property topic, about once
  a second (PLAN-136). Only the accessory descriptor nested inside it -
  the bus address and the wallbox's own serial - is read; the settings
  block behind it is a later plan. No entity is built from these two
  fields; they exist to build the start/stop command's addressing.

HeartBeat's field `8` is a nested record carrying the live charge readings
(power, the three phase voltages, the three phase currents); it is pulled
out of the generic field loop the same way `wave3_proto.py` pulls out its
own nested records, and decoded by `_decode_charge_readings`.

Field notes:

- `1` (device status: 1 available, 3 charging, 6 finished) and `101` (a
  session-scoped status: 0 idle, 2 charging, 3 finished) track each other in
  every captured frame - `101` changes exactly when `1` does - but they are
  not the same number on the wire, so both are mapped as their own key
  rather than collapsed into one.
- `9`, `44` and the nested `8.2` are the same lifetime energy counter
  (Wh), byte-identical in ten of the eleven captured frames and one Wh
  apart in the eleventh (frame at 12:08, property topic). Only `44` is
  read; `9` and `8.2` are left unmapped entirely, the same reasoning
  `wave3_proto.py` applies to its own duplicate fields - two sources for
  one entity disagree the moment a frame like that one arrives, and `44` is
  the field the session identity below is defined against.
- `42` and `46` are the same session-energy counter; only `42` is read, for
  the same reason.
- `43` (the lifetime counter's value when the current session began) and
  `44` (the lifetime counter now) always satisfy `44 - 43 == 42` in every
  captured frame that carries a session. That identity is the test suite's
  main correctness check.
- `17` and `18` are two different current settings, both deci-amps on the
  wire, and they are easy to confuse because on a charging wallbox they
  often agree. `18` is the configured maximum current, the limit the vendor
  app calls the maximum output current: in the second owner's idle-wallbox
  recording (#7, 2026-09-10) it followed his setting step by step (110, 100,
  60 for 11, 10, 6 A) while `17` sat at 60 throughout, and in the first
  owner's charging capture it is a constant 160 for his 16 A limit. `17` is
  the charging current setpoint of the session: 6, 8, 10 and 16 A in the
  first capture, where it moved with what the app showed while charging.
  The first pre-release published `17` as the maximum current; the second
  owner's live test showed the sensor stuck at 6 A while he stepped the
  limit, which is how the two were told apart.
- `21` (phase mode) is 1 while only the first phase current in field `8`
  carries a real reading and 0 while all three do - confirmed against
  every captured frame, not inferred from the field name. It is the phase
  mode in effect, not the app's phase selection: that setting has a third
  value, automatic, which this field never carries (the second owner's
  test on #7 confirmed the field reads 1 or 0 under automatic as well).
- `20` (`input_phase`) is constant across the whole capture and is not
  mapped. `29` and `30` do not walk as clean protobuf in this capture and
  are left alone rather than guessed at.
- Field `1` doubles as a gate for the four session-scoped fields (`40`
  session start, `41` session duration, `42` session energy, `43` the
  lifetime counter at session start): a plug that is available (status 1)
  still has last session's numbers sitting in these fields on the wire, and
  publishing them under an idle status would show a stale session as if it
  were current. So they are only published while `1` reports something
  other than "available" - matching what the vendor app itself shows on an
  idle plug. `44`, the running lifetime counter, is not part of this gate;
  it is valid regardless of session state and is only ever withheld if the
  device reports it as exactly zero (never observed in this capture).
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

# HeartBeat's nested charge-readings record. Pulled out of the generic field
# loop in `_decode_heartbeat_fields`, the same way `wave3_proto.py` pulls its
# own nested records out of its field loop.
_CHARGE_READINGS_FIELD = 8

# cmd_func 2, cmd_id 33 (HeartBeat) - top-level scalar fields only. Field 8
# (nested) and field 9 (the `44` twin) are deliberately absent; see the
# module docstring.
_HEARTBEAT_FIELD_MAP: dict[int, tuple[str, str]] = {
    1: ("_plug_status_raw", _TYPE_INT),
    17: ("_charge_current_da_raw", _TYPE_INT),
    18: ("_max_current_da_raw", _TYPE_INT),
    21: ("_phase_mode_raw", _TYPE_INT),
    40: ("_session_start_ts_raw", _TYPE_INT),
    41: ("_session_duration_s_raw", _TYPE_INT),
    42: ("_session_energy_wh_raw", _TYPE_INT),
    43: ("_session_start_energy_wh_raw", _TYPE_INT),
    44: ("_total_energy_wh_raw", _TYPE_INT),
    101: ("_session_status_raw", _TYPE_INT),
}

# HeartBeat field 8's sub-fields. Sub-field 2 is the same lifetime counter
# as top-level field 44 and is deliberately left unmapped (see docstring).
_CHARGE_READINGS_FIELD_MAP: dict[int, tuple[str, str]] = {
    4: ("ev_charge_power_w", _TYPE_FLOAT),
    7: ("ev_voltage_l1_v", _TYPE_FLOAT),
    8: ("ev_voltage_l2_v", _TYPE_FLOAT),
    9: ("ev_voltage_l3_v", _TYPE_FLOAT),
    10: ("ev_current_l1_a", _TYPE_FLOAT),
    11: ("ev_current_l2_a", _TYPE_FLOAT),
    12: ("ev_current_l3_a", _TYPE_FLOAT),
}

# cmd_func 2, cmd_id 34 (ParamReport) - only the cable-lock toggle is mapped.
_PARAM_REPORT_FIELD_MAP: dict[int, tuple[str, str]] = {
    19: ("_cable_lock_raw", _TYPE_INT),
}

# Field 1 is the same enum the PowerOcean relay carried on `(241, 3)`: the
# same numbers with the same meanings, which is why this parser writes the
# existing `ev_charge_status` key rather than registering a synonym for a
# value the tree already addresses (ADR-008 addendum 2026-09-09). The
# spellings are the ones that entity already declares in its `options` list,
# so value 6 is `finishing` and not `finished` - a state outside the declared
# options is an error in Home Assistant, not a wording preference.
_PLUG_STATUS_NAMES: dict[int, str] = {1: "available", 3: "charging", 6: "finishing"}
# Field 101 is a different number on the wire (0/2/3 where field 1 has
# 1/3/6), so it keeps a key of its own.
_SESSION_STATUS_NAMES: dict[int, str] = {0: "idle", 2: "charging", 3: "finished"}
_PHASE_MODE_NAMES: dict[int, str] = {0: "three_phase", 1: "single_phase"}

# The plug status value that means "no session in progress". Everything in
# `_SESSION_SCOPED_RAW_KEYS` is withheld while field 1 reports this value.
_PLUG_STATUS_NO_SESSION = 1

# The four session-scoped raw keys and the final keys they become once a
# session is in progress. Order matters: `zip` below pairs them positionally.
_SESSION_SCOPED_RAW_KEYS: tuple[str, ...] = (
    "_session_start_ts_raw",
    "_session_duration_s_raw",
    "_session_energy_wh_raw",
    "_session_start_energy_wh_raw",
)
_SESSION_SCOPED_FINAL_KEYS: tuple[str, ...] = (
    "ev_session_start_ts",
    "ev_session_duration_s",
    "ev_session_energy_wh",
    "ev_session_start_energy_wh",
)


def _tidy_float(value: float) -> float:
    """Round a float32 charge reading to two decimals.

    The device sends single-precision floats, so a reading of 233.43 arrives
    as 233.43051147460938. Two decimals keep everything the unit's own
    display can show and drop the representation noise (PLAN-132; the same
    reasoning `wave3_proto.py` applies to its own float readings).
    """
    return round(value, 2)


def _decode_charge_readings(raw: bytes) -> dict[str, Any]:
    """Decode HeartBeat's nested charge-readings record (field 8)."""
    result: dict[str, Any] = {}
    for sub_num, sub_wire, sub_raw in _iter_fields(raw):
        mapping = _CHARGE_READINGS_FIELD_MAP.get(sub_num)
        if mapping is None:
            continue
        key, scalar_type = mapping
        value = _decode_scalar(sub_wire, sub_raw, scalar_type)
        if value is None:
            continue
        result[key] = _tidy_float(value) if scalar_type is _TYPE_FLOAT else value
    return result


def _decode_heartbeat_fields(pdata: bytes) -> dict[str, Any]:
    """Decode one HeartBeat (2/33) message: its scalars plus field 8."""
    result: dict[str, Any] = {}
    for field_num, wire_type, raw in _iter_fields(pdata):
        if field_num == _CHARGE_READINGS_FIELD and wire_type == 2:
            result.update(_decode_charge_readings(raw))
            continue

        mapping = _HEARTBEAT_FIELD_MAP.get(field_num)
        if mapping is None:
            continue
        sensor_key, scalar_type = mapping
        value = _decode_scalar(wire_type, raw, scalar_type)
        if value is not None:
            result[sensor_key] = value
    return result


def _decode_param_report_fields(pdata: bytes) -> dict[str, Any]:
    """Decode one ParamReport (2/34) message: only the cable lock."""
    result: dict[str, Any] = {}
    for field_num, wire_type, raw in _iter_fields(pdata):
        mapping = _PARAM_REPORT_FIELD_MAP.get(field_num)
        if mapping is None:
            continue
        sensor_key, scalar_type = mapping
        value = _decode_scalar(wire_type, raw, scalar_type)
        if value is not None:
            result[sensor_key] = value
    return result


def _finalize(parsed: dict[str, Any]) -> dict[str, Any]:
    """Normalize near-zero floats and resolve the raw enum/gated fields."""
    result = dict(parsed)

    for key, value in list(result.items()):
        if (
            isinstance(value, float)
            and isfinite(value)
            and abs(value) < _FLOAT_ZERO_EPS
        ):
            result[key] = 0.0

    status_raw = result.pop("_plug_status_raw", None)
    if isinstance(status_raw, int):
        status_name = _PLUG_STATUS_NAMES.get(status_raw)
        # An unknown state would crash the enum sensor with "not in list of
        # options", so it is dropped rather than passed through - the same
        # reasoning powerocean_proto.py applies to the AC31 path that feeds
        # this same ev_charge_status key.
        if status_name is not None:
            result["ev_charge_status"] = status_name

    session_status_raw = result.pop("_session_status_raw", None)
    if isinstance(session_status_raw, int):
        session_status_name = _SESSION_STATUS_NAMES.get(session_status_raw)
        if session_status_name is not None:  # same reasoning as above
            result["ev_session_status"] = session_status_name

    phase_mode_raw = result.pop("_phase_mode_raw", None)
    if isinstance(phase_mode_raw, int):
        phase_mode_name = _PHASE_MODE_NAMES.get(phase_mode_raw)
        if phase_mode_name is not None:  # same reasoning as above
            result["ev_phase_mode"] = phase_mode_name

    max_current_raw = result.pop("_max_current_da_raw", None)
    if isinstance(max_current_raw, int):
        result["ev_max_current_a"] = round(max_current_raw / 10.0, 1)

    charge_current_raw = result.pop("_charge_current_da_raw", None)
    if isinstance(charge_current_raw, int):
        result["ev_charge_current_a"] = round(charge_current_raw / 10.0, 1)

    cable_lock_raw = result.pop("_cable_lock_raw", None)
    if isinstance(cable_lock_raw, int):
        result["ev_cable_lock_enabled"] = bool(cable_lock_raw)

    # The lifetime counter is valid on its own, independent of session
    # state. It is only withheld if the device ever reports it as exactly
    # zero - never observed in this capture, but a 0 here would otherwise
    # look like a meter reset to a total_increasing sensor (PLAN-132
    # contract rule).
    total_energy_raw = result.pop("_total_energy_wh_raw", None)
    if isinstance(total_energy_raw, int) and total_energy_raw > 0:
        result["ev_total_energy_wh"] = total_energy_raw

    # See the module docstring: the four session-scoped fields are only
    # published while a session exists (status is not "available").
    if status_raw == _PLUG_STATUS_NO_SESSION:
        for raw_key in _SESSION_SCOPED_RAW_KEYS:
            result.pop(raw_key, None)
    else:
        for raw_key, final_key in zip(
            _SESSION_SCOPED_RAW_KEYS, _SESSION_SCOPED_FINAL_KEYS, strict=True
        ):
            value = result.pop(raw_key, None)
            if value is not None:
                result[final_key] = value

    return result


def _decode_run_data_sync_fields(pdata: bytes) -> dict[str, Any]:
    """Decode EDevRunDataSync (241/44): the accessory descriptor only.

    Walks `pdata` field 1 (the message body) -> field 1 (`dev_info`) ->
    fields 1 (`dev_addr`, varint) and 2 (`dev_sn`, 16-byte serial). Field 3
    of `dev_info` (present on every settings report, absent on every
    captured write) is ignored - the descriptor measurement note of
    PLAN-136 records why. The settings block behind `dev_info` (`f4.f8`) is
    a later plan; nothing else from this message is parsed here.

    Both keys are emitted only when `dev_addr` is present and `dev_sn`
    decodes as exactly 16 ASCII bytes; otherwise an empty dict is returned,
    which is a clean decode of a frame this function does not (yet) read
    fully, not an error. The serial is never logged.
    """
    result: dict[str, Any] = {}
    for field_num, wire_type, body in _iter_fields(pdata):
        if field_num != 1 or wire_type != 2:
            continue
        for sub_num, sub_wire, dev_info in _iter_fields(body):
            if sub_num != 1 or sub_wire != 2:
                continue
            dev_addr: int | None = None
            dev_sn: str | None = None
            for leaf_num, leaf_wire, leaf_raw in _iter_fields(dev_info):
                if leaf_num == 1 and leaf_wire == 0:
                    value = _decode_scalar(leaf_wire, leaf_raw, _TYPE_INT)
                    if isinstance(value, int):
                        dev_addr = value
                elif leaf_num == 2 and leaf_wire == 2 and len(leaf_raw) == 16:
                    try:
                        dev_sn = leaf_raw.decode("ascii")
                    except UnicodeDecodeError:
                        dev_sn = None
            if dev_addr is not None and dev_sn is not None:
                result["ev_charger_dev_addr"] = dev_addr
                result["ev_charger_sn"] = dev_sn
            break
        break
    return result


def parse_powerpulse_message(payload: bytes) -> dict[str, Any] | None:
    """Parse a PowerPulse 2 (C376) protobuf frame into flat sensor keys."""
    try:
        headers, _ = decode_header_message(payload)
        if not headers:
            return None

        merged: dict[str, Any] = {}
        for header in headers:
            cmd_key = (int(header.get("cmd_func", -1)), int(header.get("cmd_id", -1)))
            if cmd_key == (2, 33):
                decoder = _decode_heartbeat_fields
            elif cmd_key == (2, 34):
                decoder = _decode_param_report_fields
            elif cmd_key == (241, 44):
                decoder = _decode_run_data_sync_fields
            else:
                continue

            for pdata in _pdata_candidates(header):
                try:
                    decoded = decoder(pdata)
                except (IndexError, ValueError):
                    # Only a payload that is not valid protobuf falls through
                    # to the next candidate; a clean decode ends the attempt,
                    # empty or not (see _pdata_candidates).
                    continue
                merged.update(decoded)
                break
    except Exception:
        return None
    if not merged:
        return None

    finalized = _finalize(merged)
    return finalized or None
