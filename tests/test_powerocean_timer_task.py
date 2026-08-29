"""The PowerOcean scheduled charge task, read from the device's own list.

Every payload below is the `96/10` header's own pdata, lifted byte for byte
out of the reporter capture on #328 and named after the moment it was
recorded. The capture spans one evening on a J329: no schedule until 19:52
local, then a task created for 20:00 to 20:30 at 1000 W, raised to 1500 W at
20:05, disarmed at 20:10 and deleted at 20:12.

The times in the constant names are UTC, as recorded. The reporter is on BST,
one hour ahead, which is why a window of 20:00 local opens in a bundle
timestamped 19:0x.
"""

from __future__ import annotations

from base64 import b64encode

from custom_components.ecoflow_energy.ecoflow.parsers.powerocean_proto import (
    remap_timer_task_keys,
)
from custom_components.ecoflow_energy.ecoflow.proto.runtime import (
    decode_proto_runtime_frame,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

# 18:59:22, the task as created: armed, 1000 W, window not yet open.
LIST_ARMED_1000W = bytes.fromhex(
    "0a191002180120012800300138e8074044489daa3f5204b089b826"
)
# 19:06:32, after the power change: armed, 1500 W, window open.
LIST_ARMED_1500W = bytes.fromhex(
    "0a191002180120012801300138dc0b4044489daa3f5204b089b826"
)
# 19:11:37, first copy in the bundle: the armed flag is gone, 96 s after the
# schedule was disarmed from the app.
LIST_DISARMED_1500W = bytes.fromhex(
    "0a17100218012800300138dc0b4044489daa3f5204b089b826"
)
# 19:11:37, second copy in the same bundle: still carries the armed flag, and
# is therefore 96 s stale.
LIST_STALE_ARMED_1500W = bytes.fromhex(
    "0a191002180120012800300138dc0b4044489daa3f5204b089b826"
)
# 17:46:56 and 19:12:42: the header arrives with no payload at all, before the
# task was created and again after it was deleted.
LIST_EMPTY = b""


def _header(cmd_func: int, cmd_id: int, pdata: bytes) -> bytes:
    """Wrap a payload in the one-header envelope the device sends it in."""
    header = bytearray()
    header.extend(encode_field_bytes(1, pdata))
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    return encode_field_bytes(1, bytes(header))


def _decode(pdata: bytes) -> dict:
    """Run one payload through the registry the way a real frame does."""
    result = decode_proto_runtime_frame(_header(96, 10, pdata))
    assert result.mapped.get("_is_timer_task_list"), result.parse_path
    return {k: v for k, v in result.mapped.items() if not k.startswith("_")}


def test_the_registered_command_decodes_the_task_list() -> None:
    """Field 10 is length-delimited, and a naive binding would drop it.

    The command family declares `time_table` a varint. Every frame that
    carries it uses wire type 2 instead, so a binding taken from the
    declaration files the field as unknown and loses the window without a
    word. The assertion on the window is what would fail.
    """
    keys = remap_timer_task_keys(_decode(LIST_ARMED_1500W), set())

    assert keys == {
        "schedule_1_enabled": True,
        "schedule_1_running": True,
        "schedule_1_power_w": 1500,
        "schedule_1_window": "20:00-20:30",
        "schedule_1_type": 1,
        "schedule_1_time_mode": 68,
        "schedule_1_time_param": 1037597,
        "schedule_1_time_table": 80610480,
    }


def test_the_task_before_its_window_opens_is_armed_but_not_running() -> None:
    """Armed and running are two flags, and only one of them is writable."""
    keys = remap_timer_task_keys(_decode(LIST_ARMED_1000W), set())

    assert keys["schedule_1_enabled"] is True
    assert keys["schedule_1_running"] is False
    assert keys["schedule_1_power_w"] == 1000
    assert keys["schedule_1_window"] == "20:00-20:30"


def test_an_absent_armed_flag_reads_as_disarmed() -> None:
    """Disable is expressed by omitting field 4, on both sides of the wire.

    A false bool is not serialised, so the field is missing rather than zero.
    Reading that as unknown would show a schedule the owner switched off as
    one whose state has not arrived yet, and reading it as the number zero
    would put an integer where a switch expects a state.
    """
    keys = remap_timer_task_keys(_decode(LIST_DISARMED_1500W), set())

    assert keys["schedule_1_enabled"] is False
    assert keys["schedule_1_enabled"] is not None
    assert not isinstance(keys["schedule_1_enabled"], int) or isinstance(
        keys["schedule_1_enabled"], bool
    )
    # The rest of the task survives the missing flag.
    assert keys["schedule_1_power_w"] == 1500
    assert keys["schedule_1_window"] == "20:00-20:30"


def test_a_task_that_leaves_the_list_has_its_keys_retracted() -> None:
    """A deleted task stops being mentioned, it is not reported empty.

    Nothing else would ever clear these keys, so the readings would go on
    describing a schedule the device no longer holds.
    """
    known: set[int] = set()
    remap_timer_task_keys(_decode(LIST_ARMED_1500W), known)
    assert known == {1}

    keys = remap_timer_task_keys(_decode(LIST_EMPTY), known)

    assert keys == {
        "schedule_1_enabled": None,
        "schedule_1_running": None,
        "schedule_1_power_w": None,
        "schedule_1_window": None,
        "schedule_1_type": None,
        "schedule_1_time_mode": None,
        "schedule_1_time_param": None,
        "schedule_1_time_table": None,
    }
    assert known == set()


def test_a_device_that_never_had_a_schedule_publishes_nothing() -> None:
    """Four of the capture's ten bundles are this case, and it is the norm."""
    assert remap_timer_task_keys(_decode(LIST_EMPTY), set()) == {}


def test_an_index_outside_the_slot_range_is_ignored() -> None:
    """A malformed list must not create keys without end.

    Only slot 1 has ever been seen, so the cap is a guard rather than a
    statement about how many slots exist.
    """
    task = {"task_index": 99, "is_enable": True, "sys_chg_dsg_pwr": 1500}

    assert remap_timer_task_keys({"time_task_cfg": [task]}, set()) == {}


def test_an_unreadable_window_block_publishes_no_window() -> None:
    """Half a window is worse than none.

    The block's reading rests on a single sample, so anything that is not one
    terminated varint is refused rather than guessed at.
    """
    task = {"task_index": 1, "time_table": "sIm4", "sys_chg_dsg_pwr": 1500}

    keys = remap_timer_task_keys({"time_task_cfg": [task]}, set())

    assert keys["schedule_1_window"] is None
    assert keys["schedule_1_time_table"] is None
    assert keys["schedule_1_power_w"] == 1500


def _time_table(start: int, end: int) -> str:
    """Pack a window the way the device does, base64 as the decode hands it.

    Anchored against the capture by the test below: a helper that invented its
    own encoding would let every boundary test below pass while proving
    nothing about the block the device actually sends.
    """
    packed = start | (end << 16)
    raw = bytearray()
    while True:
        byte = packed & 0x7F
        packed >>= 7
        if packed:
            raw.append(byte | 0x80)
        else:
            raw.append(byte)
            break
    return b64encode(bytes(raw)).decode()


def _window(start: int, end: int) -> str | None:
    """Read one packed window back out through the parser."""
    task = {"task_index": 1, "time_table": _time_table(start, end)}
    return remap_timer_task_keys({"time_task_cfg": [task]}, set())[
        "schedule_1_window"
    ]


def test_the_packing_helper_reproduces_the_captured_block() -> None:
    """20:00 to 20:30 packs to the four bytes the reporter's frame carries."""
    assert _time_table(1200, 1230) == "sIm4Jg=="
    assert _window(1200, 1230) == "20:00-20:30"


def test_a_window_starting_at_midnight_is_a_real_window() -> None:
    """Zero is a legitimate half on its own - only both halves at zero are not."""
    assert _window(0, 30) == "00:00-00:30"


def test_a_window_crossing_midnight_is_rendered_as_reported() -> None:
    """An end before its start is a window over midnight, not a broken read.

    The two halves are independent minutes-of-day, so nothing here needs the
    end to be the larger number, and refusing this pair would hide a schedule
    that runs through the night.
    """
    assert _window(1380, 60) == "23:00-01:00"


def test_a_block_of_zeroes_publishes_no_window() -> None:
    """An unset block, not a window from midnight to midnight.

    The raw block is still published, because a write against this slot has to
    hand it back exactly as it was reported. Only the rendered window refuses.
    """
    task = {"task_index": 1, "time_table": _time_table(0, 0), "is_enable": True}

    keys = remap_timer_task_keys({"time_task_cfg": [task]}, set())

    assert keys["schedule_1_window"] is None
    assert keys["schedule_1_time_table"] == 0
    assert keys["schedule_1_enabled"] is True


def test_the_last_minute_of_the_day_is_still_a_window() -> None:
    """1439 is the last minute there is, and it has to survive the bound."""
    assert _window(1200, 1439) == "20:00-23:59"


def test_a_half_at_exactly_one_full_day_publishes_no_window() -> None:
    """1440 is one past the end of the day, not the end of it.

    It may well be how a device expresses "until midnight", but none has been
    seen doing it, and the arithmetic would render it as the hour `24:00`.
    """
    assert _window(1200, 1440) is None
    assert _window(1440, 1230) is None


def test_a_half_beyond_a_day_publishes_no_window() -> None:
    """The guard that a mutation removed without a single test noticing."""
    assert _window(1200, 2000) is None
    assert _window(5000, 1230) is None


def test_an_absent_power_publishes_none_rather_than_zero() -> None:
    """A missing number is not the number zero.

    Zero watts is a real setpoint on this device, so publishing it for a task
    that never carried the field would show a schedule set to charge at
    nothing instead of a reading that has not arrived.
    """
    task = {"task_index": 1, "is_enable": True, "time_table": _time_table(1200, 1230)}

    keys = remap_timer_task_keys({"time_task_cfg": [task]}, set())

    assert keys["schedule_1_power_w"] is None
    assert keys["schedule_1_window"] == "20:00-20:30"


def test_a_slot_listed_twice_reports_the_last_of_the_two() -> None:
    """Two entries for one slot cannot both be the slot.

    No capture holds this, so the choice is a convention rather than a
    reading: the later entry wins, on the same grounds as everywhere else that
    a list is replayed into a mapping. One slot is claimed, not two.
    """
    known: set[int] = set()
    tasks = [
        {"task_index": 1, "is_enable": True, "sys_chg_dsg_pwr": 1000},
        {"task_index": 1, "is_enable": False, "sys_chg_dsg_pwr": 1500},
    ]

    keys = remap_timer_task_keys({"time_task_cfg": tasks}, known)

    assert keys["schedule_1_power_w"] == 1500
    assert keys["schedule_1_enabled"] is False
    assert known == {1}
