"""PR 1 checks for the Stream work mode and task list (read only).

Fixtures are real status frames (cmd 254/21) from a Stream Ultra (BK11) and a
Stream AC Pro (BK31), recorded on 14 Nov 2026. They keep only field 393 (work
mode) and field 584 (task list); the serial is masked (16 x "X") and the MAC,
region and firmware fields are dropped. The expected values were read off the
frames with a separate decoder and checked against the vendor app, they are
not produced by the parser under test.

App state when the frames were taken: tasks 2 (22:00-06:00, grid and solar) and
3 (06:00-22:00, solar) charge, task 1 (17:00-19:30, 300 W) and task 5
(14:30-17:30, 790 W) discharge, both discharge tasks off.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from ecoflow_energy.ecoflow.parsers.stream_proto import parse_stream_proto_message
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

FIXTURES = Path(__file__).parent / "fixtures" / "stream_bk"
MODE_KEY = "work_mode"
LIST_KEY = "scheduled_tasks"

CHARGE_KEYS = {
    "slot",
    "kind",
    "enabled",
    "start",
    "end",
    "source",
    "power_w",
    "soc_target",
}
DISCHARGE_KEYS = {"slot", "kind", "enabled", "start", "end", "power_w"}


def _frames(prefix: str) -> dict[str, bytes]:
    path = FIXTURES / f"stream_{prefix.lower()}_tasks_masked.json"
    data = json.loads(path.read_text())
    assert data["device_prefix"] == prefix
    return {f["role"]: bytes.fromhex(f["hex"]) for f in data["frames"]}


def _parse(prefix: str, role: str) -> dict:
    result = parse_stream_proto_message(_frames(prefix)[role])
    assert result is not None
    return result


def _status_frame(*fields: bytes) -> bytes:
    """A minimal 254/21 frame around the given encoded fields."""
    pdata = b"".join(fields)
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 254)
        + encode_field_varint(9, 21)
    )
    return encode_field_bytes(1, header)


def _mode_field(sub_field: int, value: int = 1) -> bytes:
    return encode_field_bytes(393, encode_field_varint(sub_field, value))


BK11_CUSTOM_LIST = [
    {
        "slot": 2,
        "kind": "charge",
        "enabled": False,
        "start": "22:00",
        "end": "06:00",
        "source": "grid_and_solar",
        "power_w": 1050,
        "soc_target": 95,
    },
    {
        "slot": 3,
        "kind": "charge",
        "enabled": True,
        "start": "06:00",
        "end": "22:00",
        "source": "solar_only",
        "power_w": 1050,
        "soc_target": 95,
    },
    {
        "slot": 1,
        "kind": "discharge",
        "enabled": False,
        "start": "17:00",
        "end": "19:30",
        "power_w": 300,
    },
    {
        "slot": 5,
        "kind": "discharge",
        "enabled": False,
        "start": "14:30",
        "end": "17:30",
        "power_w": 790,
    },
]

BK11_TASK3_OFF_LIST = [
    {
        "slot": 2,
        "kind": "charge",
        "enabled": False,
        "start": "22:00",
        "end": "06:00",
        "source": "grid_and_solar",
        "power_w": 1050,
        "soc_target": 95,
    },
    {
        "slot": 3,
        "kind": "charge",
        "enabled": False,
        "start": "06:00",
        "end": "22:00",
        "source": "solar_only",
        "power_w": 1050,
        "soc_target": 95,
    },
    {
        "slot": 1,
        "kind": "discharge",
        "enabled": False,
        "start": "17:00",
        "end": "19:30",
        "power_w": 300,
    },
    {
        "slot": 5,
        "kind": "discharge",
        "enabled": False,
        "start": "14:30",
        "end": "17:30",
        "power_w": 790,
    },
]

BK11_TASK2_ON_LIST = [
    {
        "slot": 2,
        "kind": "charge",
        "enabled": True,
        "start": "22:00",
        "end": "06:00",
        "source": "grid_and_solar",
        "power_w": 1050,
        "soc_target": 95,
    },
    {
        "slot": 3,
        "kind": "charge",
        "enabled": False,
        "start": "06:00",
        "end": "22:00",
        "source": "solar_only",
        "power_w": 1050,
        "soc_target": 95,
    },
    {
        "slot": 1,
        "kind": "discharge",
        "enabled": False,
        "start": "17:00",
        "end": "19:30",
        "power_w": 300,
    },
    {
        "slot": 5,
        "kind": "discharge",
        "enabled": False,
        "start": "14:30",
        "end": "17:30",
        "power_w": 790,
    },
]

BK31_CUSTOM_LIST = [
    {
        "slot": 2,
        "kind": "charge",
        "enabled": False,
        "start": "22:00",
        "end": "06:00",
        "source": "grid_and_solar",
        "power_w": 1050,
        "soc_target": 95,
    },
    {
        "slot": 3,
        "kind": "charge",
        "enabled": True,
        "start": "06:00",
        "end": "22:00",
        "source": "solar_only",
        "power_w": 1050,
        "soc_target": 95,
    },
    {
        "slot": 1,
        "kind": "discharge",
        "enabled": False,
        "start": "17:00",
        "end": "19:30",
        "power_w": 300,
    },
    {
        "slot": 5,
        "kind": "discharge",
        "enabled": False,
        "start": "14:30",
        "end": "17:30",
        "power_w": 790,
    },
]

BK31_ALL_OFF_LIST = [
    {
        "slot": 2,
        "kind": "charge",
        "enabled": False,
        "start": "22:00",
        "end": "06:00",
        "source": "grid_and_solar",
        "power_w": 1050,
        "soc_target": 95,
    },
    {
        "slot": 3,
        "kind": "charge",
        "enabled": False,
        "start": "06:00",
        "end": "22:00",
        "source": "solar_only",
        "power_w": 1050,
        "soc_target": 95,
    },
    {
        "slot": 1,
        "kind": "discharge",
        "enabled": False,
        "start": "17:00",
        "end": "19:30",
        "power_w": 300,
    },
    {
        "slot": 5,
        "kind": "discharge",
        "enabled": False,
        "start": "14:30",
        "end": "17:30",
        "power_w": 790,
    },
]


class TestWorkMode:
    @pytest.mark.parametrize(
        ("prefix", "role", "mode"),
        [
            ("BK11", "get_reply_custom_mode", "custom"),
            ("BK11", "get_reply_ai_mode", "ai_mode"),
            ("BK31", "get_reply_custom_mode", "custom"),
        ],
    )
    def test_mode_is_read_from_field_393(
        self, prefix: str, role: str, mode: str
    ) -> None:
        assert _parse(prefix, role)[MODE_KEY] == mode

    def test_sub_field_one_is_self_powered(self) -> None:
        result = parse_stream_proto_message(_status_frame(_mode_field(1)))
        assert result is not None
        assert result[MODE_KEY] == "self_powered"

    def test_an_unknown_sub_field_is_not_published_as_a_number(self) -> None:
        result = parse_stream_proto_message(_status_frame(_mode_field(8)))
        assert result is not None
        assert result[MODE_KEY] is None

    def test_two_flags_set_is_unknown(self) -> None:
        both = encode_field_bytes(
            393, encode_field_varint(1, 1) + encode_field_varint(4, 1)
        )
        result = parse_stream_proto_message(_status_frame(both))
        assert result is not None
        assert result[MODE_KEY] is None

    def test_a_frame_without_field_393_publishes_no_mode_key(self) -> None:
        """Absent, not None: a partial report must not wipe the last mode."""
        assert MODE_KEY not in _parse("BK11", "property_task3_switched_off")

    def test_a_broken_mode_container_does_not_drop_the_other_readings(self) -> None:
        broken = encode_field_bytes(393, b"\x08")
        result = parse_stream_proto_message(
            _status_frame(broken, encode_field_varint(262, 64))
        )
        assert result is not None
        assert result["soc_pct"] == 64
        assert result[MODE_KEY] is None


class TestTaskList:
    @pytest.mark.parametrize(
        ("prefix", "role", "expected"),
        [
            ("BK11", "get_reply_custom_mode", BK11_CUSTOM_LIST),
            ("BK11", "get_reply_ai_mode", BK11_CUSTOM_LIST),
            ("BK11", "property_task3_switched_off", BK11_TASK3_OFF_LIST),
            ("BK11", "property_task2_switched_on", BK11_TASK2_ON_LIST),
            ("BK31", "get_reply_custom_mode", BK31_CUSTOM_LIST),
            ("BK31", "property_tasks_off", BK31_ALL_OFF_LIST),
        ],
    )
    def test_list_matches_the_app(
        self, prefix: str, role: str, expected: list[dict]
    ) -> None:
        assert _parse(prefix, role)[LIST_KEY] == expected

    def test_entries_keep_the_report_order(self) -> None:
        tasks = _parse("BK11", "get_reply_custom_mode")[LIST_KEY]
        assert [(t["slot"], t["kind"]) for t in tasks] == [
            (2, "charge"),
            (3, "charge"),
            (1, "discharge"),
            (5, "discharge"),
        ]

    def test_only_charge_entries_carry_source_and_soc_target(self) -> None:
        for task in _parse("BK11", "get_reply_custom_mode")[LIST_KEY]:
            expected = CHARGE_KEYS if task["kind"] == "charge" else DISCHARGE_KEYS
            assert set(task) == expected

    def test_source_follows_the_grid_flag_of_field_11(self) -> None:
        one, two, *_ = _parse("BK11", "get_reply_custom_mode")[LIST_KEY]
        assert (one["slot"], one["source"]) == (2, "grid_and_solar")
        assert (two["slot"], two["source"]) == (3, "solar_only")

    def test_a_window_past_midnight_keeps_start_after_end(self) -> None:
        first = _parse("BK11", "get_reply_custom_mode")[LIST_KEY][0]
        assert (first["start"], first["end"]) == ("22:00", "06:00")

    def test_discharge_power_is_the_plain_value_of_field_12(self) -> None:
        tasks = _parse("BK11", "get_reply_custom_mode")[LIST_KEY]
        discharge = [t for t in tasks if t["kind"] == "discharge"]
        assert [t["power_w"] for t in discharge] == [300, 790]

    def test_bk11_and_bk31_decode_the_same_way(self) -> None:
        bk11 = _parse("BK11", "get_reply_custom_mode")[LIST_KEY]
        bk31 = _parse("BK31", "get_reply_custom_mode")[LIST_KEY]
        assert bk11 == bk31

    def test_switching_one_task_changes_only_that_entry(self) -> None:
        before = _parse("BK11", "get_reply_custom_mode")[LIST_KEY]
        after = _parse("BK11", "property_task3_switched_off")[LIST_KEY]
        changed = [b["slot"] for b, a in zip(before, after, strict=True) if b != a]
        assert changed == [3]

    def test_a_frame_without_field_584_publishes_no_list_key(self) -> None:
        result = parse_stream_proto_message(_status_frame(_mode_field(1)))
        assert result is not None
        assert LIST_KEY not in result

    def test_an_empty_list_container_publishes_an_empty_list(self) -> None:
        result = parse_stream_proto_message(_status_frame(encode_field_bytes(584, b"")))
        assert result is not None
        assert result[LIST_KEY] == []

    def test_a_task_that_does_not_decode_publishes_none_not_a_partial_list(
        self,
    ) -> None:
        good = _frames("BK11")["get_reply_custom_mode"]
        good_task = encode_field_bytes(
            1,
            encode_field_varint(3, 1)
            + encode_field_bytes(10, bytes([0xFC, 0x87, 0xC8, 0x24]))
            + encode_field_varint(12, 300),
        )
        no_window = encode_field_bytes(1, encode_field_varint(3, 9))
        result = parse_stream_proto_message(
            _status_frame(encode_field_bytes(584, good_task + no_window))
        )
        assert result is not None
        assert result[LIST_KEY] is None
        assert parse_stream_proto_message(good) is not None

    def test_a_window_that_is_not_on_a_slot_boundary_is_refused(self) -> None:
        off_grid = encode_field_bytes(
            1,
            encode_field_varint(3, 1)
            + encode_field_bytes(10, bytes([31]))
            + encode_field_varint(12, 300),
        )
        result = parse_stream_proto_message(
            _status_frame(encode_field_bytes(584, off_grid))
        )
        assert result is not None
        assert result[LIST_KEY] is None


class TestFixturesAreMasked:
    @pytest.mark.parametrize("prefix", ["BK11", "BK31"])
    def test_no_real_identifier_is_left_in_the_fixture(self, prefix: str) -> None:
        path = FIXTURES / f"stream_{prefix.lower()}_tasks_masked.json"
        text = path.read_text()
        assert not re.search(r"[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}", text)
        for frame in json.loads(text)["frames"]:
            raw = bytes.fromhex(frame["hex"])
            for run in re.findall(rb"[A-Za-z0-9]{8,}", raw):
                assert set(run) == {ord("X")}, run
            assert b"X" * 16 in raw
