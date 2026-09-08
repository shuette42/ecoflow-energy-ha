"""WAVE 3 (AC71) control table and SET-frame builders.

The WAVE 3 line writes over the same app WebSocket ConfigWrite envelope as
Delta 3 (`build_delta3_config_write_payload`), but with its own routing id
(header field 3 = 66, not 2) and, for the temperature-shaped fields, a
float32 pdata instead of a varint. Both were measured against the
maintainer's own unit on 2026-09-07 (PLAN-047, issue #161): the self-test
frame in `scripts/probe/probe_wave3_set.py` and four live writes all acked
with this header shape, and the pdata vectors below are the app's own
frames from the same captures.

`WAVE3_CONTROLS` is one table for every writable setting: its ConfigWrite
field number, the shape of value it accepts, and - where the app itself
never sends certain values or gates a control to specific operating modes -
the rule that says so. Those rules come from the app's own handlers as
observed in the public EcoFlow app bundle and from the recorded traffic,
not from guesswork: every allowed value here is a value the app itself was
observed to write, and every excluded one is a value it was never observed
to write.

The table is split across two gates because they need different inputs:

- `build_write` checks a value against its own control - range, step, or
  allowed set - and needs nothing else. It raises `Wave3WriteRefused` for a
  value the control's own table would never accept, regardless of what the
  device is currently doing.
- `write_refusal` checks a value against the device's accumulated state -
  the operating mode a control is gated to, or a value the app's own client
  never writes at all (submode `none`/`normal`). It needs accumulated state
  rather than a single message, because `operating_mode` can arrive on a
  different frame than the write being considered - the same discipline the
  parsers use for aggregates.

A caller sends a write only when both gates pass. Field 3 (`cfg_power_off`)
is deliberately absent from this table: the app's own client never writes
it either; power is always the pair of action triggers in
`WAVE3_POWER_ON_FIELD` / `WAVE3_STANDBY_FIELD`.

This module has no dependency on the HA platform layer - it is part of the
HA-free core library and is exercised directly by `tests/test_wave3_commands.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

from .delta3_commands import (
    parse_config_write_ack,  # noqa: F401 - re-exported for the coordinator
)
from .energy_stream import build_delta3_config_write_payload
from .parsers.wave3_proto import (
    _DISPLAY_TEMPERATURE_SOURCE_NAMES,
    _MOOD_LIGHT_MODE_NAMES,
    _OPERATING_MODE_NAMES,
    _SUBMODE_NAMES,
)

# Header field 3 (dest) for every WAVE 3 write - the Delta 3 default of 2
# would route the frame to the wrong device family.
WAVE3_DEST = 66

# Power is a pair of action triggers, both written as True - not an on/off
# flag. The read-back is `dev_sleep_state` (field 212 of the
# DisplayPropertyUpload), not an echo of either field.
WAVE3_POWER_ON_FIELD = 4
WAVE3_STANDBY_FIELD = 172

# The constant-temperature band: both fields always travel in one ConfigWrite
# (observed in the app's own traffic, PLAN-047 capture analysis). Never
# separately - a caller wanting to move one side has to read the other out
# of accumulated state and send both.
WAVE3_BAND_UPPER_FIELD = 158
WAVE3_BAND_LOWER_FIELD = 159
# The narrowest band in twelve app writes (PC-A frame 89, 21.5/17.5 = 4.0);
# the device has not been observed refusing anything narrower. This is a
# client-side inference from app behaviour rather than a device refusal -
# revisit if an owner reports a narrower band being rejected.
WAVE3_BAND_MIN_WIDTH_K = 4.0
_BAND_MIN_C = 15.5
_BAND_MAX_C = 30.0


class Wave3WriteRefused(ValueError):
    """A value does not fit a control's own table (unknown key, out of range,
    off the step grid, or not one of the allowed values)."""


class Wave3Control(NamedTuple):
    """One writable WAVE 3 setting: its ConfigWrite field and value shape."""

    field: int
    kind: str  # "bool" | "int" | "float" | "enum"
    minimum: float | None
    maximum: float | None
    step: float | None
    allowed_values: tuple[Any, ...] | None
    modes: frozenset[str] | None
    read_key: str


def _invert(names: dict[int, Any]) -> dict[Any, int]:
    """label -> wire code, the reverse of a parser's code -> label table."""
    return {label: code for code, label in names.items()}


_OPERATING_MODE_CODES = _invert(_OPERATING_MODE_NAMES)
_SUBMODE_CODES = _invert(_SUBMODE_NAMES)
_MOOD_LIGHT_CODES = _invert(_MOOD_LIGHT_MODE_NAMES)
_DISPLAY_TEMP_SOURCE_CODES = _invert(_DISPLAY_TEMPERATURE_SOURCE_NAMES)

# Which label->code table an enum control's value is looked up in.
_ENUM_CODE_LOOKUP: dict[str, dict[str, int]] = {
    "operating_mode": _OPERATING_MODE_CODES,
    "operating_submode": _SUBMODE_CODES,
    "mood_light_mode": _MOOD_LIGHT_CODES,
    "display_temperature_source": _DISPLAY_TEMP_SOURCE_CODES,
}

WAVE3_CONTROLS: dict[str, Wave3Control] = {
    "operating_mode": Wave3Control(
        field=153,
        kind="enum",
        minimum=None,
        maximum=None,
        step=None,
        allowed_values=tuple(_OPERATING_MODE_CODES),
        modes=None,
        read_key="operating_mode",
    ),
    "operating_submode": Wave3Control(
        field=154,
        kind="enum",
        minimum=None,
        maximum=None,
        step=None,
        allowed_values=tuple(_SUBMODE_CODES),
        modes=frozenset({"cooling", "heating"}),
        read_key="operating_submode",
    ),
    "airflow_speed_pct": Wave3Control(
        field=155,
        kind="int",
        # allowed_values is the only gate _validate_and_encode evaluates for
        # a control that has one - minimum/maximum are never reached.
        minimum=None,
        maximum=None,
        step=None,
        allowed_values=(20, 40, 60, 80, 100),
        modes=None,  # gated by exclusion (constant_temp), not inclusion - see write_refusal
        read_key="airflow_speed_pct",
    ),
    "target_temp_c": Wave3Control(
        field=156,
        kind="float",
        minimum=15.5,
        maximum=30.0,
        step=0.5,
        allowed_values=None,
        modes=frozenset({"cooling", "heating"}),
        read_key="target_temp_c",
    ),
    "target_humidity_pct": Wave3Control(
        # Wire type 5 (float32), not the varint its "pct" name suggests -
        # the app's own frame for 49% is `ed0900004442`, a float32 49.0
        # (measured 2026-09-07, PLAN-047). Value domain is whole percentages.
        field=157,
        kind="float",
        minimum=40.0,
        maximum=80.0,
        step=1.0,
        allowed_values=None,
        modes=frozenset({"dehumidify"}),
        read_key="target_humidity_pct",
    ),
    "screen_brightness_pct": Wave3Control(
        field=14,
        kind="int",
        minimum=10,
        maximum=100,
        step=1,
        allowed_values=None,
        modes=None,
        read_key="screen_brightness_pct",
    ),
    "beep_enabled": Wave3Control(
        field=9,
        kind="bool",
        minimum=None,
        maximum=None,
        step=None,
        allowed_values=None,
        modes=None,
        read_key="beep_enabled",
    ),
    "display_temperature_source": Wave3Control(
        field=162,
        kind="enum",
        minimum=None,
        maximum=None,
        step=None,
        allowed_values=tuple(_DISPLAY_TEMP_SOURCE_CODES),
        modes=None,
        read_key="display_temperature_source",
    ),
    "mood_light_mode": Wave3Control(
        field=161,
        kind="enum",
        minimum=None,
        maximum=None,
        step=None,
        allowed_values=tuple(_MOOD_LIGHT_CODES),
        modes=None,
        read_key="mood_light_mode",
    ),
    "automatic_drainage": Wave3Control(
        field=160,
        kind="bool",
        minimum=None,
        maximum=None,
        step=None,
        allowed_values=None,
        modes=None,
        read_key="automatic_drainage",
    ),
    "pet_care_enabled": Wave3Control(
        field=163,
        kind="bool",
        minimum=None,
        maximum=None,
        step=None,
        allowed_values=None,
        modes=None,
        read_key="pet_care_enabled",
    ),
    "pet_care_warning_temp_c": Wave3Control(
        field=164,
        kind="float",
        minimum=25.0,
        maximum=45.0,
        step=1.0,
        allowed_values=None,
        modes=None,
        read_key="pet_care_warning_temp_c",
    ),
    "screen_off_time_s": Wave3Control(
        field=12,
        kind="int",
        # allowed_values is the only gate _validate_and_encode evaluates for
        # a control that has one - minimum/maximum are never reached.
        minimum=None,
        maximum=None,
        step=None,
        allowed_values=(10, 30, 60, 300, 600, 0),
        modes=None,
        read_key="screen_off_time_s",
    ),
}

# Submode values the parser can decode but the app's own client never writes
# (no frame of either on record; the app sent 2, 3, 4 - max, sleep, eco).
_SUBMODE_WRITE_REFUSED_LABELS = frozenset({"none", "normal"})

# Controls that need `operating_mode` in accumulated state before their own
# mode gate can even be evaluated - refused outright while it is missing.
_MODE_REQUIRED_KEYS = frozenset(
    {"operating_submode", "target_temp_c", "target_humidity_pct", "airflow_speed_pct"}
)


def _validate_and_encode(key: str, control: Wave3Control, value: Any) -> int | float:
    """Check `value` against `control`'s own table and return its wire form."""
    if control.kind == "bool":
        if not isinstance(value, bool):
            raise Wave3WriteRefused(f"{key} needs a bool, got {value!r}")
        return 1 if value else 0

    if control.kind == "enum":
        codes = _ENUM_CODE_LOOKUP[key]
        if value not in codes:
            raise Wave3WriteRefused(
                f"{key} does not accept {value!r}; allowed: {sorted(codes)}"
            )
        return codes[value]

    # int / float share the same range/step/allowed-set check. allowed_values
    # takes precedence and skips minimum/maximum entirely - the two controls
    # that have one (airflow_speed_pct, screen_off_time_s) carry None for
    # both, so this is not a fallback that happens to never fire, it is the
    # only path for those two.
    if control.allowed_values is not None:
        if value not in control.allowed_values:
            raise Wave3WriteRefused(
                f"{key} does not accept {value!r}; allowed: {control.allowed_values}"
            )
    else:
        if control.minimum is not None and value < control.minimum:
            raise Wave3WriteRefused(f"{key} is below its minimum {control.minimum}")
        if control.maximum is not None and value > control.maximum:
            raise Wave3WriteRefused(f"{key} is above its maximum {control.maximum}")
        if control.step:
            steps = (value - control.minimum) / control.step
            if abs(steps - round(steps)) > 1e-6:
                # Refused rather than snapped to the grid: a silent snap
                # would let the caller believe the device received the
                # value it asked for, when it received a different one.
                raise Wave3WriteRefused(
                    f"{key} must land on a {control.step} step from {control.minimum}"
                )

    if control.kind == "int":
        # Home Assistant's number platform coerces every service value to
        # float before calling (`vol.Coerce(float)`), so 54 arrives as 54.0.
        # The allowed-set check above does not catch this - 60.0 in
        # (20, 40, 60, 80, 100) is True - so encode_varint would receive a
        # float and raise TypeError instead of Wave3WriteRefused. Refuse a
        # genuinely non-integral value here instead of truncating it: a
        # silent truncation would let the caller believe the device
        # received the value it asked for.
        if float(value) != int(value):
            raise Wave3WriteRefused(f"{key} takes whole numbers, got {value!r}")
        return int(value)

    return value


def build_write(key: str, value: Any, device_sn: str, seq: int = 0) -> bytes:
    """Build a WAVE 3 ConfigWrite SET frame for one control.

    Raises `Wave3WriteRefused` when the key is unknown or the value falls
    outside the control's own table (range, step, or allowed set). This does
    NOT evaluate mode-gated rules - those need the device's accumulated
    state and are `write_refusal`'s job. A value can pass here and still be
    refused there.
    """
    control = WAVE3_CONTROLS.get(key)
    if control is None:
        raise Wave3WriteRefused(f"unknown WAVE 3 control: {key!r}")

    wire_value = _validate_and_encode(key, control, value)
    return build_delta3_config_write_payload(
        control.field,
        wire_value,
        device_sn,
        seq=seq,
        source="ios",
        dest=WAVE3_DEST,
        float32=control.kind == "float",
    )


def build_power_write(turn_on: bool, device_sn: str, seq: int = 0) -> bytes:
    """Build the WAVE 3 power frame: field 4 to turn on, field 172 for standby.

    Both are action triggers written as True; the read-back is
    `dev_sleep_state` (field 212 of the DisplayPropertyUpload), not an echo
    of this value. Field 3 (`cfg_power_off`) is deliberately never used -
    the app's own client never writes it either.
    """
    field = WAVE3_POWER_ON_FIELD if turn_on else WAVE3_STANDBY_FIELD
    return build_delta3_config_write_payload(
        field, 1, device_sn, seq=seq, source="ios", dest=WAVE3_DEST
    )


def _validate_band_value(label: str, value: float) -> None:
    """Check one band limit against its range only, no step grid.

    `target_temp_c` (field 156) is on a 0.5 K grid, but the band limits are
    not the same field and are not on that grid: seven of PC-A's twelve
    captured pairs sit on a 0.1 grid instead (21.9/17.7, 22.7/16.9, 25.4/20.1
    among them), and the device acks them. An earlier revision of this
    function enforced the 0.5 grid anyway, carried over from
    `target_temp_c`'s own table, and refused the exact pairs the app itself
    sends (E1, PLAN-047 Phase C review).
    """
    if value < _BAND_MIN_C or value > _BAND_MAX_C:
        raise Wave3WriteRefused(
            f"{label} must be between {_BAND_MIN_C} and {_BAND_MAX_C}"
        )


def build_band_write(lower: float, upper: float, device_sn: str, seq: int = 0) -> bytes:
    """Build the WAVE 3 constant-temperature band ConfigWrite frame.

    Both limits travel in one frame - field 158 (upper) and field 159
    (lower), each a float32, upper always first (observed in the app's own
    traffic, PLAN-047 capture analysis; `build_delta3_config_write_payload`
    sorts by field number, which already produces that order since
    158 < 159). Raises `Wave3WriteRefused` when either value is out of
    range or the band is narrower than `WAVE3_BAND_MIN_WIDTH_K` - the
    caller must not guess a missing side, it must refuse.
    """
    _validate_band_value("upper limit", upper)
    _validate_band_value("lower limit", lower)
    if upper - lower < WAVE3_BAND_MIN_WIDTH_K:
        raise Wave3WriteRefused(
            f"the band must be at least {WAVE3_BAND_MIN_WIDTH_K} degrees wide"
        )
    return build_delta3_config_write_payload(
        WAVE3_BAND_UPPER_FIELD,
        upper,
        device_sn,
        seq=seq,
        companions=((WAVE3_BAND_LOWER_FIELD, lower),),
        source="ios",
        dest=WAVE3_DEST,
        float32=True,
    )


def band_write_refusal(state: Mapping[str, Any]) -> str | None:
    """Return why the app itself would refuse a band write, or None.

    Evaluated against accumulated device state, same reasoning as
    `write_refusal`: the band is only meaningful in `constant_temp`, and
    `operating_mode` can arrive on a different frame than the write under
    consideration.
    """
    mode = state.get("operating_mode")
    if mode != "constant_temp":
        return (
            "the constant temperature band is only writable in constant_temp, "
            f"current mode is {mode!r}"
        )
    return None


def write_refusal(
    key: str, value: Any, state: Mapping[str, Any], mode: str | None = None
) -> str | None:
    """Return why the app itself would refuse this write, or None if it would not.

    Evaluated against accumulated device state, never a single MQTT message -
    `operating_mode` regularly arrives on a different frame than the write
    under consideration. A caller sends a write only once both this and
    `build_write`'s own table check pass.

    `mode` overrides the accumulated `operating_mode` for a gesture that
    switches mode and writes a mode-gated value in the same call
    (`climate.async_set_temperature` with both `hvac_mode` and a setpoint):
    the mode frame goes out first, but its ack has not landed in accumulated
    state yet, so checking the pre-switch mode would refuse a value that
    becomes valid the instant the switch does (E2, PLAN-047 Phase C review).
    Passing the target mode does not exempt the write from the mode gate -
    a target mode that itself has no such value (fan speed switching into
    `constant_temp`, a setpoint switching into `fan`) still refuses.
    """
    control = WAVE3_CONTROLS.get(key)
    if control is None:
        return f"unknown WAVE 3 control: {key!r}"

    if key == "operating_submode" and value in _SUBMODE_WRITE_REFUSED_LABELS:
        return f"{value!r} is never written by the app; observed writes are max, sleep, or eco"

    effective_mode = mode if mode is not None else state.get("operating_mode")

    if key in _MODE_REQUIRED_KEYS and effective_mode is None:
        return f"{key} needs operating_mode, which has not been reported yet"

    if key == "airflow_speed_pct":
        if effective_mode == "constant_temp":
            return "fan speed is fixed in constant_temp mode"
        return None

    if control.modes is not None and effective_mode not in control.modes:
        return (
            f"{key} is only writable in {sorted(control.modes)}, "
            f"current mode is {effective_mode!r}"
        )

    return None
