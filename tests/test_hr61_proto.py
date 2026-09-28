"""Tests for the OCEAN Smart Electrical Panel 40 (`HR61`) protobuf parser.

Frames come from a 46-frame capture of a live installation (issue #434), with
serial numbers already masked to `X` runs at the source. A subset needed by
these tests is copied verbatim into `tests/fixtures/hr61/hr61_frames.json`,
keyed by the original capture index (`i`) so a value quoted here can be
checked back against the owner's recording on issue #434.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ecoflow_energy.ecoflow.parsers.hr61_proto import (
    CIRCUIT_COUNT,
    _parse_property,
    parse_hr61_proto_message,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "hr61" / "hr61_frames.json"


def _load_frames() -> dict[int, bytes]:
    frames = json.loads(_FIXTURE_PATH.read_text())
    return {frame["i"]: bytes.fromhex(frame["hex"]) for frame in frames}


_FRAMES = _load_frames()


def _parse(i: int, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Parse frame `i`; with `state`, merge into it the way the coordinator's
    `_device_data.update()` does and return the merged store."""
    frame = parse_hr61_proto_message(_FRAMES[i])
    if state is None:
        result = frame
    else:
        state.update(frame or {})
        result = state
    assert result is not None, f"frame {i} produced no HR61 fields"
    return result


class TestLoadBalance:
    """`load = grid + pv - battery` on two different frames, plus the L1/L2 sum."""

    @pytest.mark.parametrize("frame_i", [5, 16])
    def test_load_equals_grid_plus_pv_minus_battery(self, frame_i: int) -> None:
        data = _parse(frame_i)
        computed_load = (
            data["grid_power_w"] + data["pv_power_w"] - data["battery_power_w"]
        )
        assert computed_load == pytest.approx(data["load_power_w"], abs=0.01)

    @pytest.mark.parametrize("frame_i", [5, 16])
    def test_grid_l1_plus_l2_matches_grid_total(self, frame_i: int) -> None:
        data = _parse(frame_i)
        l1_l2_sum = data["grid_l1_power_w"] + data["grid_l2_power_w"]
        assert l1_l2_sum == pytest.approx(data["grid_power_w"], abs=15.0)

    def test_frame_16_matches_the_decode_note_numbers(self) -> None:
        # From the owner's recording on issue #434: 0 + 2 + 7644.78 = 7646.78
        data = _parse(16)
        assert data["grid_power_w"] == pytest.approx(0.0, abs=0.01)
        assert data["pv_power_w"] == pytest.approx(2.0, abs=0.01)
        assert data["battery_power_w"] == pytest.approx(-7644.78, abs=0.01)
        assert data["load_power_w"] == pytest.approx(7646.78, abs=0.01)


class TestIncrementalMerge:
    """A `254/21` push is incremental: absent keys keep the prior value.

    Frame 11 is a full-state `get_reply`. Frame 15 is a live push that
    carries no top-level field at all (only `LoadChSta` for circuits 12-20) -
    a genuinely keyless frame for `grid_l1_voltage_v`, so the merge is
    exercised rather than assumed. Frame 22 does carry the field, with a
    different value, and must overwrite it.
    """

    def test_key_absent_from_a_push_keeps_its_prior_value(self) -> None:
        state = _parse(11, {})
        assert state["grid_l1_voltage_v"] == pytest.approx(121.0)

        # Frame 15 carries no `956` - confirmed against the capture, not
        # assumed: a push that happened to carry the same value would pass
        # this test even with the merge broken.
        state = _parse(15, state)
        assert state["grid_l1_voltage_v"] == pytest.approx(121.0)

    def test_a_later_push_that_carries_the_key_updates_it(self) -> None:
        state = _parse(11, {})
        state = _parse(15, state)
        state = _parse(22, state)
        assert state["grid_l1_voltage_v"] == pytest.approx(122.0)


class TestCircuitSign:
    """Raw negative (device consumption) becomes exposed positive (drawing)."""

    def test_consuming_circuit_power_is_negated(self) -> None:
        data = _parse(5)
        # circuit 7 on frame 5: raw field `1021.2` = -188.5430908203125
        assert data["circuit_7_power_w"] == pytest.approx(188.5430908203125)


class TestCreateOnReport:
    """Circuits get keys only once their message is seen, never pre-declared."""

    def test_all_reported_circuits_have_keys_and_no_others_do(self) -> None:
        data = _parse(5)
        for n in range(1, CIRCUIT_COUNT + 1):
            assert f"circuit_{n}_voltage_v" in data, f"circuit {n} missing"
            assert f"circuit_{n}_name" in data, f"circuit {n} name missing"
        for n in range(CIRCUIT_COUNT + 1, CIRCUIT_COUNT + 9):
            assert f"circuit_{n}_voltage_v" not in data
            assert f"circuit_{n}_name" not in data


class TestTwoPole:
    """Circuits 38/40 ("OCEAN Pro", a source) stay separate, cross-linked keys."""

    def test_circuits_38_and_40_are_independent_and_cross_linked(self) -> None:
        data = _parse(5)
        assert data["circuit_38_power_w"] == pytest.approx(-1470.8646240234375)
        assert data["circuit_40_power_w"] == pytest.approx(-1250.884033203125)
        assert data["circuit_38_power_w"] != data["circuit_40_power_w"]
        assert data["circuit_38_link"] == 40
        assert data["circuit_40_link"] == 38
        assert "circuit_38_40_power_w" not in data
        assert "circuit_40_38_power_w" not in data


class TestSafetyParams:
    """`254/25` is parsed once and survives a later `254/21`-only push."""

    def test_grid_code_and_nominal_values_survive_a_later_property_push(self) -> None:
        state = _parse(11, {})
        assert state["grid_code"] == 6
        assert state["grid_nominal_voltage_v"] == pytest.approx(120.0)
        assert state["grid_nominal_frequency_hz"] == pytest.approx(60.0)

        # Frame 15 carries no `254/25` header at all.
        state = _parse(15, state)
        assert state["grid_code"] == 6
        assert state["grid_nominal_voltage_v"] == pytest.approx(120.0)
        assert state["grid_nominal_frequency_hz"] == pytest.approx(60.0)


class TestCurrentScaling:
    """Grid L1/L2 current is milliamps on the wire, published in amps."""

    def test_grid_l1_current_is_raw_field_over_1000(self) -> None:
        data = _parse(5)
        # raw field `1485` on frame 5 = 6849
        assert data["grid_l1_current_a"] == pytest.approx(6.849)


class TestPowerZeroFill:
    """A circuit's `.2` (power) is omitted when the reading is exactly zero,
    the same rule already applied to `.3` (current) - not "unchanged"."""

    def test_a_later_full_state_without_dot2_zeroes_the_prior_power(self) -> None:
        # Frame 8 and frame 9 are consecutive real get_reply frames (issue
        # #434): circuit 6 reports raw `1020.2` = -157.98477172851562 on
        # frame 8, then frame 9's circuit 6 message carries no `.2` at all -
        # a real device pair, not a hand-edited one.
        state = _parse(8, {})
        assert state["circuit_6_power_w"] == pytest.approx(157.98477172851562)

        state = _parse(9, state)
        assert state["circuit_6_power_w"] == pytest.approx(0.0)

    def test_circuit_1_gets_a_zero_power_key_on_its_first_report(self) -> None:
        # Circuit 1 never carries `.2` in this capture (it draws 0 W
        # throughout) - unfixed, it never got a `circuit_1_power_w` key at
        # all rather than one holding 0.0.
        data = _parse(5)
        assert data["circuit_1_power_w"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Shapes the recording does not contain, built as raw protobuf. Circuit 5's
# sample block is field 1019 (1014 + 5), its state block field 798 (793 + 5).
# ---------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint(field << 3 | wire)


def _f32(field: int, value: float) -> bytes:
    import struct

    return _tag(field, 5) + struct.pack("<f", value)


def _msg(field: int, body: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(body)) + body


def _sample(voltage: float | None, power: float | None) -> bytes:
    body = b""
    if voltage is not None:
        body += _f32(1, voltage)
    if power is not None:
        body += _f32(2, power)
    return _msg(1019, body)


class TestShapesNotInTheRecording:
    def test_a_breaker_state_block_without_its_state_field_is_off(self) -> None:
        on = _parse_property(_msg(798, _tag(1, 0) + _varint(1)))
        off = _parse_property(_msg(798, _msg(5, b"Garage")))
        assert on["circuit_5_on"] is True
        assert off["circuit_5_on"] is False
        assert off["circuit_5_name"] == "Garage"

    def test_a_blank_circuit_name_is_published_empty(self) -> None:
        parsed = _parse_property(_msg(798, _tag(1, 0) + _varint(1)))
        assert parsed["circuit_5_name"] == ""

    def test_a_nan_power_is_not_taken_for_an_omitted_zero(self) -> None:
        parsed = _parse_property(_sample(121.0, float("nan")))
        assert "circuit_5_power_w" not in parsed

    def test_voltage_is_not_zero_filled(self) -> None:
        parsed = _parse_property(_sample(None, -300.0))
        assert "circuit_5_voltage_v" not in parsed
        assert parsed["circuit_5_power_w"] == 300.0

    def test_a_circuit_missing_from_a_push_keeps_its_values(self) -> None:
        state: dict[str, Any] = {}
        state.update(_parse_property(_sample(121.0, -300.0)))
        state.update(_parse_property(_f32(515, 10.0)))
        assert state["circuit_5_power_w"] == 300.0
        assert state["circuit_5_voltage_v"] == 121.0
