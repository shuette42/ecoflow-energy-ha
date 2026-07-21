"""Tests for the Delta 3 SET command builders.

The envelope and the value ranges are vendor-specified, see
docs/reference/ecoflow-api-delta3-max-plus.md. These tests pin both so a
refactor cannot silently change what reaches the device.
"""

from __future__ import annotations

import pytest

from custom_components.ecoflow_energy.ecoflow.delta3_commands import (
    DELTA3_NUMBER_PARAMS,
    DELTA3_SWITCH_PARAMS,
    build_number_command,
    build_proto_command,
    build_switch_command,
    parse_config_write_ack,
)
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

EXPECTED_ENVELOPE = {
    "cmdId": 17,
    "cmdFunc": 254,
    "dest": 2,
    "dirDest": 1,
    "dirSrc": 1,
    "needAck": True,
}


class TestEnvelope:
    def test_every_switch_carries_the_documented_envelope(self) -> None:
        for key in DELTA3_SWITCH_PARAMS:
            cmd = build_switch_command(key, True)
            assert cmd is not None
            for field, value in EXPECTED_ENVELOPE.items():
                assert cmd[field] == value, f"{key}: {field}"

    def test_every_number_carries_the_documented_envelope(self) -> None:
        for key in DELTA3_NUMBER_PARAMS:
            cmd = build_number_command(key, 10)
            assert cmd is not None
            for field, value in EXPECTED_ENVELOPE.items():
                assert cmd[field] == value, f"{key}: {field}"

    def test_source_direction_is_dir_src_not_dir_soc(self) -> None:
        """The MQTT sample in the vendor docs spells this `dirSoc`.

        That reads like a typo for a direction *source*, and the HTTP samples
        all use `dirSrc`. Pinned so nobody "fixes" it back.
        """
        cmd = build_switch_command("ac_out_switch", True)
        assert "dirSrc" in cmd
        assert "dirSoc" not in cmd


class TestSwitchCommands:
    @pytest.mark.parametrize(
        ("key", "params_key"),
        [
            ("ac_out_switch", "cfgAcOutOpen"),
            ("ac2_out_switch", "cfgAc2OutOpen"),
            ("dc_12v_out_switch", "cfgDc12vOutOpen"),
            ("xboost_switch", "cfgXboostEn"),
            ("beeper_switch", "cfgBeepEn"),
            ("bypass_out_disable_switch", "cfgBypassOutDisable"),
        ],
    )
    def test_flat_switches_map_to_the_documented_param(
        self, key: str, params_key: str
    ) -> None:
        assert build_switch_command(key, True)["params"] == {params_key: True}
        assert build_switch_command(key, False)["params"] == {params_key: False}

    def test_energy_backup_is_nested(self) -> None:
        """cfgEnergyBackup is the only control with a nested payload."""
        cmd = build_switch_command("energy_backup_switch", True)
        assert cmd["params"] == {"cfgEnergyBackup": {"energyBackupEn": True}}

    def test_values_are_real_booleans(self) -> None:
        """The contract types these as bool, not 0/1."""
        params = build_switch_command("ac_out_switch", 1)["params"]
        assert params["cfgAcOutOpen"] is True

    def test_unknown_key_returns_none(self) -> None:
        assert build_switch_command("no_such_switch", True) is None


class TestNumberCommands:
    @pytest.mark.parametrize(
        ("key", "params_key", "value"),
        [
            ("backup_reserve_soc", "cfgBackupReverseSoc", 41),
            ("max_charge_soc", "cfgMaxChgSoc", 80),
            ("min_discharge_soc", "cfgMinDsgSoc", 20),
        ],
    )
    def test_in_range_values_pass_through(
        self, key: str, params_key: str, value: int
    ) -> None:
        assert build_number_command(key, value)["params"] == {params_key: value}

    @pytest.mark.parametrize(
        ("key", "params_key", "low", "high"),
        [
            ("backup_reserve_soc", "cfgBackupReverseSoc", 0, 50),
            ("max_charge_soc", "cfgMaxChgSoc", 50, 100),
            ("min_discharge_soc", "cfgMinDsgSoc", 0, 30),
        ],
    )
    def test_values_are_clamped_to_the_vendor_range(
        self, key: str, params_key: str, low: int, high: int
    ) -> None:
        assert build_number_command(key, low - 10)["params"][params_key] == low
        assert build_number_command(key, high + 10)["params"][params_key] == high
        assert build_number_command(key, low)["params"][params_key] == low
        assert build_number_command(key, high)["params"][params_key] == high

    def test_backup_reserve_tops_out_at_fifty_not_hundred(self) -> None:
        """Easy to get wrong: this is a ratio, not a SoC target."""
        assert build_number_command("backup_reserve_soc", 100)["params"][
            "cfgBackupReverseSoc"
        ] == 50

    def test_float_input_is_rounded_to_int(self) -> None:
        params = build_number_command("max_charge_soc", 79.6)["params"]
        assert params["cfgMaxChgSoc"] == 80
        assert isinstance(params["cfgMaxChgSoc"], int)

    def test_unknown_key_returns_none(self) -> None:
        assert build_number_command("no_such_number", 50) is None


class TestProtoCommands:
    """The binary variant used by app logins, which have no HTTP endpoint.

    The frame layout is hardware-verified (ack plus readback of the new
    value). These tests pin the payload bytes so a refactor cannot silently
    change which setting reaches the device.
    """

    SN = "D3M1TESTSN000000"

    def _pdata(self, frame: bytes) -> str:
        """Return the ConfigWrite payload hex out of a full SET frame."""
        headers, _ = decode_header_message(frame)
        assert len(headers) == 1
        return headers[0]["pdata"]

    @pytest.mark.parametrize(
        ("key", "expected_pdata"),
        [
            ("beeper_switch", "4801"),                  # field 9
            ("ac_out_switch", "e00401"),                # field 76
            ("ac2_out_switch", "c81701"),               # field 377
            ("dc_12v_out_switch", "900101"),            # field 18
            ("xboost_switch", "c80101"),                # field 25
            ("bypass_out_disable_switch", "d00101"),    # field 26
            ("energy_backup_switch", "da02020801"),     # field 43, nested
        ],
    )
    def test_switch_payload_bytes(self, key: str, expected_pdata: str) -> None:
        frame = build_proto_command(build_switch_command(key, True), self.SN)
        assert frame is not None
        assert self._pdata(frame) == expected_pdata

    def test_switch_off_writes_zero(self) -> None:
        frame = build_proto_command(build_switch_command("beeper_switch", False), self.SN)
        assert self._pdata(frame) == "4800"

    @pytest.mark.parametrize(
        ("key", "value", "expected_pdata"),
        [
            ("max_charge_soc", 80, "880250"),        # field 33
            ("min_discharge_soc", 10, "90020a"),     # field 34
            ("backup_reserve_soc", 30, "b0061e"),    # field 102
        ],
    )
    def test_number_payload_bytes(
        self, key: str, value: int, expected_pdata: str
    ) -> None:
        frame = build_proto_command(build_number_command(key, value), self.SN)
        assert frame is not None
        assert self._pdata(frame) == expected_pdata

    def test_number_values_stay_inside_the_vendor_range(self) -> None:
        """The clamp is shared with the HTTP path, so it applies here too."""
        frame = build_proto_command(build_number_command("max_charge_soc", 200), self.SN)
        assert self._pdata(frame) == "880264"  # clamped to 100
        frame = build_proto_command(build_number_command("backup_reserve_soc", 99), self.SN)
        assert self._pdata(frame) == "b00632"  # clamped to 50

    def test_frame_carries_the_hardware_verified_header(self) -> None:
        frame = build_proto_command(
            build_switch_command("beeper_switch", True), self.SN
        )
        headers, _ = decode_header_message(frame)
        header = headers[0]
        assert header["src"] == 32
        assert header["dest"] == 2
        assert header["d_src"] == 1
        assert header["d_dest"] == 1
        assert header["check_type"] == 3
        assert header["cmd_func"] == 254
        assert header["cmd_id"] == 17
        assert header["data_len"] == 2
        assert header["need_ack"] == 1
        # Decoder field names: 16 -> "version", 17 -> "payload_ver".
        assert header["version"] == 3
        assert header["payload_ver"] == 1
        assert header["device_sn"] == self.SN
        # Fields 15 and 23 must stay absent - the device ignored the frame with them.
        assert "product_id" not in header
        assert "from" not in header

    def test_every_control_has_a_binary_counterpart(self) -> None:
        """One table drives both wires, so neither can lose a control."""
        for key in DELTA3_SWITCH_PARAMS:
            assert build_proto_command(build_switch_command(key, True), self.SN)
        for key in DELTA3_NUMBER_PARAMS:
            assert build_proto_command(build_number_command(key, 10), self.SN)

    def test_unknown_parameter_returns_none(self) -> None:
        command = {"cmdId": 17, "params": {"cfgSomethingElse": 1}}
        assert build_proto_command(command, self.SN) is None

    def test_multi_parameter_command_returns_none(self) -> None:
        """One frame carries exactly one setting."""
        command = {"cmdId": 17, "params": {"cfgBeepEn": True, "cfgXboostEn": True}}
        assert build_proto_command(command, self.SN) is None


class TestConfigWriteAck:
    """The device answers every write, and a rejection must be visible."""

    @staticmethod
    def _ack_frame(action_id: int, config_ok: int, cmd_id: int = 18) -> bytes:
        pdata = encode_field_varint(1, action_id) + encode_field_varint(2, config_ok)
        header = bytearray()
        header.extend(encode_field_bytes(1, pdata))
        header.extend(encode_field_varint(8, 254))
        header.extend(encode_field_varint(9, cmd_id))
        header.extend(encode_field_varint(12, 1))  # is_ack
        return encode_field_bytes(1, bytes(header))

    def test_success_ack(self) -> None:
        ack = parse_config_write_ack(self._ack_frame(9, 1))
        assert ack is not None
        assert ack.action_id == 9
        assert ack.config_ok == 1
        assert ack.applied is True

    def test_rejection_ack(self) -> None:
        ack = parse_config_write_ack(self._ack_frame(102, 0))
        assert ack is not None
        assert ack.action_id == 102
        assert ack.config_ok == 0
        assert ack.applied is False

    def test_other_frames_are_ignored(self) -> None:
        assert parse_config_write_ack(self._ack_frame(9, 1, cmd_id=21)) is None

    def test_garbage_does_not_raise(self) -> None:
        assert parse_config_write_ack(b"\xff\xff\xff") is None
