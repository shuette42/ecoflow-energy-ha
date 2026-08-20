"""Tests for the STREAM AC 5000 (ES22) SET command builders.

The three vectors below are the EcoFlow app's own frames, captured from the
`/app/{uid}/{sn}/thing/property/set` topic while the settings were changed in
the app. Rebuilding them byte for byte is what makes this a reproduction of a
verified format rather than an interpretation of one.
"""

from __future__ import annotations

import pytest

from ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from ecoflow_energy.ecoflow.stream_ac5000_commands import (
    TASK_ADD,
    TASK_REMOVE,
    TASK_UPDATE,
    WORK_MODES,
    build_backup_reserve_payload,
    build_backup_socket_payload,
    build_soc_limits_payload,
    build_task_payload,
    build_work_mode_payload,
)

# The serial of the unit the frames were captured from, replaced by a
# placeholder of the same length so the vectors still line up byte for byte.
SN = "ES22TEST00000147"

# Whole frame, SoC limits set to 90 / 20, captured at 15:17:16 with seq 14.
APP_SOC_FRAME_SEQ14 = (
    "0a5c0a09081dea0104085a1014102018022001280140fe01482650095801700e788cfffffffffff"
    "fffff01800104ba0107416e64726f6964d2011045533232544553543030303030313437da011045"
    "533232544553543030303030313437"
)

# pdata only, charge task 13:00-16:00 at 600 W, captured at 15:19:11.
APP_CHARGE_TASK_PDATA = (
    "0827ba02310a2f080210011801200028013a048c86801e421d080110001a170a10"
    "455332325445535430303030303134371064 18d804".replace(" ", "")
)

# pdata only, discharge task 00:00-23:00 at 600 W, captured at 15:32:55.
APP_DISCHARGE_TASK_PDATA = "0827ba02170a15080110021801200028013a048080902b4a0308d804"

# pdata only, backup reserve switched on at 55% and then changed to 30%.
APP_BACKUP_RESERVE_ON_55 = "081ef2010408011037"
APP_BACKUP_RESERVE_ON_30 = "081ef201040801101e"

# pdata only, the backup socket control toggled on and then off.
APP_BACKUP_SOCKET_ON = "08139a01020801"
APP_BACKUP_SOCKET_OFF = "08139a01020800"


def _pdata(frame: bytes) -> str:
    headers, _ = decode_header_message(frame)
    assert headers, "expected a decodable header frame"
    return headers[0]["pdata"]


def _header(frame: bytes) -> dict:
    headers, _ = decode_header_message(frame)
    assert headers
    return headers[0]


class TestCapturedFrameReproduction:
    def test_soc_limit_frame_matches_the_app_byte_for_byte(self) -> None:
        frame = build_soc_limits_payload(90, 20, SN, seq=14)

        assert frame.hex() == APP_SOC_FRAME_SEQ14

    def test_charge_task_payload_matches_the_app(self) -> None:
        frame = build_task_payload(
            "charge", 780, 960, 600, SN, operation=TASK_UPDATE, seq=1
        )

        assert _pdata(frame) == APP_CHARGE_TASK_PDATA

    def test_discharge_task_payload_matches_the_app(self) -> None:
        frame = build_task_payload(
            "discharge", 0, 1380, 600, SN, operation=TASK_ADD, seq=1
        )

        assert _pdata(frame) == APP_DISCHARGE_TASK_PDATA


class TestBackupReserve:
    """Config field 30 carries the on/off flag and the level together."""

    def test_matches_the_app_at_55_percent(self) -> None:
        frame = build_backup_reserve_payload(True, 55, SN, seq=1)

        assert _pdata(frame) == APP_BACKUP_RESERVE_ON_55

    def test_matches_the_app_at_30_percent(self) -> None:
        frame = build_backup_reserve_payload(True, 30, SN, seq=1)

        assert _pdata(frame) == APP_BACKUP_RESERVE_ON_30

    def test_off_clears_the_flag_and_keeps_the_level(self) -> None:
        pdata = bytes.fromhex(_pdata(build_backup_reserve_payload(False, 30, SN, seq=1)))

        assert pdata == bytes([0x08, 30, 0xF2, 0x01, 0x04, 0x08, 0, 0x10, 30])

    @pytest.mark.parametrize("reserve", [-1, 101])
    def test_out_of_range_is_rejected(self, reserve: int) -> None:
        with pytest.raises(ValueError):
            build_backup_reserve_payload(True, reserve, SN)


class TestBackupSocket:
    def test_on_matches_the_app(self) -> None:
        assert _pdata(build_backup_socket_payload(True, SN, seq=1)) == APP_BACKUP_SOCKET_ON

    def test_off_matches_the_app(self) -> None:
        assert _pdata(build_backup_socket_payload(False, SN, seq=1)) == APP_BACKUP_SOCKET_OFF


class TestEnvelope:
    def test_header_fields(self) -> None:
        header = _header(build_work_mode_payload("custom", SN, seq=42))

        assert int(header["cmd_func"]) == 254
        # 38, not the Delta 3 ConfigWrite id 17: a different command on a
        # different device family.
        assert int(header["cmd_id"]) == 38
        assert int(header["seq"]) == 42
        assert int(header["src"]) == 32
        assert int(header["dest"]) == 2
        assert int(header["need_ack"]) == 1

    def test_serial_is_required(self) -> None:
        with pytest.raises(ValueError):
            build_work_mode_payload("custom", "")

    def test_seq_is_generated_when_not_given(self) -> None:
        first = build_work_mode_payload("custom", SN)
        assert int(_header(first)["seq"]) > 0

    def test_same_inputs_produce_the_same_frame(self) -> None:
        assert build_work_mode_payload("custom", SN, seq=7) == build_work_mode_payload(
            "custom", SN, seq=7
        )


class TestWorkMode:
    @pytest.mark.parametrize(("mode", "wire"), sorted(WORK_MODES.items()))
    def test_each_mode_encodes_its_wire_value(self, mode: str, wire: int) -> None:
        pdata = bytes.fromhex(_pdata(build_work_mode_payload(mode, SN, seq=1)))

        # field 1 = 25 (which config field), then field 25 = the mode.
        assert pdata == bytes([0x08, 25, 0xC8, 0x01, wire])

    def test_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_work_mode_payload("eco", SN)


class TestSocLimits:
    def test_both_limits_travel_in_one_frame(self) -> None:
        pdata = bytes.fromhex(_pdata(build_soc_limits_payload(85, 20, SN, seq=1)))

        # field 1 = 29, then field 29 = {1: charge, 2: discharge}
        assert pdata == bytes([0x08, 29, 0xEA, 0x01, 0x04, 0x08, 85, 0x10, 20])

    def test_discharge_at_or_above_charge_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_soc_limits_payload(50, 50, SN)
        with pytest.raises(ValueError):
            build_soc_limits_payload(50, 60, SN)

    @pytest.mark.parametrize(("charge", "discharge"), [(101, 20), (90, -1)])
    def test_out_of_range_is_rejected(self, charge: int, discharge: int) -> None:
        with pytest.raises(ValueError):
            build_soc_limits_payload(charge, discharge, SN)


class TestTask:
    def test_window_packs_end_above_start(self) -> None:
        frame = build_task_payload("discharge", 0, 1380, 600, SN, seq=1)
        pdata = bytes.fromhex(_pdata(frame))

        # 0x05640000 = (1380 << 16) | 0
        assert bytes.fromhex("3a048080902b") in pdata

    def test_disabled_task_inverts_both_flags(self) -> None:
        enabled = bytes.fromhex(
            _pdata(build_task_payload("discharge", 0, 1380, 600, SN, seq=1))
        )
        disabled = bytes.fromhex(
            _pdata(
                build_task_payload(
                    "discharge", 0, 1380, 600, SN, enabled=False, seq=1
                )
            )
        )

        assert bytes.fromhex("18012000") in enabled
        assert bytes.fromhex("18002001") in disabled

    def test_charge_power_is_per_device_and_discharge_is_not(self) -> None:
        charge = bytes.fromhex(_pdata(build_task_payload("charge", 780, 960, 600, SN, seq=1)))
        discharge = bytes.fromhex(
            _pdata(build_task_payload("discharge", 0, 1380, 600, SN, seq=1))
        )

        assert SN.encode() in charge
        assert SN.encode() not in discharge

    def test_zero_power_is_allowed(self) -> None:
        """Zero means idle on this device, so it has to be writable."""
        frame = build_task_payload("discharge", 0, 1380, 0, SN, seq=1)

        # field 9, length 2, holding field 1 = 0
        assert bytes.fromhex("4a020800") in bytes.fromhex(_pdata(frame))

    @pytest.mark.parametrize(
        ("start", "end"),
        [(1380, 0), (600, 600), (-1, 600), (0, 1441)],
    )
    def test_invalid_window_is_rejected(self, start: int, end: int) -> None:
        with pytest.raises(ValueError):
            build_task_payload("discharge", start, end, 600, SN)

    def test_power_above_the_hardware_ceiling_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_task_payload("discharge", 0, 1380, 2600, SN)

    def test_unknown_kind_and_operation_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_task_payload("both", 0, 1380, 600, SN)
        with pytest.raises(ValueError):
            build_task_payload("discharge", 0, 1380, 600, SN, operation=9)

    def test_the_task_number_defaults_to_the_kind(self) -> None:
        """Which is what keeps the two tasks written here from colliding."""
        charge = bytes.fromhex(_pdata(build_task_payload("charge", 780, 960, 600, SN, seq=1)))
        discharge = bytes.fromhex(
            _pdata(build_task_payload("discharge", 0, 1380, 600, SN, seq=1))
        )

        assert bytes.fromhex("1001") in charge
        assert bytes.fromhex("1002") in discharge

    def test_an_observed_number_overrides_the_kind_but_not_the_power_block(self) -> None:
        """A removal has to name the task the device knows.

        The number and the power block are decided separately: the number is
        whatever the device reported, and the block still follows `kind`, or a
        discharge removal would carry no discharge power to remove.
        """
        pdata = bytes.fromhex(
            _pdata(
                build_task_payload(
                    "discharge", 0, 1439, 0, SN,
                    operation=TASK_REMOVE, task_slot=1, seq=1,
                )
            )
        )

        # operation 3 (remove) carrying the observed number 1, not the kind's 2
        assert bytes.fromhex("08031001") in pdata
        # field 9, length 2, holding field 1 = 0: still a discharge block
        assert bytes.fromhex("4a020800") in pdata

    def test_an_unknown_kind_is_still_rejected_with_a_number_given(self) -> None:
        with pytest.raises(ValueError):
            build_task_payload("both", 0, 1380, 600, SN, task_slot=1)

    @pytest.mark.parametrize("slot", [0, -1, 70000])
    def test_an_out_of_range_task_number_is_rejected(self, slot: int) -> None:
        with pytest.raises(ValueError):
            build_task_payload("discharge", 0, 1380, 600, SN, task_slot=slot)


def test_work_mode_write_and_read_tables_are_inverses() -> None:
    """The mode a write sends must be the mode a read reports back.

    `WORK_MODES` turns a Home Assistant option into a wire value; the parser's
    `_WORK_MODE` turns that wire value back into the option. They are two hand
    written tables in two files describing one fact, so nothing but this
    assertion stops an edit to one of them from making a written mode read back
    as a different mode - silently, and hidden for the length of the optimistic
    lock.
    """
    from ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import _WORK_MODE

    assert {value: option for option, value in WORK_MODES.items()} == _WORK_MODE
