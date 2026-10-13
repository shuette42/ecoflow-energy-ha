"""Tests for the PowerGlow (HF33 heating rod) write builder and the extra
fields the 212/8 report now carries (PLAN-190 phase 2, issue #7).

The 17 `set` frames are the app's own writes from one recording of a real
PowerGlow, stored in `tests/fixtures/powerglow/hf33_set_frames.json` with both
serials masked as sixteen `X`. Every one of them is a target power write, so
the other three settings (run state, mode, target temperature) are built from
the message definition and the app's limits rather than from a frame.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ecoflow_energy.diagnostics import _redact_serials
from ecoflow_energy.ecoflow.const import DEVICE_TYPE_POWEROCEAN
from ecoflow_energy.ecoflow.energy_stream import (
    build_powerglow_param_set_payload,
    powerglow_max_power_w,
)
from ecoflow_energy.ecoflow.parsers.powerocean_proto import remap_heating_rod_keys
from ecoflow_energy.ecoflow.parsers.stream_proto import _iter_fields, _read_varint
from ecoflow_energy.ecoflow.proto.runtime import decode_proto_runtime_headers

FIXTURE = Path(__file__).parent / "fixtures" / "powerglow" / "hf33_set_frames.json"

ROD_SN = "X" * 16
POWEROCEAN_SN = "Y" * 16


def _frames(topic: str) -> list[dict[str, Any]]:
    frames = json.loads(FIXTURE.read_text())["frames"]
    return [f for f in frames if f["topic"] == topic]


def _is_212(frame: dict[str, Any], cmd_id: int) -> bool:
    return {"cmd_func": 212, "cmd_id": cmd_id} in frame["cmds"]


_SET_FRAMES = [f for f in _frames("set") if _is_212(f, 99)]


def _varint(raw: bytes) -> int:
    return _read_varint(memoryview(raw), 0)[0]


def _pdata_fields(frame_bytes: bytes) -> dict[int, bytes]:
    """The pdata fields of one built frame, by field number."""
    ((_, _, header),) = _iter_fields(frame_bytes)
    pdata = next(raw for num, _, raw in _iter_fields(header) if num == 1)
    return {num: raw for num, _, raw in _iter_fields(pdata)}


def test_the_fixture_holds_all_17_captured_writes() -> None:
    """Guards the parametrized test below: an empty list would skip silently."""
    assert len(_SET_FRAMES) == 17


@pytest.mark.parametrize("frame", _SET_FRAMES, ids=lambda f: f["ts_iso"][11:23])
def test_builder_reproduces_every_captured_app_write_byte_for_byte(
    frame: dict[str, Any],
) -> None:
    captured = bytes.fromhex(frame["hex"])
    ((_, _, header),) = _iter_fields(captured)
    header_fields = {num: raw for num, _, raw in _iter_fields(header)}
    pdata = _pdata_fields(captured)
    assert set(pdata) == {1, 5}

    rebuilt = build_powerglow_param_set_payload(
        pdata[1].decode("ascii"),
        header_fields[25].decode("ascii"),
        target_power_w=_varint(pdata[5]),
        seq=_varint(header_fields[14]),
    )

    assert rebuilt == captured


@pytest.mark.parametrize(
    ("kwargs", "field", "value"),
    [
        ({"run_on": True}, 2, 1),
        ({"run_on": False}, 2, 0),
        ({"mode": 0}, 3, 0),
        ({"mode": 1}, 3, 1),
        ({"target_temp_c": 10}, 4, 10),
        ({"target_temp_c": 80}, 4, 80),
        ({"target_power_w": 0}, 5, 0),
        ({"target_power_w": 3500}, 5, 3500),
    ],
)
def test_each_setting_lands_on_its_own_field_and_zero_is_explicit(
    kwargs: dict[str, Any], field: int, value: int
) -> None:
    pdata = _pdata_fields(
        build_powerglow_param_set_payload(ROD_SN, POWEROCEAN_SN, seq=1, **kwargs)
    )

    assert pdata[1] == ROD_SN.encode("ascii")
    assert set(pdata) == {1, field}
    assert _varint(pdata[field]) == value


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"run_on": True, "mode": 1},
        {"target_temp_c": 50, "target_power_w": 500},
        {"run_on": False, "mode": 0, "target_temp_c": 50, "target_power_w": 0},
    ],
    ids=["none", "two", "two-numeric", "all-four"],
)
def test_exactly_one_setting_is_required(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        build_powerglow_param_set_payload(ROD_SN, POWEROCEAN_SN, **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": 2},
        {"mode": -1},
        {"mode": True},
        {"target_temp_c": 9},
        {"target_temp_c": 81},
        {"target_temp_c": 50.0},
        {"target_power_w": -1},
        {"target_power_w": 3501},
        {"target_power_w": True},
        {"target_power_w": "500"},
        {"run_on": 1},
    ],
)
def test_out_of_range_and_non_int_values_are_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        build_powerglow_param_set_payload(ROD_SN, POWEROCEAN_SN, **kwargs)


@pytest.mark.parametrize(
    ("hr_sn", "device_sn"),
    [
        ("X" * 15, POWEROCEAN_SN),
        ("X" * 17, POWEROCEAN_SN),
        (ROD_SN, "Y" * 15),
        (ROD_SN, "Y" * 17),
        ("", POWEROCEAN_SN),
    ],
)
def test_a_serial_that_is_not_16_characters_is_refused(
    hr_sn: str, device_sn: str
) -> None:
    with pytest.raises(ValueError, match="16 alphanumeric"):
        build_powerglow_param_set_payload(hr_sn, device_sn, target_power_w=100)


@pytest.mark.parametrize(
    ("prefix", "ceiling"),
    [("HF33", 9000), ("HF32", 6000), ("HF31", 3500), ("XXXX", 3500)],
)
def test_the_power_ceiling_follows_the_rod_serial_prefix(
    prefix: str, ceiling: int
) -> None:
    hr_sn = prefix + "0" * 12
    assert powerglow_max_power_w(hr_sn) == ceiling

    build_powerglow_param_set_payload(hr_sn, POWEROCEAN_SN, target_power_w=ceiling)
    with pytest.raises(ValueError, match="target_power_w"):
        build_powerglow_param_set_payload(
            hr_sn, POWEROCEAN_SN, target_power_w=ceiling + 1
        )


def test_a_captured_report_carries_mode_run_state_and_serial() -> None:
    """The first 212/8 after the first captured write (target power 100 W)."""
    report = next(f for f in _frames("property") if _is_212(f, 8))
    (decoded,) = decode_proto_runtime_headers(
        bytes.fromhex(report["hex"]), device_type=DEVICE_TYPE_POWEROCEAN
    )
    raw = {k: v for k, v in decoded.mapped.items() if not k.startswith("_")}

    result = remap_heating_rod_keys(raw)

    assert result["heating_rod_target_power_w"] == 100.0
    assert result["heating_rod_mode"] == 1
    assert result["heating_rod_run_state"] == 1
    assert result["heating_rod_sn"] == ROD_SN
    assert type(result["heating_rod_mode"]) is int


def test_mode_and_run_state_are_not_taken_from_each_other_or_a_wrong_serial() -> None:
    """Every captured report has mode 1 and run state 1, so these values are
    chosen apart: a swapped pair or a neighbouring field cannot pass."""
    raw = {
        "hr_sn": ROD_SN,
        "hr_mode": 0,
        "hr_run_stat": 2,
        "hr_online_bits": 7,
        "hr_run_flag": 5,
    }

    assert remap_heating_rod_keys(raw) == {
        "heating_rod_sn": ROD_SN,
        "heating_rod_mode": 0,
        "heating_rod_run_state": 2,
        "heating_rod_operating_mode": "solar",
    }
    assert "heating_rod_sn" not in remap_heating_rod_keys({"hr_sn": "X" * 15})
    assert "heating_rod_sn" not in remap_heating_rod_keys({"hr_sn": 1234567890123456})


def test_diagnostics_redacts_the_heating_rod_serial() -> None:
    serial = "HF33DUMMYDUMMY00"
    redacted = _redact_serials({"heating_rod_sn": serial, "heating_rod_mode": 1})

    assert serial not in str(redacted)
    assert redacted["heating_rod_mode"] == 1
