"""Deleting a device an entry no longer serves.

A PowerOcean deselected in a cloud entry's options (to run it as a Local entry
instead) leaves its device in the registry. Home Assistant offers the delete
button only when the integration's ``async_remove_config_entry_device`` agrees.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import support_remove_from_device
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy import async_remove_config_entry_device
from custom_components.ecoflow_energy.const import DOMAIN

from .test_local_modbus_entities import SERIAL, _poll_frame, _setup_local
from .test_powerglow_gating import POWEROCEAN_DEVICE, _entry

DELTA_DEVICE: dict[str, Any] = {
    "sn": "R351TEST00000001",
    "name": "Delta 2 Max",
    "product_name": "DELTA 2 Max",
    "device_type": "delta",
    "online": 1,
}


def _device(
    hass: HomeAssistant, entry: MockConfigEntry, identifiers: set[tuple[str, str]]
) -> dr.DeviceEntry:
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers=identifiers
    )


async def test_a_deselected_device_may_be_deleted(hass: HomeAssistant) -> None:
    """The PowerOcean left the entry's selection: deleting it is allowed."""
    entry = _entry([DELTA_DEVICE])
    entry.add_to_hass(hass)
    device = _device(hass, entry, {(DOMAIN, POWEROCEAN_DEVICE["sn"])})

    assert await async_remove_config_entry_device(hass, entry, device) is True


async def test_a_selected_device_may_not_be_deleted(hass: HomeAssistant) -> None:
    """Negative control: a device still in the selection stays."""
    entry = _entry([DELTA_DEVICE, POWEROCEAN_DEVICE])
    entry.add_to_hass(hass)
    device = _device(hass, entry, {(DOMAIN, POWEROCEAN_DEVICE["sn"])})

    assert await async_remove_config_entry_device(hass, entry, device) is False


async def test_a_device_a_running_coordinator_serves_may_not_be_deleted(
    hass: HomeAssistant,
) -> None:
    """A Local entry's own PowerOcean is served, even though it is its only device."""
    entry, _stub, _coordinator = await _setup_local(hass, _poll_frame(status=0x1014))
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, SERIAL)})
    assert device is not None

    assert await async_remove_config_entry_device(hass, entry, device) is False


async def test_a_deselected_device_still_running_may_not_be_deleted(
    hass: HomeAssistant,
) -> None:
    """Between saving the options and the reload, the old coordinator still runs."""
    entry = _entry([DELTA_DEVICE])
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        POWEROCEAN_DEVICE["sn"]: object()
    }
    device = _device(hass, entry, {(DOMAIN, POWEROCEAN_DEVICE["sn"])})

    assert await async_remove_config_entry_device(hass, entry, device) is False


@pytest.mark.parametrize(
    "identifiers", [set(), {("other_domain", POWEROCEAN_DEVICE["sn"])}]
)
async def test_a_device_without_our_identifier_may_not_be_deleted(
    hass: HomeAssistant, identifiers: set[tuple[str, str]]
) -> None:
    entry = _entry([DELTA_DEVICE])
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers=identifiers,
        connections={("mac", "00:11:22:33:44:55")},
    )

    assert await async_remove_config_entry_device(hass, entry, device) is False


async def test_the_delete_button_removes_a_deselected_device(
    hass: HomeAssistant, hass_ws_client: Any
) -> None:
    """End to end: the websocket call behind the UI's delete button."""
    assert await async_setup_component(hass, "config", {})
    entry = _entry([DELTA_DEVICE])
    entry.add_to_hass(hass)
    # Home Assistant sets this flag when it sets the entry up, from whether the
    # integration module defines the hook; the UI hides the button without it.
    entry.supports_remove_device = await support_remove_from_device(hass, DOMAIN)
    assert entry.supports_remove_device is True
    stale = _device(hass, entry, {(DOMAIN, POWEROCEAN_DEVICE["sn"])})
    kept = _device(hass, entry, {(DOMAIN, DELTA_DEVICE["sn"])})
    client = await hass_ws_client(hass)

    for device_id, expect in ((stale.id, True), (kept.id, False)):
        await client.send_json_auto_id(
            {
                "type": "config/device_registry/remove_config_entry",
                "config_entry_id": entry.entry_id,
                "device_id": device_id,
            }
        )
        response = await client.receive_json()
        assert response["success"] is expect, response

    registry = dr.async_get(hass)
    assert registry.async_get(stale.id) is None
    assert registry.async_get(kept.id) is not None
