"""Tests for the Ocean 2 Backup Reserve SET builder and its acknowledgement.

The golden frames are the EcoFlow app's own write and the device's answer,
captured from the `/app/{uid}/{sn}/thing/property/set` topic while the
reserve was changed in the app. The serial is masked to sixteen `X`, same
length, so no identity travels with the evidence. Rebuilding the write byte
for byte is what makes the builder a reproduction of an observed format.
"""

from __future__ import annotations

import pytest
from ecoflow_energy.ecoflow.ocean2_commands import (
    build_ocean2_backup_reserve_payload,
    parse_ocean2_property_ack,
)
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
) -> bytes:
    """A device reply frame, assembled field by field like the captured one."""
    serial = SERIAL.encode("ascii")
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(2, 96)
        + encode_field_varint(3, 32)
        + encode_field_varint(4, 1)
        + encode_field_varint(5, 1)
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
