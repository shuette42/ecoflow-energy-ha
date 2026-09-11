"""Tests for the PowerPulse 2 (C376) start/stop command builder and the
accessory descriptor read from the wallbox's own settings report
(PLAN-136 Phase 2, ADR-009).

The four app frames are byte-for-byte captures from one recording of a
real charging session stopped and started twice from the vendor app
(PLAN-136, ADR-009 decision 1). The descriptor frames are the wallbox's own
settings report, sanitized into
`tests/fixtures/powerpulse/c376_run_data_sync_20260824.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ecoflow_energy.ecoflow.energy_stream import (
    _build_powerocean_set_envelope,
    build_powerpulse_charge_action_payload,
)
from ecoflow_energy.ecoflow.parsers.powerpulse_proto import parse_powerpulse_message
from ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from ecoflow_energy.ecoflow.proto_encoding import encode_field_bytes

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "powerpulse"
RUN_DATA_SYNC_FIXTURE = FIXTURE_DIR / "c376_run_data_sync_20260824.json"
HEARTBEAT_FIXTURE = FIXTURE_DIR / "c376_frames_plan132.json"

DEV_ADDR = 215
DEV_SN = "X" * 16

# (seq, action, full frame hex) - the four app frames on record, PLAN-115
# section 1. `on_off_set`: 1 stop, 2 start.
APP_FRAMES = [
    (
        85,
        "stop",
        "0a3c0a190a1508d70112105858585858585858585858585858585810011020"
        "186020012801380340f1014864501958017055800103880101ba0103696f73",
    ),
    (
        68,
        "start",
        "0a3c0a190a1508d70112105858585858585858585858585858585810021020"
        "186020012801380340f1014864501958017044800103880101ba0103696f73",
    ),
    (
        19,
        "stop",
        "0a3c0a190a1508d70112105858585858585858585858585858585810011020"
        "186020012801380340f1014864501958017013800103880101ba0103696f73",
    ),
    (
        178,
        "start",
        "0a3d0a190a1508d70112105858585858585858585858585858585810021020"
        "186020012801380340f10148645019580170b201800103880101ba0103696f73",
    ),
]


@pytest.mark.parametrize("seq,action,hex_frame", APP_FRAMES)
def test_builder_reproduces_the_four_app_frames_byte_for_byte(
    seq: int, action: str, hex_frame: str
) -> None:
    built = build_powerpulse_charge_action_payload(action, DEV_ADDR, DEV_SN, seq=seq)
    assert built == bytes.fromhex(hex_frame)


def test_pdata_is_25_bytes_and_dev_info_is_21_with_field_1_before_field_2() -> None:
    built = build_powerpulse_charge_action_payload("stop", DEV_ADDR, DEV_SN, seq=85)
    headers, _ = decode_header_message(built)
    header = headers[0]

    assert header["cmd_func"] == 241
    assert header["cmd_id"] == 100
    assert header["dest"] == 96
    assert "device_sn" not in header  # field 25: the app frames carry none
    assert "product_id" not in header  # field 15: the app frames carry none

    pdata = bytes.fromhex(header["pdata"])
    assert len(pdata) == 25
    # dev_info: outer tag 0x0a (field 1, length-delimited), length 0x15 (21).
    assert pdata[0:2] == bytes([0x0A, 0x15])
    dev_info = pdata[2:23]
    assert len(dev_info) == 21
    # dev_addr (field 1, varint) comes before dev_sn (field 2, bytes).
    assert dev_info[0] == 0x08


def test_existing_powerocean_envelope_still_carries_cmd_func_96() -> None:
    """The `cmd_func` default keeps every existing PowerOcean write intact."""
    built = _build_powerocean_set_envelope(b"\x08\x01", cmd_id=98, seq=1)
    headers, _ = decode_header_message(built)
    assert headers[0]["cmd_func"] == 96


@pytest.mark.parametrize(
    "action,dev_addr,dev_sn",
    [
        ("pause", DEV_ADDR, DEV_SN),
        ("stop", DEV_ADDR, ""),
        ("stop", DEV_ADDR, "X" * 15),
    ],
)
def test_builder_rejects_bad_input(action: str, dev_addr: int, dev_sn: str) -> None:
    with pytest.raises(ValueError):
        build_powerpulse_charge_action_payload(action, dev_addr, dev_sn)


def test_builder_rejects_a_serial_with_control_characters() -> None:
    """Sixteen ASCII bytes are not a serial when they are not alphanumeric:
    a nested submessage of small values is ASCII too (review finding of
    2026-09-11)."""
    with pytest.raises(ValueError):
        build_powerpulse_charge_action_payload(
            "stop", 215, "C376TEST0000\x01\x02\x03\x04"
        )


def test_settings_report_yields_the_descriptor_on_all_nine_frames() -> None:
    frames = json.loads(RUN_DATA_SYNC_FIXTURE.read_text())["frames"]
    checked = 0
    for frame in frames:
        result = parse_powerpulse_message(bytes.fromhex(frame["hex"]))
        assert result is not None
        assert result["ev_charger_dev_addr"] == 215
        assert result["ev_charger_sn"] == "X" * 16
        checked += 1
    # A floor on frames actually compared, not on the loop itself - catches
    # a parser that quietly stops decoding partway through the fixture.
    assert checked == 9


def test_settings_report_without_the_marker_yields_no_descriptor() -> None:
    """A settings body without `dev_info` fields is a clean decode, not an error."""
    dev_info = b""  # no dev_addr (field 1), no dev_sn (field 2)
    body = encode_field_bytes(1, dev_info)  # EDevRunDataSync.dev_info
    pdata = encode_field_bytes(1, body)  # pdata.f1 = EDevRunDataSync body

    built = _build_powerocean_set_envelope(pdata, cmd_id=44, cmd_func=241)
    result = parse_powerpulse_message(built)

    if result is not None:
        assert "ev_charger_dev_addr" not in result
        assert "ev_charger_sn" not in result


def test_settings_report_rejects_a_serial_that_is_not_16_bytes() -> None:
    """`dev_sn` present but the wrong length must not become a descriptor."""
    from ecoflow_energy.ecoflow.proto_encoding import encode_field_varint

    dev_info = encode_field_varint(1, DEV_ADDR) + encode_field_bytes(2, b"short")
    body = encode_field_bytes(1, dev_info)
    pdata = encode_field_bytes(1, body)

    built = _build_powerocean_set_envelope(pdata, cmd_id=44, cmd_func=241)
    result = parse_powerpulse_message(built)

    if result is not None:
        assert "ev_charger_dev_addr" not in result
        assert "ev_charger_sn" not in result


def test_settings_report_rejects_a_serial_that_is_not_alphanumeric() -> None:
    """Sixteen ASCII bytes of small values are what a nested submessage looks
    like, not a serial (review finding of 2026-09-11)."""
    from ecoflow_energy.ecoflow.proto_encoding import encode_field_varint

    not_a_serial = b"C376TEST0000" + bytes([1, 2, 3, 4])
    dev_info = encode_field_varint(1, DEV_ADDR) + encode_field_bytes(2, not_a_serial)
    body = encode_field_bytes(1, dev_info)
    pdata = encode_field_bytes(1, body)

    built = _build_powerocean_set_envelope(pdata, cmd_id=44, cmd_func=241)
    result = parse_powerpulse_message(built)

    if result is not None:
        assert "ev_charger_dev_addr" not in result
        assert "ev_charger_sn" not in result


def test_heartbeat_parsing_is_unchanged() -> None:
    frames = json.loads(HEARTBEAT_FIXTURE.read_text())["frames"]
    frame = next(f for f in frames if f["cmds"][0]["cmd_id"] == 33)
    result = parse_powerpulse_message(bytes.fromhex(frame["hex"]))

    assert result is not None
    assert "ev_charge_status" in result
    assert "ev_charger_dev_addr" not in result
    assert "ev_charger_sn" not in result
