"""The PowerOcean feed-to-grid schedule, read from the device's own list.

Every payload below is the `96/14` header's own pdata, lifted byte for byte
out of the reporter diagnostics attached to #381 (J327, account sign-in) and
indexed by the moment it was recorded (UTC, as the fixture holds it). The
capture covers slot 4 being created, its power raised, its repeat schedule
changed three times, and slot 4 leaving the list again - while slots 2 and 3
sit unchanged in every one of the nine pushes.

The window integers asserted below are computed from the wire bytes, not
copied from the plan document's prose: two of the plan's own example decimals
(66568882 for slot 3's window, for instance) do not match the bytes the
capture actually carries, while the rendered `HH:MM-HH:MM` strings do. The
plan is explicit that the strings are the contract, not the prose decimals -
so every assertion here was checked against `remap_tou_task_keys` once while
writing the test and is now a literal, per the rule that an expectation is
never derived by calling the code under test at assertion time.
"""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.ecoflow_energy.ecoflow.const import DEVICE_TYPE_POWEROCEAN
from custom_components.ecoflow_energy.ecoflow.parsers.powerocean_proto import (
    _format_windows,
    remap_timer_task_keys,
    remap_tou_task_keys,
)
from custom_components.ecoflow_energy.ecoflow.proto.runtime import (
    decode_proto_runtime_frame,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

_FIXTURE = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "powerocean"
        / "j327_tou_task_write_masked.json"
    ).read_text()
)
_LISTS_96_14: dict[str, str] = {
    entry["utc"]: entry["pdata"] for entry in _FIXTURE["lists_96_14"]
}


def _header(cmd_func: int, cmd_id: int, pdata: bytes) -> bytes:
    """Wrap a payload in the one-header envelope the device sends it in."""
    header = bytearray()
    header.extend(encode_field_bytes(1, pdata))
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    return encode_field_bytes(1, bytes(header))


def _decode(pdata: bytes) -> dict:
    """Run one payload through the registry the way a real frame does."""
    result = decode_proto_runtime_frame(
        _header(96, 14, pdata), device_type=DEVICE_TYPE_POWEROCEAN
    )
    assert result.mapped.get("_is_tou_task_list"), result.parse_path
    return {k: v for k, v in result.mapped.items() if not k.startswith("_")}


def _decode_list(utc: str) -> dict:
    """Decode one of the fixture's `96/14` pushes by its recorded UTC time."""
    return _decode(bytes.fromhex(_LISTS_96_14[utc]))


def test_the_first_list_carries_two_untouched_slots() -> None:
    """12:00:39 (get_reply), before slot 4 exists: only 2 and 3 are reported.

    Slot 3 is armed and running (its window is open); slot 2 is armed but not
    running. Both carry two independent readings the write-path tests never
    touch, so this is the read path's only source for them.
    """
    known: set[int] = set()
    keys = remap_tou_task_keys(_decode_list("12:00:39"), known)

    assert known == {2, 3}
    assert keys == {
        "feed_schedule_3_enabled": True,
        "feed_schedule_3_running": True,
        "feed_schedule_3_power_w": 3500,
        "feed_schedule_3_window": "11:30-17:00",
        "feed_schedule_3_type": 2,
        "feed_schedule_3_time_mode": 65,
        "feed_schedule_3_time_param": None,
        "feed_schedule_3_time_table": [66847410],
        "feed_schedule_2_enabled": True,
        "feed_schedule_2_running": False,
        "feed_schedule_2_power_w": 2100,
        "feed_schedule_2_window": "08:00-11:30, 17:00-19:00",
        "feed_schedule_2_type": 2,
        "feed_schedule_2_time_mode": 65,
        "feed_schedule_2_time_param": None,
        "feed_schedule_2_time_table": [45220320, 74712060],
    }


def test_the_created_slot_reports_its_daily_window_and_power() -> None:
    """12:04:06: slot 4 created, daily (time_mode 65), 1300 W, two windows."""
    keys = remap_tou_task_keys(_decode_list("12:04:06"), set())

    assert keys["feed_schedule_4_enabled"] is True
    assert keys["feed_schedule_4_running"] is False
    assert keys["feed_schedule_4_power_w"] == 1300
    assert keys["feed_schedule_4_window"] == "21:00-21:30, 22:00-22:30"
    assert keys["feed_schedule_4_time_mode"] == 65
    assert keys["feed_schedule_4_time_param"] is None
    assert keys["feed_schedule_4_time_table"] == [84542700, 88474920]


def test_a_power_change_leaves_the_window_untouched() -> None:
    """12:04:22: slot 4's power moves to 2600 W, the two windows do not."""
    keys = remap_tou_task_keys(_decode_list("12:04:22"), set())

    assert keys["feed_schedule_4_power_w"] == 2600
    assert keys["feed_schedule_4_window"] == "21:00-21:30, 22:00-22:30"
    assert keys["feed_schedule_4_time_table"] == [84542700, 88474920]


def test_a_repeat_change_to_one_off_extends_the_second_window() -> None:
    """12:04:39: time_mode 68 (one-off), time_param 1037609, window to 23:00.

    The window's second half moved from 22:30 to 23:00 in an earlier write in
    the same capture; this list is the first to report it alongside the new
    repeat mode.
    """
    keys = remap_tou_task_keys(_decode_list("12:04:39"), set())

    assert keys["feed_schedule_4_time_mode"] == 68
    assert keys["feed_schedule_4_time_param"] == 1037609
    assert keys["feed_schedule_4_window"] == "21:00-21:30, 22:00-23:00"
    assert keys["feed_schedule_4_time_table"] == [84542700, 90441000]


def test_repeat_mode_and_param_change_independently_of_the_window() -> None:
    """12:04:45 and 12:04:51: two weekly repeat masks, same unchanged window."""
    weekly_wed_thu = remap_tou_task_keys(_decode_list("12:04:45"), set())
    assert weekly_wed_thu["feed_schedule_4_time_mode"] == 66
    assert weekly_wed_thu["feed_schedule_4_time_param"] == 12
    assert weekly_wed_thu["feed_schedule_4_window"] == "21:00-21:30, 22:00-23:00"

    weekly_wed_only = remap_tou_task_keys(_decode_list("12:04:51"), set())
    assert weekly_wed_only["feed_schedule_4_time_mode"] == 66
    assert weekly_wed_only["feed_schedule_4_time_param"] == 4


def test_a_slot_that_leaves_the_list_has_its_keys_retracted() -> None:
    """12:04:55: slot 4 is gone again, only 2 and 3 remain known.

    Nothing else would ever clear the `feed_schedule_4_*` keys, so this is the
    only place they stop describing a slot the device no longer holds.
    """
    known: set[int] = set()
    remap_tou_task_keys(_decode_list("12:04:51"), known)
    assert known == {2, 3, 4}

    keys = remap_tou_task_keys(_decode_list("12:04:55"), known)

    assert known == {2, 3}
    retracted = {k: v for k, v in keys.items() if k.startswith("feed_schedule_4_")}
    assert retracted == {
        "feed_schedule_4_enabled": None,
        "feed_schedule_4_running": None,
        "feed_schedule_4_power_w": None,
        "feed_schedule_4_window": None,
        "feed_schedule_4_type": None,
        "feed_schedule_4_time_mode": None,
        "feed_schedule_4_time_param": None,
        "feed_schedule_4_time_table": None,
    }


def test_a_pou_task_cfg_entry_is_never_read() -> None:
    """Re-tag slot 3's entry from field 1 to field 2 and it produces nothing.

    Field 2 is `pou_task_cfg`, declared on the message and never populated in
    any capture held here. The 12:00:39 list's first entry (slot 3) is
    re-tagged from 0x0a (field 1, length-delimited) to 0x12 (field 2, same
    wire type) - the second entry (slot 2) is untouched and stays in
    `tou_task_cfg`. Only slot 2's keys must appear.
    """
    original = _LISTS_96_14["12:00:39"]
    assert original.startswith("0a")
    retagged = "12" + original[2:]

    keys = remap_tou_task_keys(_decode(bytes.fromhex(retagged)), set())

    assert keys["feed_schedule_2_power_w"] == 2100
    assert not any(key.startswith("feed_schedule_3_") for key in keys)


def test_an_empty_payload_retracts_every_known_slot() -> None:
    """The header can carry a zero-length payload, decoded as an empty list."""
    raw = _decode(b"")
    known = {2, 3, 4}

    keys = remap_tou_task_keys(raw, known)

    assert known == set()
    assert all(value is None for value in keys.values())
    assert {"feed_schedule_2_power_w", "feed_schedule_4_time_table"} <= keys.keys()


def test_the_timer_and_feed_lists_use_independent_index_sets() -> None:
    """Slot 4 on the timer list and slot 2 on the feed list do not collide.

    The reporter capture on #381 carries feed indices 2, 3 and 4 while a
    timer-task capture on a different unit (#328) carries indices 3 and 4 -
    two independent numbering spaces that must not share bookkeeping.
    """
    timer_known: set[int] = set()
    feed_known: set[int] = set()

    remap_timer_task_keys(
        {"time_task_cfg": [{"task_index": 4, "is_enable": True}]}, timer_known
    )
    remap_tou_task_keys(
        {"tou_task_cfg": [{"task_index": 2, "is_enable": True}]}, feed_known
    )

    assert timer_known == {4}
    assert feed_known == {2}


def test_format_windows_of_an_empty_list_is_none() -> None:
    """No windows is not a window from midnight to midnight, twice."""
    assert _format_windows([]) is None
    assert _format_windows(None) is None


def test_format_windows_refuses_the_whole_string_on_one_bad_entry() -> None:
    """A block worth of 0xFFFFFFFF is a start and end past the end of the day.

    Half of a schedule is worse than none - the same rule the single-window
    renderer already applies to itself.
    """
    assert _format_windows([84542700, 0xFFFFFFFF]) is None
