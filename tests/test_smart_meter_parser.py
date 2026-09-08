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

FIXTURE = (
    Path(__file__).parent / "fixtures" / "smart_meter" / "bk21_frames_issue331.json"
)
STREAM_FIXTURE = (
    Path(__file__).parent / "fixtures" / "stream" / "bk01_capture_masked.json"
)
MIDNIGHT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "smart_meter" / "bk21_midnight_issue331.json"
)


def _frames(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())["frames"]


def _payload(index: int) -> bytes:
    return bytes.fromhex(_frames(FIXTURE)[index]["hex"])


def _midnight_payload(index: int) -> bytes:
    return bytes.fromhex(_frames(MIDNIGHT_FIXTURE)[index]["hex"])


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
    0: {
        "grid_w": 328.6029968261719,
        "grid_l1_w": 0.0,
        "grid_l2_w": 239.2700653076172,
        "grid_l3_w": 89.33292388916016,
    },
    2: {
        "grid_w": 333.8434143066406,
        "grid_l1_w": 0.0,
        "grid_l2_w": 244.86236572265625,
        "grid_l3_w": 88.98104095458984,
    },
    5: {
        "grid_w": 432.6658020019531,
        "grid_l1_w": 0.0,
        "grid_l2_w": 343.8987121582031,
        "grid_l3_w": 88.76708984375,
    },
    8: {
        "grid_w": 426.4959716796875,
        "grid_l1_w": 0.0,
        "grid_l2_w": 338.1103210449219,
        "grid_l3_w": 88.38567352294922,
    },
    13: {
        "grid_w": 355.0791931152344,
        "grid_l1_w": 0.0,
        "grid_l2_w": 266.21881103515625,
        "grid_l3_w": 88.86038208007812,
    },
    14: {
        "grid_w": 333.9139404296875,
        "grid_l1_w": 0.0,
        "grid_l2_w": 245.07046508789062,
        "grid_l3_w": 88.84347534179688,
    },
}

_WITH_ENERGY_RECORD = {
    6: {
        "grid_w": 432.4347839355469,
        "grid_l1_w": 0.0,
        "grid_l2_w": 342.93017578125,
        "grid_l3_w": 89.50462341308594,
        "grid_l2_net_energy_wh": 967.0,
        "grid_l3_net_energy_wh": 411.0,
        "grid_import_energy_wh": 1378.0,
        "grid_net_energy_wh": 1378.0,
        "grid_export_energy_wh": 0.0,
    },
    11: {
        "grid_w": 420.10107421875,
        "grid_l1_w": 0.0,
        "grid_l2_w": 331.5275573730469,
        "grid_l3_w": 88.57351684570312,
        "grid_l2_net_energy_wh": 992.0,
        "grid_l3_net_energy_wh": 417.0,
        "grid_import_energy_wh": 1409.0,
        "grid_net_energy_wh": 1409.0,
        "grid_export_energy_wh": 0.0,
    },
}

# The 146-byte full upload, frame 4, and the two bundles that followed it
# eleven minutes later. Those two carry the same voltages, currents and
# counters but different per-phase power, so they are not one reading sent
# twice.
_FULL = {
    4: {
        "grid_w": 406.6517333984375,
        "grid_l1_w": 0.0,
        "grid_l2_w": 317.8153991699219,
        "grid_l3_w": 88.83634185791016,
        "grid_l1_voltage_v": 239.96511840820312,
        "grid_l2_voltage_v": 239.4418182373047,
        "grid_l3_voltage_v": 240.87403869628906,
        "grid_l1_current_a": 0.0,
        "grid_l2_current_a": 2.1072933673858643,
        "grid_l3_current_a": 0.8354451060295105,
        "grid_l2_net_energy_wh": 941.0,
        "grid_l3_net_energy_wh": 404.0,
        "grid_import_energy_wh": 1345.0,
        "grid_net_energy_wh": 1345.0,
        "grid_export_energy_wh": 0.0,
        "grid_power_factor": None,
        "grid_connection_state": "grid_in",
        "grid_l1_connected": True,
        "grid_l2_connected": True,
        "grid_l3_connected": True,
    },
    1: {
        "grid_w": 328.6647033691406,
        "grid_l1_w": 0.0,
        "grid_l2_w": 240.10012817382812,
        "grid_l3_w": 88.56456756591797,
        "grid_l1_voltage_v": 240.170166015625,
        "grid_l2_voltage_v": 239.49620056152344,
        "grid_l3_voltage_v": 241.2024383544922,
        "grid_l1_current_a": 0.0,
        "grid_l2_current_a": 1.789912462234497,
        "grid_l3_current_a": 0.8386775255203247,
        "grid_l2_net_energy_wh": 923.0,
        "grid_l3_net_energy_wh": 398.0,
        "grid_import_energy_wh": 1321.0,
        "grid_net_energy_wh": 1321.0,
        "grid_export_energy_wh": 0.0,
        "grid_power_factor": None,
        "grid_connection_state": "grid_in",
        "grid_l1_connected": True,
        "grid_l2_connected": True,
        "grid_l3_connected": True,
    },
    9: {
        "grid_w": 429.0445251464844,
        "grid_l1_w": 0.0,
        "grid_l2_w": 340.832275390625,
        "grid_l3_w": 88.21222686767578,
        "grid_l1_voltage_v": 239.77590942382812,
        "grid_l2_voltage_v": 239.911376953125,
        "grid_l3_voltage_v": 241.05319213867188,
        "grid_l1_current_a": 0.0,
        "grid_l2_current_a": 2.2456891536712646,
        "grid_l3_current_a": 0.8519787788391113,
        "grid_l2_net_energy_wh": 984.0,
        "grid_l3_net_energy_wh": 415.0,
        "grid_import_energy_wh": 1399.0,
        "grid_net_energy_wh": 1399.0,
        "grid_export_energy_wh": 0.0,
        "grid_power_factor": None,
        "grid_connection_state": "grid_in",
        "grid_l1_connected": True,
        "grid_l2_connected": True,
        "grid_l3_connected": True,
    },
    10: {
        "grid_w": 429.0445251464844,
        "grid_l1_w": 0.0,
        "grid_l2_w": 338.96844482421875,
        "grid_l3_w": 89.3239517211914,
        "grid_l1_voltage_v": 239.77590942382812,
        "grid_l2_voltage_v": 239.911376953125,
        "grid_l3_voltage_v": 241.05319213867188,
        "grid_l1_current_a": 0.0,
        "grid_l2_current_a": 2.2456891536712646,
        "grid_l3_current_a": 0.8519787788391113,
        "grid_l2_net_energy_wh": 984.0,
        "grid_l3_net_energy_wh": 415.0,
        "grid_import_energy_wh": 1399.0,
        "grid_net_energy_wh": 1399.0,
        "grid_export_energy_wh": 0.0,
        "grid_power_factor": None,
        "grid_connection_state": "grid_in",
        "grid_l1_connected": True,
        "grid_l2_connected": True,
        "grid_l3_connected": True,
    },
}

_EXPECTED: dict[int, dict[str, Any]] = {
    **_INCREMENTAL,
    **_WITH_ENERGY_RECORD,
    **_FULL,
}

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
            f
            for f in frames
            if f["topic"] == "property"
            and f["cmds"] == [{"cmd_func": 254, "cmd_id": 21}]
        ]
        property_22 = [
            f
            for f in frames
            if f["topic"] == "property"
            and f["cmds"] == [{"cmd_func": 254, "cmd_id": 22}]
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
        """Frame 4, the 146-byte full upload, is the widest frame captured.

        The export fill adds `grid_export_energy_wh` to every frame whose
        energy record is present, including this one, where `.6` itself is
        absent from the wire. The power factor is not among the keys: this
        meter sends it as zero while power flows, which is not a reading
        and is dropped (#331).
        """
        result = parse_smart_meter_message(_payload(4))

        assert result is not None
        assert len(result) == 20
        assert result["grid_power_factor"] is None
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

        phase_sum = result["grid_l1_w"] + result["grid_l2_w"] + result["grid_l3_w"]
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
        """`.4` now names the import counter, not "today" (ADR-018): removing
        its map entry must drop `grid_import_energy_wh` alone and leave the
        sibling `.7` (`grid_net_energy_wh`) decoding normally."""
        assert "grid_import_energy_wh" in (parse_smart_meter_message(_payload(4)) or {})

        monkeypatch.delitem(_ENERGY_RECORD_MAP, 4)

        result = parse_smart_meter_message(_payload(4))
        assert result is not None
        assert "grid_import_energy_wh" not in result
        assert "grid_net_energy_wh" in result


class TestPowerFactorZero:
    """A power factor of 0.0 while power is flowing is not a reading.

    Promised to the reporter on #331 (2026-09-03): the meter sends the
    field on every complete upload and reads 0.0 in all of them, on two
    separate installations, with power and current both non-zero. A power
    factor of zero at 319 W is not physically possible, so the device is
    sending an empty field rather than a measurement, and an impossible
    number is worse than no number.

    The sensor stays. It reports nothing rather than zero, and only while
    power is actually flowing, so a genuine zero on an idle meter is
    untouched.
    """

    def test_a_zero_factor_while_power_flows_is_not_published(self) -> None:
        inner = _encode_fixed32_field(515, 319.0) + _encode_fixed32_field(618, 0.0)

        result = parse_smart_meter_message(_build_frame(254, 21, inner))

        assert result is not None
        assert result["grid_w"] == 319.0
        assert result["grid_power_factor"] is None

    def test_a_zero_factor_on_an_idle_meter_is_a_reading(self) -> None:
        """No power, no contradiction - the zero stands."""
        inner = _encode_fixed32_field(515, 0.0) + _encode_fixed32_field(618, 0.0)

        result = parse_smart_meter_message(_build_frame(254, 21, inner))

        assert result is not None
        assert result["grid_power_factor"] == 0.0

    def test_a_real_factor_is_untouched(self) -> None:
        """Positive control: the rule must not eat an ordinary reading."""
        inner = _encode_fixed32_field(515, 319.0) + _encode_fixed32_field(618, 0.92)

        result = parse_smart_meter_message(_build_frame(254, 21, inner))

        assert result is not None
        assert result["grid_power_factor"] == pytest.approx(0.92, rel=1e-6)


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
        """`_LIFETIME_KEYS` narrows to import alone (ADR-018, decision 4): an
        explicit zero on `.4` is still a glitch and is dropped, while a zero
        net (`.7`, `.1`) is a reading a net exporter passes through and is
        published, and an absent `.6` fills to zero rather than staying
        missing.

        Before the rename, `.7` was the lifetime key and `.4`/`.1` were the
        daily ones that kept their zero; this test held the same record and
        proved the opposite drop.
        """
        record = bytearray()
        record.extend(_encode_fixed32_field(1, 0.0))
        record.extend(_encode_fixed32_field(4, 0.0))
        record.extend(_encode_fixed32_field(7, 0.0))
        inner = encode_field_bytes(773, bytes(record))

        result = parse_smart_meter_message(_build_frame(254, 21, inner))

        assert result is not None
        assert "grid_import_energy_wh" not in result
        assert result["grid_l1_net_energy_wh"] == 0.0
        assert result["grid_net_energy_wh"] == 0.0
        assert result["grid_export_energy_wh"] == 0.0

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
            "grid_l1_w",
            "grid_l2_w",
            "grid_l3_w",
            "grid_l1_voltage_v",
            "grid_l2_voltage_v",
            "grid_l3_voltage_v",
            "grid_l1_current_a",
            "grid_l2_current_a",
            "grid_l3_current_a",
            "grid_l1_net_energy_wh",
            "grid_l2_net_energy_wh",
            "grid_l3_net_energy_wh",
            "grid_import_energy_wh",
            "grid_export_energy_wh",
            "grid_net_energy_wh",
            "grid_l1_connected",
            "grid_l2_connected",
            "grid_l3_connected",
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


# The five `773`-carrying records of the midnight capture (#331, @wildnet,
# comment of 2026-09-05), in the fixture's own order: one before local
# midnight, four after. Every value is read directly off the wire (a walk of
# field 773's subfields on the reporter's own hardware), not derived from the
# parser under test - see the module docstring's rule for why.
_MIDNIGHT_RECORDS: list[dict[str, float]] = [
    {
        "grid_l1_net_energy_wh": 3.0,
        "grid_l2_net_energy_wh": 10666.0,
        "grid_l3_net_energy_wh": 16074.0,
        "grid_import_energy_wh": 26771.0,
        "grid_export_energy_wh": 28.0,
        "grid_net_energy_wh": 26743.0,
    },
    {
        "grid_l1_net_energy_wh": 3.0,
        "grid_l2_net_energy_wh": 13663.0,
        "grid_l3_net_energy_wh": 20259.0,
        "grid_import_energy_wh": 33953.0,
        "grid_export_energy_wh": 28.0,
        "grid_net_energy_wh": 33925.0,
    },
    {
        "grid_l1_net_energy_wh": 3.0,
        "grid_l2_net_energy_wh": 13666.0,
        "grid_l3_net_energy_wh": 20267.0,
        "grid_import_energy_wh": 33964.0,
        "grid_export_energy_wh": 28.0,
        "grid_net_energy_wh": 33936.0,
    },
    {
        "grid_l1_net_energy_wh": 3.0,
        "grid_l2_net_energy_wh": 13667.0,
        "grid_l3_net_energy_wh": 20275.0,
        "grid_import_energy_wh": 33973.0,
        "grid_export_energy_wh": 28.0,
        "grid_net_energy_wh": 33945.0,
    },
    {
        "grid_l1_net_energy_wh": 3.0,
        "grid_l2_net_energy_wh": 13668.0,
        "grid_l3_net_energy_wh": 20284.0,
        "grid_import_energy_wh": 33983.0,
        "grid_export_energy_wh": 28.0,
        "grid_net_energy_wh": 33955.0,
    },
]

_ENERGY_KEYS = (
    "grid_l1_net_energy_wh",
    "grid_l2_net_energy_wh",
    "grid_l3_net_energy_wh",
    "grid_import_energy_wh",
    "grid_export_energy_wh",
    "grid_net_energy_wh",
)


class TestMidnightFixture:
    def test_the_midnight_fixture_carries_the_capture_it_claims_to(self) -> None:
        """A shrunk or re-cut fixture must not quietly narrow the tests."""
        frames = _frames(MIDNIGHT_FIXTURE)
        assert len(frames) == 5

        get_reply = [f for f in frames if f["topic"] == "get_reply"]
        property_21 = [
            f
            for f in frames
            if f["topic"] == "property"
            and f["cmds"] == [{"cmd_func": 254, "cmd_id": 21}]
        ]

        assert len(get_reply) == 1
        assert get_reply[0]["cmds"] == [
            {"cmd_func": 254, "cmd_id": 22},
            {"cmd_func": 254, "cmd_id": 21},
        ]
        assert len(property_21) == 4
        # Fixture order is capture order: one record before local midnight
        # (2026-09-03 CEST), four after (2026-09-04 CEST).
        assert [f["ts_iso"] for f in frames] == sorted(f["ts_iso"] for f in frames)


class TestMidnightRecords:
    """The BK21 energy record (`773`) holds six lifetime counters, not a
    lifetime pair and four daily ones - direction decides the class, not
    period (ADR-018, PLAN-123)."""

    @pytest.mark.parametrize("index", range(5))
    def test_the_midnight_records_decode_to_the_reporters_table(
        self, index: int
    ) -> None:
        result = parse_smart_meter_message(_midnight_payload(index))
        assert result is not None

        expected = _MIDNIGHT_RECORDS[index]
        for key, want in expected.items():
            assert result.get(key) == pytest.approx(want), key

    @pytest.mark.parametrize("index", range(5))
    def test_import_minus_export_is_net_in_every_record(self, index: int) -> None:
        result = parse_smart_meter_message(_midnight_payload(index))
        assert result is not None

        assert result["grid_import_energy_wh"] - result["grid_export_energy_wh"] == (
            pytest.approx(result["grid_net_energy_wh"])
        )

    @pytest.mark.parametrize("index", range(5))
    def test_the_phases_sum_to_net_not_to_import(self, index: int) -> None:
        result = parse_smart_meter_message(_midnight_payload(index))
        assert result is not None

        phase_sum = (
            result["grid_l1_net_energy_wh"]
            + result["grid_l2_net_energy_wh"]
            + result["grid_l3_net_energy_wh"]
        )
        assert phase_sum == pytest.approx(result["grid_net_energy_wh"])
        # Every one of the five records carries a non-zero export (28 Wh),
        # so the phases must disagree with import - if they agreed, the
        # parser would be reading the wrong subfield as the phase total.
        assert result["grid_export_energy_wh"] > 0
        assert phase_sum != pytest.approx(result["grid_import_energy_wh"])

    def test_no_counter_falls_across_midnight(self) -> None:
        """None of the six counters may go backwards, in either direction
        the device counts - across the actual local midnight the reporter's
        meter crossed while the capture ran."""
        results = [parse_smart_meter_message(_midnight_payload(i)) for i in range(5)]
        assert all(r is not None for r in results)

        for key in _ENERGY_KEYS:
            values = [r[key] for r in results]
            assert values == sorted(values), (key, values)


class TestExportFill:
    """Decision 4 (ADR-018): absence of `.6` inside a present `773` record
    reads as zero; the fill is `.6` alone."""

    def test_export_absent_from_a_present_record_reads_zero(self) -> None:
        record = bytearray()
        record.extend(_encode_fixed32_field(1, 3.0))
        record.extend(_encode_fixed32_field(2, 100.0))
        record.extend(_encode_fixed32_field(3, 200.0))
        record.extend(_encode_fixed32_field(4, 303.0))
        record.extend(_encode_fixed32_field(7, 303.0))
        inner = encode_field_bytes(773, bytes(record))

        result = parse_smart_meter_message(_build_frame(254, 21, inner))

        assert result is not None
        assert result["grid_export_energy_wh"] == 0.0

    def test_import_absent_from_a_present_record_is_not_filled(self) -> None:
        """The fill is for `.6` alone - a missing `.4` must stay missing,
        or a house that has always exported and never imported would get a
        fabricated import reading of zero."""
        record = bytearray()
        record.extend(_encode_fixed32_field(1, 3.0))
        record.extend(_encode_fixed32_field(2, 100.0))
        record.extend(_encode_fixed32_field(3, 200.0))
        record.extend(_encode_fixed32_field(6, 5.0))
        record.extend(_encode_fixed32_field(7, 295.0))
        inner = encode_field_bytes(773, bytes(record))

        result = parse_smart_meter_message(_build_frame(254, 21, inner))

        assert result is not None
        assert "grid_import_energy_wh" not in result
        # The fields that were on the wire still decode normally.
        assert result["grid_export_energy_wh"] == 5.0
        assert result["grid_net_energy_wh"] == 295.0

    def test_an_undecodable_export_field_is_not_treated_as_absent(self) -> None:
        """Present but unreadable is not the same as missing.

        The fill exists because this encoder omits a zero export, so an
        absent `.6` means no export. A `.6` that is on the wire and does not
        decode says nothing at all, and turning that into a zero writes a
        fabricated reading onto a counter that only ever rises, which Home
        Assistant reads as a meter change.
        """
        record = bytearray()
        record.extend(_encode_fixed32_field(1, 3.0))
        record.extend(_encode_fixed32_field(4, 303.0))
        record.extend(_encode_fixed32_field(7, 303.0))
        # Field 6 present, wire type 2, which `_decode_scalar` refuses.
        record.extend(encode_field_bytes(6, b"\x01\x02"))
        inner = encode_field_bytes(773, bytes(record))

        result = parse_smart_meter_message(_build_frame(254, 21, inner))

        assert result is not None
        assert "grid_export_energy_wh" not in result

    def test_a_record_carrying_no_mapped_counter_publishes_nothing(self) -> None:
        """A record that decodes but holds no counter we read is not a
        reading, and the fill must not invent one.

        Field 5 is on the wire in the vendor's schema and is deliberately
        unmapped. A record carrying only that one would otherwise come back
        as a lone export of zero, which also makes the whole message look
        like it carried data.
        """
        record = bytearray()
        record.extend(_encode_fixed32_field(5, 42.0))
        inner = encode_field_bytes(773, bytes(record))

        result = parse_smart_meter_message(_build_frame(254, 21, inner))

        assert result is None or "grid_export_energy_wh" not in result

    def test_an_explicit_zero_import_is_dropped_and_a_zero_net_is_published(
        self,
    ) -> None:
        """`_LIFETIME_KEYS` narrows to the import key alone (decision 4).

        An explicit zero on import is still a glitch - this encoder omits
        zeros, so an explicit one is not a reading. A zero on a net figure
        (total or per phase) is a reading a net exporter passes through, and
        must be published, not dropped.
        """
        record = bytearray()
        record.extend(_encode_fixed32_field(1, 0.0))
        record.extend(_encode_fixed32_field(2, 0.0))
        record.extend(_encode_fixed32_field(3, 0.0))
        record.extend(_encode_fixed32_field(4, 0.0))
        record.extend(_encode_fixed32_field(6, 0.0))
        record.extend(_encode_fixed32_field(7, 0.0))
        inner = encode_field_bytes(773, bytes(record))

        result = parse_smart_meter_message(_build_frame(254, 21, inner))

        assert result is not None
        assert "grid_import_energy_wh" not in result
        assert result["grid_export_energy_wh"] == 0.0
        assert result["grid_net_energy_wh"] == 0.0
        assert result["grid_l1_net_energy_wh"] == 0.0
        assert result["grid_l2_net_energy_wh"] == 0.0
        assert result["grid_l3_net_energy_wh"] == 0.0
