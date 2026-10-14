"""PowerPulse 2 display block relayed in its 32-byte form (#553).

A wallbox linked to a PowerOcean reports its display in a 32-byte block and
the app's copy of the settings (an ack copy with older values) rides in the
same frames. These tests replay the timed recording of the reporter's display
changes (`c376_display_block_553_20261014.json`, 45 frames, 0-based index)
through the PowerOcean's receive path into the wallbox coordinator, and check
the write side: the block goes back in the form it was read in, and the
relayed route gets the longer age limit and read-back window the measured gaps
need.

Wiring is the two-coordinator setup of `test_powerpulse2_relayed_settings.py`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ecoflow_energy import const
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.parsers.powerpulse_proto import (
    RELAYED_WALLBOX_SETTINGS_KEY,
)
from tests.ha.test_powerpulse2_charge_action import (
    POWEROCEAN_DEVICE,
    POWEROCEAN_SN,
    POWERPULSE2_SN,
    _mqtt,
    _set_descriptor,
    _wire_entry,
)
from tests.ha.test_powerpulse2_display_writes import _published_display
from tests.ha.test_powerpulse2_settings_writes import _SET_COMMANDS

_FRAMES = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "powerpulse"
        / "c376_display_block_553_20261014.json"
    ).read_text()
)["frames"]
_PO_TOPIC = f"/app/device/property/{POWEROCEAN_SN}"
_CORE_CLOCK = "custom_components.ecoflow_energy.coordinator.core.time.monotonic"

# Frames the PowerOcean sends (topic property / get_reply); the app's writes
# and the set replies are not part of its received stream.
_PO_FRAMES = [
    i for i, f in enumerate(_FRAMES) if f["topic"] in ("property", "get_reply")
]
# Own reports (src 96) and frames that carry only the app's ack copy, from the
# recording's per-frame header read (0-based index).
_REPORT_FRAMES = (1, 3, 5, 6, 7, 8, 11, 13, 17, 22, 24, 28, 31, 37, 39, 41, 43)
_ACK_ONLY_FRAMES = (2, 4, 12, 14, 18, 21, 23, 27, 32, 38, 40, 42, 44)
_DISPLAY = (
    "ev_led_enabled",
    "ev_screen_enabled",
    "ev_led_brightness_pct",
    "ev_screen_brightness_pct",
)
# Frame 33, the app's write "LED on, 25 %": the 32-byte block it puts on the wire.
_BLOCK_LED_ON_25 = bytes([1, 1, 25, 100, 2]) + bytes(27)


@pytest.fixture(autouse=True)
def _short_fresh_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal test only needs the refusal, not the 5 s wait for a report."""
    monkeypatch.setattr(f"{_SET_COMMANDS}.POWERPULSE2_SETTINGS_FRESH_WAIT_S", 0.05)


def _frame(index: int) -> bytes:
    """One recorded envelope, the masked wallbox serial (sixteen `X`) replaced
    by the test wallbox's serial: same length, so the envelope stays valid."""
    raw = bytes.fromhex(_FRAMES[index]["hex"])
    return raw.replace(b"X" * 16, POWERPULSE2_SN.encode())


def _relay(ocean: EcoFlowDeviceCoordinator, index: int) -> None:
    """Deliver one recorded frame the way the PowerOcean receives it."""
    parsed = ocean._parse_message(_PO_TOPIC, _frame(index))
    if parsed is not None:
        ocean._apply_data(parsed)


def _state(wallbox: EcoFlowDeviceCoordinator) -> tuple[Any, ...]:
    data = wallbox.device_data
    return (
        data.get("ev_settings_switch_bits"),
        data.get("ev_phase_setting"),
        *(data.get(key) for key in _DISPLAY),
    )


def _scale_windows(monkeypatch: pytest.MonkeyPatch, relayed: float, own: float) -> None:
    """Shrink the two read-back windows so a test can cross one and not the other."""
    monkeypatch.setattr(
        f"{_SET_COMMANDS}.POWERPULSE2_RELAYED_DISPLAY_WINDOW_S", relayed
    )
    monkeypatch.setattr(f"{_SET_COMMANDS}.POWERPULSE2_SETTINGS_WINDOW_S", own)


async def test_the_recording_replays_to_the_real_reports_and_not_the_ack_copies(
    hass: HomeAssistant,
) -> None:
    """Over the recording's 31 property and get_reply frames the wallbox
    follows the PowerOcean's own reports: the switch bits and the phase never
    move after the first report (the app's ack copies carry older ones), the
    display values change only on a report frame, and the form of the block
    stays out of the store.

    Mutation probes: dropping the `_display_block_len` pop in `_apply_data`
    fails the last assertion (the snapshot keeps it, the store must not).
    """
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    ocean = oceans[0]

    first_report = _REPORT_FRAMES[0]
    assert set(_REPORT_FRAMES) | set(_ACK_ONLY_FRAMES) <= set(_PO_FRAMES)
    compared_ack = compared_report = 0
    display_changes = 0
    after_first: tuple[Any, Any] | None = None
    for index in _PO_FRAMES:
        before = _state(wallbox)
        _relay(ocean, index)
        after = _state(wallbox)
        if index in _ACK_ONLY_FRAMES:
            assert after == before, f"ack copy in frame {index} moved the wallbox"
            compared_ack += 1
        elif index in _REPORT_FRAMES:
            compared_report += 1
            if after[2:] != before[2:]:
                display_changes += 1
        if index == first_report:
            after_first = after[:2]
        if after_first is not None:
            assert after[:2] == after_first, f"bits/phase moved at frame {index}"
        if index == 31:
            assert after == (19, 2, False, True, 25, 100)
        if index == 32:
            assert after == before

    assert compared_ack == len(_ACK_ONLY_FRAMES) > 0
    assert compared_report == len(_REPORT_FRAMES) > 0
    # Each display change follows a report frame.
    assert 0 < display_changes <= compared_report
    assert wallbox._settings_report is not None
    assert wallbox._settings_report[1]["_display_block_len"] == 32
    assert "_display_block_len" not in wallbox.device_data
    assert RELAYED_WALLBOX_SETTINGS_KEY not in ocean.device_data
    assert "_display_block_len" not in ocean.device_data


async def test_a_write_after_a_relayed_report_goes_out_in_the_32_byte_form(
    hass: HomeAssistant,
) -> None:
    """After report 31 (LED off, screen on, 25 %, 100 %) switching the LED on
    publishes the block the app wrote in frame 33 (`01 01 19 64`, byte 4 =
    0x02, 27 zeros), and the next report (frame 37) confirms it.

    Mutation probe: not passing `block_len=` to the builder sends the six-byte
    form and fails the published-block assertion.
    """
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set

    _relay(oceans[0], 31)
    assert _state(wallbox)[2:] == (False, True, 25, 100)
    task = asyncio.create_task(
        wallbox.async_set_powerpulse_display("ev_led_enabled", 1)
    )
    await asyncio.sleep(0.05)
    assert send.call_count == 1
    published = _published_display(send.call_args)
    assert published == [(21, 2, _BLOCK_LED_ON_25)]
    # The app's own frame 33 carries the same block on the wire.
    app_write = SimpleNamespace(args=(_frame(33),))
    assert _published_display(app_write)[0][2] == _BLOCK_LED_ON_25

    _relay(oceans[0], 37)
    await task
    assert wallbox._pending_write is None
    assert wallbox.device_data["ev_led_enabled"] is True


async def test_an_old_relayed_snapshot_is_written_on_and_a_late_readback_counts(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relayed snapshot 30.9 s old (the oldest of the seven app writes) is
    still a basis, and a read-back that comes after the own route's window is
    accepted inside the relayed one. The real constants cover the measured
    gaps (30.9 s old, confirmed 26.342 s after the write). On the own-report
    route the same age is refused as stale and nothing is sent.

    Mutation probes: using `POWERPULSE2_SETTINGS_MAX_AGE_S` for a relayed
    snapshot refuses the write; using `POWERPULSE2_SETTINGS_WINDOW_S` for it
    times the late read-back out.
    """
    assert const.POWERPULSE2_RELAYED_DISPLAY_MAX_AGE_S > 30.9
    assert const.POWERPULSE2_RELAYED_DISPLAY_WINDOW_S > 26.342
    assert const.POWERPULSE2_SETTINGS_MAX_AGE_S == 10.0
    assert const.POWERPULSE2_SETTINGS_WINDOW_S == 20.0
    _scale_windows(monkeypatch, relayed=1.0, own=0.1)

    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    with patch(_CORE_CLOCK, return_value=1000.0):
        _relay(oceans[0], 31)
    assert wallbox._own_settings_report_seen is False
    aged = SimpleNamespace(monotonic=lambda: 1000.0 + 30.9)

    with patch(f"{_SET_COMMANDS}.time", aged):
        task = asyncio.create_task(
            wallbox.async_set_powerpulse_display("ev_led_enabled", 1)
        )
        await asyncio.sleep(0.05)
        assert send.call_count == 1
        assert _published_display(send.call_args) == [(21, 2, _BLOCK_LED_ON_25)]
        await asyncio.sleep(0.3)  # past the own window, inside the relayed one
        assert not task.done()
        _relay(oceans[0], 37)
        await task
    assert wallbox.device_data["ev_led_enabled"] is True

    # The own-report route keeps the 10 s age limit: same snapshot, same age.
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    with patch(_CORE_CLOCK, return_value=1000.0):
        _relay(oceans[0], 31)
    wallbox._own_settings_report_seen = True
    with (
        patch(f"{_SET_COMMANDS}.time", aged),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await wallbox.async_set_powerpulse_display("ev_led_enabled", 1)
    assert excinfo.value.translation_key == "powerpulse_continuous_report_stale"
    assert send.call_count == 0


async def test_a_relayed_write_without_a_readback_is_not_confirmed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No report within the relayed window: the existing not-confirmed error
    is raised, the block was sent once, and the entity keeps the reported
    value (LED off) instead of the requested one.

    Mutation probe: storing the requested value in the device data after the
    send (an optimistic apply) fails the last assertion.
    """
    _scale_windows(monkeypatch, relayed=0.2, own=0.1)
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _relay(oceans[0], 31)

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_display("ev_led_enabled", 1)
    assert excinfo.value.translation_key == "powerpulse_continuous_not_confirmed"
    assert send.call_count == 1
    assert wallbox._pending_write is None
    assert wallbox.device_data["ev_led_enabled"] is False


async def test_the_own_report_route_keeps_the_six_byte_form_and_its_window(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wallbox that reports its settings on its own channel (six-byte
    snapshot, no block length) is written in six bytes, and a read-back that
    arrives after the own window (here 0.1 s, 20 s in production) is too late
    although the relayed window (1 s) would still have taken it.

    Mutation probe: giving every display write the relayed window accepts the
    late read-back and the write does not raise.
    """
    _scale_windows(monkeypatch, relayed=1.0, own=0.1)
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    wallbox._apply_data(
        {
            "ev_settings_switch_bits": 18,
            "ev_led_enabled": False,
            "ev_screen_enabled": True,
            "ev_led_brightness_pct": 25,
            "ev_screen_brightness_pct": 100,
        }
    )
    assert wallbox._own_settings_report_seen is True

    task = asyncio.create_task(
        wallbox.async_set_powerpulse_display("ev_led_enabled", 1)
    )
    await asyncio.sleep(0.05)
    assert send.call_count == 1
    assert _published_display(send.call_args) == [(21, 2, bytes([1, 1, 25, 100, 0, 0]))]
    await asyncio.sleep(0.3)
    assert task.done()  # given up at the own window, not at the relayed one
    with pytest.raises(HomeAssistantError) as excinfo:
        await task
    assert excinfo.value.translation_key == "powerpulse_continuous_not_confirmed"


def _recorded_at(index: int) -> float:
    return datetime.fromisoformat(_FRAMES[index]["ts_iso"]).timestamp()


def test_the_relayed_limits_cover_the_gaps_in_the_recording() -> None:
    """The comment at the constants and the CHANGELOG rest on these figures,
    recomputed here from the fixture: the largest gap between the PowerOcean's
    own reports, the age of the last report at each of the app's seven display
    writes, and the delay of the read-back after the write in frame 25."""
    reports = [_recorded_at(i) for i in _REPORT_FRAMES]
    largest_gap = max(b - a for a, b in zip(reports, reports[1:], strict=False))
    writes = [i for i, f in enumerate(_FRAMES) if f["topic"] == "set"]
    ages = [
        _recorded_at(w) - _recorded_at(max(r for r in _REPORT_FRAMES if r < w))
        for w in writes
    ]
    readback = _recorded_at(28) - _recorded_at(25)

    assert 49 <= largest_gap < 50
    assert len(writes) == 7
    assert 6 <= min(ages) < 7
    assert 30.8 < max(ages) < 31
    assert 26.3 < readback < 26.4
    assert largest_gap < const.POWERPULSE2_RELAYED_DISPLAY_MAX_AGE_S
    assert largest_gap < const.POWERPULSE2_RELAYED_DISPLAY_WINDOW_S


_WRITE_SITES = {
    "solar_min": lambda w: w.async_set_powerpulse_solar_min_current(7),
    "phase": lambda w: w.async_set_powerpulse_phase_setting("auto"),
    "switch_bit": lambda w: w.async_set_powerpulse_switch_bit(
        const.POWERPULSE2_SWITCH_BIT_CONTINUOUS, True, "ev_continuous_charging"
    ),
    "display": lambda w: w.async_set_powerpulse_display("ev_led_enabled", 1),
}


@pytest.mark.parametrize("site", _WRITE_SITES)
async def test_every_settings_write_waits_the_window_of_its_route(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    """Solar minimum, phase, a switch bit and the display each wait the long
    window for a read-back when the settings come from the PowerOcean's copy,
    and the short one once the wallbox has sent its own report.

    Mutation probe: a write site that falls back to the own window gives up
    at 0.1 s on the relayed route and fails the first assertion.
    """
    _scale_windows(monkeypatch, relayed=0.6, own=0.1)
    for own_seen in (False, True):
        _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _set_descriptor(wallbox)
        _relay(oceans[0], 31)
        wallbox._own_settings_report_seen = own_seen

        task = asyncio.create_task(_WRITE_SITES[site](wallbox))
        await asyncio.sleep(0.3)
        assert task.done() is own_seen
        with pytest.raises(HomeAssistantError) as excinfo:
            await task
        assert str(excinfo.value.translation_key).endswith("not_confirmed")


async def test_a_switch_bit_write_builds_on_an_old_relayed_report(
    hass: HomeAssistant,
) -> None:
    """A relayed report 30.9 s old is a basis for a switch-bit write; an own
    report of that age is not.

    Mutation probe: giving the switch-bit write the own age limit on a relayed
    report refuses the first write.
    """
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _relay(oceans[0], 31)
    assert wallbox._settings_report is not None
    aged = SimpleNamespace(monotonic=lambda: wallbox._settings_report[0] + 30.9)

    wallbox._own_settings_report_seen = True
    with (
        patch(f"{_SET_COMMANDS}.time", aged),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await _WRITE_SITES["switch_bit"](wallbox)
    assert excinfo.value.translation_key == "powerpulse_continuous_report_stale"
    assert send.call_count == 0

    wallbox._own_settings_report_seen = False
    with patch(f"{_SET_COMMANDS}.time", aged):
        task = asyncio.create_task(_WRITE_SITES["switch_bit"](wallbox))
        await asyncio.sleep(0.05)
        assert send.call_count == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
