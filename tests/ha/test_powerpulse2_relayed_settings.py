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
from typing import Any

from homeassistant.core import HomeAssistant

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
from tests.ha.test_powerpulse2_settings_writes import _published_fields

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
    assert wallbox.relayed_settings_stats == {"applied": 1, "dropped_own_seen": 1}
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
    accepted, published through the PowerOcean as `{4: 70}`, and confirmed by
    the next relayed block carrying 7 A.

    Mutation probe: recording the settings report only for `own_connection`
    makes the write refuse with `report_missing` before it publishes.
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
