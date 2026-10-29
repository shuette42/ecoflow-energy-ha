"""The Stream Backup Reserve number exists only behind the write gate (#561).

It used to stay on every Stream outside the gate so existing registrations were
not orphaned. On a Stream Ultra X (`BK61`) linked to a STREAM 5000 it wrote a
bare level to the Ultra X itself, never confirmed on that model. These tests
render the platform setup and run the registry cleanup, not the helpers.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy import _async_remove_retired_platform_entities
from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_STREAM,
    DEVICE_TYPE_STREAM_AC5000,
    DOMAIN,
    MODE_ENHANCED,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.number import async_setup_entry as number_setup
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

OUTSIDE_GATE = ("BK11", "BK41", "BK51", "BK61")


def _device(prefix: str, device_type: str = DEVICE_TYPE_STREAM) -> dict[str, Any]:
    return {
        "sn": f"{prefix}TEST00000001",
        "name": "",
        "product_name": "",
        "device_type": device_type,
        "online": 1,
    }


def _entry(devices: list[dict[str, Any]]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: devices,
        },
        unique_id="test@example.com",
    )


async def _keys(
    hass: HomeAssistant, device: dict[str, Any], platform_setup
) -> set[str]:
    """Run one platform's setup and return the keys of the entities it created."""
    entry = _entry([device])
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {device["sn"]: coordinator}
    created: list[Any] = []
    await platform_setup(hass, entry, created.extend)
    return {e._definition.key for e in created if hasattr(e, "_definition")}


@pytest.mark.parametrize("prefix", OUTSIDE_GATE)
async def test_no_reserve_number_outside_the_gate(
    hass: HomeAssistant, prefix: str
) -> None:
    assert await _keys(hass, _device(prefix), number_setup) == set()


async def test_bk31_keeps_its_reserve_number(hass: HomeAssistant) -> None:
    keys = await _keys(hass, _device("BK31"), number_setup)

    assert "backup_reserve" in keys


async def test_reserve_value_stays_visible_on_a_bk61(hass: HomeAssistant) -> None:
    keys = await _keys(hass, _device("BK61"), sensor_setup)

    assert "backup_reserve_pct" in keys


async def test_stream_ac5000_numbers_are_untouched(hass: HomeAssistant) -> None:
    keys = await _keys(hass, _device("ES21", DEVICE_TYPE_STREAM_AC5000), number_setup)

    assert {"max_charge_soc_pct", "min_discharge_soc_pct"} <= keys


def _register(
    hass: HomeAssistant, entry: MockConfigEntry, domain: str, unique_id: str
) -> str:
    return (
        er.async_get(hass)
        .async_get_or_create(domain, DOMAIN, unique_id, config_entry=entry)
        .entity_id
    )


def test_cleanup_removes_the_old_reserve_number_outside_the_gate(
    hass: HomeAssistant,
) -> None:
    entry = _entry([])
    entry.add_to_hass(hass)
    old = {
        prefix: _register(hass, entry, "number", f"{prefix}TEST00000001_backup_reserve")
        for prefix in OUTSIDE_GATE
    }
    keep = {
        # The gated model keeps its number.
        "bk31": _register(hass, entry, "number", "BK31TEST00000001_backup_reserve"),
        # The value stays: the sensor of the same serial is not touched.
        "sensor": _register(
            hass, entry, "sensor", "BK61TEST00000001_backup_reserve_pct"
        ),
        # Other numbers of an ungated Stream are not part of this retirement.
        "led": _register(hass, entry, "number", "BK61TEST00000001_led_brightness"),
        # The STREAM 5000 carries a reserve of its own.
        "es21": _register(hass, entry, "number", "ES21TEST00000001_backup_reserve"),
    }

    _async_remove_retired_platform_entities(hass, entry)

    remaining = set(er.async_get(hass).entities)
    assert not (set(old.values()) & remaining)
    assert set(keep.values()) <= remaining
