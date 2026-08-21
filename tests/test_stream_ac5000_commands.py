"""Tests for the STREAM AC 5000 (ES22) SET command builders.

The three vectors below are the EcoFlow app's own frames, captured from the
`/app/{uid}/{sn}/thing/property/set` topic while the settings were changed in
the app. Rebuilding them byte for byte is what makes this a reproduction of a
verified format rather than an interpretation of one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecoflow_energy.const import supports_stream_ac5000_controls
from ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from ecoflow_energy.ecoflow.stream_ac5000_commands import (
    CMD_FUNC_CONFIG,
    CMD_ID_CONFIG_WRITE,
    TASK_ADD,
    _build_envelope,
    TASK_REMOVE,
    TASK_UPDATE,
    WORK_MODES,
    build_backup_reserve_payload,
    build_backup_socket_payload,
    build_grid_output_power_payload,
    build_soc_limits_payload,
    build_task_payload,
    build_work_mode_payload,
)

# Writes the EcoFlow app sent to a live ES21, and the device's answer to one.
ES21_WRITE_FRAMES = (
    Path(__file__).parent
    / "fixtures"
    / "stream_ac5000"
    / "es21_write_frames_masked.json"
)

# The capture masks the serial as a run of X of the same length, so the frames
# still rebuild byte for byte from it.
_ES21_MASKED_SN = "X" * 16

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


def _es21_frame(index: int) -> dict:
    return json.loads(ES21_WRITE_FRAMES.read_text())["frames"][index]


def _config_fields(frame: bytes) -> dict[int, dict[int, int]]:
    """Read a config write or readback as {field: {subfield: value}}.

    A write names the field it changes in field 1 and puts the contents in
    the field of that number. A readback carries the contents the same way
    but no field 1, so the number is read off the field itself in both
    directions and field 1 skipped where it appears.
    """
    pdata = bytes.fromhex(_header(frame)["pdata"])
    fields: dict[int, dict[int, int]] = {}
    for number, value in _walk(pdata):
        if number == 1 or not isinstance(value, bytes):
            continue
        fields[number] = {sub: v for sub, v in _walk(value) if isinstance(v, int)}
    return fields


def _walk(buf: bytes):
    """Yield (field number, value) for one level of a protobuf message."""
    offset = 0
    while offset < len(buf):
        key, offset = _varint(buf, offset)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, offset = _varint(buf, offset)
        elif wire == 2:
            length, offset = _varint(buf, offset)
            value, offset = buf[offset : offset + length], offset + length
        else:  # pragma: no cover - not present in these frames
            raise AssertionError(f"unexpected wire type {wire}")
        yield number, value


def _varint(buf: bytes, offset: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, offset


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


class TestES21WriteFrames:
    """The app's own writes to a live ES21, and the device confirming one.

    The gate on this family was an allowlist because reading a device is no
    evidence that it accepts a config write. These frames are that evidence:
    the reporter of #231 lowered the grid-tied output on his STREAM 5000 and
    set it back while the raw capture was running, and the download carries
    both what the app sent and what the device answered.
    """

    def test_the_app_envelope_to_an_es21_is_the_one_we_build(self) -> None:
        """Rebuild a captured ES21 frame from its own payload, byte for byte.

        The payload is lifted out of the recorded frame rather than
        constructed, so what is under test is the envelope alone: every header
        field, its order, and the negative product id. A difference anywhere in
        it means the app addresses an ES21 differently from an ES22, which is
        exactly what the allowlist was holding out for.
        """
        recorded = bytes.fromhex(_es21_frame(1)["hex"])
        header = _header(recorded)

        rebuilt = _build_envelope(
            bytes.fromhex(header["pdata"]),
            device_sn=_ES21_MASKED_SN,
            seq=header["seq"],
        )

        assert rebuilt == recorded

    def test_the_second_captured_write_rebuilds_as_well(self) -> None:
        """The same envelope with a different seq and a different value.

        One frame could be matched by a builder that is accidentally right
        for one sequence number. Two, recorded thirteen seconds apart with
        seq 7 and 8 and carrying 1000 W and 2000 W, cannot be.
        """
        recorded = bytes.fromhex(_es21_frame(3)["hex"])
        header = _header(recorded)

        rebuilt = _build_envelope(
            bytes.fromhex(header["pdata"]),
            device_sn=_ES21_MASKED_SN,
            seq=header["seq"],
        )

        assert rebuilt == recorded
        assert _config_fields(recorded)[10][1] == 2000
        assert header["seq"] != _header(bytes.fromhex(_es21_frame(1)["hex"]))["seq"]

    def test_the_session_opener_is_a_different_command(self) -> None:
        """The app opens with cmd_id 37, which nothing here builds.

        Pinned so the default in `_build_envelope` cannot drift onto it
        unnoticed: every control this integration offers writes on 38.
        """
        header = _header(bytes.fromhex(_es21_frame(0)["hex"]))

        assert header["cmd_func"] == CMD_FUNC_CONFIG
        assert header["cmd_id"] == 37
        assert CMD_ID_CONFIG_WRITE != 37

    def test_the_captured_write_is_a_config_write_on_the_shared_command(self) -> None:
        header = _header(bytes.fromhex(_es21_frame(1)["hex"]))

        assert header["cmd_func"] == CMD_FUNC_CONFIG
        assert header["cmd_id"] == CMD_ID_CONFIG_WRITE

    def test_the_device_reports_the_written_value_back(self) -> None:
        """The write said 1000 W and the device's next report agrees.

        Without this the capture would only show a frame leaving the phone.
        Field 10 is the grid-tied output setpoint; it is not written by this
        integration, and it is here because it is the field the reporter
        happened to change.
        """
        written = _config_fields(bytes.fromhex(_es21_frame(1)["hex"]))
        reported = _config_fields(bytes.fromhex(_es21_frame(2)["hex"]))

        assert reported[10][1] == written[10][1]
        # Pinned as well, so a helper that returned the same empty answer for
        # both frames could not satisfy the line above.
        assert written[10][1] == 1000

    def test_es21_is_allowed_to_be_written_to(self) -> None:
        assert supports_stream_ac5000_controls("ES21" + "0" * 12)

    def test_an_unrecorded_prefix_is_still_held_back(self) -> None:
        assert not supports_stream_ac5000_controls("ES29" + "0" * 12)


class TestGridOutputPower:
    """Config field 10, the one write recorded on the model it is offered to.

    Every other builder here reproduces an ES22 frame. This one reproduces an
    ES21 frame from #231, and the device answered it, so the two recorded
    writes are the vectors rather than an illustration of them.
    """

    def test_it_rebuilds_the_recorded_write_at_1000_w(self) -> None:
        recorded = bytes.fromhex(_es21_frame(1)["hex"])

        built = build_grid_output_power_payload(
            1000, 21, 800, _ES21_MASKED_SN, seq=_header(recorded)["seq"]
        )

        assert built == recorded

    def test_it_rebuilds_the_recorded_write_at_2000_w(self) -> None:
        recorded = bytes.fromhex(_es21_frame(3)["hex"])

        built = build_grid_output_power_payload(
            2000, 21, 800, _ES21_MASKED_SN, seq=_header(recorded)["seq"]
        )

        assert built == recorded

    def test_the_companion_values_reach_the_wire(self) -> None:
        """Not decoration: an ES22 reported 5 and 600 where this ES21 has 21
        and 800, so a builder ignoring them would send one unit's numbers to
        another and no test comparing only the setpoint would notice.
        """
        frame = build_grid_output_power_payload(1000, 5, 600, _ES21_MASKED_SN, seq=7)

        assert _config_fields(frame)[10] == {1: 1000, 4: 5, 5: 600}

    def test_the_setpoint_is_the_only_value_the_caller_chooses(self) -> None:
        as_written = _config_fields(
            build_grid_output_power_payload(1500, 21, 800, _ES21_MASKED_SN, seq=7)
        )

        assert as_written[10] == {1: 1500, 4: 21, 5: 800}

    @pytest.mark.parametrize(
        ("power", "field_4", "field_5"),
        [(-1, 21, 800), (1000, -1, 800), (1000, 21, -1)],
    )
    def test_a_negative_value_is_rejected(
        self, power: int, field_4: int, field_5: int
    ) -> None:
        with pytest.raises(ValueError):
            build_grid_output_power_payload(power, field_4, field_5, _ES21_MASKED_SN)

    def test_no_upper_bound_is_enforced_here(self) -> None:
        """The device carries its own ceiling and clamps silently, so a limit
        in the builder could only be wrong. The control bounds itself.
        """
        frame = build_grid_output_power_payload(9999, 21, 800, _ES21_MASKED_SN, seq=7)

        assert _config_fields(frame)[10][1] == 9999


class TestGridOutputRoundTrip:
    """Parse a real report, build a real write, compare to the recorded one.

    The two halves of this control were tested separately: the parser against
    a synthetic frame, the builder against the recorded write with the
    companion values typed in by hand. Nothing joined them, so the parser
    could read the wrong subfield and the builder would faithfully put that
    wrong value on the wire with the whole suite green. Two mutations on
    2026-08-21 proved it, which is what this closes.
    """

    def test_the_values_the_parser_reads_rebuild_the_recorded_write(self) -> None:
        from ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import (
            parse_stream_ac5000_message,
        )

        # The reporter's own unit reporting its configuration...
        reported: dict = {}
        for frame in json.loads(
            (
                Path(__file__).parent
                / "fixtures"
                / "stream_ac5000"
                / "es21_pv_masked.json"
            ).read_text()
        )["frames"]:
            reported.update(parse_stream_ac5000_message(bytes.fromhex(frame["hex"])) or {})

        # ...and the same unit's app write, recorded a few days later.
        recorded = bytes.fromhex(_es21_frame(1)["hex"])

        built = build_grid_output_power_payload(
            1000,
            reported["_grid_output_field_4"],
            reported["_grid_output_field_5"],
            _ES21_MASKED_SN,
            seq=_header(recorded)["seq"],
        )

        assert built == recorded

    def test_the_parsed_ceiling_would_admit_the_recorded_setpoint(self) -> None:
        """The bound and the value it bounds, from the same unit."""
        from ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import (
            parse_stream_ac5000_message,
        )

        reported: dict = {}
        for frame in json.loads(
            (
                Path(__file__).parent
                / "fixtures"
                / "stream_ac5000"
                / "es21_pv_masked.json"
            ).read_text()
        )["frames"]:
            reported.update(parse_stream_ac5000_message(bytes.fromhex(frame["hex"])) or {})

        written = _config_fields(bytes.fromhex(_es21_frame(3)["hex"]))[10][1]

        assert written <= reported["_grid_output_ceiling_w"]
