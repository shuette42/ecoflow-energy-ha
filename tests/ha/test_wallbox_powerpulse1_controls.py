"""PowerPulse 1 settings on a PowerOcean coordinator (#731).

Reports and writes are the reporter's real frames
(`tests/fixtures/powerpulse/r372_settings_731_20261113.json`). The PowerOcean
parses the wallbox's 209/8 report into four settings and its serial; a write
returns only once the next report carries the new value.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ecoflow_energy.const import DOMAIN
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)
from custom_components.ecoflow_energy.select import async_setup_entry as select_setup
from custom_components.ecoflow_energy.switch import async_setup_entry as switch_setup
from tests.ha.conftest import add_entities_collector
from tests.ha.test_powerpulse2_charge_action import (
    POWEROCEAN_DEVICE,
    _connected_mqtt,
    _entry,
    _mqtt,
)
from tests.ha.test_powerpulse_edev_ingest import POWERPULSE2_DEVICE

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "powerpulse"
    / "r372_settings_731_20261113.json"
)
FRAMES = {f["label"]: f for f in json.loads(FIXTURE.read_text())["frames"]}
WINDOW = (
    "custom_components.ecoflow_energy.coordinator.set_commands.POWERGLOW_WRITE_WINDOW_S"
)
SETTINGS = (
    "ev_plug_and_play",
    "ev_charge_mode_raw",
    "ev_battery_discharge_block",
    "ev_continuous_charging",
)
START = {
    "ev_charger_sn": "X" * 16,
    "ev_plug_and_play": False,
    "ev_charge_mode_raw": 2,
    "ev_battery_discharge_block": True,
    "ev_continuous_charging": False,
}


def _parse(ocean: EcoFlowDeviceCoordinator, label: str) -> dict[str, Any]:
    topic = f"/app/user123/{ocean.device_sn}/thing/property"
    parsed = ocean._parse_message(topic, bytes.fromhex(FRAMES[label]["hex"]))
    assert parsed is not None
    return parsed


def _ocean(hass: HomeAssistant, *extra: dict[str, Any]) -> EcoFlowDeviceCoordinator:
    entry = _entry(hass, [POWEROCEAN_DEVICE, *extra])
    ocean = EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN_DEVICE)
    ocean._mqtt_client = _connected_mqtt()
    coordinators = {POWEROCEAN_DEVICE["sn"]: ocean}
    for device in extra:
        coordinators[device["sn"]] = EcoFlowDeviceCoordinator(hass, entry, device)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinators
    return ocean


async def test_parser_reads_the_start_state_with_types(hass: HomeAssistant) -> None:
    """Mutation probe: `bool(value & 1)` -> `value` for the switch bits leaves
    an int where a bool is expected and the `type(...) is bool` check fails."""
    parsed = _parse(_ocean(hass), "get_all_reply_start")
    assert {k: parsed.get(k) for k in START} == START
    assert type(parsed["ev_charge_mode_raw"]) is int
    for key in ("ev_plug_and_play", "ev_battery_discharge_block"):
        assert type(parsed[key]) is bool
    assert type(parsed["ev_continuous_charging"]) is bool


@pytest.mark.parametrize(
    ("label", "moved"),
    [
        ("report_after_set_f5_1", {"ev_charge_mode_raw": 1}),
        ("report_after_set_f4_1", {"ev_plug_and_play": True}),
        ("report_after_set_f7_1", {"ev_continuous_charging": True}),
        ("report_after_set_f6_0", {"ev_battery_discharge_block": False}),
    ],
)
async def test_each_report_moves_exactly_its_setting(
    hass: HomeAssistant, label: str, moved: dict[str, Any]
) -> None:
    parsed = _parse(_ocean(hass), label)
    expected = {**START, **moved}
    assert {k: parsed[k] for k in SETTINGS} == {k: expected[k] for k in SETTINGS}


@pytest.mark.parametrize(
    ("entity_key", "value", "write", "report"),
    [
        ("ev_plug_and_play", True, "set_f4_1", "report_after_set_f4_1"),
        ("ev_charge_mode", "fast", "set_f5_1", "report_after_set_f5_1"),
        ("ev_continuous_charging", True, "set_f7_1", "report_after_set_f7_1"),
        ("ev_battery_discharge_block", False, "set_f6_0", "report_after_set_f6_0"),
    ],
)
async def test_write_sends_the_app_pdata_and_returns_on_the_report(
    hass: HomeAssistant, entity_key: str, value: Any, write: str, report: str
) -> None:
    ocean = _ocean(hass)
    ocean._apply_data(_parse(ocean, "get_all_reply_start"))

    task = asyncio.create_task(ocean.async_set_wallbox_setting(entity_key, value))
    await asyncio.sleep(0.05)
    sent = _mqtt(ocean).send_proto_set.call_args.args[0]
    (header,), _ = decode_header_message(sent)
    (app,), _ = decode_header_message(bytes.fromhex(FRAMES[write]["hex"]))
    assert header["pdata"] == app["pdata"]
    assert not task.done()

    ocean._apply_data(_parse(ocean, report))
    await task
    assert ocean._pending_write is None


async def test_write_without_report_is_not_confirmed(hass: HomeAssistant) -> None:
    """The ack proves nothing: without a 209/8 the write fails."""
    ocean = _ocean(hass)
    ocean._apply_data(_parse(ocean, "get_all_reply_start"))
    with (
        patch(WINDOW, 0.05),
        pytest.raises(HomeAssistantError) as err,
    ):
        await ocean.async_set_wallbox_setting("ev_plug_and_play", True)
    assert err.value.translation_key == "wallbox_not_confirmed"
    assert ocean._pending_write is None


async def test_write_without_a_reported_wallbox_is_refused(
    hass: HomeAssistant,
) -> None:
    ocean = _ocean(hass)
    with pytest.raises(HomeAssistantError) as err:
        await ocean.async_set_wallbox_setting("ev_plug_and_play", True)
    assert err.value.translation_key == "wallbox_not_reported"
    assert _mqtt(ocean).send_proto_set.call_count == 0


async def _wallbox_entities(
    hass: HomeAssistant, ocean: EcoFlowDeviceCoordinator
) -> set[str]:
    entry = hass.config_entries.async_entries(DOMAIN)[-1]
    found: list[Any] = []
    await switch_setup(hass, entry, add_entities_collector(found))
    await select_setup(hass, entry, add_entities_collector(found))
    ocean._apply_data(_parse(ocean, "get_all_reply_start"))
    ocean.async_set_updated_data(dict(ocean.device_data))
    await hass.async_block_till_done()
    return {
        e._definition.key
        for e in found
        if e.coordinator is ocean and e._definition.key.startswith("ev_")
    }


async def test_controls_appear_only_after_a_report(hass: HomeAssistant) -> None:
    ocean = _ocean(hass)
    assert await _wallbox_entities(hass, ocean) == {
        "ev_plug_and_play",
        "ev_charge_mode",
        "ev_continuous_charging",
        "ev_battery_discharge_block",
    }


async def test_no_controls_on_an_entry_with_a_powerpulse2(
    hass: HomeAssistant,
) -> None:
    """A PowerOcean coupled to a PowerPulse 2 reports the same 209/8 settings
    (#553 recording); the PowerPulse 2's own controls cover them.

    Mutation probe: returning False from `wallbox_control_left_to_powerpulse2`
    creates the four controls here and fails the test.
    """
    ocean = _ocean(hass, POWERPULSE2_DEVICE)
    assert await _wallbox_entities(hass, ocean) == set()
