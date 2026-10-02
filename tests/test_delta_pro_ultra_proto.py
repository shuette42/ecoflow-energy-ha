"""Tests for the DELTA Pro Ultra (`Y711`) protobuf parser.

Frames come from the reporter's two diagnostics downloads on issue #464
(2026-10-02), copied unchanged into
`tests/fixtures/delta_pro_ultra/y711_frames_issue464.json`. Frames are named
by their position in that file's `frames` list, and `_frame` checks the cmd
pair at that position, so a reordered fixture fails loudly instead of
quietly testing another message.
"""

from __future__ import annotations

import json
import math
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from ecoflow_energy.ecoflow.parsers.delta_pro_ultra_proto import (
    _parse_bp_info,
    parse_delta_pro_ultra_message,
)
from ecoflow_energy.ecoflow.parsers.stream_proto import _iter_fields

_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "delta_pro_ultra" / "y711_frames_issue464.json"
)
_FRAMES: list[dict[str, Any]] = json.loads(_FIXTURE_PATH.read_text())["frames"]

# Expected key sets, by message.
_POWER_KEYS = {
    "watts_in_sum",
    "watts_out_sum",
    "ac_out_l1_1_w",
    "ac_out_l1_2_w",
    "ac_out_l2_1_w",
    "ac_out_l2_2_w",
    "ac_out_tt30_w",
    "ac_out_l14_w",
    "power_io_out_w",
    "power_io_in_w",
    "ac_in_w",
    "solar_lv_in_w",
    "solar_hv_in_w",
}
_SHOW_KEYS = _POWER_KEYS | {"soc", "remain_time_min"}
_RECORD_KEYS = {
    "batt_voltage_v",
    "batt_charge_power_w",
    "batt_discharge_power_w",
    "inv_ac_temp_c",
    "pd_temp_c",
}
_PARA_KEYS = {"backup_reserve_pct"}
_PACK_KEYS = {f"bp{n}_{what}" for n in (1, 2, 3) for what in ("soc_pct", "temp_c")}


def _frame(index: int, func: int, cmd_id: int) -> bytes:
    """Frame at `index`, after checking it carries exactly cmd `func.cmd_id`."""
    entry = _FRAMES[index]
    assert entry["cmds"] == [{"cmd_func": func, "cmd_id": cmd_id}]
    return bytes.fromhex(entry["hex"])


def _frames_of(func: int, cmd_id: int) -> list[bytes]:
    """Every single-header frame of cmd `func.cmd_id` in the fixture."""
    return [
        bytes.fromhex(entry["hex"])
        for entry in _FRAMES
        if entry["cmds"] == [{"cmd_func": func, "cmd_id": cmd_id}]
    ]


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


def _rebuild(payload: bytes, patch: Callable[[int, int, bytes], bytes]) -> bytes:
    """Re-encode one protobuf level, passing each field's bytes through `patch`."""
    out = bytearray()
    for num, wire, raw in _iter_fields(payload):
        raw = patch(num, wire, raw)
        out += _varint(num << 3 | wire)
        if wire == 2:
            out += _varint(len(raw))
        out += raw
    return bytes(out)


def _readdress(frame: bytes, src: int) -> bytes:
    """The same frame with every header's `src` (header field 2) replaced."""

    def set_src(num: int, wire: int, raw: bytes) -> bytes:
        return _varint(src) if (num == 2 and wire == 0) else raw

    def into_header(num: int, wire: int, raw: bytes) -> bytes:
        return _rebuild(raw, set_src) if (num == 1 and wire == 2) else raw

    return _rebuild(frame, into_header)


def _bp_entry(bp_no: int, soc: int | None = None, temp: int | None = None) -> bytes:
    """One repeated `BPInfo` entry (2.4 field 1) with the given readings."""
    inner = b"\x08" + _varint(bp_no)
    if soc is not None:
        inner += b"\x18" + _varint(soc)
    if temp is not None:
        inner += b"\x58" + _varint(temp & 0xFFFFFFFFFFFFFFFF)
    return b"\x0a" + _varint(len(inner)) + inner


def _show_frame_with(pdata: bytes) -> bytes:
    """Frame 2 with its 2.1 payload replaced by `pdata`, sent unmasked (enc_type 0)."""

    def patch(num: int, wire: int, raw: bytes) -> bytes:
        if num == 1 and wire == 2:  # header pdata
            return pdata
        if num == 6 and wire == 0:  # header enc_type
            return _varint(0)
        return raw

    def into_header(num: int, wire: int, raw: bytes) -> bytes:
        return _rebuild(raw, patch) if (num == 1 and wire == 2) else raw

    return _rebuild(_frame(2, 2, 1), into_header)


def _float_field(number: int, value: float) -> bytes:
    return _varint(number << 3 | 5) + struct.pack("<f", value)


def test_full_show_frame_gives_soc_and_the_whole_power_block() -> None:
    """2.1 (frame 2, enc_type 1): SOC 93 and all 13 power keys, plus remaining time."""
    data = parse_delta_pro_ultra_message(_frame(2, 2, 1))

    assert data is not None
    assert set(data) == _SHOW_KEYS
    assert data["soc"] == 93
    assert isinstance(data["remain_time_min"], int)
    for key in _POWER_KEYS:
        assert isinstance(data[key], float), key

    # The capture is idle and holds no negative zero, so the float rules are
    # pinned on a synthetic push: -0.0 and a value that rounds to it publish as
    # +0.0, and power is rounded to one decimal.
    synthetic = parse_delta_pro_ultra_message(
        _show_frame_with(
            _varint(21 << 3)
            + _varint(50)
            + _float_field(41, -0.0)
            + _float_field(42, -0.04)
            + _float_field(56, 12.34)
        )
    )
    assert synthetic == {
        "soc": 50,
        "watts_in_sum": 0.0,
        "watts_out_sum": 0.0,
        "ac_in_w": 12.3,
    }
    assert math.copysign(1.0, synthetic["watts_in_sum"]) > 0
    assert math.copysign(1.0, synthetic["watts_out_sum"]) > 0


@pytest.mark.parametrize(
    ("index", "minutes"),
    [(0, 34365), (7, 34364), (27, 34347), (36, 34333), (61, 34289)],
)
def test_partial_show_frame_carries_exactly_one_key(index: int, minutes: int) -> None:
    """A 5-byte 2.1 push holds only `remain_time`: no zeros for the absent fields."""
    data = parse_delta_pro_ultra_message(_frame(index, 2, 1))

    assert data == {"remain_time_min": minutes}


def test_record_frames_give_voltage_power_and_temperatures() -> None:
    """2.2: pack voltage 106.57 V, idle discharge ~20 W, temperatures in range."""
    frames = _frames_of(2, 2)
    assert frames, "fixture holds no 2.2 frame"

    for frame in frames:
        data = parse_delta_pro_ultra_message(frame)
        assert data is not None
        assert set(data) == _RECORD_KEYS
        assert data["batt_voltage_v"] == pytest.approx(106.57, abs=0.1)
        assert data["batt_charge_power_w"] == 0.0
        assert 17 <= data["batt_discharge_power_w"] <= 22
        assert data["inv_ac_temp_c"] == 31.0
        assert 28 <= data["pd_temp_c"] <= 31
        assert isinstance(data["pd_temp_c"], int)


def test_bp_info_gives_three_packs_and_reads_signed_temperatures() -> None:
    """2.4: pack SOC 99/98/82-83 and temperature 32/30/29; pack numbers 1-5 only."""
    data = parse_delta_pro_ultra_message(_frame(1, 2, 4))

    assert data is not None
    assert set(data) == _PACK_KEYS
    assert (data["bp1_soc_pct"], data["bp2_soc_pct"]) == (99, 98)
    assert data["bp3_soc_pct"] in (82, 83)
    assert (data["bp1_temp_c"], data["bp2_temp_c"], data["bp3_temp_c"]) == (32, 30, 29)

    # A negative temperature is the 64-bit two's complement of the value; a
    # pack number outside 1-5 (0 and 6 here) is not a pack and publishes nothing.
    synthetic = _parse_bp_info(
        _bp_entry(4, soc=40, temp=-5)
        + _bp_entry(6, soc=10, temp=10)
        + _bp_entry(0, soc=10, temp=10)
        + _bp_entry(5, temp=7)
    )
    assert synthetic == {"bp4_soc_pct": 40, "bp4_temp_c": -5, "bp5_temp_c": 7}


def test_get_reply_bundle_carries_all_four_messages_at_once() -> None:
    """A `get_reply` bundle (no enc_type, unmasked): 2.1, 2.2, 2.3 and 2.4 together."""
    bundle = next(e for e in _FRAMES if e["topic"] == "get_reply")
    assert {c["cmd_id"] for c in bundle["cmds"] if c["cmd_func"] == 2} == {1, 2, 3, 4}

    data = parse_delta_pro_ultra_message(bytes.fromhex(bundle["hex"]))

    assert data is not None
    assert set(data) == _SHOW_KEYS | _RECORD_KEYS | _PARA_KEYS | _PACK_KEYS
    assert data["soc"] == 93
    assert data["batt_voltage_v"] == pytest.approx(106.57, abs=0.1)
    assert data["backup_reserve_pct"] == 50
    assert data["bp1_soc_pct"] == 99
    assert data["bp3_temp_c"] == 29


def test_other_messages_and_other_sources_are_ignored() -> None:
    """3.28, 254.21, 50.2 and 1.88 give None, and so does a 2.1 frame from src 6."""
    for index, func, cmd_id in [(4, 3, 28), (3, 254, 21), (18, 50, 2), (26, 1, 88)]:
        assert parse_delta_pro_ultra_message(_frame(index, func, cmd_id)) is None

    # The same bytes addressed from src 2 are read (control for the helper) ...
    show = _frame(2, 2, 1)
    assert _readdress(show, 2) == show
    # ... and from the battery management module (src 6) they are not.
    assert parse_delta_pro_ultra_message(_readdress(show, 6)) is None
