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
    build_powerpulse_param_set_current_payload,
    build_powerpulse_param_set_mode_payload,
    build_powerpulse_standalone_charge_ctrl_payload,
    build_powerpulse_standalone_current_ctrl_payload,
)
from ecoflow_energy.ecoflow.parsers.powerpulse_proto import parse_powerpulse_message
from ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from ecoflow_energy.ecoflow.proto_encoding import encode_field_bytes

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "powerpulse"
RUN_DATA_SYNC_FIXTURE = FIXTURE_DIR / "c376_run_data_sync_20260824.json"
HEARTBEAT_FIXTURE = FIXTURE_DIR / "c376_frames_plan132.json"
STANDALONE_CTRL_FIXTURE = FIXTURE_DIR / "c376_standalone_charge_ctrl_20260912.json"
STANDALONE_CURRENT_CTRL_FIXTURE = (
    FIXTURE_DIR / "c376_standalone_current_ctrl_20260914.json"
)
PARAM_SET_WRITES_FIXTURE = FIXTURE_DIR / "c376_param_set_writes_20260824.json"
CHARGE_MODE_WRITES_FIXTURE = FIXTURE_DIR / "c376_charging_mode_writes_20260913.json"

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


# (frame index, seq, action) - the five `set` frames of one recording of a
# real charging session stopped, started, stopped, started and stopped from
# the vendor app on an account with no PowerOcean (PLAN-140, ADR-009
# addendum of 2026-09-12).
STANDALONE_SET_FRAMES = [
    (2, 7, "stop"),
    (4, 8, "start"),
    (9, 100, "stop"),
    (13, 125, "start"),
    (24, 126, "stop"),
]


@pytest.mark.parametrize("frame_index,seq,action", STANDALONE_SET_FRAMES)
def test_standalone_builder_reproduces_the_five_app_frames_byte_for_byte(
    frame_index: int, seq: int, action: str
) -> None:
    frames = json.loads(STANDALONE_CTRL_FIXTURE.read_text())["frames"]
    expected = bytes.fromhex(frames[frame_index]["hex"])
    built = build_powerpulse_standalone_charge_ctrl_payload(action, DEV_SN, seq=seq)
    assert built == expected


def test_standalone_stop_envelope_addresses_module_2_with_the_wallbox_serial() -> None:
    built = build_powerpulse_standalone_charge_ctrl_payload("stop", DEV_SN, seq=7)
    headers, _ = decode_header_message(built)
    header = headers[0]

    assert header["dest"] == 2
    assert header["cmd_func"] == 2
    assert header["cmd_id"] == 81
    assert header["need_ack"] == 1
    assert header["device_sn"] == DEV_SN
    assert bytes.fromhex(header["pdata"]) == b"\x20\x02"


def test_standalone_start_envelope_carries_the_start_pdata() -> None:
    built = build_powerpulse_standalone_charge_ctrl_payload("start", DEV_SN, seq=8)
    headers, _ = decode_header_message(built)
    assert bytes.fromhex(headers[0]["pdata"]) == b"\x20\x01"


def test_relayed_envelope_default_dest_did_not_move() -> None:
    """The `dest` default stays 96 - only the standalone builder passes 2."""
    built = build_powerpulse_charge_action_payload("stop", DEV_ADDR, DEV_SN, seq=85)
    headers, _ = decode_header_message(built)
    assert headers[0]["dest"] == 96


@pytest.mark.parametrize(
    "action,device_sn",
    [
        ("pause", DEV_SN),
        ("stop", ""),
        ("stop", "X" * 15),
    ],
)
def test_standalone_builder_rejects_bad_input(action: str, device_sn: str) -> None:
    with pytest.raises(ValueError):
        build_powerpulse_standalone_charge_ctrl_payload(action, device_sn)


# --- Standalone charge current (issue #7, 2026-09-14 recording) ------------

# (frame index in the fixture's full frame list, seq, current_a in whole
# amps) - the two `set` frames of one recording carrying `current_ctrl`
# (field 5, deci-amps on the wire: 100 and 60) on a wallbox's own topic,
# without a PowerOcean in the account (PLAN-140).
STANDALONE_CURRENT_SET_FRAMES = [
    (12, 205, 10),
    (25, 27, 6),
]


@pytest.mark.parametrize("frame_index,seq,current_a", STANDALONE_CURRENT_SET_FRAMES)
def test_standalone_current_builder_reproduces_the_two_app_frames_byte_for_byte(
    frame_index: int, seq: int, current_a: int
) -> None:
    frames = json.loads(STANDALONE_CURRENT_CTRL_FIXTURE.read_text())["frames"]
    expected = bytes.fromhex(frames[frame_index]["hex"])
    built = build_powerpulse_standalone_current_ctrl_payload(current_a, DEV_SN, seq=seq)
    assert built == expected


def test_standalone_current_pdata_carries_field_5_before_field_7_in_deci_amps() -> None:
    """`current_ctrl` (field 5, tag 0x28) precedes `work_mode` (field 7, tag
    0x38) - the order both app frames use on the wire, verified against the
    fixture's raw bytes rather than assumed from the message definition."""
    built = build_powerpulse_standalone_current_ctrl_payload(6, DEV_SN, seq=27)
    headers, _ = decode_header_message(built)
    pdata = bytes.fromhex(headers[0]["pdata"])
    assert pdata == bytes.fromhex("283c3802")  # field5=60 (0.1A), field7=2


def test_standalone_current_envelope_differs_from_charge_ctrl_only_by_pdata() -> None:
    current_built = build_powerpulse_standalone_current_ctrl_payload(6, DEV_SN, seq=42)
    action_built = build_powerpulse_standalone_charge_ctrl_payload(
        "stop", DEV_SN, seq=42
    )
    h_current = decode_header_message(current_built)[0][0]
    h_action = decode_header_message(action_built)[0][0]

    for key in (
        "src",
        "dest",
        "d_src",
        "d_dest",
        "cmd_func",
        "cmd_id",
        "need_ack",
        "seq",
        "version",
        "payload_ver",
        "from",
        "device_sn",
    ):
        assert h_current[key] == h_action[key]

    assert h_current["data_len"] != h_action["data_len"]
    assert bytes.fromhex(h_current["pdata"]) != bytes.fromhex(h_action["pdata"])


@pytest.mark.parametrize(
    "current_a,device_sn",
    [
        (5, DEV_SN),
        (17, DEV_SN),
        (6.0, DEV_SN),
        (True, DEV_SN),
        ("6", DEV_SN),
        (6, ""),
        (6, "X" * 15),
    ],
)
def test_standalone_current_builder_rejects_bad_input(
    current_a: object, device_sn: str
) -> None:
    with pytest.raises(ValueError):
        build_powerpulse_standalone_current_ctrl_payload(current_a, device_sn)


def test_param_set_builder_reproduces_the_app_frame_byte_for_byte() -> None:
    """Seven app writes of one maximum-current recording (PLAN-146).

    Frame 0 (13:45:38, max_current_a=11) is rebuilt byte-for-byte from the
    builder's inputs. The other six writes set values this fixture does not
    decode a clean `max_current_a` for (later param changes on the same
    topic), so they are only checked at the envelope level below - the
    positive control that the fixture is what the docstring claims.
    """
    frames = json.loads(PARAM_SET_WRITES_FIXTURE.read_text())["frames"]
    set_frames = [f for f in frames if f["topic"] == "set"]
    assert len(set_frames) == 7

    first = set_frames[0]
    expected = bytes.fromhex(first["hex"])
    headers, _ = decode_header_message(expected)
    seq = headers[0]["seq"]
    powerocean_sn = headers[0]["device_sn"]

    built = build_powerpulse_param_set_current_payload(
        max_current_a=11,
        dev_addr=DEV_ADDR,
        dev_sn=DEV_SN,
        powerocean_sn=powerocean_sn,
        seq=seq,
    )
    assert built == expected

    for frame in set_frames:
        header = decode_header_message(bytes.fromhex(frame["hex"]))[0][0]
        assert header["cmd_func"] == 241
        assert header["cmd_id"] == 102
        assert header["check_type"] == 3
        assert header["need_ack"] == 1
        assert header["version"] == 3
        assert header["payload_ver"] == 1
        assert header["from"] == "ios"
        assert "device_sn" in header


def test_param_set_pdata_layout() -> None:
    built = build_powerpulse_param_set_current_payload(
        max_current_a=11,
        dev_addr=DEV_ADDR,
        dev_sn=DEV_SN,
        powerocean_sn=DEV_SN,
        seq=149,
    )
    headers, _ = decode_header_message(built)
    pdata = bytes.fromhex(headers[0]["pdata"])

    assert len(pdata) == 27
    # dev_info (field 1, length-delimited, 21-byte content) comes first.
    assert pdata[0:2] == bytes([0x0A, 0x15])
    # EDevPileParamSet (field 4) follows, encoding {3: 110} (11.0 A).
    assert pdata[23:27] == bytes.fromhex("2202186e")


@pytest.mark.parametrize(
    "max_current_a,dev_sn,powerocean_sn",
    [
        (5, DEV_SN, DEV_SN),
        (17, DEV_SN, DEV_SN),
        (11.0, DEV_SN, DEV_SN),
        (True, DEV_SN, DEV_SN),
        ("11", DEV_SN, DEV_SN),
        (11, "short", DEV_SN),
        (11, DEV_SN, "short"),
    ],
)
def test_param_set_builder_rejects_bad_input(
    max_current_a: object, dev_sn: str, powerocean_sn: str
) -> None:
    with pytest.raises(ValueError):
        build_powerpulse_param_set_current_payload(
            max_current_a, DEV_ADDR, dev_sn, powerocean_sn
        )


def test_param_set_envelope_differs_from_charge_action_only_by_device_sn() -> None:
    charge_action = build_powerpulse_charge_action_payload(
        "start", DEV_ADDR, DEV_SN, seq=42
    )
    param_set = build_powerpulse_param_set_current_payload(
        max_current_a=11,
        dev_addr=DEV_ADDR,
        dev_sn=DEV_SN,
        powerocean_sn=DEV_SN,
        seq=42,
    )
    h_action = decode_header_message(charge_action)[0][0]
    h_param = decode_header_message(param_set)[0][0]

    for key in (
        "src",
        "dest",
        "d_src",
        "d_dest",
        "check_type",
        "cmd_func",
        "need_ack",
        "seq",
        "version",
        "payload_ver",
        "from",
    ):
        assert h_action[key] == h_param[key]

    assert h_action["cmd_id"] == 100
    assert h_param["cmd_id"] == 102
    assert h_action["data_len"] != h_param["data_len"]
    assert "device_sn" not in h_action
    assert h_param["device_sn"] == DEV_SN


# --- Charging mode (PLAN-147, @Xygen's capture of 2026-09-13, issue #7) -----


def test_mode_builder_reproduces_the_fast_write_byte_for_byte() -> None:
    """The Fast write (22:48:03.443 UTC) of the five recorded mode writes,
    reproduced byte-for-byte from the builder's inputs."""
    frames = json.loads(CHARGE_MODE_WRITES_FIXTURE.read_text())["frames"]
    set_frames = [f for f in frames if f["topic"] == "set"]
    assert len(set_frames) == 5

    fast = next(f for f in set_frames if f["ts_iso"].startswith("2026-09-13T22:48:03"))
    expected = bytes.fromhex(fast["hex"])
    headers, _ = decode_header_message(expected)
    seq = headers[0]["seq"]
    powerocean_sn = headers[0]["device_sn"]

    built = build_powerpulse_param_set_mode_payload(
        work_mode=1,
        dev_addr=DEV_ADDR,
        dev_sn=DEV_SN,
        powerocean_sn=powerocean_sn,
        seq=seq,
    )
    assert built == expected

    for frame in set_frames:
        header = decode_header_message(bytes.fromhex(frame["hex"]))[0][0]
        assert header["cmd_func"] == 241
        assert header["cmd_id"] == 102
        assert header["check_type"] == 3
        assert header["need_ack"] == 1
        assert header["version"] == 3
        assert header["payload_ver"] == 1
        assert header["from"] == "ios"
        assert "device_sn" in header


def test_mode_pdata_carries_only_work_mode() -> None:
    """The app's Solar and Custom writes carry extra fields
    (`switch_bits`/`solar_current_min`, `user_current_set`); this builder's
    `EDevPileParamSet` (field 4) is the bare setting alone, `10 02` for
    Solar and `10 03` for Custom - the shape the app itself uses for Fast
    (PLAN-147 decision 2)."""
    for work_mode in (2, 3):
        built = build_powerpulse_param_set_mode_payload(
            work_mode=work_mode,
            dev_addr=DEV_ADDR,
            dev_sn=DEV_SN,
            powerocean_sn=DEV_SN,
            seq=149,
        )
        headers, _ = decode_header_message(built)
        pdata = bytes.fromhex(headers[0]["pdata"])
        assert len(pdata) == 27
        # dev_info (field 1) is unchanged from the current-set builder.
        assert pdata[0:2] == bytes([0x0A, 0x15])
        # EDevPileParamSet (field 4): tag, length 2, bare {2: work_mode}.
        assert pdata[23:27] == bytes.fromhex(f"220210{work_mode:02x}")


@pytest.mark.parametrize(
    "work_mode,dev_sn,powerocean_sn",
    [
        (0, DEV_SN, DEV_SN),
        (5, DEV_SN, DEV_SN),
        (1.0, DEV_SN, DEV_SN),
        (True, DEV_SN, DEV_SN),
        ("1", DEV_SN, DEV_SN),
        (1, "short", DEV_SN),
        (1, DEV_SN, "short"),
    ],
)
def test_mode_builder_rejects_bad_input(
    work_mode: object, dev_sn: str, powerocean_sn: str
) -> None:
    with pytest.raises(ValueError):
        build_powerpulse_param_set_mode_payload(
            work_mode, DEV_ADDR, dev_sn, powerocean_sn
        )


def test_mode_envelope_differs_from_charge_action_only_by_device_sn() -> None:
    charge_action = build_powerpulse_charge_action_payload(
        "start", DEV_ADDR, DEV_SN, seq=42
    )
    mode_set = build_powerpulse_param_set_mode_payload(
        work_mode=1,
        dev_addr=DEV_ADDR,
        dev_sn=DEV_SN,
        powerocean_sn=DEV_SN,
        seq=42,
    )
    h_action = decode_header_message(charge_action)[0][0]
    h_mode = decode_header_message(mode_set)[0][0]

    for key in (
        "src",
        "dest",
        "d_src",
        "d_dest",
        "check_type",
        "cmd_func",
        "need_ack",
        "seq",
        "version",
        "payload_ver",
        "from",
    ):
        assert h_action[key] == h_mode[key]

    assert h_action["cmd_id"] == 100
    assert h_mode["cmd_id"] == 102
    assert h_action["data_len"] != h_mode["data_len"]
    assert "device_sn" not in h_action
    assert h_mode["device_sn"] == DEV_SN
