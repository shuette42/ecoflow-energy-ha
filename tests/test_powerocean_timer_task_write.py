"""Byte-exact tests for the PowerOcean scheduled-task write frames.

Every expected value here is a frame the reporter's device actually accepted,
copied from the capture on #328. Nothing is constructed from what the builder
is understood to do: a test written that way would agree with the builder about
a field the device disagrees with, which is the whole failure mode this command
has. The reference bytes are the contract.

The capture's slot is task 1, a grid-charge schedule running 20:00 to 20:30
local at first 1000 W and later 1500 W.
"""

from __future__ import annotations

import pytest

from custom_components.ecoflow_energy.ecoflow.energy_stream import (
    TIMER_TASK_POWER_MAX_W,
    build_timer_task_set_payload,
)
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)

# The serial the diagnostics sanitiser left in the capture, sixteen X.
CAPTURE_SN = "XXXXXXXXXXXXXXXX"

# Header field 14 of the 18:52:26 frame.
CAPTURE_SEQ = 267241023

# The device's own values for the fields whose meaning is unresolved. A write
# hands these back exactly as the last read reported them.
CAPTURE_TYPE = 1
CAPTURE_TIME_MODE = 68
CAPTURE_TIME_PARAM = 1037597
# Field 10 as the read path decodes it: one varint, 1200 | 1230 << 16.
CAPTURE_TIME_TABLE = 80610480

# Spec, "What a builder would have to send". Payload bytes only.
ARM_PDATA = bytes.fromhex("100218012001")
DISARM_PDATA = bytes.fromhex("10021801")
POWER_1500_PDATA = bytes.fromhex("10021801300138dc0b4044489daa3f5204b089b826")

# Spec, "Full known-good frame for diffing": the create at 18:52:26.958,
# complete, envelope included, exactly as captured.
CAPTURE_CREATE_FRAME = bytes.fromhex(
    "0a530a17100118012001300138e8074044489daa3f5204b089b826"
    "10201860200128014060487d5017580170bf8cb77f7801800113880101"
    "ba0107616e64726f6964"
    "ca011058585858585858585858585858585858"
)


def pdata_of(frame: bytes) -> bytes:
    """Pull the inner payload back out of a built frame.

    Uses the integration's own decoder rather than a slice, so the envelope has
    to be well-formed for the payload assertions to even run.
    """
    headers, _ = decode_header_message(frame)
    assert len(headers) == 1, f"expected one header, got {len(headers)}"
    return bytes.fromhex(headers[0]["pdata"])


def build(operation: str, **kwargs) -> bytes:
    return build_timer_task_set_payload(
        operation, 1, device_sn=CAPTURE_SN, seq=CAPTURE_SEQ, **kwargs
    )


def build_power(*, watts: int, armed: bool) -> bytes:
    return build(
        "power",
        power_w=watts,
        task_type=CAPTURE_TYPE,
        time_mode=CAPTURE_TIME_MODE,
        time_param=CAPTURE_TIME_PARAM,
        time_table=CAPTURE_TIME_TABLE,
        armed=armed,
    )


# ---------------------------------------------------------------------------
# Byte-exact against the frames the device accepted
# ---------------------------------------------------------------------------


def test_arm_matches_the_captured_enable_frame():
    """The six-byte enable observed at 19:05:23.695, reply {2: 1}."""
    assert pdata_of(build("arm")) == ARM_PDATA


def test_disarm_matches_the_captured_disable_frame():
    """The four-byte disable observed at 19:05:12.945 and 19:10:01.810."""
    assert pdata_of(build("disarm")) == DISARM_PDATA


def test_power_change_matches_the_captured_full_body():
    """The 1500 W change observed at 19:05:22.207, 21 bytes, reply {1: 1, 2: 1}.

    The app's own frame omits field 4, so this is the `armed=False` shape.
    """
    assert pdata_of(build_power(watts=1500, armed=False)) == POWER_1500_PDATA


def test_armed_power_change_differs_from_the_known_good_frame_by_one_byte():
    """The composed single frame, diffed against the whole captured create.

    The create at 18:52:26 carries fields 2, 3, 4, 6, 7, 8, 9 and 10 together
    and the device accepted it. The single-frame power change is that same
    union with `is_cfg` moved from 1 (create) to 2 (modify), so with the
    capture's own 1000 W it must reproduce the captured frame end to end -
    envelope included - apart from that one byte.
    """
    built = build_power(watts=1000, armed=True)
    expected = bytearray(CAPTURE_CREATE_FRAME)

    # The frame opens `0a 53 0a 17`, so the payload starts at offset 4 with
    # `10 01`: field 2 `is_cfg`, tag then value.
    assert expected[4] == 0x10, "payload does not start with the is_cfg tag"
    assert expected[5] == 0x01, "captured frame is not a create"
    expected[5] = 0x02

    assert built == bytes(expected)


def test_the_envelope_reproduces_the_captured_header():
    """No check_type, product_id 1, version 19, android - as captured.

    The envelope this shares with the SoC commands defaults to a different
    shape (check_type 3, version 3, ios). Reproducing the capture rather than
    normalising to the default is deliberate, and this pins it.
    """
    headers, _ = decode_header_message(build("arm"))
    header = headers[0]
    assert header.get("check_type") is None
    assert header["product_id"] == 1
    assert header["version"] == 19
    assert header["cmd_func"] == 96
    assert header["cmd_id"] == 125
    assert header["seq"] == CAPTURE_SEQ
    assert header["data_len"] == len(ARM_PDATA)


# ---------------------------------------------------------------------------
# Presence is the contract
# ---------------------------------------------------------------------------


def test_disarm_carries_no_is_enable_field_at_all():
    """Not `4=0`. The absence is the off state, on both sides of the wire.

    A proto3 false bool is not serialised, and the device reports a disarmed
    task by leaving field 4 out. An explicit zero is a no-op that leaves the
    schedule armed, and nothing about the reply would say so.

    Asserted on the bytes rather than on a decode, because the point is that
    the tag never appears.
    """
    pdata = pdata_of(build("disarm"))
    assert b"\x20" not in pdata, f"field 4 tag present in {pdata.hex()}"
    assert len(pdata) == 4


def test_power_change_without_armed_carries_no_is_enable_field():
    """The app's two-frame sequence, first frame: full body, no field 4."""
    pdata = pdata_of(build_power(watts=1500, armed=False))
    assert pdata[:4] == bytes.fromhex("10021801")
    assert pdata[4] != 0x20, "field 4 must not follow the task index here"


def test_power_change_with_armed_carries_is_enable_once():
    """The single-frame shape, field 4 in ascending order between 3 and 6."""
    pdata = pdata_of(build_power(watts=1500, armed=True))
    assert pdata[:6] == bytes.fromhex("100218012001")
    assert pdata.count(b"\x20\x01") == 1


def test_the_two_frame_fallback_is_two_calls_to_the_same_builder():
    """The app's own sequence stays reachable without a rewrite.

    A power change that omits the enable flag, then the six-byte re-arm it
    sends 1.5 s later. Both are frames the device accepted as captured.
    """
    first = pdata_of(build_power(watts=1500, armed=False))
    second = pdata_of(build("arm"))
    assert first == POWER_1500_PDATA
    assert second == ARM_PDATA


# ---------------------------------------------------------------------------
# The echoed fields belong to the device
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dropped",
    ["task_type", "time_mode", "time_param", "time_table"],
)
def test_power_change_refuses_to_invent_an_echoed_field(dropped):
    """No default, no zero, no omission. The caller hands them in or it fails."""
    kwargs = {
        "power_w": 1500,
        "task_type": CAPTURE_TYPE,
        "time_mode": CAPTURE_TIME_MODE,
        "time_param": CAPTURE_TIME_PARAM,
        "time_table": CAPTURE_TIME_TABLE,
        "armed": True,
    }
    kwargs[dropped] = None
    with pytest.raises(TypeError, match=dropped):
        build("power", **kwargs)


def test_power_change_needs_an_explicit_armed_decision():
    """A full body clears the enable flag, so the caller has to choose."""
    with pytest.raises(TypeError, match="armed"):
        build(
            "power",
            power_w=1500,
            task_type=CAPTURE_TYPE,
            time_mode=CAPTURE_TIME_MODE,
            time_param=CAPTURE_TIME_PARAM,
            time_table=CAPTURE_TIME_TABLE,
        )


def test_echoed_values_reach_the_wire_unchanged():
    """A second device's numbers, none of them the capture's."""
    frame = build(
        "power",
        power_w=800,
        task_type=2,
        time_mode=71,
        time_param=99,
        time_table=(300 | 420 << 16),
        armed=True,
    )
    pdata = pdata_of(frame)
    # Hand-encoded from the arguments above, not from the builder:
    #   10 02        is_cfg = 2          18 01        task_index = 1
    #   20 01        is_enable = true    30 02        type = 2
    #   38 a0 06     power = 800         40 47        time_mode = 71
    #   48 63        time_param = 99     52 04 ...    time_table = 27525420
    assert pdata == bytes.fromhex("100218012001300238a006404748635204ac82900d")


# ---------------------------------------------------------------------------
# What the builder refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["create", "delete"])
def test_create_and_delete_are_not_reachable(operation):
    """Refused in the signature, not merely left unimplemented.

    Create needs two recurrence fields with one sample each. Delete is a
    one-way door while create is impossible.
    """
    with pytest.raises(ValueError, match=operation):
        build(operation)


def test_an_unknown_operation_is_refused():
    with pytest.raises(ValueError, match="operation"):
        build("enable")


@pytest.mark.parametrize("field", ["power_w", "task_type", "time_table"])
def test_a_short_frame_refuses_the_full_body_arguments(field):
    """Accepting and dropping them would let a caller believe a write went out."""
    with pytest.raises(TypeError, match=field):
        build("disarm", **{field: 1})


def test_a_short_frame_refuses_an_armed_argument():
    with pytest.raises(TypeError, match="armed"):
        build("arm", armed=True)


@pytest.mark.parametrize("index", [0, -1, 9])
def test_the_task_index_is_bounded(index):
    with pytest.raises(ValueError, match="task_index"):
        build_timer_task_set_payload("arm", index, device_sn=CAPTURE_SN)


def test_a_bool_is_not_a_task_index():
    """True is an int in Python and would encode as slot 1."""
    with pytest.raises(TypeError, match="task_index"):
        build_timer_task_set_payload("arm", True, device_sn=CAPTURE_SN)


@pytest.mark.parametrize("watts", [-1, TIMER_TASK_POWER_MAX_W + 1])
def test_the_power_is_bounded(watts):
    with pytest.raises(ValueError, match="power_w"):
        build_power(watts=watts, armed=True)


def test_the_serial_is_required():
    """Every captured frame carries it; a frame without one was never tried."""
    with pytest.raises(ValueError, match="device_sn"):
        build_timer_task_set_payload("arm", 1, device_sn="")
