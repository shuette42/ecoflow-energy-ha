"""Byte-exact tests for the PowerOcean feed-to-grid schedule write frames.

Every expected value here is a frame the reporter's device actually accepted,
copied from the capture attached to #381 (J327, account sign-in). Nothing is
constructed from what the builder is understood to do: a test written that
way would agree with the builder about a field the device disagrees with,
which is the whole failure mode this command has. The reference bytes are the
contract.

The capture's slot is task 4, a feed-to-grid schedule created at 1300 W,
raised to 2600 W, then walked through a window change, a one-off repeat and
two weekly repeats before being deleted again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.ecoflow_energy.ecoflow.energy_stream import (
    POWEROCEAN_TASK_POWER_MAX_W,
    build_tou_task_set_payload,
)
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)

_FIXTURE = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "powerocean"
        / "j327_tou_task_write_masked.json"
    ).read_text()
)
_WRITES: dict[str, dict] = {entry["utc"]: entry for entry in _FIXTURE["writes_96_143"]}

# The serial the diagnostics sanitiser left in the capture, sixteen X.
CAPTURE_SN = "XXXXXXXXXXXXXXXX"
CAPTURE_TASK_INDEX = 4

# The device's own values for the fields the write path echoes back.
CAPTURE_TYPE = 2
CAPTURE_TIME_MODE = 65
CAPTURE_TIME_TABLE = [84542700, 88474920]

# After the 12:04:30 window change, the second window moves and stays moved
# for every write that follows it in the capture.
CHANGED_TIME_TABLE = [84542700, 90441000]


def pdata_of(frame: bytes) -> bytes:
    """Pull the inner payload back out of a built frame.

    Uses the integration's own decoder rather than a slice, so the envelope
    has to be well-formed for the payload assertions to even run.
    """
    headers, _ = decode_header_message(frame)
    assert len(headers) == 1, f"expected one header, got {len(headers)}"
    return bytes.fromhex(headers[0]["pdata"])


def build(operation: str, **kwargs) -> bytes:
    return build_tou_task_set_payload(
        operation, CAPTURE_TASK_INDEX, device_sn=CAPTURE_SN, **kwargs
    )


def build_power(
    *,
    watts: int,
    task_type: int = CAPTURE_TYPE,
    time_mode: int = CAPTURE_TIME_MODE,
    time_param: int | None = None,
    time_table=CAPTURE_TIME_TABLE,
    armed: bool = True,
) -> bytes:
    return build(
        "power",
        power_w=watts,
        task_type=task_type,
        time_mode=time_mode,
        time_param=time_param,
        time_table=time_table,
        armed=armed,
    )


def _pdata(utc: str) -> bytes:
    return bytes.fromhex(_WRITES[utc]["pdata"])


def _read_varint(data: bytes, index: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    return result, index


def _decode_fields(data: bytes) -> list[tuple[int, int, object]]:
    """Minimal generic protobuf field walker, independent of the builder.

    Returns (field_number, wire_type, value) triples. Used only to inspect
    the wire shape the builder produced - never to derive an expectation.
    """
    index = 0
    fields: list[tuple[int, int, object]] = []
    while index < len(data):
        tag, index = _read_varint(data, index)
        field, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            value: object
            value, index = _read_varint(data, index)
        elif wire_type == 2:
            length, index = _read_varint(data, index)
            value = data[index : index + length]
            index += length
        else:
            raise ValueError(f"unsupported wire type {wire_type} for field {field}")
        fields.append((field, wire_type, value))
    return fields


def _bytes_field(fields: list[tuple[int, int, object]], field_number: int) -> bytes:
    """Pull one length-delimited field's raw bytes out of a decoded list."""
    value = next(value for field, _, value in fields if field == field_number)
    assert isinstance(value, (bytes, bytearray))
    return bytes(value)


# ---------------------------------------------------------------------------
# Byte-exact against the frames the device accepted
# ---------------------------------------------------------------------------


def test_arm_matches_the_captured_enable_frame():
    """The six-byte enable observed at 12:04:14, reply {2: 1, 4: 4}."""
    assert pdata_of(build("arm")) == _pdata("12:04:14")


def test_disarm_matches_the_captured_disable_frame():
    """The four-byte disable observed at 12:04:12."""
    assert pdata_of(build("disarm")) == _pdata("12:04:12")


def test_power_change_reproduces_the_create_body_apart_from_is_cfg():
    """1300 W, daily, two windows: the create's own values, `is_cfg` 1 -> 2.

    The create at 12:04:06 carries `is_cfg=1` and this builder never produces
    a create, so this is diffed against the mutated capture the same way the
    timer family's armed-power test is: one byte, and only that byte.
    """
    built = pdata_of(build_power(watts=1300))
    expected = bytearray(_pdata("12:04:06"))
    assert expected[0] == 0x10, "payload does not start with the is_cfg tag"
    assert expected[1] == 0x01, "captured frame is not a create"
    expected[1] = 0x02
    assert built == bytes(expected)


def test_power_change_to_2600_matches_the_capture():
    """The 1300 -> 2600 W change observed at 12:04:22, full body untouched."""
    assert pdata_of(build_power(watts=2600)) == _pdata("12:04:22")


def test_window_change_matches_the_capture():
    """The second window's end moves 22:30 -> 23:00, observed at 12:04:30."""
    built = build_power(watts=2600, time_table=CHANGED_TIME_TABLE)
    assert pdata_of(built) == _pdata("12:04:30")


def test_one_off_repeat_matches_the_capture():
    """Repeat -> one-off (time_mode 68, time_param 1037609), at 12:04:37."""
    built = build_power(
        watts=2600,
        time_mode=68,
        time_param=1037609,
        time_table=CHANGED_TIME_TABLE,
    )
    assert pdata_of(built) == _pdata("12:04:37")


def test_weekly_repeat_wed_and_thu_matches_the_capture():
    """Repeat -> weekly Wed+Thu (time_mode 66, time_param 12), at 12:04:44."""
    built = build_power(
        watts=2600,
        time_mode=66,
        time_param=12,
        time_table=CHANGED_TIME_TABLE,
    )
    assert pdata_of(built) == _pdata("12:04:44")


def test_weekly_repeat_wed_only_matches_the_capture():
    """Weekly narrowed to Wed only (time_param 4), at 12:04:49."""
    built = build_power(
        watts=2600,
        time_mode=66,
        time_param=4,
        time_table=CHANGED_TIME_TABLE,
    )
    assert pdata_of(built) == _pdata("12:04:49")


def test_the_whole_frame_matches_the_capture_apart_from_is_cfg():
    """The composed frame, diffed against the whole captured create.

    The create at 12:04:06 carries fields 2, 3, 4, 5, 6 and 8 together and the
    device accepted it. The composed power write with `armed=True` is that
    same union with `is_cfg` moved from 1 (create) to 2 (modify), so with the
    capture's own seq and serial it must reproduce the captured frame end to
    end - envelope included - apart from that one byte.
    """
    seq = _WRITES["12:04:06"]["seq"]
    built = build_tou_task_set_payload(
        "power",
        CAPTURE_TASK_INDEX,
        device_sn=CAPTURE_SN,
        power_w=1300,
        task_type=CAPTURE_TYPE,
        time_mode=CAPTURE_TIME_MODE,
        time_param=None,
        time_table=CAPTURE_TIME_TABLE,
        armed=True,
        seq=seq,
    )
    expected = bytearray(bytes.fromhex(_WRITES["12:04:06"]["frame"]))

    # The frame opens `0a 56 0a 19`, so the payload starts at offset 4 with
    # `10 01`: field 2 `is_cfg`, tag then value.
    assert expected[4] == 0x10, "payload does not start with the is_cfg tag"
    assert expected[5] == 0x01, "captured frame is not a create"
    expected[5] = 0x02

    assert built == bytes(expected)


def test_the_envelope_reproduces_the_captured_header():
    """No check_type, product_id 1, version 19, android - as captured."""
    seq = _WRITES["12:04:06"]["seq"]
    headers, _ = decode_header_message(build("arm", seq=seq))
    header = headers[0]
    assert header.get("check_type") is None
    assert header["product_id"] == 1
    assert header["version"] == 19
    assert header["cmd_func"] == 96
    assert header["cmd_id"] == 143
    assert header["seq"] == seq
    assert header["from"] == "android"
    assert header["device_sn"] == CAPTURE_SN


# ---------------------------------------------------------------------------
# The windows are packed, not repeated tags
# ---------------------------------------------------------------------------


def test_the_windows_are_packed_in_one_field_not_repeated_tags():
    """One field-3 block carrying both varints, never one tag per window.

    Decoded independently of the builder's own encoder: this walks the raw
    bytes with a from-scratch varint reader, so a builder that regressed to
    `encode_field_varint(3, w)` per window - wire type 0, one tag per window -
    is caught even though it would still put the right numbers on the wire.
    """
    pdata = pdata_of(build_power(watts=2600, time_table=CHANGED_TIME_TABLE))
    fields = _decode_fields(pdata)
    sub_info = _bytes_field(fields, 8)

    sub_fields = _decode_fields(sub_info)
    field_3_values = [value for field, _, value in sub_fields if field == 3]
    assert len(field_3_values) == 1, (
        f"expected exactly one field-3 block, got {len(field_3_values)}"
    )
    raw_blob = field_3_values[0]
    assert isinstance(raw_blob, (bytes, bytearray))

    blob = bytes(raw_blob)
    windows = []
    index = 0
    while index < len(blob):
        window, index = _read_varint(blob, index)
        windows.append(window)
    assert windows == CHANGED_TIME_TABLE


# ---------------------------------------------------------------------------
# Presence is the contract
# ---------------------------------------------------------------------------


def test_disarm_carries_no_is_enable_field_at_all():
    """Not `4=0`. The absence is the off state, on both sides of the wire."""
    pdata = pdata_of(build("disarm"))
    assert b"\x20" not in pdata, f"field 4 tag present in {pdata.hex()}"
    assert len(pdata) == 4


def test_power_change_with_armed_carries_is_enable_once():
    """The single-frame shape, field 4 in ascending order between 3 and 5."""
    pdata = pdata_of(build_power(watts=2600, armed=True))
    assert pdata[:6] == bytes.fromhex("100218042001")
    assert pdata.count(b"\x20\x01") == 1


def test_power_change_without_armed_carries_no_is_enable_field():
    """Reachable, mirroring the timer builder - unobserved on this family."""
    pdata = pdata_of(build_power(watts=2600, armed=False))
    assert pdata[:4] == bytes.fromhex("10021804")
    assert pdata[4] != 0x20, "field 4 must not follow the task index here"


def test_a_daily_task_omits_time_param_from_the_wire():
    """time_mode 65 (daily): field 8 carries no field-2 tag at all."""
    pdata = pdata_of(build_power(watts=1300, time_param=None))
    sub_info = _bytes_field(_decode_fields(pdata), 8)
    sub_fields = _decode_fields(sub_info)
    assert not any(field == 2 for field, _, _ in sub_fields)


def test_a_one_off_task_carries_time_param_on_the_wire():
    """time_mode 68 (one-off): field 8 carries field 2 with the given value."""
    pdata = pdata_of(
        build_power(
            watts=2600,
            time_mode=68,
            time_param=1037609,
            time_table=CHANGED_TIME_TABLE,
        )
    )
    sub_info = _bytes_field(_decode_fields(pdata), 8)
    sub_fields = _decode_fields(sub_info)
    param_hits = [value for field, _, value in sub_fields if field == 2]
    assert param_hits == [1037609]


# ---------------------------------------------------------------------------
# The echoed fields belong to the device
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dropped", ["task_type", "time_mode", "time_table"])
def test_power_change_refuses_to_invent_a_required_echoed_field(dropped):
    """No default, no zero, no omission for the three that are never absent."""
    kwargs = {
        "power_w": 1500,
        "task_type": CAPTURE_TYPE,
        "time_mode": CAPTURE_TIME_MODE,
        "time_param": None,
        "time_table": CAPTURE_TIME_TABLE,
        "armed": True,
    }
    kwargs[dropped] = None  # type: ignore[assignment]
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
            time_param=None,
            time_table=CAPTURE_TIME_TABLE,
        )


# ---------------------------------------------------------------------------
# What the builder refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["create", "delete"])
def test_create_and_delete_are_not_reachable(operation):
    """Refused in the signature, not merely left unimplemented."""
    with pytest.raises(ValueError, match=operation):
        build(operation)


def test_an_unknown_operation_is_refused():
    with pytest.raises(ValueError, match="operation"):
        build("enable")


@pytest.mark.parametrize("field", ["power_w", "task_type", "time_table"])
def test_a_short_frame_refuses_the_full_body_arguments(field):
    with pytest.raises(TypeError, match=field):
        build("disarm", **{field: 1})


def test_a_short_frame_refuses_an_armed_argument():
    with pytest.raises(TypeError, match="armed"):
        build("arm", armed=True)


@pytest.mark.parametrize("watts", [-1, POWEROCEAN_TASK_POWER_MAX_W + 1])
def test_the_power_is_bounded(watts):
    with pytest.raises(ValueError, match="power_w"):
        build_power(watts=watts)


@pytest.mark.parametrize(
    "time_table",
    [[], [84542700, 88474920, 90441000]],
)
def test_the_time_table_length_is_bounded(time_table):
    with pytest.raises(ValueError, match="time_table"):
        build_power(watts=1300, time_table=time_table)


def test_a_window_past_the_end_of_day_is_refused():
    """start=1260 (valid), end=1441 - one minute past a full day."""
    bad_window = 1260 | (1441 << 16)
    with pytest.raises(ValueError, match="time_table window"):
        build_power(watts=1300, time_table=[bad_window])


def test_the_serial_is_required():
    with pytest.raises(ValueError, match="device_sn"):
        build_tou_task_set_payload("arm", CAPTURE_TASK_INDEX, device_sn="")
