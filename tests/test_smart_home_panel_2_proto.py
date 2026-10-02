"""Tests for the Smart Home Panel 2 (`HD31`) protobuf parser.

The 51 frames in `tests/fixtures/smart_home_panel_2/hd31_frames_issue464.json`
come from the two diagnostics downloads on issue #464 (serial numbers and the
installation address already masked to `X` runs at the source). The frame
indices quoted here are positions in that file's `frames` list.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any

from ecoflow_energy.ecoflow.parsers.smart_home_panel_2_proto import (
    CIRCUIT_COUNT,
    parse_smart_home_panel_2_message,
)

_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "smart_home_panel_2"
    / "hd31_frames_issue464.json"
)
_FRAMES: list[dict[str, Any]] = json.loads(_FIXTURE.read_text())["frames"]
_RAW: list[bytes] = [bytes.fromhex(frame["hex"]) for frame in _FRAMES]

# Frames by shape, from the field-set survey of the capture.
_GET_REPLY_BUNDLES = (21, 23, 24, 25, 26, 28, 29, 32, 33)
_GRID_VOLTAGE_ONLY = (9, 10, 11, 13, 14, 16, 17, 38, 41, 42, 44, 45, 47, 48)
_BATTERY_ONLY_PUSH = 30

_CIRCUITS = range(1, CIRCUIT_COUNT + 1)
_FULL_KEYS = {
    "grid_power_w",
    "load_power_w",
    "grid_l1_current_a",
    "grid_l2_current_a",
    "backup_runtime_min",
    "battery_soc_pct",
    "battery_full_capacity_wh",
    "battery_remaining_energy_wh",
    "storage_ch1_soc_pct",
    "grid_voltage_v",
} | {
    f"circuit_{n}_{suffix}"
    for n in _CIRCUITS
    for suffix in ("power_w", "current_a", "name")
}


def _parse(i: int) -> dict[str, Any]:
    result = parse_smart_home_panel_2_message(_RAW[i])
    assert result is not None, f"frame {i} produced no HD31 keys"
    return result


# --- synthetic frames --------------------------------------------------------


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


def _ld(field: int, payload: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(payload)) + payload


def _vint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _f32(field: int, value: float) -> bytes:
    return _varint(field << 3 | 5) + struct.pack("<f", value)


def _frame(cmd_func: int, cmd_id: int, pdata: bytes, src: int = 11) -> bytes:
    header = _ld(1, pdata) + _vint(2, src) + _vint(8, cmd_func) + _vint(9, cmd_id)
    return _ld(1, header)


def _packed(*values: float) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


# --- tests -------------------------------------------------------------------


def test_get_reply_bundle_carries_the_full_key_set() -> None:
    for i in _GET_REPLY_BUNDLES:
        data = _parse(i)
        assert set(data) == _FULL_KEYS, f"frame {i}"
        assert data["battery_soc_pct"] == 93
        assert data["battery_full_capacity_wh"] == 18432
        assert data["battery_remaining_energy_wh"] == 17141.8
        assert data["storage_ch1_soc_pct"] == 93
        assert 121 <= data["grid_voltage_v"] <= 123
        assert 1230 <= data["grid_power_w"] <= 1601
        assert [data[f"circuit_{n}_name"] for n in _CIRCUITS] == [
            f"Circuit {n}" for n in _CIRCUITS
        ]
        # Channels 2 and 3 have nothing attached: no key, not a 0 % battery.
        assert "storage_ch2_soc_pct" not in data
        assert "storage_ch3_soc_pct" not in data


def test_circuits_sum_to_home_power_on_every_snapshot() -> None:
    snapshots = [
        i
        for i, frame in enumerate(_FRAMES)
        if {"cmd_func": 12, "cmd_id": 1} in frame["cmds"]
    ]
    assert len(snapshots) == 22
    for i in snapshots:
        data = _parse(i)
        total = sum(data[f"circuit_{n}_power_w"] for n in _CIRCUITS)
        assert abs(data["load_power_w"] - total) <= 1.5, f"frame {i}"


def test_grid_voltage_only_push_gives_exactly_the_voltage() -> None:
    for i in _GRID_VOLTAGE_ONLY:
        data = _parse(i)
        assert set(data) == {"grid_voltage_v"}, f"frame {i}"
        assert 121 <= data["grid_voltage_v"] <= 123


def test_battery_only_push_gives_only_battery_keys() -> None:
    data = _parse(_BATTERY_ONLY_PUSH)
    assert data == {
        "battery_soc_pct": 93,
        "battery_full_capacity_wh": 18432,
        "battery_remaining_energy_wh": 17141.8,
        "storage_ch1_soc_pct": 93,
    }


def test_no_address_timezone_or_serial_reaches_the_result() -> None:
    # Positive control: the masked address and serials are on the wire, so a
    # scan that finds nothing in the output is not scanning an empty input.
    assert any(b"XXXX" in raw for raw in _RAW)

    parsed = 0
    for i, raw in enumerate(_RAW):
        data = parse_smart_home_panel_2_message(raw)
        if data is None:
            continue
        parsed += 1
        for key, value in data.items():
            assert not {"area", "sn", "serial", "timezone", "address"} & set(
                key.split("_")
            ), f"frame {i}: {key}"
            assert "XXXX" not in str(value), f"frame {i}: {key}"
    assert parsed == 37


def test_other_headers_alone_return_none() -> None:
    func_254 = [
        i
        for i, frame in enumerate(_FRAMES)
        if frame["cmds"] and all(c["cmd_func"] == 254 for c in frame["cmds"])
    ]
    assert len(func_254) == 14
    for i in func_254:
        assert parse_smart_home_panel_2_message(_RAW[i]) is None, f"frame {i}"

    # The same mapped payload is read on 12.32 and ignored on 254.32, on a
    # cmd_id the parser does not know, and from a source other than the panel.
    pdata = _ld(82, _vint(3, 122))
    assert parse_smart_home_panel_2_message(_frame(12, 32, pdata)) == {
        "grid_voltage_v": 122
    }
    assert parse_smart_home_panel_2_message(_frame(254, 32, pdata)) is None
    assert parse_smart_home_panel_2_message(_frame(254, 1, pdata)) is None
    assert parse_smart_home_panel_2_message(_frame(12, 21, pdata)) is None
    assert parse_smart_home_panel_2_message(_frame(12, 32, pdata, src=2)) is None


def test_synthetic_zero_nan_short_array_and_storage_gate() -> None:
    nan = float("nan")

    # -0.0 reads as 0.0, a NaN is dropped, and a short packed array yields
    # only the indices it carries: one grid leg, three circuit powers (the
    # third is NaN), and no circuit currents at all.
    time_pdata = _ld(4, _f32(1, nan) + _f32(21, -0.0) + _ld(30, _packed(7.987))) + _ld(
        2, _ld(1, _packed(442.04, -0.0, nan))
    )
    data = parse_smart_home_panel_2_message(_frame(12, 1, time_pdata))
    assert data == {
        "load_power_w": 0.0,
        "circuit_1_power_w": 442.0,
        "circuit_2_power_w": 0.0,
        "grid_l1_current_a": 7.99,
    }
    assert data is not None
    assert math.copysign(1.0, data["load_power_w"]) == 1.0
    assert math.copysign(1.0, data["circuit_2_power_w"]) == 1.0

    # A storage channel's SOC is read only from a channel that is ready or
    # connected in the same frame: ch1 ready, ch3 connected, ch2 neither.
    push_pdata = _ld(
        80,
        _ld(80, _vint(8, 93))
        + _ld(60, _vint(1, 1))
        + _ld(81, _vint(8, 55))
        + _ld(82, _vint(8, 44) + _vint(3, 1)),
    )
    assert parse_smart_home_panel_2_message(_frame(12, 32, push_pdata)) == {
        "storage_ch1_soc_pct": 93,
        "storage_ch3_soc_pct": 44,
    }
