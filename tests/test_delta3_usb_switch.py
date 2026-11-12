"""Delta 3 USB output switch (#685).

Every frame comes from the reporter's diagnostics of a P231: the app's own
writes of ConfigWrite field 19, the device's acks, and the status frames whose
field 13 (`flow_info_qcusb1`) reports the USB output. On is bit 1: the unit
reported 14 and 15 while on, 4 and 5 while off.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ecoflow_energy.ecoflow.const import DEVICE_TYPE_DELTA3
from ecoflow_energy.ecoflow.delta3_commands import (
    DELTA3_SWITCH_PARAMS,
    build_proto_payload,
    build_switch_command,
)
from ecoflow_energy.ecoflow.parsers.delta3_http import parse_delta3_http_quota
from ecoflow_energy.ecoflow.parsers.delta3_proto import (
    parse_delta3_display_property,
)
from ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from ecoflow_energy.ecoflow.proto.runtime import decode_proto_runtime_frame

FIXTURE = Path(__file__).parent / "fixtures" / "delta3" / "p231_usb_toggle_685.json"
P231_SN = "P231TESTDUMMY001"


def _frames(topic: str) -> list[dict]:
    frames = json.loads(FIXTURE.read_text())["frames"]
    return [f for f in frames if f["topic"] in topic.split("|")]


def _status(frame_hex: str) -> dict:
    result = decode_proto_runtime_frame(
        bytes.fromhex(frame_hex), device_type=DEVICE_TYPE_DELTA3
    )
    assert result.mapped["_is_delta3_display"] is True
    return parse_delta3_display_property(
        {k: v for k, v in result.mapped.items() if not k.startswith("_")}
    )


class TestReadBack:
    def test_recorded_status_frames_follow_the_switch(self) -> None:
        """Field 13 = 14 and 15 read on, 4 and 5 read off, as recorded."""
        by_raw: dict[int, int] = {}
        for frame in _frames("property|get_reply"):
            raw = decode_proto_runtime_frame(
                bytes.fromhex(frame["hex"]), device_type=DEVICE_TYPE_DELTA3
            ).mapped["flow_info_qcusb1"]
            by_raw[raw] = _status(frame["hex"])["usb_out_flow"]
        assert by_raw == {14: 1, 15: 1, 5: 0, 4: 0}

    @pytest.mark.parametrize(
        ("raw", "expected"), [(14, 1), (15, 1), (4, 0), (5, 0), (0, 0), (2, 1)]
    )
    def test_usb_flow_reads_bit_1(self, raw: int, expected: int) -> None:
        """5 is the trap: the {0, 4} rule would read it as on."""
        assert parse_delta3_http_quota({"flowInfoQcusb1": raw})["usb_out_flow"] == (
            expected
        )

    def test_absent_field_publishes_nothing(self) -> None:
        assert "usb_out_flow" not in parse_delta3_http_quota({"flowInfoAcOut": 14})

    @pytest.mark.parametrize(
        ("raw", "expected"), [(0, 0), (2, 1), (4, 0), (5, 1), (14, 1)]
    )
    def test_existing_ports_keep_their_rule(self, raw: int, expected: int) -> None:
        """AC, AC2 and 12 V stay on the {0, 4} rule: 5 still reads on there."""
        parsed = parse_delta3_http_quota(
            {"flowInfoAcOut": raw, "flowInfoAc2Out": raw, "flowInfo12v": raw}
        )
        assert parsed["ac_out_flow"] == expected
        assert parsed["ac2_out_flow"] == expected
        assert parsed["dc_12v_out_flow"] == expected


class TestCommand:
    def test_row_matches_the_app_write(self) -> None:
        assert DELTA3_SWITCH_PARAMS["usb_out_switch"].params_key == "cfgUsbOpen"
        assert DELTA3_SWITCH_PARAMS["usb_out_switch"].config_field == 19

    @pytest.mark.parametrize("turn_on", [True, False])
    def test_frame_equals_the_app_write(self, turn_on: bool) -> None:
        """Header and payload match each app write on file, both recordings."""
        app_writes = [
            f
            for f in _frames("set")
            if decode_header_message(bytes.fromhex(f["hex"]))[0][0]["pdata"]
            == ("9801" + ("01" if turn_on else "00"))
        ]
        assert len(app_writes) == 2

        command = build_switch_command("usb_out_switch", turn_on)
        assert command is not None
        assert command["params"] == {"cfgUsbOpen": turn_on}
        ours = decode_header_message(build_proto_payload(command, P231_SN, seq=1))[0]
        for write in app_writes:
            app = decode_header_message(bytes.fromhex(write["hex"]))[0]
            for key in ("pdata", "cmd_func", "cmd_id", "dest", "need_ack"):
                assert ours[0].get(key) == app[0].get(key), key

    def test_device_acked_both_values(self) -> None:
        """Pins the evidence in the recording, not code under test.

        Every app write on file was answered with 254/18 applied (field 2 = 1),
        which is what the command row above rests on.
        """
        acks = [
            decode_header_message(bytes.fromhex(f["hex"]))[0][0]
            for f in _frames("set_reply")
        ]
        assert len(acks) == 4
        for ack in acks:
            assert (ack["cmd_func"], ack["cmd_id"]) == (254, 18)
            assert ack["pdata"].startswith("08131001")  # field 1 = 19, field 2 = 1
