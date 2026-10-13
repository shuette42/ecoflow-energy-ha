"""Tests for the Ocean 2 SET builders (Backup Reserve, Storm Guard) and acks.

The golden frames are the EcoFlow app's own write and the device's answer,
captured from the `/app/{uid}/{sn}/thing/property/set` topic while the
reserve was changed in the app. The serial is masked to sixteen `X`, same
length, so no identity travels with the evidence. Rebuilding the write byte
for byte is what makes the builder a reproduction of an observed format.

The storm guard frames come from `fixtures/ocean2/storm_guard_frames.json`,
two recordings of the app switching the guard (serial masked the same way).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ecoflow_energy.ecoflow.ocean2_commands import (
    ACTION_STORM,
    build_ocean2_backup_reserve_payload,
    build_ocean2_storm_guard_payload,
    parse_ocean2_action_ack,
    parse_ocean2_property_ack,
)
from ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

SERIAL = "X" * 16

# Reserve 29 %, edev SoC ratio 80 %, seq 10.
GOLDEN_SET_29 = (
    "0a5e0a05501de005501020186020012801380340fe01482650055801700a800104"
    "ba0103696f73"
    "ca011058585858585858585858585858585858"
    "d2011058585858585858585858585858585858"
    "da011058585858585858585858585858585858"
)

# The device's answer: cmd_ok = 1, 10 = 29, 92 = 80.
GOLDEN_REPLY_29 = (
    "0a730a071001501de005501060182020012801380340fe01482650076001700a78fe01"
    "800104880101"
    "c2011058585858585858585858585858585858"
    "ca011058585858585858585858585858585858"
    "d2011058585858585858585858585858585858"
    "da011058585858585858585858585858585858"
)

# Payload of the reply above: cmd_ok = 1, then fields 10 and 92.
REPLY_PDATA = bytes.fromhex("1001501de00550")


def _reply(
    pdata: bytes,
    *,
    cmd_func: int = 254,
    cmd_id: int = 38,
    is_ack: int = 1,
    enc_type: int = 0,
) -> bytes:
    """A device reply frame, assembled field by field like the captured one.

    `enc_type` (header field 6) is left out when zero, as the captured reply
    has it.
    """
    serial = SERIAL.encode("ascii")
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(2, 96)
        + encode_field_varint(3, 32)
        + encode_field_varint(4, 1)
        + encode_field_varint(5, 1)
        + (encode_field_varint(6, enc_type) if enc_type else b"")
        + encode_field_varint(7, 3)
        + encode_field_varint(8, cmd_func)
        + encode_field_varint(9, cmd_id)
        + encode_field_varint(10, len(pdata))
        + encode_field_varint(12, is_ack)
        + encode_field_varint(14, 10)
        + encode_field_varint(15, 254)
        + encode_field_varint(16, 4)
        + encode_field_varint(17, 1)
        + encode_field_bytes(24, serial)
        + encode_field_bytes(25, serial)
        + encode_field_bytes(26, serial)
        + encode_field_bytes(27, serial)
    )
    return encode_field_bytes(1, header)


def test_the_helper_reproduces_the_captured_reply() -> None:
    # Keeps the hand-built replies below honest: the same assembly, fed the
    # captured payload, has to land on the captured bytes.
    assert _reply(REPLY_PDATA).hex() == GOLDEN_REPLY_29


class TestBuilder:
    @pytest.mark.parametrize(
        ("reserve_pct", "seq", "expected"),
        [
            (29, 10, GOLDEN_SET_29),
            # Second capture: only the payload value and the sequence differ.
            (59, 11, GOLDEN_SET_29.replace("501d", "503b").replace("700a", "700b")),
        ],
    )
    def test_reproduces_the_captured_app_write(
        self, reserve_pct: int, seq: int, expected: str
    ) -> None:
        built = build_ocean2_backup_reserve_payload(reserve_pct, 80, SERIAL, seq=seq)
        assert built.hex() == expected

    @pytest.mark.parametrize(
        ("reserve_pct", "edev_soc_ratio", "device_sn"),
        [
            (101, 80, SERIAL),
            (-1, 80, SERIAL),
            (29, 101, SERIAL),
            (29, -1, SERIAL),
            (29, 80, ""),
        ],
    )
    def test_refuses_what_the_device_was_never_sent(
        self, reserve_pct: int, edev_soc_ratio: int, device_sn: str
    ) -> None:
        with pytest.raises(ValueError):
            build_ocean2_backup_reserve_payload(reserve_pct, edev_soc_ratio, device_sn)

    def test_the_range_ends_are_accepted(self) -> None:
        # 0 % is a real reserve and 100 % a real ratio; the guard is inclusive.
        assert build_ocean2_backup_reserve_payload(0, 100, SERIAL, seq=1)
        assert build_ocean2_backup_reserve_payload(100, 0, SERIAL, seq=1)


class TestAck:
    def test_the_captured_reply_is_a_success(self) -> None:
        assert parse_ocean2_property_ack(bytes.fromhex(GOLDEN_REPLY_29)) is True

    def test_a_reply_without_cmd_ok_is_a_refusal_not_silence(self) -> None:
        # The device answered and did not say it applied the write: False,
        # which the caller reports, not None, which it would ignore.
        no_cmd_ok = bytes.fromhex("501de00550")
        assert parse_ocean2_property_ack(_reply(no_cmd_ok)) is False

    def test_an_explicit_cmd_ok_zero_is_a_refusal(self) -> None:
        assert (
            parse_ocean2_property_ack(_reply(bytes.fromhex("1000501de00550"))) is False
        )

    def test_other_frames_are_not_an_answer_to_this_write(self) -> None:
        # Another command id on the same function, even with is_ack set.
        assert parse_ocean2_property_ack(_reply(REPLY_PDATA, cmd_id=37)) is None
        # The right command id under another function.
        assert parse_ocean2_property_ack(_reply(REPLY_PDATA, cmd_func=32)) is None
        # The set frame itself: right pair, but no is_ack.
        sent = build_ocean2_backup_reserve_payload(29, 80, SERIAL, seq=10)
        assert parse_ocean2_property_ack(sent) is None
        # A reply on the right pair that is not flagged as an acknowledgement.
        assert parse_ocean2_property_ack(_reply(REPLY_PDATA, is_ack=0)) is None

    def test_a_masked_payload_is_not_read_as_a_refusal(self) -> None:
        # Nothing on this path unmasks a payload. The bytes below are the
        # refusal shape (no cmd_ok); behind a mask they say nothing, so the
        # reply is not an answer rather than a false "the device refused".
        refusal_shape = bytes.fromhex("501de00550")
        assert parse_ocean2_property_ack(_reply(refusal_shape, enc_type=1)) is None
        # Control: the same payload unmasked is the refusal it looks like.
        assert parse_ocean2_property_ack(_reply(refusal_shape)) is False


_STORM_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "ocean2" / "storm_guard_frames.json"
)
_STORM_FIXTURE = json.loads(_STORM_FIXTURE_PATH.read_text())
STORM_SERIAL: str = _STORM_FIXTURE["serial_mask"]
STORM_FRAMES: list[dict[str, Any]] = _STORM_FIXTURE["frames"]
STORM_SETS = [f for f in STORM_FRAMES if f["role"] == "set"]
STORM_REPLIES = [f for f in STORM_FRAMES if f["role"] == "set_reply"]


def _header(frame: bytes) -> dict[str, Any]:
    return decode_header_message(frame)[0][0]


class TestStormGuardBuilder:
    @pytest.mark.parametrize("captured", STORM_SETS, ids=lambda f: f["tag"])
    def test_reproduces_every_captured_app_write_byte_for_byte(
        self, captured: dict[str, Any]
    ) -> None:
        frame = bytes.fromhex(captured["hex"])
        header = _header(frame)
        built = build_ocean2_storm_guard_payload(
            captured["kind"] == "on", STORM_SERIAL, seq=int(header["seq"])
        )
        # The payload is the part that carries the switch (off sends explicit
        # zeros), so it is compared on its own before the whole frame.
        assert _header(built)["pdata"] == header["pdata"]
        assert built == frame

    def test_the_envelope_is_the_backup_reserve_envelope(self) -> None:
        storm = _header(build_ocean2_storm_guard_payload(True, STORM_SERIAL, seq=7))
        reserve = _header(
            build_ocean2_backup_reserve_payload(29, 80, STORM_SERIAL, seq=7)
        )
        assert (storm["cmd_func"], storm["cmd_id"]) == (254, 37)
        assert (reserve["cmd_func"], reserve["cmd_id"]) == (254, 38)
        # Everything that is not the payload, its length or the command id is
        # the same field for field: source, destination, from, the serials.
        differing = {"pdata", "data_len", "cmd_id"}
        assert {k: v for k, v in storm.items() if k not in differing} == {
            k: v for k, v in reserve.items() if k not in differing
        }

    @pytest.mark.parametrize("bad", [1, 0, None, "on"])
    def test_a_non_bool_enable_is_refused(self, bad: Any) -> None:
        with pytest.raises(ValueError):
            build_ocean2_storm_guard_payload(bad, STORM_SERIAL, seq=1)

    def test_a_missing_serial_is_refused(self) -> None:
        with pytest.raises(ValueError):
            build_ocean2_storm_guard_payload(True, "", seq=1)


class TestStormGuardAck:
    def test_every_captured_reply_is_a_success_for_its_own_set(self) -> None:
        assert len(STORM_REPLIES) == len(STORM_SETS) == 5
        for set_frame, reply in zip(STORM_SETS, STORM_REPLIES, strict=True):
            sent = _header(bytes.fromhex(set_frame["hex"]))
            answer = bytes.fromhex(reply["hex"])
            # The pairing is by sequence number, not by position in the file.
            assert _header(answer)["seq"] == sent["seq"], reply["tag"]
            assert parse_ocean2_action_ack(answer, ACTION_STORM) is True, reply["tag"]

    @pytest.mark.parametrize(
        ("pdata_hex", "expected"),
        [
            ("08111001", True),
            ("08111000", False),  # cmd_ok = 0: answered, did not apply
            ("0811", False),  # no cmd_ok at all
            ("08121001", None),  # another action's answer is not ours
            ("1001", None),  # no action id echoed
        ],
    )
    def test_synthetic_replies(self, pdata_hex: str, expected: bool | None) -> None:
        reply = _reply(bytes.fromhex(pdata_hex), cmd_id=37)
        assert parse_ocean2_action_ack(reply, ACTION_STORM) is expected

    def test_other_frames_are_not_an_answer_to_a_storm_guard_write(self) -> None:
        # A property reply carries cmd_ok too, under the other command id.
        assert (
            parse_ocean2_action_ack(bytes.fromhex(GOLDEN_REPLY_29), ACTION_STORM)
            is None
        )
        # The write itself has no is_ack.
        sent = build_ocean2_storm_guard_payload(True, SERIAL, seq=10)
        assert parse_ocean2_action_ack(sent, ACTION_STORM) is None
        # A masked payload cannot be told from a refusal, so it is no answer.
        masked = _reply(bytes.fromhex("08111001"), cmd_id=37, enc_type=1)
        assert parse_ocean2_action_ack(masked, ACTION_STORM) is None
