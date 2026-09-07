"""Protobuf telemetry parser for the EcoFlow WAVE 3 (AC71) portable AC unit.

Derived from a 24-hour listen-only capture of a live AC71 in app-auth MQTT
mode (PLAN-047, issue #161), spanning standby, a running cycle and a sleep
transition. The device is a portable air conditioner with a small battery
and PV input, not a power station: it reports climate readings (indoor and
outdoor temperature, humidity, condensate level) alongside the power and
battery figures a Stream-family device would carry.

The envelope is the BK-series one, so everything that unwraps a frame is
imported from `stream_proto.py`: the header decode, the per-header XOR mask
keyed on the low byte of the sequence number, the one-level field walk and
the scalar decoder.

Two message types carry readings, both under cmd_func 254:

- `21` DisplayPropertyUpload, the telemetry frame. Three shapes were
  observed: a small incremental (one or two fields, most often the AC input
  power), a sleep-state flip (field 212 alone), and a full upload of ~50
  fields including the two nested records below.
- `22` RuntimePropertyUpload, the temperature and firmware frame - the
  indoor/outdoor sensor readings the `21` full upload does not carry, plus
  the packed firmware version.

A third message, `23` DevRequest, carries no reading fields at all and is
left out of the map entirely, the same way the Smart Meter parser leaves its
own upload-period message unmapped.

Both nested records live inside the `21` full upload:

- `514` wave_mode_info is the per-mode configuration list: five entries, one
  per operating mode, each holding that mode's fan speed, target
  temperature or humidity, and (for cooling/heating) its submode. The
  entry's position in the list is the mode value itself (`operating_mode`
  1-5 select entries 1-5; entry 0 carries nothing). A present `514` is
  always treated as the complete list - all thirteen keys across the five
  modes are published, `None` where an entry or a field inside it is
  absent - so a caller reading the currently active mode never has to
  guess whether a missing key means "not sent" or "cleared".
- `627` dev_errcode_list holds one packed repeated error-code field; it
  collapses to a single `fault` flag (any code non-zero), because no
  captured frame ever carried a non-zero code to name.

Field notes:

- `53` pow_get_ac and `777` pow_get_self_consume are byte-identical in
  every captured frame. `777` is left unmapped rather than aliased to the
  same sensor key, on the same reasoning `stream_proto.py` applies to its
  own duplicate led fields: two sources for one entity flap the moment they
  diverge, and nothing in this capture proves they always agree.
- `212` dev_sleep_state is the device's own idle flag, not a power state:
  it flips to 1 while otherwise sending nothing but that one field, which
  is the "incremental sleep" shape above. `running` is its inversion.
- `176` pd_firm_ver packs the firmware revision the way `firmware.py`
  already decodes for other devices (`decode_version`); it is only
  published when non-zero, so a `22` upload that has not yet reported it
  does not overwrite a previously known version with an empty string.
- Field names above are the AC71's own message definition
  (Ac517AplComm.proto): `lcd_light`, `pow_get_ac`, `bms_batt_soc`,
  `wave_operating_mode`, `wave_mode_info`, `dev_errcode_list`, and so on.

Deliberately not mapped: cmd `23` (DevRequest, no reading fields at all) and
field `777` (see above). Values are published unrounded, as in
`stream_proto.py`.

Three enum label maps (`automatic_drainage`, `mood_light_mode`,
`display_temperature_source`) have no proto enum to read from; the labels
rest on the vendor app's own option lists and drainage toggle handler, and
the capture showed each field at one value only. No entity is built on any
of the three yet - reading them back is Phase B work.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Mapping

from ..firmware import decode_version
from ..proto.decoder import _read_varint, decode_header_message
from .stream_proto import (
    _FLOAT_ZERO_EPS,
    _TYPE_FLOAT,
    _TYPE_INT,
    _decode_scalar,
    _iter_fields,
    _pdata_candidates,
)

# The two nested records inside the `254/21` full upload. Neither is a flat
# scalar, so both are pulled out of the generic field loop in
# `_decode_mapped_fields` and handed to their own decoders.
_MODE_INFO_FIELD = 514
_ERRCODE_LIST_FIELD = 627

# cmd_func/cmd_id -> field_number -> (sensor_key, scalar_type)
_WAVE3_FIELD_MAP: dict[tuple[int, int], dict[int, tuple[str, str]]] = {
    (254, 21): {
        5: ("screen_brightness_pct", _TYPE_INT),
        18: ("screen_off_time_s", _TYPE_INT),
        53: ("ac_input_power_w", _TYPE_FLOAT),
        61: ("_ac_input_connected_raw", _TYPE_INT),
        158: ("battery_power_w", _TYPE_FLOAT),
        195: ("_beep_enabled_raw", _TYPE_INT),
        212: ("_dev_sleep_state_raw", _TYPE_INT),
        242: ("battery_soc_pct", _TYPE_FLOAT),
        361: ("pv_input_power_w", _TYPE_FLOAT),
        484: ("temp_ambient_c", _TYPE_FLOAT),
        485: ("humi_ambient_pct", _TYPE_FLOAT),
        486: ("_operating_mode_raw", _TYPE_INT),
        494: ("temp_indoor_supply_air_c", _TYPE_FLOAT),
        504: ("condensate_water_level", _TYPE_FLOAT),
        505: ("_draining_raw", _TYPE_INT),
        506: ("_drainage_mode_raw", _TYPE_INT),
        507: ("_mood_light_mode_raw", _TYPE_INT),
        508: ("_display_temperature_source_raw", _TYPE_INT),
        509: ("_pet_care_enabled_raw", _TYPE_INT),
        510: ("pet_care_warning_temp_c", _TYPE_FLOAT),
        513: ("_pet_care_alarm_raw", _TYPE_INT),
        # 514 and 627 are nested records; see _decode_mapped_fields.
    },
    (254, 22): {
        68: ("ac_input_voltage_v", _TYPE_FLOAT),
        174: ("_bms_communication_error_raw", _TYPE_INT),
        176: ("_pd_firm_ver_raw", _TYPE_INT),
        223: ("ac_input_current_a", _TYPE_FLOAT),
        244: ("battery_voltage_v", _TYPE_FLOAT),
        245: ("battery_current_a", _TYPE_FLOAT),
        493: ("temp_indoor_return_air_c", _TYPE_FLOAT),
        495: ("temp_outdoor_ambient_c", _TYPE_FLOAT),
        496: ("temp_condenser_c", _TYPE_FLOAT),
        499: ("temp_evaporator_c", _TYPE_FLOAT),
        503: ("temp_compressor_discharge_c", _TYPE_FLOAT),
    },
}

# wave_mode_info (field 514) sub-records, keyed by the entry's position in
# the repeated list - which is the operating-mode value that entry belongs
# to (1 cooling .. 5 constant_temp; position 0 carries nothing). Sub-field 1
# is the submode, 2 the fan speed, 3 the target temperature, 4 the target
# humidity, 5/6 the constant-temp upper/lower limit.
_MODE_INFO_ENTRY_FIELDS: dict[int, dict[int, tuple[str, str]]] = {
    1: {  # cooling
        1: ("cooling_submode", _TYPE_INT),
        2: ("cooling_fan_speed_pct", _TYPE_INT),
        3: ("cooling_target_temp_c", _TYPE_FLOAT),
    },
    2: {  # heating
        1: ("heating_submode", _TYPE_INT),
        2: ("heating_fan_speed_pct", _TYPE_INT),
        3: ("heating_target_temp_c", _TYPE_FLOAT),
    },
    3: {  # fan only
        2: ("fan_only_fan_speed_pct", _TYPE_INT),
    },
    4: {  # dehumidify
        2: ("dehumidify_fan_speed_pct", _TYPE_INT),
        4: ("dehumidify_target_humidity_pct", _TYPE_FLOAT),
    },
    5: {  # constant temp
        2: ("constant_temp_fan_speed_pct", _TYPE_INT),
        3: ("constant_temp_target_temp_c", _TYPE_FLOAT),
        5: ("constant_temp_upper_limit_c", _TYPE_FLOAT),
        6: ("constant_temp_lower_limit_c", _TYPE_FLOAT),
    },
}

# All thirteen keys a present 514 publishes, so a caller can tell "not
# reported this message" (key absent, e.g. a message that carries no 514 at
# all) from "reported and cleared" (key present, value None).
_MODE_INFO_ALL_KEYS: tuple[str, ...] = tuple(
    key for fields in _MODE_INFO_ENTRY_FIELDS.values() for key, _ in fields.values()
)

# The device always sends exactly this many entries: position 0 (empty) plus
# one per mode 1-5. Measured in all 12 full 514 records of the capture
# (PLAN-047 review, finding A3). A record with a different count is a shape
# this map cannot label without risking a wrong mode under a right name, so
# `_decode_mode_info` refuses to guess and publishes the all-None dict.
_MODE_INFO_ENTRY_COUNT = 6

_SUBMODE_NAMES: dict[int, str] = {0: "none", 1: "normal", 2: "max", 3: "sleep", 4: "eco"}
_SUBMODE_KEYS = frozenset({"cooling_submode", "heating_submode"})

_OPERATING_MODE_NAMES: dict[int, str] = {
    1: "cooling",
    2: "heating",
    3: "fan",
    4: "dehumidify",
    5: "constant_temp",
}
_MOOD_LIGHT_MODE_NAMES: dict[int, str] = {0: "off", 1: "on", 2: "screen_time"}
_DISPLAY_TEMPERATURE_SOURCE_NAMES: dict[int, str] = {0: "ambient", 1: "outlet"}
_AUTOMATIC_DRAINAGE_NAMES: dict[int, bool] = {0: False, 1: True}

# Plain boolean raw fields: 0/1 on the wire, a bool in the published state.
_BOOL_RAW_KEYS: dict[str, str] = {
    "_ac_input_connected_raw": "ac_input_connected",
    "_beep_enabled_raw": "beep_enabled",
    "_draining_raw": "draining",
    "_pet_care_enabled_raw": "pet_care_enabled",
    "_pet_care_alarm_raw": "pet_care_alarm",
    "_bms_communication_error_raw": "bms_communication_error",
}

# The four keys `resolve_active_mode` fills for each operating mode. `None`
# means that mode has no reading for that generic slot (fan mode has no
# target temperature or humidity, for instance).
_ACTIVE_MODE_KEYS: dict[str, dict[str, str | None]] = {
    "cooling": {
        "target_temp_c": "cooling_target_temp_c",
        "airflow_speed_pct": "cooling_fan_speed_pct",
        "target_humidity_pct": None,
        "operating_submode": "cooling_submode",
    },
    "heating": {
        "target_temp_c": "heating_target_temp_c",
        "airflow_speed_pct": "heating_fan_speed_pct",
        "target_humidity_pct": None,
        "operating_submode": "heating_submode",
    },
    "fan": {
        "target_temp_c": None,
        "airflow_speed_pct": "fan_only_fan_speed_pct",
        "target_humidity_pct": None,
        "operating_submode": None,
    },
    "dehumidify": {
        "target_temp_c": None,
        "airflow_speed_pct": "dehumidify_fan_speed_pct",
        "target_humidity_pct": "dehumidify_target_humidity_pct",
        "operating_submode": None,
    },
    "constant_temp": {
        "target_temp_c": "constant_temp_target_temp_c",
        "airflow_speed_pct": "constant_temp_fan_speed_pct",
        "target_humidity_pct": None,
        "operating_submode": None,
    },
}

# Every accumulated-state key `resolve_active_mode` can read: every non-None
# source key across `_ACTIVE_MODE_KEYS` plus `operating_mode`, which selects
# the mode before any source key is read. Exported so the HA layer can guard
# its own accumulated-state passthrough against this table instead of
# hand-copying it (PLAN-047 review, finding item 5).
WAVE3_ACTIVE_MODE_INPUTS: frozenset[str] = frozenset(
    {"operating_mode"}
    | {
        source_key
        for key_map in _ACTIVE_MODE_KEYS.values()
        for source_key in key_map.values()
        if source_key is not None
    }
)

_ACTIVE_MODE_EMPTY: dict[str, Any] = {
    "target_temp_c": None,
    "airflow_speed_pct": None,
    "target_humidity_pct": None,
    "operating_submode": None,
}


def _tidy_float(value: float) -> float:
    """Round a float32 reading to two decimals.

    The device sends single-precision floats: a setpoint of 19.55 arrives as
    19.549999237060547, which a sensor hides behind its display precision and
    a number entity shows exactly as it is. Two decimals keep every value the
    unit can express at the resolution its app shows (0.5 K, whole percent,
    0.01 A) and nothing of the representation noise.
    """
    return round(value, 2)


def _decode_mode_info(raw: bytes) -> dict[str, Any]:
    """Decode wave_mode_info (field 514) into its thirteen keys.

    Every key is initialized to `None` before the walk, so a mode entry
    that is present but empty (position 0 in every capture) or a field
    missing from an otherwise-present entry both come out as an explicit
    `None`, never a missing key.

    The entry's position - not its ordinal among every field the record
    holds - is what selects the mode, so the counter only advances on an
    accepted entry (field 1, wire type 2); a stray field before the list
    no longer shifts every mode by one (PLAN-047 review, finding A2). And
    a record whose entry count is not exactly `_MODE_INFO_ENTRY_COUNT` is
    a list this map was not built for, so it is left unlabelled rather
    than guessed at (finding A3).
    """
    result: dict[str, Any] = dict.fromkeys(_MODE_INFO_ALL_KEYS)

    entries: list[bytes] = [
        item for field_num, wire_type, item in _iter_fields(raw) if field_num == 1 and wire_type == 2
    ]
    if len(entries) != _MODE_INFO_ENTRY_COUNT:
        return result

    for position, item in enumerate(entries):
        entry_fields = _MODE_INFO_ENTRY_FIELDS.get(position)
        if entry_fields is None:
            continue
        for sub_num, sub_wire, sub_raw in _iter_fields(item):
            mapping = entry_fields.get(sub_num)
            if mapping is None:
                continue
            key, scalar_type = mapping
            value = _decode_scalar(sub_wire, sub_raw, scalar_type)
            if value is None:
                continue
            if scalar_type is _TYPE_FLOAT:
                value = _tidy_float(value)
            if key in _SUBMODE_KEYS:
                result[key] = _SUBMODE_NAMES.get(int(value))
            else:
                result[key] = value

    return result


def _decode_errcode_list(raw: bytes) -> bool:
    """Decode dev_errcode_list (field 627) into a single `fault` flag.

    Field 1 inside the record is a packed repeated uint32 error-code list;
    `fault` is true when any code in it is non-zero.
    """
    for field_num, _wire_type, packed in _iter_fields(raw):
        if field_num != 1:
            continue
        mv = memoryview(packed)
        pos = 0
        while pos < len(mv):
            value, pos = _read_varint(mv, pos)
            if value is None:
                break
            if value:
                return True
    return False


def _decode_mapped_fields(
    pdata: bytes,
    field_map: dict[int, tuple[str, str]],
) -> dict[str, Any]:
    """Decode the mapped scalars plus the two nested records at 514/627."""
    result: dict[str, Any] = {}

    for field_num, wire_type, raw in _iter_fields(pdata):
        if field_num == _MODE_INFO_FIELD and wire_type == 2:
            result.update(_decode_mode_info(raw))
            continue
        if field_num == _ERRCODE_LIST_FIELD and wire_type == 2:
            result["fault"] = _decode_errcode_list(raw)
            continue

        mapping = field_map.get(field_num)
        if mapping is None:
            continue

        sensor_key, scalar_type = mapping
        value = _decode_scalar(wire_type, raw, scalar_type)
        if value is not None and scalar_type is _TYPE_FLOAT:
            value = _tidy_float(value)
        if value is not None:
            result[sensor_key] = value

    return result


def _finalize(parsed: dict[str, Any]) -> dict[str, Any]:
    """Normalize near-zero floats and resolve the raw enum/boolean fields."""
    result = dict(parsed)

    for key, value in list(result.items()):
        if isinstance(value, float) and isfinite(value) and abs(value) < _FLOAT_ZERO_EPS:
            result[key] = 0.0

    sleep_raw = result.pop("_dev_sleep_state_raw", None)
    if isinstance(sleep_raw, int):
        result["running"] = not bool(sleep_raw)

    for raw_key, sensor_key in _BOOL_RAW_KEYS.items():
        raw_value = result.pop(raw_key, None)
        if isinstance(raw_value, int):
            result[sensor_key] = bool(raw_value)

    mode_raw = result.pop("_operating_mode_raw", None)
    if isinstance(mode_raw, int):
        result["operating_mode"] = _OPERATING_MODE_NAMES.get(mode_raw)

    mood_raw = result.pop("_mood_light_mode_raw", None)
    if isinstance(mood_raw, int):
        result["mood_light_mode"] = _MOOD_LIGHT_MODE_NAMES.get(mood_raw)

    display_raw = result.pop("_display_temperature_source_raw", None)
    if isinstance(display_raw, int):
        result["display_temperature_source"] = _DISPLAY_TEMPERATURE_SOURCE_NAMES.get(display_raw)

    drainage_raw = result.pop("_drainage_mode_raw", None)
    if isinstance(drainage_raw, int):
        result["automatic_drainage"] = _AUTOMATIC_DRAINAGE_NAMES.get(drainage_raw)

    firmware_raw = result.pop("_pd_firm_ver_raw", None)
    if isinstance(firmware_raw, int) and firmware_raw:
        result["firmware_version"] = decode_version(firmware_raw)

    return result


def parse_wave3_message(payload: bytes) -> dict[str, Any] | None:
    """Parse a WAVE 3 (AC71) protobuf frame into flat sensor keys."""
    try:
        headers, _ = decode_header_message(payload)
        if not headers:
            return None

        merged: dict[str, Any] = {}
        for header in headers:
            cmd_key = (int(header.get("cmd_func", -1)), int(header.get("cmd_id", -1)))
            field_map = _WAVE3_FIELD_MAP.get(cmd_key)
            if field_map is None:
                continue
            for pdata in _pdata_candidates(header):
                try:
                    decoded = _decode_mapped_fields(pdata, field_map)
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


def resolve_active_mode(state: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve the four generic active-mode keys from accumulated device state.

    The device always sends the full per-mode configuration in `514`, so
    `state` carries all five modes' settings at once; `operating_mode`
    selects which group is the one currently in effect. Pure and HA-free:
    the coordinator/sensor layer is expected to call this with the merged
    device state, never a single message's fields.
    """
    if "operating_mode" not in state:
        return {}

    mode = state["operating_mode"]
    if mode is None:
        return dict(_ACTIVE_MODE_EMPTY)

    key_map = _ACTIVE_MODE_KEYS.get(mode)
    if key_map is None:
        return dict(_ACTIVE_MODE_EMPTY)

    return {
        out_key: (state.get(source_key) if source_key is not None else None)
        for out_key, source_key in key_map.items()
    }
