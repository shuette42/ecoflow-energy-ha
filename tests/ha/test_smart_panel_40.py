"""OCEAN Smart Electrical Panel 40 (`HR61`): frames in, Home Assistant states out.

The parser is covered on its own in test_hr61_proto.py. These tests run the
owner's recorded frames through the coordinator's own message path - the
dispatch in `_parse_message`, the merge in `_apply_data` - and read the
values the sensor entities render, so a missing dispatch branch or a merge
that loses keys fails here even while the parser tests stay green.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_SMART_PANEL_40,
    DOMAIN,
    ENHANCED_ONLY_DEVICE_TYPES,
    MODE_ENHANCED,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

from .conftest import add_entities_collector

FIXTURE = Path(__file__).parent.parent / "fixtures" / "hr61" / "hr61_frames.json"

PANEL_DEVICE: dict[str, Any] = {
    "sn": "HR61TEST00000001",
    "name": "Smart Panel 40",
    "product_name": "",
    "device_type": DEVICE_TYPE_SMART_PANEL_40,
    "online": 1,
}


def _frames() -> dict[int, dict[str, Any]]:
    data = json.loads(FIXTURE.read_text())
    frames = data if isinstance(data, list) else data["frames"]
    return {frame["i"]: frame for frame in frames}


def _topic(frame: dict[str, Any]) -> str:
    sn = PANEL_DEVICE["sn"]
    if frame["topic"] == "get_reply":
        return f"/app/user123/{sn}/thing/property/get_reply"
    return f"/app/device/property/{sn}"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [PANEL_DEVICE],
        },
        unique_id="test@example.com",
    )


def _feed(coordinator: EcoFlowDeviceCoordinator, index: int) -> dict[str, Any]:
    frame = _frames()[index]
    parsed = coordinator._parse_message(_topic(frame), bytes.fromhex(frame["hex"]))
    assert parsed is not None, f"frame i={index} did not reach the HR61 parser"
    coordinator._apply_data(parsed)
    return parsed


async def _rendered(
    hass: HomeAssistant, entry: MockConfigEntry, coordinator: EcoFlowDeviceCoordinator
) -> dict[str, Any]:
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {PANEL_DEVICE["sn"]: coordinator}
    created: list[Any] = []
    await sensor_setup(hass, entry, add_entities_collector(created))
    return {
        entity._definition.key: entity.native_value
        for entity in created
        if hasattr(entity, "_definition")
    }


def test_the_panel_is_enhanced_only() -> None:
    assert DEVICE_TYPE_SMART_PANEL_40 in ENHANCED_ONLY_DEVICE_TYPES


async def test_a_full_state_frame_fills_the_panel_sensors(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, PANEL_DEVICE)
    _feed(coordinator, 16)

    values = await _rendered(hass, entry, coordinator)

    # i=16: grid 0.0 + solar 2.0 - battery -7644.78 = home 7646.78, shown
    # as whole watts.
    assert values["grid_power_w"] == 0
    assert values["pv_power_w"] == 2
    assert values["battery_power_w"] == -7645
    assert values["load_power_w"] == 7647
    assert values["grid_nominal_voltage_v"] == 120
    assert values["grid_nominal_frequency_hz"] == 60


async def test_an_incremental_push_keeps_what_it_does_not_carry(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, PANEL_DEVICE)
    full = _feed(coordinator, 18)
    push = _feed(coordinator, 22)

    kept = sorted(key for key in full if key not in push)
    assert kept, "the push carries every key of the full state; pick another pair"
    for key in kept:
        assert coordinator._device_data[key] == full[key], key
    for key, value in push.items():
        assert coordinator._device_data[key] == value, key
