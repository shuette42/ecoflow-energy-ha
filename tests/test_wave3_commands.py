"""Tests for the EcoFlow WAVE 3 (AC71) SET-frame builders and control table.

Every write vector below is a pdata the app itself put on the wire, captured
2026-09-07 (PLAN-047, issue #161). Expectations are written out per vector
rather than recomputed from the parser, so a test that derives its
expectation from the code under test does not hold for any field map. The
ConfigWriteAck fixture is the device's own reply from the same session.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ecoflow_energy.ecoflow.parsers.stream_proto import _iter_fields
from ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from ecoflow_energy.ecoflow.wave3_commands import (
    WAVE3_CONTROLS,
    WAVE3_POWER_ON_FIELD,
    WAVE3_STANDBY_FIELD,
    Wave3WriteRefused,
    build_power_write,
    build_write,
    parse_config_write_ack,
    write_refusal,
)

FIXTURE = Path(__file__).parent / "fixtures" / "wave3" / "ac71_set_reply_20260907.json"
DEVICE_SN = "X" * 16

# (control key, value, expected pdata hex) - every write vector the app
# itself sent, captured 2026-09-07 (PLAN-047, issue #161).
_WRITE_VECTORS: tuple[tuple[str, Any, str], ...] = (
    ("operating_mode", "cooling", "c80901"),
    ("operating_mode", "dehumidify", "c80904"),
    ("target_temp_c", 27.0, "e5090000d841"),
    ("target_temp_c", 18.5, "e50900009441"),
    ("airflow_speed_pct", 60, "d8093c"),
    ("operating_submode", "max", "d00902"),
    ("target_humidity_pct", 49.0, "ed0900004442"),
    ("beep_enabled", False, "4800"),
    ("mood_light_mode", "screen_time", "880a02"),
    ("display_temperature_source", "ambient", "900a00"),
    ("pet_care_enabled", True, "980a01"),
    ("pet_care_warning_temp_c", 35.0, "a50a00000c42"),
    ("automatic_drainage", True, "800a01"),
    ("screen_off_time_s", 10, "600a"),
    ("screen_brightness_pct", 54, "7036"),
)

# Power is not a WAVE3_CONTROLS entry - build_power_write gets its own pair.
_POWER_VECTORS: tuple[tuple[bool, str], ...] = (
    (False, "e00a01"),  # standby
    (True, "2001"),  # power on
)


def _pdata_hex(payload: bytes) -> str:
    headers, _ = decode_header_message(payload)
    return headers[0]["pdata"]


def test_every_captured_control_vector_round_trips() -> None:
    """T1: build_write reproduces the app's own pdata bytes for every vector."""
    for key, value, expected_hex in _WRITE_VECTORS:
        payload = build_write(key, value, DEVICE_SN)
        assert _pdata_hex(payload) == expected_hex, key


def test_every_captured_power_vector_round_trips() -> None:
    """T1 (power half): build_power_write matches both action triggers."""
    for turn_on, expected_hex in _POWER_VECTORS:
        payload = build_power_write(turn_on, DEVICE_SN)
        assert _pdata_hex(payload) == expected_hex, turn_on


def test_the_header_matches_the_reference_frame() -> None:
    """T2: header shape (dest=66, src=32, seq, serial) matches the self-test
    frame in scripts/probe/probe_wave3_set.py, proven against hardware. The
    reference carries pdata `2001` - the power-on vector."""
    payload = build_power_write(True, DEVICE_SN, seq=165)
    expected = (
        "0a390a0220011020184220012801380340fe0148115002580170a501800103"
        "880101ba0103696f73ca011058585858585858585858585858585858"
    )
    assert payload.hex() == expected


def test_every_built_pdata_carries_exactly_one_field() -> None:
    """T3: one field per frame - no control accidentally carries a companion."""
    for key, value, _ in _WRITE_VECTORS:
        payload = build_write(key, value, DEVICE_SN)
        headers, _ = decode_header_message(payload)
        pdata = bytes.fromhex(headers[0]["pdata"])
        assert len(_iter_fields(pdata)) == 1, key


def test_field_3_never_appears_and_standby_is_172() -> None:
    """T4: field 3 (cfg_power_off) is never used; standby is 172, not 3."""
    assert all(control.field != 3 for control in WAVE3_CONTROLS.values())
    assert WAVE3_STANDBY_FIELD == 172
    assert WAVE3_POWER_ON_FIELD == 4
    assert _pdata_hex(build_power_write(False, DEVICE_SN)) == "e00a01"


def test_write_refusal_matrix() -> None:
    """T5: both sides of every app-observed refusal rule.

    R1 - submode is gated to cooling/heating, and `none`/`normal` are
    refused outright because the app never writes them (only max/sleep/eco
    are on record).
    """
    assert write_refusal("operating_submode", "eco", {"operating_mode": "fan"}) is not None
    assert write_refusal("operating_submode", "eco", {"operating_mode": "cooling"}) is None
    assert write_refusal("operating_submode", "none", {"operating_mode": "cooling"}) is not None

    # R2 - fan speed is fixed while the unit is in constant_temp, and 50 is
    # not one of the five speeds the app ever writes.
    assert write_refusal("airflow_speed_pct", 60, {"operating_mode": "constant_temp"}) is not None
    with pytest.raises(Wave3WriteRefused):
        build_write("airflow_speed_pct", 50, DEVICE_SN)
    build_write("airflow_speed_pct", 60, DEVICE_SN)  # does not raise

    # R3 - the setpoint only applies in cooling/heating, stays on the 0.5
    # step grid, and inside 15.5..30.
    assert write_refusal("target_temp_c", 22.0, {"operating_mode": "fan"}) is not None
    with pytest.raises(Wave3WriteRefused):
        build_write("target_temp_c", 31, DEVICE_SN)
    build_write("target_temp_c", 22.0, DEVICE_SN)  # does not raise
    with pytest.raises(Wave3WriteRefused):
        # 22.3 is refused rather than snapped to the grid: a silent snap
        # would let the caller believe the device took the value it asked
        # for, when it took a different one.
        build_write("target_temp_c", 22.3, DEVICE_SN)

    # R4 - humidity only applies in dehumidify, and 85 is outside 40..80.
    assert write_refusal("target_humidity_pct", 60.0, {"operating_mode": "cooling"}) is not None
    with pytest.raises(Wave3WriteRefused):
        build_write("target_humidity_pct", 85, DEVICE_SN)

    # R5 - the four mode-gated keys refuse outright while operating_mode has
    # not been reported yet; a non-gated key like beep_enabled does not care.
    unreported_state: dict[str, Any] = {}
    gated_samples: dict[str, Any] = {
        "operating_submode": "max",
        "target_temp_c": 22.0,
        "target_humidity_pct": 60.0,
        "airflow_speed_pct": 60,
    }
    for key, sample in gated_samples.items():
        assert write_refusal(key, sample, unreported_state) is not None, key
    assert write_refusal("beep_enabled", True, unreported_state) is None


def test_enum_controls_expose_the_full_parser_label_set() -> None:
    """T6: an enum control's label set matches the parser's own name table,
    including labels the app never writes (submode's none/normal) - those
    are refused in write_refusal, not hidden from the table.

    Written out as literal label sets rather than re-imported from
    `wave3_proto` - `WAVE3_CONTROLS.allowed_values` is built from those same
    names via `_invert()`, so comparing against the same dict object is
    circular and can only catch a duplicate-label collapse, never a label
    that is missing from both sides alike (PLAN-047 review lens A, LOW-3).
    """
    expected_labels: dict[str, set[str]] = {
        "operating_mode": {"cooling", "heating", "fan", "dehumidify", "constant_temp"},
        "operating_submode": {"none", "normal", "max", "sleep", "eco"},
        "mood_light_mode": {"off", "on", "screen_time"},
        "display_temperature_source": {"ambient", "outlet"},
    }
    for key, labels in expected_labels.items():
        control = WAVE3_CONTROLS[key]
        assert control.kind == "enum", key
        assert set(control.allowed_values) == labels, key


def test_int_kind_controls_accept_the_float_home_assistant_sends() -> None:
    """T7: Home Assistant's number platform coerces every service value to
    float before calling us, so an int-kind control must accept 54.0 the
    same as 54 - and refuse a genuinely non-integral value instead of
    truncating it silently (HIGH-1, PLAN-047 review lens A). The
    allowed-set check alone does not catch this: 60.0 in (20, 40, 60, 80,
    100) is True."""
    assert _pdata_hex(build_write("screen_brightness_pct", 54.0, DEVICE_SN)) == "7036"
    assert _pdata_hex(build_write("airflow_speed_pct", 60.0, DEVICE_SN)) == "d8093c"
    assert _pdata_hex(build_write("screen_off_time_s", 0.0, DEVICE_SN)) == "6000"
    with pytest.raises(Wave3WriteRefused):
        build_write("screen_brightness_pct", 54.5, DEVICE_SN)


def test_float32_writes_refuse_nested_and_submessage() -> None:
    """T8: float32 (the WAVE 3 setpoint fields) cannot combine with a nested
    wrapper or a submessage - those reshape the pdata layout float32 does not
    use, so the builder refuses the combination outright.

    Companions ARE combinable with float32 (PLAN-047 Phase C, ADR-021 D-C6):
    the constant-temperature band write is two float32 fields in one frame,
    encoded on this same path - see
    test_band_write_encodes_two_float32_fields_in_field_order below. This
    test used to assert the opposite (COSMETIC-2, PLAN-047 review lens A);
    that restriction was lifted deliberately once the band write needed it,
    and only the nested/submessage restriction still holds."""
    from ecoflow_energy.ecoflow.energy_stream import build_delta3_config_write_payload

    with pytest.raises(ValueError):
        build_delta3_config_write_payload(156, 27.0, DEVICE_SN, float32=True, nested=True)
    with pytest.raises(ValueError):
        build_delta3_config_write_payload(
            156, 27.0, DEVICE_SN, float32=True, submessage=b"\x01"
        )


def test_band_write_encodes_two_float32_fields_in_field_order() -> None:
    """T9: the constant-temperature band write combines float32 with a
    companion on the same encoder path T8 checks the restrictions of -
    upper (158) first, lower (159) second, matching the app's own traffic
    (PLAN-047 capture analysis, PC-A)."""
    from ecoflow_energy.ecoflow.energy_stream import build_delta3_config_write_payload

    payload = build_delta3_config_write_payload(
        158, 21.9, DEVICE_SN, float32=True, companions=((159, 17.7),)
    )
    assert _pdata_hex(payload) == "f5093333af41fd099a998d41"


def test_the_capture_ack_confirms_the_screen_brightness_write() -> None:
    """T7: the device's own ConfigWriteAck for the screen-brightness write
    (field 14) parses to action_id 14 and config_ok true."""
    frames = json.loads(FIXTURE.read_text())["frames"]
    payload = bytes.fromhex(frames[0]["hex"])
    ack = parse_config_write_ack(payload)
    assert ack is not None
    assert ack.action_id == 14
    assert ack.config_ok == 1
    assert ack.applied is True
