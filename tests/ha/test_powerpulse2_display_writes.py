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

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
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
    _start,
)

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "powerpulse"
    / "c376_settings_481_482_20261004.json"
)


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
    assert wallbox._wallbox_action_pending is None


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
        ("three_keys", 0, "powerpulse_continuous_report_missing", None),
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
    in it is not writable (an LED brightness of 30), or when the report lacks a
    display value altogether (the parser leaves an invalid byte out, so this is
    what a bad byte looks like to the coordinator).

    Mutation probes: dropping the age comparison in the plan fails the stale
    case; accepting the report with a key left out and filling the byte from a
    default fails the last case.
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
    assert wallbox._wallbox_action_pending is None


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
    `state_key not in parsed` return in `_resolve_wallbox_action` fails the
    keyless report with a KeyError; making the future never resolve leaves the
    first half hanging.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _mqtt(oceans[0])
    _report(wallbox, True, True, 25, 25)
    wallbox.set_device_value("ev_screen_enabled", False)

    task = await _start(wallbox.async_set_powerpulse_display("ev_screen_enabled", 0))
    pending = wallbox._wallbox_action_pending
    assert pending is not None and not pending.future.done()

    _apply_report(wallbox, ev_settings_switch_bits=18)  # a report without the key
    assert not pending.future.done()
    _report(wallbox, True, True, 25, 25)  # a newer report, still on
    assert not pending.future.done()
    assert not task.done()

    _report(wallbox, True, False, 25, 25)  # the target byte
    await task
    assert wallbox._wallbox_action_pending is None

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
    assert wallbox._wallbox_action_pending is None


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
    assert wallbox._wallbox_action_pending is None
