"""Stream Ultra (BK11) and Ultra X (BK61) AC outlet switches, issues #677, #753.

The fixture holds real frames of one BK11 from the integration's diagnostics:
the app switching AC outlet 1 on and off, the device's acknowledgement of each
write and the telemetry push that reports the new state about two seconds
later. The diagnostics masked the serial bytes as ``X``, so the builder is fed
the same mask and the comparison covers every other byte of the frame.

Outlet 2 was not switched in that recording. Its tests check our own builder
and parser against the outlet 1 frames with the other field number, and say so
in their names (``likely``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ecoflow_energy.const import (
    STREAM_SWITCHES,
    supports_stream_controls,
    supports_stream_outlet_switches,
)
from ecoflow_energy.ecoflow.energy_stream import build_stream_ac_outlet_payload
from ecoflow_energy.ecoflow.parsers.stream_proto import parse_stream_proto_message
from ecoflow_energy.ecoflow.proto_encoding import encode_field_varint

FIXTURE = Path(__file__).parent / "fixtures" / "stream" / "bk11_outlet_toggle_677.json"
MASKED_SN = "X" * 16

# ConfigWrite payload of the app frames: field 380 (tag e0 17), value 1 or 0.
OUTLET_1_ON = bytes.fromhex("e01701")
OUTLET_1_OFF = bytes.fromhex("e01700")
# The same write for field 381 (tag e8 17).
OUTLET_2_ON = bytes.fromhex("e81701")
OUTLET_2_OFF = bytes.fromhex("e81700")
# Read-back tags in the telemetry push: field 980 (a0 3d), field 982 (b0 3d).
READBACK_980 = bytes.fromhex("a03d")
READBACK_982 = bytes.fromhex("b03d")


def _frame(label: str) -> bytes:
    frames = json.loads(FIXTURE.read_text())["frames"]
    return bytes.fromhex(next(f for f in frames if f["label"] == label)["hex"])


class TestOutletGate:
    def test_bk11_and_bk31_get_the_outlet_switches(self) -> None:
        assert supports_stream_outlet_switches("BK31TEST00000001")
        assert supports_stream_outlet_switches("BK11TEST00000001")
        assert supports_stream_outlet_switches("bk11test00000001")

    def test_bk61_gets_the_outlet_switches(self) -> None:
        """Recorded on two Ultra X units (#753)."""
        assert supports_stream_outlet_switches("BK61TEST00000001")
        assert supports_stream_outlet_switches("bk61test00000001")

    @pytest.mark.parametrize("prefix", ["BK01", "BK12", "BK41", "BK51"])
    def test_other_streams_do_not(self, prefix: str) -> None:
        assert not supports_stream_outlet_switches(f"{prefix}TEST00000001")

    def test_empty_serial_is_closed(self) -> None:
        assert not supports_stream_outlet_switches("")

    @pytest.mark.parametrize("prefix", ["BK11", "BK61"])
    def test_outlet_gate_does_not_open_the_bk31_write_gate(self, prefix: str) -> None:
        """Limits, LED and Backup Reserve stay on the BK31 alone."""
        assert not supports_stream_controls(f"{prefix}TEST00000001")
        assert supports_stream_controls("BK31TEST00000001")


class TestOutlet1AgainstTheRecording:
    @pytest.mark.parametrize(
        ("label", "turn_on", "seq"),
        [("app_outlet_1_on", True, 15), ("app_outlet_1_off", False, 16)],
    )
    def test_builder_bytes_equal_the_app_write(
        self, label: str, turn_on: bool, seq: int
    ) -> None:
        ours = build_stream_ac_outlet_payload(1, turn_on, device_sn=MASKED_SN, seq=seq)
        assert ours == _frame(label)

    @pytest.mark.parametrize(
        ("label", "expected"),
        [("readback_outlet_1_on", 1), ("readback_outlet_1_off", 0)],
    )
    def test_readback_push_reports_the_outlet_state(
        self, label: str, expected: int
    ) -> None:
        result = parse_stream_proto_message(_frame(label))
        assert result is not None
        assert result["ac_outlet_1_enabled"] == expected
        assert "ac_outlet_2_enabled" not in result

    def test_switch_state_key_is_the_readback_key(self) -> None:
        outlet_1 = next(d for d in STREAM_SWITCHES if d.key == "ac_outlet_1_switch")
        result = parse_stream_proto_message(_frame("readback_outlet_1_on"))
        assert result is not None
        assert result[outlet_1.state_key] == 1


class TestOutlet2Likely:
    """Same message as outlet 1, field 381; not yet recorded on a BK11."""

    @pytest.mark.parametrize(
        ("label", "turn_on", "seq", "theirs", "ours"),
        [
            ("app_outlet_1_on", True, 15, OUTLET_1_ON, OUTLET_2_ON),
            ("app_outlet_1_off", False, 16, OUTLET_1_OFF, OUTLET_2_OFF),
        ],
    )
    def test_likely_outlet_2_writes_field_381_in_the_same_frame(
        self, label: str, turn_on: bool, seq: int, theirs: bytes, ours: bytes
    ) -> None:
        recorded = _frame(label)
        assert recorded.count(theirs) == 1
        expected = recorded.replace(theirs, ours)
        built = build_stream_ac_outlet_payload(2, turn_on, device_sn=MASKED_SN, seq=seq)
        assert built == expected

    @pytest.mark.parametrize(
        ("label", "expected"),
        [("readback_outlet_1_on", 1), ("readback_outlet_1_off", 0)],
    )
    def test_likely_outlet_2_reads_back_field_982(
        self, label: str, expected: int
    ) -> None:
        """The recorded push with its 980 tag moved to 982, same length."""
        recorded = _frame(label)
        assert recorded.count(READBACK_980) == 1
        result = parse_stream_proto_message(
            recorded.replace(READBACK_980, READBACK_982)
        )
        assert result is not None
        outlet_2 = next(d for d in STREAM_SWITCHES if d.key == "ac_outlet_2_switch")
        assert result[outlet_2.state_key] == expected
        assert "ac_outlet_1_enabled" not in result


BK61_FIXTURE = (
    Path(__file__).parent / "fixtures" / "stream" / "bk61_outlet_toggle_753.json"
)


def _bk61(label: str) -> bytes:
    frames = json.loads(BK61_FIXTURE.read_text())["frames"]
    return bytes.fromhex(next(f for f in frames if f["label"] == label)["hex"])


class TestBk61AgainstTheRecordings:
    """Real frames of two Ultra X units: outlet 2 from the Android app, outlet 1
    from an iOS app and as an acknowledgement, with the read-back pushes."""

    @pytest.mark.parametrize(
        ("label", "turn_on", "seq"),
        [("app_outlet_1_on_ios", True, 9), ("app_outlet_1_off_ios", False, 10)],
    )
    def test_outlet_1_builder_bytes_equal_the_ios_app_write(
        self, label: str, turn_on: bool, seq: int
    ) -> None:
        ours = build_stream_ac_outlet_payload(1, turn_on, device_sn=MASKED_SN, seq=seq)
        assert ours == _bk61(label)

    @pytest.mark.parametrize(
        ("label", "turn_on", "seq", "theirs", "ours"),
        [
            ("app_outlet_1_on_ios", True, 9, OUTLET_1_ON, OUTLET_2_ON),
            ("app_outlet_1_off_ios", False, 10, OUTLET_1_OFF, OUTLET_2_OFF),
        ],
    )
    def test_likely_outlet_2_builder_is_the_ios_frame_with_field_381(
        self, label: str, turn_on: bool, seq: int, theirs: bytes, ours: bytes
    ) -> None:
        """No iOS frame of outlet 2 exists; the Android one below proves 381."""
        expected = _bk61(label).replace(theirs, ours)
        built = build_stream_ac_outlet_payload(2, turn_on, device_sn=MASKED_SN, seq=seq)
        assert built == expected

    @pytest.mark.parametrize(
        ("label", "turn_on", "value_tag", "timestamp"),
        [
            ("app_outlet_2_on", True, OUTLET_2_ON, 1795010282),
            ("app_outlet_2_off", False, OUTLET_2_OFF, 1795010347),
        ],
    )
    def test_android_outlet_2_write_carries_the_field_our_builder_writes(
        self, label: str, turn_on: bool, value_tag: bytes, timestamp: int
    ) -> None:
        """The Android app adds a timestamp (field 6) and its own sequence, our
        builder adds neither (as for the BK11); the outlet field is the same."""
        recorded = _bk61(label)
        built = build_stream_ac_outlet_payload(2, turn_on, device_sn=MASKED_SN, seq=1)
        assert recorded.count(value_tag) == 1
        assert built.count(value_tag) == 1
        field_6 = encode_field_varint(6, timestamp)
        assert recorded.count(field_6) == 1
        assert field_6 not in built

    @pytest.mark.parametrize(
        ("label", "key", "expected"),
        [
            ("readback_outlet_1_on", "ac_outlet_1_enabled", 1),
            ("readback_outlet_2_on", "ac_outlet_2_enabled", 1),
        ],
    )
    def test_readback_push_reports_the_outlet_state(
        self, label: str, key: str, expected: int
    ) -> None:
        result = parse_stream_proto_message(_bk61(label))
        assert result is not None
        assert result[key] == expected

    def test_outlet_1_push_reports_outlet_2_off(self) -> None:
        """The full push after the outlet 1 write still reads 982 as 0."""
        result = parse_stream_proto_message(_bk61("readback_outlet_1_on"))
        assert result is not None
        assert result["ac_outlet_2_enabled"] == 0

    def test_outlet_2_delta_leaves_outlet_1_state_alone(self) -> None:
        """The outlet 2 push of 6 fields has 982 only; no 980 to read."""
        result = parse_stream_proto_message(_bk61("readback_outlet_2_on"))
        assert result is not None
        assert "ac_outlet_1_enabled" not in result

    def test_acknowledgements_echo_the_written_field(self) -> None:
        for label, tag in (
            ("device_ack_outlet_1_on", OUTLET_1_ON),
            ("device_ack_outlet_1_on_ios", OUTLET_1_ON),
            ("device_ack_outlet_1_off_ios", OUTLET_1_OFF),
            ("device_ack_outlet_2_on", OUTLET_2_ON),
            ("device_ack_outlet_2_off", OUTLET_2_OFF),
        ):
            assert _bk61(label).count(tag) == 1, label
