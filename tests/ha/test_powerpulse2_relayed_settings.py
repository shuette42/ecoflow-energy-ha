"""PowerPulse 2 settings relayed by the PowerOcean on `241/3` (#553).

A wallbox linked to the PowerOcean as a system component can report its
settings only to the PowerOcean; its own `241/44` then carries no settings
block and the ten settings controls were never created. These tests replay the
reporter's real `241/3` frames into the PowerOcean coordinator and check that
the settings land on the wallbox, through the same apply path and with the same
one-source rule its own reports follow.

Wiring is the two-coordinator setup of `test_powerpulse2_charge_action.py`.
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
from custom_components.ecoflow_energy.ecoflow.parsers.powerpulse_proto import (
    RELAYED_WALLBOX_SETTINGS_KEY,
)
from custom_components.ecoflow_energy.entity import reading_reported
from tests.ha.test_powerpulse2_charge_action import (
    POWEROCEAN_DEVICE,
    POWEROCEAN_SN,
    _mqtt,
    _set_descriptor,
    _wire_entry,
)
from tests.ha.test_powerpulse2_display_writes import _published_display
from tests.ha.test_powerpulse2_settings_writes import (
    _SET_COMMANDS,
    _published_fields,
    _set_continuous,
)

_FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "powerpulse"
        / "c376_relayed_settings_553_20261014.json"
    ).read_text()
)
_PO_TOPIC = f"/app/device/property/{POWEROCEAN_SN}"

_SETTINGS_KEYS = (
    "ev_settings_switch_bits",
    "ev_settings_work_mode",
    "ev_solar_min_current_a",
    "ev_phase_setting",
    "ev_custom_current_a",
    "ev_led_enabled",
    "ev_screen_enabled",
    "ev_led_brightness_pct",
    "ev_screen_brightness_pct",
)


@pytest.fixture(autouse=True)
def _short_fresh_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stale-report refusal only needs the refusal, not the 5 s wait."""
    monkeypatch.setattr(f"{_SET_COMMANDS}.POWERPULSE2_SETTINGS_FRESH_WAIT_S", 0.05)


def _frame(label: str) -> bytes:
    for frame in _FIXTURE["frames"]:
        if frame["label"] == label:
            return bytes.fromhex(frame["hex"])
    raise KeyError(label)


def _relay(ocean: EcoFlowDeviceCoordinator, label: str = "baseline") -> None:
    """Deliver one recorded `241/3` frame the way the PowerOcean receives it."""
    parsed = ocean._parse_message(_PO_TOPIC, _frame(label))
    assert parsed is not None and RELAYED_WALLBOX_SETTINGS_KEY in parsed
    ocean._apply_data(parsed)


async def test_relayed_settings_land_on_the_wallbox_and_nowhere_else(
    hass: HomeAssistant,
) -> None:
    """The baseline frame gives the wallbox all nine settings keys with the
    reporter's values, records them as the settings report, and leaves the
    PowerOcean's store and the wallbox's liveness untouched.

    Mutation probes: dropping the `relayed_settings` branch of
    `_record_settings_report` fails the report assertion; handing the block
    with `own_connection=True` fails the liveness assertion.
    """
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    ocean = oceans[0]
    assert wallbox._last_mqtt_ts == 0.0

    _relay(ocean)

    data = wallbox.device_data
    assert data["ev_settings_switch_bits"] == 2
    assert data["ev_settings_work_mode"] == "solar"
    assert data["ev_solar_min_current_a"] == 6.0
    assert data["ev_phase_setting"] == 2
    assert data["ev_custom_current_a"] == 6.0
    assert data["ev_led_enabled"] is True
    assert data["ev_screen_enabled"] is True
    assert data["ev_led_brightness_pct"] == 75
    assert data["ev_screen_brightness_pct"] == 75
    for key in _SETTINGS_KEYS:
        assert reading_reported(wallbox, key), key

    assert wallbox._settings_report is not None
    assert wallbox._settings_report[1]["ev_settings_switch_bits"] == 2
    assert wallbox._last_mqtt_ts == 0.0
    assert "ev_charger_sn" not in data
    assert "ev_charger_dev_addr" not in data

    for key in (*_SETTINGS_KEYS, RELAYED_WALLBOX_SETTINGS_KEY):
        assert key not in ocean.device_data, key
    assert ocean.relayed_settings_stats == {"handed_over": 1}
    assert wallbox.relayed_settings_stats == {"applied": 1}


async def test_a_display_block_of_another_length_leaves_the_display_keys_out(
    hass: HomeAssistant,
) -> None:
    """The second frame's display block is 32 bytes long: no byte position is
    guessed on it, the other settings still arrive."""
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])

    _relay(oceans[0], "after_test")

    data = wallbox.device_data
    assert data["ev_settings_switch_bits"] == 19
    assert data["ev_solar_min_current_a"] == 6.0
    for key in (
        "ev_led_enabled",
        "ev_screen_enabled",
        "ev_led_brightness_pct",
        "ev_screen_brightness_pct",
    ):
        assert key not in data, key


async def test_own_settings_report_makes_the_relay_drop_for_good(
    hass: HomeAssistant,
) -> None:
    """A wallbox whose own channel delivered its settings keeps that as its
    one source: a relayed copy before it is applied, every one after it is
    dropped, also when the own channel goes quiet.

    Mutation probe: never setting `_own_settings_report_seen` makes the last
    relayed frame overwrite the own report's switch bits (18 -> 19).
    """
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    ocean = oceans[0]

    _relay(ocean, "baseline")
    assert wallbox.device_data["ev_settings_switch_bits"] == 2

    wallbox._apply_data({"ev_settings_switch_bits": 18, "ev_custom_current_a": 7.0})
    assert wallbox._own_settings_report_seen is True

    _relay(ocean, "after_test")
    data = wallbox.device_data
    assert data["ev_settings_switch_bits"] == 18
    assert data["ev_custom_current_a"] == 7.0
    stats = wallbox.relayed_settings_stats
    assert stats is not None
    assert (stats["applied"], stats["dropped_own_seen"]) == (1, 1)
    assert ocean.relayed_settings_stats == {"handed_over": 1}


async def test_an_own_frame_without_settings_does_not_claim_the_source(
    hass: HomeAssistant,
) -> None:
    """The reporter's wallbox sends heartbeats and a descriptor on its own
    channel, never the block: those must not switch the relay off."""
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])

    wallbox._apply_data({"ev_charge_status": "charging", "ev_charger_dev_addr": 215})
    _relay(oceans[0])

    assert wallbox._own_settings_report_seen is False
    assert wallbox.device_data["ev_settings_switch_bits"] == 2


async def test_without_a_unique_sibling_the_block_is_not_routed(
    hass: HomeAssistant,
) -> None:
    """With two PowerOceans in the entry neither is the wallbox's sibling, so
    neither hands the block over (the writes refuse on the same condition)."""
    second = {**POWEROCEAN_DEVICE, "sn": "HJ31TEST00000002"}
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE, second])

    _relay(oceans[0])

    assert "ev_settings_switch_bits" not in wallbox.device_data
    assert oceans[0].relayed_settings_stats == {"unrouted": 1}
    assert wallbox.relayed_settings_stats is None


async def test_a_write_is_built_on_the_relayed_report_and_confirmed_by_it(
    hass: HomeAssistant,
) -> None:
    """Relay only, as on the reporter's installation: a Solar minimum write is
    published through the PowerOcean as `{4: 70}` and confirmed by the next
    relayed block carrying 7 A. This write does not read the settings report;
    the two tests below cover the writes that do.
    """
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set

    _relay(oceans[0])
    task = asyncio.create_task(wallbox.async_set_powerpulse_solar_min_current(7))
    await asyncio.sleep(0.05)
    assert send.call_count == 1
    assert _published_fields(send.call_args) == {4: 70}

    confirming: dict[str, Any] = {
        "ev_settings_switch_bits": 2,
        "ev_solar_min_current_a": 7.0,
    }
    assert wallbox.apply_relayed_wallbox_settings(confirming) is True
    await task
    assert wallbox._pending_write is None
    assert wallbox.device_data["ev_solar_min_current_a"] == 7.0


async def test_a_switch_bit_write_is_built_on_the_relayed_bits(
    hass: HomeAssistant,
) -> None:
    """Continuous Charging ON from the relayed switch bits 2 publishes
    `{1: 18}` and confirms on a relayed block with the bit set; the store is
    made to disagree first, so the bits can only come from the report.

    Mutation probe: recording the settings report only for `own_connection`
    makes the write refuse with `report_missing` and publish nothing.
    """
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set

    _relay(oceans[0], "baseline")
    wallbox.set_device_value("ev_settings_switch_bits", 0)
    task = asyncio.create_task(_set_continuous(wallbox, True))
    await asyncio.sleep(0.05)
    assert send.call_count == 1
    assert _published_fields(send.call_args) == {1: 18}

    assert wallbox.apply_relayed_wallbox_settings({"ev_settings_switch_bits": 18})
    await task
    assert wallbox._pending_write is None


async def test_a_display_write_is_built_on_the_relayed_display_block(
    hass: HomeAssistant,
) -> None:
    """LED brightness 50 from the relayed display block [1, 1, 75, 75] keeps
    the other three bytes: [1, 1, 50, 75, 0, 0]."""
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set

    _relay(oceans[0], "baseline")
    task = asyncio.create_task(
        wallbox.async_set_powerpulse_display("ev_led_brightness_pct", 50)
    )
    await asyncio.sleep(0.05)
    assert send.call_count == 1
    assert _published_display(send.call_args) == [(21, 2, bytes([1, 1, 50, 75, 0, 0]))]
    assert wallbox.apply_relayed_wallbox_settings(
        {
            "ev_settings_switch_bits": 2,
            "ev_led_enabled": True,
            "ev_screen_enabled": True,
            "ev_led_brightness_pct": 50,
            "ev_screen_brightness_pct": 75,
        }
    )
    await task


async def test_a_relayed_report_past_the_max_age_refuses_like_an_own_one(
    hass: HomeAssistant,
) -> None:
    """A relayed report 11 s old is too old for a switch-bit write, the same
    10 s limit an own report has: the write refuses and publishes nothing."""
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _relay(oceans[0], "baseline")
    assert wallbox._settings_report is not None
    stamped = wallbox._settings_report[0]

    with (
        patch(f"{_SET_COMMANDS}.time", SimpleNamespace(monotonic=lambda: stamped + 11)),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await _set_continuous(wallbox, True)
    assert excinfo.value.translation_key == "powerpulse_continuous_report_stale"
    assert send.call_count == 0


async def test_a_relay_only_frame_does_not_update_the_powerocean(
    hass: HomeAssistant,
) -> None:
    """The reporter's frames carry nothing for the PowerOcean itself: they
    must not wake its entities.

    Mutation probe: removing the early return after the hand-over makes the
    PowerOcean publish an update for each frame.
    """
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    ocean = oceans[0]
    with patch.object(ocean, "async_set_updated_data") as updated:
        _relay(ocean)
    assert updated.call_count == 0
    assert wallbox.device_data["ev_settings_switch_bits"] == 2


async def test_the_gap_between_relayed_blocks_is_counted(
    hass: HomeAssistant,
) -> None:
    """The longest gap between two relayed blocks is what the 10 s write limit
    depends on, so the diagnostics carry it."""
    _entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    clock = "custom_components.ecoflow_energy.coordinator.core.time.monotonic"
    with patch(clock, return_value=1000.0):
        _relay(oceans[0])
    with patch(clock, return_value=1007.3):
        _relay(oceans[0], "after_test")
    with patch(clock, return_value=1010.0):
        _relay(oceans[0])
    stats = wallbox.relayed_settings_stats
    assert stats is not None
    assert stats["max_gap_s"] == 7.3
    assert stats["applied"] == 3
