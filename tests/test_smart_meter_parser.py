"""Tests for the EcoFlow Smart Meter (BK21) protobuf parser.

Every expectation below is a number the reporter's meter put on the wire on
2026-08-31 (#331). They are written out per frame rather than recomputed
from the parser, because a test that derives its expectation from the code
under test holds for any field map.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

from ecoflow_energy.ecoflow.parsers.smart_meter_proto import (
    _ENERGY_RECORD_MAP,
    _SMART_METER_FIELD_MAP,
    parse_smart_meter_message,
)
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
    encode_varint,
)

FIXTURE = Path(__file__).parent / "fixtures" / "smart_meter" / "bk21_frames_issue331.json"
STREAM_FIXTURE = Path(__file__).parent / "fixtures" / "stream" / "bk01_capture_masked.json"


def _frames(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())["frames"]


def _payload(index: int) -> bytes:
    return bytes.fromhex(_frames(FIXTURE)[index]["hex"])


def _encode_fixed32_field(field_number: int, value: float) -> bytes:
    tag = (field_number << 3) | 5
    return encode_varint(tag) + struct.pack("<f", value)


def _build_frame(cmd_func: int, cmd_id: int, inner: bytes) -> bytes:
    """Build a minimal unmasked EcoFlow header frame."""
    header = bytearray()
    header.extend(encode_field_bytes(1, inner))
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    return encode_field_bytes(1, bytes(header))


# Frame index -> the reading the meter reported in that frame. Three shapes
# occur: the 24-byte incremental upload, the 47-byte one that adds the
# energy record, and the 146-byte full upload. The bundled `get_reply`
# frames (1, 9, 10) carry a full upload unmasked next to an upload-period
# message.
_INCREMENTAL = {
    0: {"grid_w": 328.6029968261719, "grid_l1_w": 0.0,
        "grid_l2_w": 239.2700653076172, "grid_l3_w": 89.33292388916016},
    2: {"grid_w": 333.8434143066406, "grid_l1_w": 0.0,
        "grid_l2_w": 244.86236572265625, "grid_l3_w": 88.98104095458984},
    5: {"grid_w": 432.6658020019531, "grid_l1_w": 0.0,
        "grid_l2_w": 343.8987121582031, "grid_l3_w": 88.76708984375},
    8: {"grid_w": 426.4959716796875, "grid_l1_w": 0.0,
        "grid_l2_w": 338.1103210449219, "grid_l3_w": 88.38567352294922},
    13: {"grid_w": 355.0791931152344, "grid_l1_w": 0.0,
         "grid_l2_w": 266.21881103515625, "grid_l3_w": 88.86038208007812},
    14: {"grid_w": 333.9139404296875, "grid_l1_w": 0.0,
         "grid_l2_w": 245.07046508789062, "grid_l3_w": 88.84347534179688},
}

_WITH_ENERGY_RECORD = {
    6: {"grid_w": 432.4347839355469, "grid_l1_w": 0.0,
        "grid_l2_w": 342.93017578125, "grid_l3_w": 89.50462341308594,
        "grid_l2_energy_today_wh": 967.0, "grid_l3_energy_today_wh": 411.0,
        "grid_energy_today_wh": 1378.0, "grid_energy_total_wh": 1378.0},
    11: {"grid_w": 420.10107421875, "grid_l1_w": 0.0,
         "grid_l2_w": 331.5275573730469, "grid_l3_w": 88.57351684570312,
         "grid_l2_energy_today_wh": 992.0, "grid_l3_energy_today_wh": 417.0,
         "grid_energy_today_wh": 1409.0, "grid_energy_total_wh": 1409.0},
}

# The 146-byte full upload, frame 4, and the two bundles that followed it
# eleven minutes later. Those two carry the same voltages, currents and
# counters but different per-phase power, so they are not one reading sent
# twice.
_FULL = {
    4: {"grid_w": 406.6517333984375,
        "grid_l1_w": 0.0, "grid_l2_w": 317.8153991699219,
        "grid_l3_w": 88.83634185791016,
        "grid_l1_voltage_v": 239.96511840820312,
        "grid_l2_voltage_v": 239.4418182373047,
        "grid_l3_voltage_v": 240.87403869628906,
        "grid_l1_current_a": 0.0,
        "grid_l2_current_a": 2.1072933673858643,
        "grid_l3_current_a": 0.8354451060295105,
        "grid_l2_energy_today_wh": 941.0, "grid_l3_energy_today_wh": 404.0,
        "grid_energy_today_wh": 1345.0, "grid_energy_total_wh": 1345.0,
        "grid_power_factor": 0.0, "grid_connection_state": "grid_in",
        "grid_l1_connected": True, "grid_l2_connected": True,
        "grid_l3_connected": True},
    1: {"grid_w": 328.6647033691406,
        "grid_l1_w": 0.0, "grid_l2_w": 240.10012817382812,
        "grid_l3_w": 88.56456756591797,
        "grid_l1_voltage_v": 240.170166015625,
        "grid_l2_voltage_v": 239.49620056152344,
        "grid_l3_voltage_v": 241.2024383544922,
        "grid_l1_current_a": 0.0,
        "grid_l2_current_a": 1.789912462234497,
        "grid_l3_current_a": 0.8386775255203247,
        "grid_l2_energy_today_wh": 923.0, "grid_l3_energy_today_wh": 398.0,
        "grid_energy_today_wh": 1321.0, "grid_energy_total_wh": 1321.0,
        "grid_power_factor": 0.0, "grid_connection_state": "grid_in",
        "grid_l1_connected": True, "grid_l2_connected": True,
        "grid_l3_connected": True},
    9: {"grid_w": 429.0445251464844,
        "grid_l1_w": 0.0, "grid_l2_w": 340.832275390625,
        "grid_l3_w": 88.21222686767578,
        "grid_l1_voltage_v": 239.77590942382812,
        "grid_l2_voltage_v": 239.911376953125,
        "grid_l3_voltage_v": 241.05319213867188,
        "grid_l1_current_a": 0.0,
        "grid_l2_current_a": 2.2456891536712646,
        "grid_l3_current_a": 0.8519787788391113,
        "grid_l2_energy_today_wh": 984.0, "grid_l3_energy_today_wh": 415.0,
        "grid_energy_today_wh": 1399.0, "grid_energy_total_wh": 1399.0,
        "grid_power_factor": 0.0, "grid_connection_state": "grid_in",
        "grid_l1_connected": True, "grid_l2_connected": True,
        "grid_l3_connected": True},
    10: {"grid_w": 429.0445251464844,
         "grid_l1_w": 0.0, "grid_l2_w": 338.96844482421875,
         "grid_l3_w": 89.3239517211914,
         "grid_l1_voltage_v": 239.77590942382812,
         "grid_l2_voltage_v": 239.911376953125,
         "grid_l3_voltage_v": 241.05319213867188,
         "grid_l1_current_a": 0.0,
         "grid_l2_current_a": 2.2456891536712646,
         "grid_l3_current_a": 0.8519787788391113,
         "grid_l2_energy_today_wh": 984.0, "grid_l3_energy_today_wh": 415.0,
         "grid_energy_today_wh": 1399.0, "grid_energy_total_wh": 1399.0,
         "grid_power_factor": 0.0, "grid_connection_state": "grid_in",
         "grid_l1_connected": True, "grid_l2_connected": True,
         "grid_l3_connected": True},
}

_EXPECTED = {**_INCREMENTAL, **_WITH_ENERGY_RECORD, **_FULL}

# Upload periods only, nothing readable.
_RUNTIME_ONLY = (3, 7, 12)


def _assert_matches(result: dict[str, Any] | None, expected: dict[str, Any]) -> None:
    assert result is not None
    assert set(result) == set(expected)
    for key, want in expected.items():
        got = result[key]
        if isinstance(want, bool):
            # `is`, not `==`: a raw 1 from the wire would satisfy `== True`
            # and reach a binary sensor as an int.
            assert got is want, key
        elif isinstance(want, float):
            assert got == pytest.approx(want, rel=1e-9), key
        else:
            assert got == want, key


class TestFixture:
    def test_the_fixture_carries_the_capture_it_claims_to(self) -> None:
        """A shrunk or re-cut fixture must not quietly narrow the tests."""
        frames = _frames(FIXTURE)
        assert len(frames) == 15

        property_21 = [
            f for f in frames
            if f["topic"] == "property" and f["cmds"] == [{"cmd_func": 254, "cmd_id": 21}]
        ]
        property_22 = [
            f for f in frames
            if f["topic"] == "property" and f["cmds"] == [{"cmd_func": 254, "cmd_id": 22}]
        ]
        get_reply = [f for f in frames if f["topic"] == "get_reply"]

        assert len(property_21) == 9
        assert len(property_22) == 3
        assert len(get_reply) == 3
        assert all(len(f["cmds"]) == 2 for f in get_reply)


class TestSmartMeterParser:
    @pytest.mark.parametrize("index", sorted(_EXPECTED))
    def test_frame_decodes_to_the_reported_reading(self, index: int) -> None:
        _assert_matches(parse_smart_meter_message(_payload(index)), _EXPECTED[index])

    def test_the_full_upload_carries_the_whole_meter(self) -> None:
        """Frame 4, the 146-byte full upload, is the widest frame captured."""
        result = parse_smart_meter_message(_payload(4))

        assert result is not None
        assert len(result) == 19
        assert result["grid_w"] == pytest.approx(406.65173, rel=1e-6)
        # The phases do not multiply out: 239.44 V at 2.107 A against
        # 317.8 W on L2. That is the meter separating apparent from active
        # power, and no factor may be applied to reconcile them.
        assert result["grid_l2_voltage_v"] == pytest.approx(239.44182, rel=1e-6)
        assert result["grid_l2_current_a"] == pytest.approx(2.1072934, rel=1e-6)
        assert result["grid_l2_w"] == pytest.approx(317.8154, rel=1e-6)
        assert result["grid_connection_state"] == "grid_in"

    def test_the_bundled_get_reply_frames_decode_unmasked(self) -> None:
        """The three bundles carry no enc_type, so the plain bytes are used.

        A masked-only reader would return None for all three, and the
        integration would lose the only frames that arrive on request.
        """
        for index in (1, 9, 10):
            result = parse_smart_meter_message(_payload(index))
            assert result is not None, index
            assert result["grid_connection_state"] == "grid_in"

    @pytest.mark.parametrize("index", _RUNTIME_ONLY)
    def test_upload_period_frames_carry_no_reading(self, index: int) -> None:
        """254/22 is upload periods only and is deliberately unmapped."""
        assert parse_smart_meter_message(_payload(index)) is None

    def test_the_aggregate_never_comes_from_a_phase(self) -> None:
        """515 is the app's current power; the phases must not sum into it.

        Frame 10 is the one frame in the capture where the meter disagrees
        with itself: it reports 429.0445 W on 515 while its phases add to
        428.2924 W. A parser that summed the phases would pass every other
        assertion in this file and fail here, which is the whole point of
        picking this frame rather than one where the two agree to a watt.
        """
        result = parse_smart_meter_message(_payload(10))
        assert result is not None

        phase_sum = (
            result["grid_l1_w"] + result["grid_l2_w"] + result["grid_l3_w"]
        )
        assert result["grid_w"] == pytest.approx(429.0445251464844, rel=1e-9)
        assert phase_sum == pytest.approx(428.2923965454102, rel=1e-9)
        assert result["grid_w"] != pytest.approx(phase_sum, rel=1e-6)


class TestFieldMapIsPinned:
    """Mutation controls: the map is held by these tests, not by a comment."""

    def test_removing_a_scalar_field_removes_exactly_its_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert "grid_l2_w" in (parse_smart_meter_message(_payload(4)) or {})

        reduced = {
            number: mapping
            for number, mapping in _SMART_METER_FIELD_MAP[(254, 21)].items()
            if number != 963
        }
        monkeypatch.setitem(_SMART_METER_FIELD_MAP, (254, 21), reduced)

        result = parse_smart_meter_message(_payload(4))
        assert result is not None
        assert "grid_l2_w" not in result
        assert "grid_l3_w" in result

    def test_removing_an_energy_subfield_removes_exactly_its_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert "grid_energy_today_wh" in (parse_smart_meter_message(_payload(4)) or {})

        monkeypatch.delitem(_ENERGY_RECORD_MAP, 4)

        result = parse_smart_meter_message(_payload(4))
        assert result is not None
        assert "grid_energy_today_wh" not in result
        assert "grid_energy_total_wh" in result


class TestGuards:
    def test_a_non_254_command_yields_nothing(self) -> None:
        """Only 254/21 is mapped; a battery frame must not be read as a meter."""
        inner = _encode_fixed32_field(515, 1234.0)
        assert parse_smart_meter_message(_build_frame(32, 50, inner)) is None

    def test_an_unknown_grid_state_never_reaches_a_sensor_as_an_integer(self) -> None:
        inner = bytearray()
        inner.extend(_encode_fixed32_field(515, 100.0))
        inner.extend(encode_field_varint(619, 9))

        result = parse_smart_meter_message(_build_frame(254, 21, bytes(inner)))

        assert result is not None
        assert result["grid_connection_state"] is None

    def test_a_zero_lifetime_counter_is_dropped(self) -> None:
        """A lifetime counter at 0 is a glitch, not a reading.

        The daily counters are the opposite case: they reset every night, so
        a zero there is a reading and stays.
        """
        record = bytearray()
        record.extend(_encode_fixed32_field(1, 0.0))
        record.extend(_encode_fixed32_field(4, 0.0))
        record.extend(_encode_fixed32_field(7, 0.0))
        inner = encode_field_bytes(773, bytes(record))

        result = parse_smart_meter_message(_build_frame(254, 21, inner))

        assert result is not None
        assert "grid_energy_total_wh" not in result
        assert result["grid_l1_energy_today_wh"] == 0.0
        assert result["grid_energy_today_wh"] == 0.0

    def test_a_stream_frame_produces_no_meter_specific_reading(self) -> None:
        """A BK31/BK01 Stream frame must not be read as a meter.

        Three fields are genuinely shared with the Stream line, because both
        speak the same BK-series message: the aggregate 515 and the state
        and power factor at 618/619. What separates the two devices is the
        device type that picks the parser, not the field numbers. Everything
        this meter is bought for - the per-phase readings and the import
        counters - must stay absent from a Stream frame, and does.
        """
        meter_only = {
            "grid_l1_w", "grid_l2_w", "grid_l3_w",
            "grid_l1_voltage_v", "grid_l2_voltage_v", "grid_l3_voltage_v",
            "grid_l1_current_a", "grid_l2_current_a", "grid_l3_current_a",
            "grid_l1_energy_today_wh", "grid_l2_energy_today_wh",
            "grid_l3_energy_today_wh",
            "grid_energy_today_wh", "grid_energy_total_wh",
            "grid_l1_connected", "grid_l2_connected", "grid_l3_connected",
        }

        decoded_any = False
        for frame in _frames(STREAM_FIXTURE):
            result = parse_smart_meter_message(bytes.fromhex(frame["hex"]))
            if result is None:
                continue
            decoded_any = True
            assert not meter_only & set(result), sorted(meter_only & set(result))

        # Positive control: without it the loop above passes on a fixture
        # that decoded to nothing at all.
        assert decoded_any
