"""PowerPulse 2 settings writes on the PowerOcean route follow its timing (#566).

A wallbox linked to a PowerOcean as a system component gets its settings only
from the PowerOcean's relayed copy. The PowerOcean relays a change within
seconds but can stay quiet for minutes when nothing changes: the diagnostics
counter of the longest gap between relayed reports read 214 s on 2026-10-15.
A write built on such a report is sent up to 300 s after it, and Charge Mode
waits the relayed read-back window as the other settings writes do. The
own-report route keeps its 10 s / 20 s limits.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ecoflow_energy import const
from tests.ha.test_powerpulse2_charge_action import (
    POWEROCEAN_DEVICE,
    _mqtt,
    _set_descriptor,
    _wire_entry,
)
from tests.ha.test_powerpulse2_charging_mode import (
    _apply_frame,
    _heartbeat_frame_with_mode,
)
from tests.ha.test_powerpulse2_display_block_553 import (
    _BLOCK_LED_ON_25,
    _CORE_CLOCK,
    _relay,
    _scale_windows,
)
from tests.ha.test_powerpulse2_display_writes import _published_display
from tests.ha.test_powerpulse2_settings_writes import _SET_COMMANDS

# The diagnostics counter `max_gap_s` of the reporter's 2026-10-15 download.
_MEASURED_GAP_S = 214.0


@pytest.fixture(autouse=True)
def _short_fresh_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal test only needs the refusal, not the 5 s wait for a report."""
    monkeypatch.setattr(f"{_SET_COMMANDS}.POWERPULSE2_SETTINGS_FRESH_WAIT_S", 0.05)


def test_the_relayed_age_limit_covers_the_measured_gap() -> None:
    """The relayed limit covers the measured gap, the own route keeps 10 s."""
    assert const.POWERPULSE2_RELAYED_SETTINGS_MAX_AGE_S == 300.0
    assert const.POWERPULSE2_RELAYED_SETTINGS_MAX_AGE_S > _MEASURED_GAP_S
    assert const.POWERPULSE2_SETTINGS_MAX_AGE_S == 10.0


async def test_a_relayed_report_from_a_quiet_stretch_is_written_on(
    hass: HomeAssistant,
) -> None:
    """Report 31 (LED off), then 214 s without a relayed report: switching the
    LED on is sent and confirmed by the next report (frame 37). On the
    own-report route the same report at the same age is refused as stale and
    nothing is sent.

    Mutation probe: setting `POWERPULSE2_RELAYED_SETTINGS_MAX_AGE_S` back to
    60 s refuses the relayed write as stale and nothing is sent.
    """
    aged = SimpleNamespace(monotonic=lambda: 1000.0 + _MEASURED_GAP_S)

    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    with patch(_CORE_CLOCK, return_value=1000.0):
        _relay(oceans[0], 31)
    assert wallbox._own_settings_report_seen is False
    with patch(f"{_SET_COMMANDS}.time", aged):
        task = asyncio.create_task(
            wallbox.async_set_powerpulse_display("ev_led_enabled", 1)
        )
        await asyncio.sleep(0.1)
        assert send.call_count == 1
        assert _published_display(send.call_args) == [(21, 2, _BLOCK_LED_ON_25)]
        _relay(oceans[0], 37)
        await asyncio.wait_for(task, 2)
    assert wallbox.device_data["ev_led_enabled"] is True

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


async def test_a_relayed_report_past_the_limit_is_still_refused(
    hass: HomeAssistant,
) -> None:
    """A relayed report older than 300 s is refused as stale and nothing is
    sent: the limit is longer, not gone.

    Mutation probe: dropping the age comparison in `_fresh_settings_report`
    sends the write.
    """
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    with patch(_CORE_CLOCK, return_value=1000.0):
        _relay(oceans[0], 31)
    past = SimpleNamespace(
        monotonic=lambda: 1000.0 + const.POWERPULSE2_RELAYED_SETTINGS_MAX_AGE_S + 1
    )
    with (
        patch(f"{_SET_COMMANDS}.time", past),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await wallbox.async_set_powerpulse_display("ev_led_enabled", 1)
    assert excinfo.value.translation_key == "powerpulse_continuous_report_stale"
    assert send.call_count == 0


async def test_charge_mode_waits_the_window_of_its_route(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the PowerOcean route a Charge Mode read-back that comes after the
    own window is still accepted inside the relayed one; on the own-report
    route a write without a read-back times out at the own window.

    Mutation probe: waiting `POWERPULSE2_SETTINGS_WINDOW_S` in
    `async_set_powerpulse_charge_mode` again times the relayed write out.
    """
    _scale_windows(monkeypatch, relayed=3.0, own=0.1)

    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_frame(wallbox, _heartbeat_frame_with_mode(1, 3))  # custom before
    assert wallbox._own_settings_report_seen is False
    task = asyncio.create_task(wallbox.async_set_powerpulse_charge_mode("solar"))
    await asyncio.sleep(0.3)  # past the own window, inside the relayed one
    assert _mqtt(oceans[0]).send_proto_set.call_count == 1
    assert not task.done()
    _apply_frame(wallbox, _heartbeat_frame_with_mode(1, 2))
    await asyncio.wait_for(task, 5)
    assert wallbox.device_data["ev_charge_mode"] == "solar"

    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_frame(wallbox, _heartbeat_frame_with_mode(1, 3))
    wallbox._own_settings_report_seen = True
    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_charge_mode("solar")
    assert excinfo.value.translation_key == "powerpulse_charge_mode_not_confirmed"
    assert wallbox._pending_write is None
