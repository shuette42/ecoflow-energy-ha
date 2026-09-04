"""Tests for the EcoFlow Solar Tracker (HZ31 / S02F) protobuf parser.

Every expectation below is a number one of the two reporter's trackers put
on the wire on 2026-09-02 (#339), decoded straight from the captured hex,
not recomputed from the parser under test - a test that derives its
expectation from the code it is supposed to check holds for any field map.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ecoflow_energy.ecoflow.parsers.solar_tracker_proto import (
    _TRACKING_MODE,
    parse_solar_tracker_message,
)

FIXTURE = Path(__file__).parent / "fixtures" / "solar_tracker" / "hz31_s02f_frames_issue339.json"
STREAM_FIXTURE = Path(__file__).parent / "fixtures" / "stream" / "bk01_capture_masked.json"


def _frames(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())["frames"]


def _frame(tag: str, ts_prefix: str, topic: str = "property") -> dict[str, Any]:
    for frame in _frames(FIXTURE):
        if frame["tag"] == tag and frame["topic"] == topic and frame["ts_iso"].startswith(ts_prefix):
            return frame
    raise AssertionError(f"no fixture frame for tag={tag!r} ts_prefix={ts_prefix!r} topic={topic!r}")


def _payload(frame: dict[str, Any]) -> bytes:
    return bytes.fromhex(frame["hex"])


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _value_span_of_first_field(data: bytes, target_field: int) -> tuple[int, int]:
    """Return the (start, end) byte range of one length-delimited field's value.

    One protobuf level only, matching the shape `decode_header_message` and
    `_iter_fields` walk: the outer frame wraps a header in field 1, and the
    header wraps `pdata` in field 1 again.
    """
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field_num, wire_type = tag >> 3, tag & 0x07
        if wire_type == 2:
            length, pos = _read_varint(data, pos)
            value_start, value_end = pos, pos + length
            if field_num == target_field:
                return value_start, value_end
            pos = value_end
        elif wire_type == 0:
            _, pos = _read_varint(data, pos)
        elif wire_type == 5:
            pos += 4
        elif wire_type == 1:
            pos += 8
        else:
            raise ValueError(wire_type)
    raise AssertionError(f"field {target_field} not found")


def _single_byte_varint_span(pdata: bytes, target_field: int) -> tuple[int, int]:
    """Return the (start, end) byte range of a field whose varint value fits one byte."""
    pos = 0
    while pos < len(pdata):
        start = pos
        tag, pos = _read_varint(pdata, pos)
        field_num, wire_type = tag >> 3, tag & 0x07
        if wire_type == 0:
            _, pos = _read_varint(pdata, pos)
        elif wire_type == 2:
            length, pos = _read_varint(pdata, pos)
            pos += length
        elif wire_type == 5:
            pos += 4
        elif wire_type == 1:
            pos += 8
        else:
            raise ValueError(wire_type)
        if field_num == target_field:
            if wire_type != 0 or pos - start != 2:
                raise AssertionError("field is not a one-byte varint; helper does not support it")
            return start, pos
    raise AssertionError(f"field {target_field} not found")


def _with_mode_field(hex_str: str, new_mode: int) -> str:
    """Patch a real captured frame's `mode` field (3) to `new_mode`.

    Only supports a replacement value that, like every mode value on the
    wire, fits in a single varint byte (< 128): the byte length of the
    frame does not change, so no surrounding length prefix needs updating.
    """
    if not 0 <= new_mode < 0x80:
        raise ValueError("helper only supports single-byte varint values")

    raw = bytearray(bytes.fromhex(hex_str))
    header_start, header_end = _value_span_of_first_field(bytes(raw), 1)
    header_bytes = bytes(raw[header_start:header_end])
    pdata_start_local, pdata_end_local = _value_span_of_first_field(header_bytes, 1)
    pdata_start_abs = header_start + pdata_start_local
    pdata_bytes = bytes(raw[pdata_start_abs : header_start + pdata_end_local])

    field_start_local, field_end_local = _single_byte_varint_span(pdata_bytes, 3)
    value_byte_abs = pdata_start_abs + field_end_local - 1
    raw[value_byte_abs] = new_mode
    return bytes(raw).hex()


class TestFixture:
    def test_the_fixture_carries_the_capture_it_claims_to(self) -> None:
        """A shrunk or re-cut fixture must not quietly narrow the tests."""
        frames = _frames(FIXTURE)
        assert len(frames) == 9

        tags = {frame["tag"] for frame in frames}
        assert {"T1", "T2"} <= tags
        assert {"S02F", "HZ31"} <= tags


class TestSolarTrackerParser:
    def test_a_manual_rest_frame_decodes_six_readings(self) -> None:
        frame = _frame("T1", "2026-09-02T13:15:03")

        result = parse_solar_tracker_message(_payload(frame))

        assert result == {
            "tilt_angle_deg": 20,
            "target_angle_deg": 20,
            "optimal_angle_deg": None,
            "light_level": 185592,
            "tracking_mode": "manual",
            "battery_pct": 97,
        }

    def test_the_optimal_angle_sentinel_is_an_explicit_unknown(self) -> None:
        frame = _frame("T1", "2026-09-02T13:15:03")

        result = parse_solar_tracker_message(_payload(frame))

        assert result is not None
        assert "optimal_angle_deg" in result
        assert result["optimal_angle_deg"] is None

    def test_the_optimal_angle_carries_the_offset_when_present(self) -> None:
        frame = _frame("T1", "2026-09-02T13:23:27")

        result = parse_solar_tracker_message(_payload(frame))

        assert result is not None
        assert result["optimal_angle_deg"] == 58

    def test_auto_mode_reads_auto_and_keeps_the_manual_setpoint(self) -> None:
        frame = _frame("T1", "2026-09-02T13:33:40")

        result = parse_solar_tracker_message(_payload(frame))

        assert result is not None
        assert result["tracking_mode"] == "auto"
        assert result["target_angle_deg"] == 10
        assert result["tilt_angle_deg"] == 80

    def test_an_unmapped_mode_value_becomes_unknown_without_raising(self) -> None:
        frame = _frame("T1", "2026-09-02T13:15:03")
        mutated_hex = _with_mode_field(frame["hex"], 7)

        result = parse_solar_tracker_message(bytes.fromhex(mutated_hex))

        assert result is not None
        assert result["tracking_mode"] is None
        # Every other key from the untouched frame stays intact.
        assert result["tilt_angle_deg"] == 20
        assert result["target_angle_deg"] == 20
        assert result["optimal_angle_deg"] is None
        assert result["light_level"] == 185592
        assert result["battery_pct"] == 97

    def test_a_get_reply_bundle_decodes_once(self) -> None:
        """Two identical headers in one bundle must not double any value.

        The bundle is the reporter's own get_reply at 12:25:45, holding the
        same reading twice - it decodes to the same result a single one of
        those headers would produce alone.
        """
        frame = _frame("T1", "2026-09-02T12:25:45", topic="get_reply")
        assert len(frame["cmds"]) == 2

        result = parse_solar_tracker_message(_payload(frame))

        assert result == {
            "tilt_angle_deg": 20,
            "target_angle_deg": 20,
            "optimal_angle_deg": None,
            "light_level": 233394,
            "tracking_mode": "manual",
            "battery_pct": 98,
        }

    def test_both_prefixes_decode_to_the_same_keys(self) -> None:
        s02f = parse_solar_tracker_message(
            _payload(_frame("S02F", "2026-09-02T13:20:45.202387"))
        )
        hz31 = parse_solar_tracker_message(
            _payload(_frame("HZ31", "2026-09-02T13:20:45.652615"))
        )

        assert s02f is not None
        assert hz31 is not None
        assert set(s02f) == set(hz31)

    def test_a_stream_frame_is_not_a_tracker_frame(self) -> None:
        """A BK01 Stream frame (254/21) must not be read as a tracker (32/1)."""
        stream_frames = _frames(STREAM_FIXTURE)
        assert stream_frames  # positive control: the fixture must carry frames

        for frame in stream_frames:
            result = parse_solar_tracker_message(bytes.fromhex(frame["hex"]))
            assert result is None

    def test_every_frame_stays_inside_the_measured_ranges(self) -> None:
        frames = _frames(FIXTURE)
        assert frames  # positive control: an empty fixture would pass vacuously

        for frame in frames:
            result = parse_solar_tracker_message(_payload(frame))
            assert result is not None, frame["ts_iso"]

            tilt = result["tilt_angle_deg"]
            assert 10 <= tilt <= 85, (frame["ts_iso"], tilt)

            target = result["target_angle_deg"]
            assert 10 <= target <= 85, (frame["ts_iso"], target)

            optimal = result["optimal_angle_deg"]
            if optimal is not None:
                assert 10 <= optimal <= 85, (frame["ts_iso"], optimal)

            assert result["tracking_mode"] in ("manual", "auto"), frame["ts_iso"]
            assert 96 <= result["battery_pct"] <= 100, (frame["ts_iso"], result["battery_pct"])
            assert result["light_level"] > 0, frame["ts_iso"]


class TestTrackingModeIsPinned:
    """Mutation control: the enum is held by this test, not by a comment."""

    def test_a_third_mapped_state_is_read_back_as_that_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frame = _frame("T1", "2026-09-02T13:15:03")
        mutated_hex = _with_mode_field(frame["hex"], 7)
        monkeypatch.setitem(_TRACKING_MODE, 7, "auto")

        result = parse_solar_tracker_message(bytes.fromhex(mutated_hex))

        assert result is not None
        assert result["tracking_mode"] == "auto"
