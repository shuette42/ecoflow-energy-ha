"""The PowerPulse 2 display writes: LED and screen on/off and brightness (PLAN-188).

The wallbox takes its display as one six-byte value, field 21 of
`EDevPileParamSet`: `[LED on, Screen on, LED brightness %, Screen brightness %,
0, 0]`. `async_set_powerpulse_display` rebuilds that block from the latest
settings report with one value changed and sends it alone. These tests pin the
exact bytes on the wire, the refusals that apply before anything is sent, and
the read-back: no optimistic state, confirmed only by a later report.

Helpers are the ones the settings-write tests built, imported rather than
duplicated. The report in the byte-exact cases is a real recorded frame
(`c376_settings_481_482_20261004.json`, frame 0: `aa01 06 01 01 19 19 02 00`),
whose trailing byte reads 2.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.coordinator.core import PendingDeviceWrite
from custom_components.ecoflow_energy.coordinator.set_commands import _SettingsWrite
from custom_components.ecoflow_energy.ecoflow.parsers.stream_proto import _iter_fields
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)
from tests.ha.test_powerpulse2_charge_action import (
    POWEROCEAN_DEVICE,
    TOPIC,
    _mqtt,
    _set_descriptor,
    _wire_entry,
)
from tests.ha.test_powerpulse2_settings_writes import (
    _SET_COMMANDS,
    _apply_report,
    _published_fields,
    _set_continuous,
    _start,
)
from tests.ha.test_powerpulse2_switch_bits import (
    DISCHARGE_BLOCK,
    PLUG_AND_PLAY,
    _write,
)

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "powerpulse"
    / "c376_settings_481_482_20261004.json"
)


@pytest.fixture(autouse=True)
def _short_fresh_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A write that finds the settings report missing or stale waits
    `POWERPULSE2_SETTINGS_FRESH_WAIT_S` for the next report before it refuses
    (#495). The refusal tests here only need the refusal, so the wait is short;
    the tests about the wait itself set their own.
    """
    monkeypatch.setattr(f"{_SET_COMMANDS}.POWERPULSE2_SETTINGS_FRESH_WAIT_S", 0.05)


def _apply_recorded_display_frame(wallbox: EcoFlowDeviceCoordinator) -> None:
    """Apply the recorded frame whose display block reads [1, 1, 25, 25, 2, 0]."""
    frame = json.loads(_FIXTURE_PATH.read_text())["frames"][0]
    parsed = wallbox._parse_message(TOPIC, bytes.fromhex(frame["hex"]))
    assert parsed is not None and parsed["ev_led_brightness_pct"] == 25
    wallbox._apply_data(parsed)


def _report(
    wallbox: EcoFlowDeviceCoordinator,
    led: bool,
    screen: bool,
    led_pct: int,
    screen_pct: int,
) -> None:
    """Apply a synthetic settings report carrying the four display values."""
    _apply_report(
        wallbox,
        ev_settings_switch_bits=18,
        ev_led_enabled=led,
        ev_screen_enabled=screen,
        ev_led_brightness_pct=led_pct,
        ev_screen_brightness_pct=screen_pct,
    )


def _published_display(call_args: Any) -> list[tuple[int, int, bytes]]:
    """Every field of the published `EDevPileParamSet` as (number, wire type,
    raw bytes), read back from the wire and not through the builder."""
    payload = call_args.args[0]
    pdata = bytes.fromhex(decode_header_message(payload)[0][0]["pdata"])
    for field_num, _wire, raw in _iter_fields(pdata):
        if field_num == 4:
            return [(n, w, bytes(r)) for n, w, r in _iter_fields(raw)]
    raise AssertionError("no EDevPileParamSet (pdata field 4) in the payload")


async def test_screen_off_sends_field_21_alone_with_trailing_bytes_zero(
    hass: HomeAssistant,
) -> None:
    """From the recorded report [1, 1, 25, 25, 2, 0], Screen off publishes
    exactly one field in the settings message: 21, length-delimited, the six
    bytes [1, 0, 25, 25, 0, 0]. The report's own byte 4 is 2 and is not copied.
    The store is made to disagree with the report first, so a block built from
    it would carry other bytes.

    Mutation probes: swapping the two brightness bytes in the builder fails
    test_led_brightness_while_screen_off_keeps_the_other_three_bytes; copying
    the report's byte 4 (2) into the block fails the exact comparison here;
    reading the bytes from `_device_data` instead of the report snapshot fails
    it too (the store says screen on).
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _apply_recorded_display_frame(wallbox)
    wallbox.set_device_value("ev_screen_enabled", False)
    wallbox.set_device_value("ev_led_enabled", False)

    task = await _start(wallbox.async_set_powerpulse_display("ev_screen_enabled", 0))
    assert send.call_count == 1
    assert _published_display(send.call_args) == [(21, 2, bytes([1, 0, 25, 25, 0, 0]))]
    _report(wallbox, True, False, 25, 25)
    await task
    assert wallbox._pending_write is None


async def test_led_brightness_while_screen_off_keeps_the_other_three_bytes(
    hass: HomeAssistant,
) -> None:
    """With the screen off (report [1, 0, 75, 25]), LED brightness 50 is written
    as [1, 0, 50, 25, 0, 0]: brightness is writable while the screen is off, the
    changed byte is the LED's (index 2) and the screen's brightness (index 3)
    keeps its reported 25. The two brightness values differ on purpose, so a
    block with them in the wrong places is not the expected one.

    Mutation probe: swapping the two brightness bytes in the builder gives
    [1, 0, 25, 50, 0, 0] and fails the comparison.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _report(wallbox, True, False, 75, 25)

    task = await _start(
        wallbox.async_set_powerpulse_display("ev_led_brightness_pct", 50)
    )
    assert send.call_count == 1
    assert _published_display(send.call_args) == [(21, 2, bytes([1, 0, 50, 25, 0, 0]))]
    _report(wallbox, True, False, 50, 25)
    await task


@pytest.mark.parametrize(
    ("report", "age_s", "translation_key", "placeholders"),
    [
        (None, 0, "powerpulse_continuous_report_missing", None),
        ((True, True, 25, 25), 11, "powerpulse_continuous_report_stale", None),
        (
            (True, True, 30, 25),
            0,
            "powerpulse_continuous_report_unusable",
            {"field": "LED brightness", "value": "30"},
        ),
        (
            "three_keys",
            0,
            "powerpulse_continuous_report_unusable",
            {"field": "screen brightness", "value": "no valid value"},
        ),
    ],
    ids=["no_report", "stale_report", "invalid_byte_in_report", "a_key_left_out"],
)
async def test_display_write_refuses_without_a_usable_fresh_report(
    hass: HomeAssistant,
    report: Any,
    age_s: float,
    translation_key: str,
    placeholders: dict[str, str] | None,
) -> None:
    """Nothing is published and no pending record is left when there is no
    report, when it is 11 s old (the limit is 10 s), when one of the four values
    in it is not writable (an LED brightness of 30), or when a settings report
    lacks a display value (the parser leaves an invalid byte out, so this is
    what a bad byte looks like to the coordinator). The last case is the
    unusable text, not the "wait for the next report" one: waiting does not
    bring the byte back.

    Mutation probes: dropping the age comparison in the plan fails the stale
    case; accepting the report with a key left out and filling the byte from a
    default fails the last case; raising `report_missing` for the left-out key
    again fails the last case on its translation key.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    stamped = 0.0
    if report == "three_keys":
        _apply_report(
            wallbox,
            ev_settings_switch_bits=18,
            ev_led_enabled=True,
            ev_screen_enabled=True,
            ev_led_brightness_pct=25,
        )
    elif report is not None:
        _report(wallbox, *report)
    if wallbox._settings_report is not None:
        stamped = wallbox._settings_report[0]

    with (
        patch(
            f"{_SET_COMMANDS}.time", SimpleNamespace(monotonic=lambda: stamped + age_s)
        ),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await wallbox.async_set_powerpulse_display("ev_screen_enabled", 0)
    assert excinfo.value.translation_key == translation_key
    assert excinfo.value.translation_placeholders == placeholders
    assert send.call_count == 0
    assert wallbox._pending_write is None


async def test_display_write_confirms_on_a_newer_report_with_the_target_byte(
    hass: HomeAssistant,
) -> None:
    """Screen off waits for the read-back. The store already holds the target
    (False) on purpose: a report that still says on, or a report that has no
    screen key, must not confirm it, and nothing is published into the store
    optimistically. A report with the screen off confirms. A second write that
    is never confirmed fails with the not-confirmed text naming the last
    reported state, on.

    Mutation probes: reading the confirmation from `_device_data` instead of
    the arriving frame makes the still-on report confirm; dropping the
    `state_key not in parsed` return in `_resolve_pending_write` fails the
    keyless report with a KeyError; making the future never resolve leaves the
    first half hanging.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _mqtt(oceans[0])
    _report(wallbox, True, True, 25, 25)
    wallbox.set_device_value("ev_screen_enabled", False)

    task = await _start(wallbox.async_set_powerpulse_display("ev_screen_enabled", 0))
    pending = wallbox._pending_write
    assert pending is not None and not pending.future.done()

    _apply_report(wallbox, ev_settings_switch_bits=18)  # a report without the key
    assert not pending.future.done()
    _report(wallbox, True, True, 25, 25)  # a newer report, still on
    assert not pending.future.done()
    assert not task.done()

    _report(wallbox, True, False, 25, 25)  # the target byte
    await task
    assert wallbox._pending_write is None

    _report(wallbox, True, True, 25, 25)
    with patch(f"{_SET_COMMANDS}.POWERPULSE2_SETTINGS_WINDOW_S", 0.1):
        task = await _start(
            wallbox.async_set_powerpulse_display("ev_led_brightness_pct", 100)
        )
        _report(wallbox, True, True, 25, 25)  # a newer report, unchanged
        with pytest.raises(HomeAssistantError) as excinfo:
            await task
    assert excinfo.value.translation_key == "powerpulse_continuous_not_confirmed"
    assert excinfo.value.translation_placeholders == {"reported": "25 %"}
    assert wallbox._pending_write is None


@pytest.mark.parametrize("value", [30, 0, 24, 125, 50.0, True])
async def test_display_brightness_outside_the_four_steps_is_refused_up_front(
    hass: HomeAssistant, value: Any
) -> None:
    """25, 50, 75 and 100 are the only brightness values the wallbox takes. A
    30, a 0, a float 50.0 (the number platform converts before it calls) and a
    bool are refused with the brightness text naming the allowed values, before
    any report is read and with nothing published.

    Mutation probe: widening the check to `0 <= value <= 100` lets 30 through
    and fails the first case.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _report(wallbox, True, True, 25, 25)

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_display("ev_screen_brightness_pct", value)
    assert excinfo.value.translation_key == "powerpulse_display_brightness_invalid"
    assert excinfo.value.translation_placeholders == {
        "allowed": "25, 50, 75, 100",
        "value": str(value),
    }
    assert send.call_count == 0
    assert wallbox._pending_write is None


async def test_a_second_display_write_waits_for_the_first_to_confirm_or_time_out(
    hass: HomeAssistant,
) -> None:
    """From the report [1, 1, 25, 25], Screen brightness 50 and then LED
    brightness 75 end at [1, 1, 75, 50] as two writes. The second call arrives
    while the first waits for its read-back: it publishes nothing and raises
    nothing until the report that confirms the first arrives, is built from that
    report, and then waits for its own (#495; it used to be refused as in
    progress). A write queued behind one that is never confirmed goes ahead when
    that one times out, and the timed-out call still raises its own error.

    Mutation probes: restoring the immediate in-progress refusal for a pending
    settings write fails the `not second.done()` assertion (the task has ended
    with the error); building the second block from the report read before the
    wait (`[1, 1, 75, 25, 0, 0]`) fails the second comparison; dropping the
    notification in the call's `finally` leaves the call queued behind the
    unconfirmed write waiting for its bound and fails the `call_count == 4`
    assertion.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _report(wallbox, True, True, 25, 25)

    first = await _start(
        wallbox.async_set_powerpulse_display("ev_screen_brightness_pct", 50)
    )
    assert send.call_count == 1
    assert _published_display(send.call_args) == [(21, 2, bytes([1, 1, 25, 50, 0, 0]))]

    second = await _start(
        wallbox.async_set_powerpulse_display("ev_led_brightness_pct", 75)
    )
    assert send.call_count == 1
    assert not first.done() and not second.done()

    _report(wallbox, True, True, 25, 50)  # the report that confirms the first
    await first
    await asyncio.sleep(0.05)
    assert send.call_count == 2
    assert _published_display(send.call_args) == [(21, 2, bytes([1, 1, 75, 50, 0, 0]))]
    assert not second.done()
    _report(wallbox, True, True, 75, 50)
    await second
    assert wallbox._pending_write is None

    # A write behind one that is never confirmed goes ahead when that one times
    # out: its call ends with the not-confirmed text, the queued call publishes.
    with patch(f"{_SET_COMMANDS}.POWERPULSE2_SETTINGS_WINDOW_S", 0.15):
        lost = await _start(
            wallbox.async_set_powerpulse_display("ev_led_brightness_pct", 50)
        )
        queued = await _start(
            wallbox.async_set_powerpulse_display("ev_screen_brightness_pct", 75)
        )
        assert send.call_count == 3 and not queued.done()
        with pytest.raises(HomeAssistantError) as excinfo:
            await lost
        assert excinfo.value.translation_key == "powerpulse_continuous_not_confirmed"
        await asyncio.sleep(0.05)
        assert send.call_count == 4
        assert _published_display(send.call_args) == [
            (21, 2, bytes([1, 1, 75, 75, 0, 0]))
        ]
        _report(wallbox, True, True, 75, 75)
        await queued
    assert wallbox._pending_write is None


async def test_a_display_write_and_a_switch_bit_write_queue_behind_each_other(
    hass: HomeAssistant,
) -> None:
    """One settings write at a time on the wire, across both kinds. While a
    Continuous charging write waits for its report, a Screen write waits behind
    it and publishes only once that is confirmed; the same holds the other way
    round, where a Continuous write waits behind a Screen write. The switch
    byte of the second write comes from the report that confirmed the display
    write (18 -> 2), not from the one before it.

    Mutation probe: dropping the `settings_write` flag from the pending record
    makes the second call refuse as in progress and fails the first `not
    display_task.done()` assertion.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _report(wallbox, True, True, 25, 25)

    switch_task = await _start(_set_continuous(wallbox, True))
    assert send.call_count == 1
    display_task = await _start(
        wallbox.async_set_powerpulse_display("ev_screen_enabled", 0)
    )
    assert send.call_count == 1
    assert not switch_task.done() and not display_task.done()
    _report(wallbox, True, True, 25, 25)  # bits 18: the Continuous write's target
    await switch_task
    await asyncio.sleep(0.05)
    assert send.call_count == 2
    assert _published_display(send.call_args) == [(21, 2, bytes([1, 0, 25, 25, 0, 0]))]

    back = await _start(_set_continuous(wallbox, False))
    assert send.call_count == 2
    assert not back.done() and not display_task.done()
    _report(wallbox, True, False, 25, 25)
    await display_task
    await asyncio.sleep(0.05)
    assert send.call_count == 3
    assert _published_fields(send.call_args) == {1: 2}
    _apply_report(
        wallbox,
        ev_settings_switch_bits=2,
        ev_led_enabled=True,
        ev_screen_enabled=False,
        ev_led_brightness_pct=25,
        ev_screen_brightness_pct=25,
    )
    await back
    assert wallbox._pending_write is None


async def test_rapid_writes_for_one_value_publish_only_the_first_and_the_last(
    hass: HomeAssistant,
) -> None:
    """The LED brightness slider is moved 50, 75, 100 while the first write waits
    for its read-back, and the Screen brightness is set to 50 in between. The 75
    is overtaken by the 100 before it was sent: its call ends at once, without an
    error and without publishing. The Screen write, which arrived before the 100,
    keeps its place in line: the wallbox gets the 50, the Screen 50 and the 100,
    in that order, each built on the report that confirmed the one before.

    Mutation probes: dropping the superseded check in
    `_async_write_powerpulse_settings` publishes the 75 as well and leaves its
    call waiting at the `second.done()` assertion; dropping the notification
    when a call replaces another leaves the 75 waiting until the first write
    ends and fails the same assertion.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _report(wallbox, True, True, 25, 25)

    first = await _start(
        wallbox.async_set_powerpulse_display("ev_led_brightness_pct", 50)
    )
    second = await _start(
        wallbox.async_set_powerpulse_display("ev_led_brightness_pct", 75)
    )
    screen = await _start(
        wallbox.async_set_powerpulse_display("ev_screen_brightness_pct", 50)
    )
    assert send.call_count == 1 and not second.done()
    third = await _start(
        wallbox.async_set_powerpulse_display("ev_led_brightness_pct", 100)
    )
    assert second.done() and second.exception() is None
    assert send.call_count == 1 and not third.done() and not screen.done()

    _report(wallbox, True, True, 50, 25)  # confirms the first
    await first
    await asyncio.sleep(0.05)
    assert send.call_count == 2
    assert _published_display(send.call_args) == [(21, 2, bytes([1, 1, 50, 50, 0, 0]))]
    _report(wallbox, True, True, 50, 50)
    await screen
    await asyncio.sleep(0.05)
    assert send.call_count == 3
    assert _published_display(send.call_args) == [(21, 2, bytes([1, 1, 100, 50, 0, 0]))]
    _report(wallbox, True, True, 100, 50)
    await third
    assert wallbox._pending_write is None


async def test_writes_to_different_switch_bits_do_not_supersede_each_other(
    hass: HomeAssistant,
) -> None:
    """The three settings switches share one state key, `ev_settings_switch_bits`,
    and differ in their bit. While Block Battery Discharge waits for its
    read-back, Plug and Play and then Continuous charging off wait behind it:
    neither replaces the other, and all three reach the wallbox in order, each on
    the byte the report before it showed (16 -> 17 -> 19 -> 3).

    Mutation probe: keying the newest-call check on the state key instead of the
    write's action ends the Plug and Play call without publishing and fails the
    `second.done()` assertion.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _apply_report(wallbox, ev_settings_switch_bits=16)

    first = await _start(_write(wallbox, DISCHARGE_BLOCK, True))
    assert _published_fields(send.call_args) == {1: 17}
    second = await _start(_write(wallbox, PLUG_AND_PLAY, True))
    third = await _start(_set_continuous(wallbox, False))
    assert not second.done() and not third.done()
    assert send.call_count == 1

    _apply_report(wallbox, ev_settings_switch_bits=17)
    await first
    await asyncio.sleep(0.05)
    assert _published_fields(send.call_args) == {1: 19}
    assert not third.done()
    _apply_report(wallbox, ev_settings_switch_bits=19)
    await second
    await asyncio.sleep(0.05)
    assert _published_fields(send.call_args) == {1: 3}
    _apply_report(wallbox, ev_settings_switch_bits=3)
    await third
    assert send.call_count == 3
    assert wallbox._pending_write is None


@pytest.mark.parametrize(
    "record",
    [
        PendingDeviceWrite(
            action="max_current",
            issued_at=0.0,
            future=None,  # type: ignore[arg-type]
            state_key="ev_max_current_a",
            expected_value=11.0,
        ),
        PendingDeviceWrite(action="stop", issued_at=0.0, future=None),  # type: ignore[arg-type]
    ],
    ids=["max_current", "start_stop"],
)
async def test_a_settings_write_still_refuses_at_once_behind_another_kind_of_write(
    hass: HomeAssistant, record: PendingDeviceWrite
) -> None:
    """Only a settings write is waited for. While a maximum-current or a
    start/stop write is pending, a display write is refused as in progress
    without waiting, publishes nothing and leaves that write in place.

    Mutation probe: letting any pending write be waited for (dropping the
    `settings_write` condition) makes the call wait, and the one-second guard
    around it ends the test with a TimeoutError.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _report(wallbox, True, True, 25, 25)
    pending = PendingDeviceWrite(
        action=record.action,
        issued_at=0.0,
        future=hass.loop.create_future(),
        state_key=record.state_key,
        expected_value=record.expected_value,
    )
    wallbox._pending_write = pending

    with pytest.raises(HomeAssistantError) as excinfo:
        async with asyncio.timeout(1):
            await wallbox.async_set_powerpulse_display("ev_screen_enabled", 0)
    assert excinfo.value.translation_key == "powerpulse_action_in_progress"
    assert send.call_count == 0
    assert wallbox._pending_write is pending


async def test_a_stale_report_waits_for_a_fresh_one_and_then_publishes(
    hass: HomeAssistant,
) -> None:
    """A report 11 s old is not refused at once: the write waits for the next
    report and sends nothing meanwhile. When one arrives (the wallbox sends
    about one a second) the write is built from it and published, with the
    block carrying the new report's values (LED brightness 50, which only the
    second report has).

    Mutation probes: refusing at once instead of waiting ends the task with the
    stale error before the second report; dropping the notification in
    `_record_settings_report` leaves the call asleep until its bound and fails
    the `call_count == 1` assertion (the bound is one second here).
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _report(wallbox, True, True, 25, 25)
    assert wallbox._settings_report is not None
    clock = {"now": wallbox._settings_report[0] + 11}

    with (
        patch(f"{_SET_COMMANDS}.POWERPULSE2_SETTINGS_FRESH_WAIT_S", 1.0),
        patch(f"{_SET_COMMANDS}.time", SimpleNamespace(monotonic=lambda: clock["now"])),
    ):
        task = await _start(
            wallbox.async_set_powerpulse_display("ev_screen_enabled", 0)
        )
        assert send.call_count == 0 and not task.done()

        _report(wallbox, True, True, 50, 25)
        clock["now"] = wallbox._settings_report[0] + 1
        await asyncio.sleep(0.05)
        assert send.call_count == 1
        assert _published_display(send.call_args) == [
            (21, 2, bytes([1, 0, 50, 25, 0, 0]))
        ]
        _report(wallbox, True, False, 50, 25)
        await task
    assert wallbox._pending_write is None


async def test_a_stale_report_is_refused_only_after_the_bounded_wait(
    hass: HomeAssistant,
) -> None:
    """With no newer report the call keeps waiting for the bound (0.3 s here, 5 s
    in production) and only then raises the stale text. Nothing is sent and no
    pending record is left.

    Mutation probe: refusing at once ends the task before the 0.1 s check and
    fails the `not task.done()` assertion.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _report(wallbox, True, True, 25, 25)
    assert wallbox._settings_report is not None
    stale_now = wallbox._settings_report[0] + 11

    with (
        patch(f"{_SET_COMMANDS}.POWERPULSE2_SETTINGS_FRESH_WAIT_S", 0.3),
        patch(f"{_SET_COMMANDS}.time", SimpleNamespace(monotonic=lambda: stale_now)),
    ):
        task = asyncio.create_task(
            wallbox.async_set_powerpulse_display("ev_screen_enabled", 0)
        )
        await asyncio.sleep(0.1)
        assert not task.done()
        with pytest.raises(HomeAssistantError) as excinfo:
            async with asyncio.timeout(2):
                await task
    assert excinfo.value.translation_key == "powerpulse_continuous_report_stale"
    assert send.call_count == 0
    assert wallbox._pending_write is None


async def test_a_write_queued_past_the_bound_is_refused_as_in_progress(
    hass: HomeAssistant,
) -> None:
    """A second settings write waits at most the confirmation window plus the
    fresh-report wait (0.1 s here, 25 s in production) for the first one to end.
    The first is still waiting for its read-back after that, so the second is
    refused as in progress, sends nothing, and the first keeps waiting.

    Mutation probe: waiting without a bound makes the one-second guard around
    the call end the test with a TimeoutError.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _report(wallbox, True, True, 25, 25)

    first = await _start(wallbox.async_set_powerpulse_display("ev_screen_enabled", 0))
    assert send.call_count == 1
    with (
        patch(f"{_SET_COMMANDS}.POWERPULSE2_SETTINGS_WINDOW_S", 0.05),
        patch(f"{_SET_COMMANDS}.POWERPULSE2_SETTINGS_FRESH_WAIT_S", 0.05),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        async with asyncio.timeout(1):
            await wallbox.async_set_powerpulse_display("ev_led_brightness_pct", 75)
    assert excinfo.value.translation_key == "powerpulse_action_in_progress"
    assert send.call_count == 1
    assert not first.done()

    _report(wallbox, True, False, 25, 25)
    await first
    assert wallbox._pending_write is None


def test_a_settings_write_is_either_fields_or_a_display_block() -> None:
    """A write that carries both would drop its fields without a word (the
    dispatcher takes the display builder and ignores them), and one that carries
    neither would send nothing. Both shapes are refused when the record is
    built; each valid shape still builds.

    Mutation probes: dropping the `display is not None and self.fields` check
    fails the first raise; dropping the `display is None and not self.fields`
    check fails the second.
    """
    with pytest.raises(ValueError):
        _SettingsWrite({1: 3}, 3.0, display=(1, 1, 25, 25))
    with pytest.raises(ValueError):
        _SettingsWrite({}, 3.0)
    assert _SettingsWrite({1: 3}, 3.0).display is None
    assert _SettingsWrite({}, 25.0, display=(1, 1, 25, 25)).fields == {}
