"""Tests for the STREAM AC 5000 task list (#681) and the pin on its scalars.

The decode used to publish one setpoint per kind. It now builds one list of
every task and derives those setpoints from it. The first test is the net under
that change: the scalar values the parser published before the list existed are
pinned in a fixture, written by the old code, and every frame of every ES21 and
ES22 fixture must still produce exactly those. Nothing in the expectation is
derived from the parser under test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import (
    parse_stream_ac5000_message,
)
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
    encode_varint,
)

FIXTURES = Path(__file__).parent / "fixtures" / "stream_ac5000"
PINNED = json.loads((FIXTURES / "scheduled_scalars_pre_681.json").read_text())[
    "scalars"
]
TOGGLE = json.loads((FIXTURES / "es22_681_task_toggle_masked.json").read_text())[
    "frames"
]
LIST_KEY = "scheduled_tasks"


def _sub(field_number: int, inner: bytes) -> bytes:
    """Wrap ``inner`` as a length-delimited submessage."""
    return encode_field_bytes(field_number, inner)


def _frame(inner: bytes) -> bytes:
    """A minimal 254/39 header frame around ``inner``."""
    header = (
        encode_field_bytes(1, inner)
        + encode_field_varint(8, 254)
        + encode_field_varint(9, 39)
    )
    return encode_field_bytes(1, header)


def _scalars(result: dict | None) -> dict | None:
    """The scheduled_* scalars of a parse result, without the task list."""
    if result is None:
        return None
    return {
        key: value
        for key, value in result.items()
        if key.startswith("scheduled_") and key != LIST_KEY
    }


def _parse_pinned(key: str) -> dict | None:
    name, index = key.split("#")
    frames = json.loads((FIXTURES / name).read_text())["frames"]
    return parse_stream_ac5000_message(bytes.fromhex(frames[int(index)]["hex"]))


def _toggle(role: str) -> list[dict]:
    frame = next(f for f in TOGGLE if f["role"] == role)
    result = parse_stream_ac5000_message(bytes.fromhex(frame["hex"]))
    assert result is not None
    return result[LIST_KEY]


def _charge(source: int, soc: int, power: int) -> bytes:
    """A charge power container: source, then SoC target and power."""
    return _sub(
        8,
        encode_field_varint(1, source)
        + _sub(
            3,
            encode_field_bytes(1, b"ES22TEST00000001")
            + encode_field_varint(2, soc)
            + encode_field_varint(3, power),
        ),
    )


def _window(start_min: int, end_min: int) -> bytes:
    return _sub(7, encode_varint((end_min << 16) | start_min))


class TestScalarsStayWhatTheyWere:
    def test_the_pin_covers_real_task_frames(self) -> None:
        """A pin that held only empty frames would pass for any refactor."""
        valued = [
            k for k, v in PINNED.items() if v and v["scheduled_charge_task_count"]
        ]
        assert len(valued) >= 10

    @pytest.mark.parametrize("key", sorted(PINNED))
    def test_every_scalar_equals_the_value_the_old_parser_published(
        self, key: str
    ) -> None:
        assert _scalars(_parse_pinned(key)) == PINNED[key]


class TestToggleCapture:
    """@sciurius' three readbacks on #681: task 2 off, on, on."""

    @pytest.mark.parametrize(
        ("role", "task2_enabled"),
        [
            ("get_reply_task2_off", False),
            ("get_reply_task2_on_again", True),
            ("get_reply_task2_on_stale", True),
        ],
    )
    def test_task_two_follows_the_toggle_and_task_one_stays_on(
        self, role: str, task2_enabled: bool
    ) -> None:
        one, two = _toggle(role)
        assert one["enabled"] is True
        assert two["enabled"] is task2_enabled

    def test_each_task_carries_the_window_and_setpoints_the_issue_names(self) -> None:
        one, two = _toggle("get_reply_task2_off")
        assert (one["slot"], one["kind"], one["start"], one["end"]) == (
            1,
            "charge",
            "07:00",
            "08:00",
        )
        assert (one["source"], one["power_w"], one["soc_target"]) == ("grid", 2500, 95)
        assert (two["slot"], two["kind"], two["start"], two["end"]) == (
            2,
            "charge",
            "08:00",
            "10:00",
        )
        # Solar only: the app shows no power for it, the zero is a fill.
        assert (two["source"], two["power_w"]) == ("solar_only", None)


class TestEntryShape:
    def _entries(self) -> list[dict]:
        grid = (
            encode_field_varint(2, 1)
            + encode_field_varint(3, 1)
            + _window(420, 480)
            + _charge(1, 90, 1500)
        )
        solar = encode_field_varint(2, 2) + _window(600, 720) + _charge(0, 80, 0)
        discharge = (
            encode_field_varint(2, 3)
            + encode_field_varint(3, 1)
            + _window(1080, 1320)
            + _sub(9, encode_field_varint(1, 700))
        )
        result = parse_stream_ac5000_message(
            _frame(_sub(40, _sub(1, grid) + _sub(1, solar) + _sub(1, discharge)))
        )
        assert result is not None
        return result[LIST_KEY]

    def test_a_charge_entry_has_source_and_soc_target(self) -> None:
        grid, _solar, _discharge = self._entries()
        assert grid == {
            "slot": 1,
            "kind": "charge",
            "enabled": True,
            "start": "07:00",
            "end": "08:00",
            "source": "grid",
            "power_w": 1500,
            "soc_target": 90,
        }

    def test_a_solar_only_entry_has_no_power(self) -> None:
        _grid, solar, _discharge = self._entries()
        assert solar["source"] == "solar_only"
        assert solar["power_w"] is None
        assert solar["enabled"] is False
        assert (solar["start"], solar["end"]) == ("10:00", "12:00")

    def test_a_discharge_entry_has_neither_source_nor_soc_target(self) -> None:
        _grid, _solar, discharge = self._entries()
        assert discharge == {
            "slot": 3,
            "kind": "discharge",
            "enabled": True,
            "start": "18:00",
            "end": "22:00",
            "power_w": 700,
        }


class TestEmptyAndUnreadable:
    def test_an_empty_task_container_publishes_an_empty_list(self) -> None:
        result = parse_stream_ac5000_message(_frame(_sub(40, b"")))
        assert result is not None
        assert result[LIST_KEY] == []

    def test_a_block_that_does_not_decode_publishes_none_not_a_partial_list(
        self,
    ) -> None:
        good = (
            encode_field_varint(2, 1)
            + encode_field_varint(3, 1)
            + _window(420, 480)
            + _charge(1, 90, 1500)
        )
        kindless = encode_field_varint(2, 2) + _window(60, 120)
        result = parse_stream_ac5000_message(
            _frame(_sub(40, _sub(1, good) + _sub(1, kindless)))
        )
        assert result is not None
        assert result[LIST_KEY] is None
