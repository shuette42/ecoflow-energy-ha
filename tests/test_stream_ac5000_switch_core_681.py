"""Tests for the core side of the STREAM AC 5000 task on/off switch (#681).

The write is pinned against the app's own two writes from @sciurius' toggle on
#681: task 2, a Solar only charge task 08:00-10:00, switched off and on again.
Each app write carries the whole list of both tasks; the builder writes one
task, so the comparison is entry against entry. The readback side is pinned
against the three get-all replies of the same recording.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import (
    parse_stream_ac5000_message,
)
from ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
    encode_varint,
)
from ecoflow_energy.ecoflow.stream_ac5000_commands import (
    TASK_ADD,
    TASK_UPDATE,
    build_task_payload,
)

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "stream_ac5000"
    / "es22_681_task_toggle_masked.json"
)
FRAMES = json.loads(FIXTURE.read_text())["frames"]
ECHO_KEY = "scheduled_task_echo"
LIST_KEY = "scheduled_tasks"

# The recording masks the serial as 16 X, so the entries rebuild from it.
MASKED_SN = "X" * 16
SN = "ES22TEST00000147"


def _fields(data: bytes) -> list[tuple[int, int | bytes, bytes]]:
    """Top-level (field number, value, raw bytes incl. tag) of a message."""

    def varint(i: int) -> tuple[int, int]:
        value = shift = 0
        while True:
            byte = data[i]
            i += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if byte < 0x80:
                return value, i

    out: list[tuple[int, int | bytes, bytes]] = []
    i = 0
    while i < len(data):
        start = i
        key, i = varint(i)
        if key & 7 == 0:
            value, i = varint(i)
            out.append((key >> 3, value, data[start:i]))
        else:
            assert key & 7 == 2, "only varint and length-delimited fields here"
            length, i = varint(i)
            out.append((key >> 3, data[i : i + length], data[start : i + length]))
            i += length
    return out


def _task_entries(frame: bytes) -> list[bytes]:
    """Each `39.1` task entry of a config write, as raw bytes incl. its tag."""
    headers, _ = decode_header_message(frame)
    assert headers
    pdata = bytes.fromhex(headers[0]["pdata"])
    (container,) = [value for number, value, _raw in _fields(pdata) if number == 39]
    assert isinstance(container, bytes)
    return [raw for number, _value, raw in _fields(container) if number == 1]


def _app_entries(role: str) -> list[bytes]:
    frame = next(f for f in FRAMES if f["role"] == role)
    return _task_entries(bytes.fromhex(frame["hex"]))


def _switch_entry(enabled: bool, power_w: int = 0) -> bytes:
    """Task 2 as the switch writes it: slot 2, daily, Solar only."""
    (entry,) = _task_entries(
        build_task_payload(
            "charge",
            480,
            600,
            power_w,
            MASKED_SN,
            operation=TASK_UPDATE,
            enabled=enabled,
            charge_soc_target=95,
            seq=1,
            task_slot=2,
            time_mode=1,
            time_param=0,
            charge_source=0,
        )
    )
    return entry


class TestSolarOnlyEntryEqualsTheAppWrite:
    def test_off_equals_task_two_of_the_app_write(self) -> None:
        """Including the explicit `.3 = 0` and the explicit 0 W of Solar only."""
        task_one, task_two = _app_entries("set_task2_off")

        assert _switch_entry(enabled=False) == task_two
        assert _switch_entry(enabled=False) != task_one

    @pytest.mark.parametrize("power_w", [0, 2500])
    def test_on_equals_task_two_of_the_app_write(self, power_w: int) -> None:
        """A power the caller still holds does not reach a Solar only task."""
        _task_one, task_two = _app_entries("set_task2_on")

        assert _switch_entry(enabled=True, power_w=power_w) == task_two

    @pytest.mark.parametrize("source", [-1, 2])
    def test_an_unknown_charge_source_is_refused(self, source: int) -> None:
        with pytest.raises(ValueError):
            build_task_payload(
                "charge", 480, 600, 0, SN, operation=TASK_UPDATE, charge_source=source
            )


class TestPowerWriteBytesUnchanged:
    """Frames built before `charge_source` existed, pinned as literals."""

    _TAIL = (
        "102018022001280140fe014826{len}58017001788cffffffffffffffff01800104ba0107"
        "416e64726f6964d2011045533232544553543030303030313437da01104553323254455354"
        "3030303030313437"
    )

    def test_grid_charge_update(self) -> None:
        frame = build_task_payload(
            "charge", 780, 960, 600, SN, operation=TASK_UPDATE, seq=1
        )
        assert frame.hex() == (
            "0a89010a360827ba02310a2f080210011801200028013a048c86801e421d080110001a"
            "170a1045533232544553543030303030313437106418d804"
            + self._TAIL.format(len="5036")
        )

    def test_charge_update_at_zero_power_with_a_once_repeat(self) -> None:
        frame = build_task_payload(
            "charge",
            480,
            600,
            0,
            SN,
            operation=TASK_UPDATE,
            seq=1,
            task_slot=2,
            enabled=False,
            charge_soc_target=95,
            time_mode=4,
            time_param=1037584,
        )
        assert frame.hex() == (
            "0a8c010a390827ba02340a32080210021800200028043090aa3f3a04e083e012421c08"
            "0110001a160a1045533232544553543030303030313437105f1800"
            + self._TAIL.format(len="5039")
        )

    def test_discharge_add_and_update(self) -> None:
        add = build_task_payload(
            "discharge", 0, 1380, 600, SN, operation=TASK_ADD, seq=1
        )
        update = build_task_payload(
            "discharge", 1380, 120, 800, SN, operation=TASK_UPDATE, seq=1, task_slot=3
        )
        assert add.hex() == (
            "0a6f0a1c0827ba02170a15080110021801200028013a048080902b4a0308d804"
            + self._TAIL.format(len="501c")
        )
        assert update.hex() == (
            "0a6f0a1c0827ba02170a15080210031801200028013a04e48ae0034a0308a006"
            + self._TAIL.format(len="501c")
        )


class TestTaskEcho:
    @pytest.mark.parametrize(
        "role", [f["role"] for f in FRAMES if f["topic"] == "get_reply"]
    )
    def test_every_get_all_echoes_both_tasks(self, role: str) -> None:
        """Task 2 is Solar only: its source is absent on the wire and reads 0."""
        frame = next(f for f in FRAMES if f["role"] == role)
        result = parse_stream_ac5000_message(bytes.fromhex(frame["hex"]))

        assert result is not None
        assert result[ECHO_KEY] == {
            1: {
                "time_mode": 1,
                "time_param": 0,
                "start_min": 420,
                "end_min": 480,
                "charge_source": 1,
            },
            2: {
                "time_mode": 1,
                "time_param": 0,
                "start_min": 480,
                "end_min": 600,
                "charge_source": 0,
            },
        }

    def test_absent_zeros_read_as_the_device_holds_them(self) -> None:
        """No `.5`/`.6` on the wire: parameter 0, repeat unknown (None).

        A discharge task has no charging source.
        """
        discharge = (
            encode_field_varint(2, 3)
            + encode_field_bytes(7, encode_varint((1320 << 16) | 1080))
            + encode_field_bytes(9, encode_field_varint(1, 700))
        )
        header = (
            encode_field_bytes(
                1, encode_field_bytes(40, encode_field_bytes(1, discharge))
            )
            + encode_field_varint(8, 254)
            + encode_field_varint(9, 39)
        )
        result = parse_stream_ac5000_message(encode_field_bytes(1, header))

        assert result is not None
        assert result[ECHO_KEY] == {
            3: {
                "time_mode": None,
                "time_param": 0,
                "start_min": 1080,
                "end_min": 1320,
                "charge_source": None,
            }
        }

    def test_an_undecodable_block_withdraws_echo_and_list_together(self) -> None:
        good = (
            encode_field_varint(2, 1)
            + encode_field_varint(3, 1)
            + encode_field_bytes(7, encode_varint((480 << 16) | 420))
            + encode_field_bytes(
                8,
                encode_field_varint(1, 1)
                + encode_field_bytes(
                    3,
                    encode_field_bytes(1, b"ES22TEST00000001")
                    + encode_field_varint(2, 90)
                    + encode_field_varint(3, 1500),
                ),
            )
        )
        kindless = encode_field_varint(2, 2) + encode_field_bytes(
            7, encode_varint((120 << 16) | 60)
        )
        tasks = encode_field_bytes(
            40, encode_field_bytes(1, good) + encode_field_bytes(1, kindless)
        )
        header = (
            encode_field_bytes(1, tasks)
            + encode_field_varint(8, 254)
            + encode_field_varint(9, 39)
        )
        result = parse_stream_ac5000_message(encode_field_bytes(1, header))

        assert result is not None
        assert result[LIST_KEY] is None
        assert result[ECHO_KEY] is None
