"""The PowerPulse 2 wallbox start/stop button platform (ADR-009, PLAN-136).

Phase 3b only: the coordinator action itself (`async_set_powerpulse_charge_action`)
is covered by `test_powerpulse2_charge_action.py`. Every test here goes through
`button.async_setup_entry` and the resulting `EcoFlowButton` entities, never the
coordinator method directly - that is what distinguishes this file from that one.
"""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.button import async_setup_entry as button_setup
from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SECRET_KEY,
    CONF_USER_ID,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_POWERPULSE2,
    DOMAIN,
    MODE_ENHANCED,
    MODE_STANDARD,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import add_entities_collector

POWEROCEAN_SN = "HJ31TEST00000001"
POWEROCEAN2_SN = "HJ31TEST00000002"
POWERPULSE2_SN = "C376TEST00000007"
DESCRIPTOR_SN = "X" * 16

POWEROCEAN_DEVICE: dict[str, Any] = {
    "sn": POWEROCEAN_SN,
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}

POWEROCEAN2_DEVICE: dict[str, Any] = {
    "sn": POWEROCEAN2_SN,
    "name": "PowerOcean 2",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}

POWERPULSE2_DEVICE: dict[str, Any] = {
    "sn": POWERPULSE2_SN,
    "name": "PowerPulse 2",
    "product_name": "",
    "device_type": DEVICE_TYPE_POWERPULSE2,
    "online": 1,
}


def _connected_mqtt() -> MagicMock:
    mock_mqtt = MagicMock()
    mock_mqtt.is_connected.return_value = True
    return mock_mqtt


def _mqtt(coordinator: EcoFlowDeviceCoordinator) -> MagicMock:
    """The MagicMock installed on a coordinator's MQTT client, typed as what it is."""
    return cast(MagicMock, coordinator.mqtt_client)


def _enhanced_entry(
    hass: HomeAssistant, devices: list[dict[str, Any]]
) -> MockConfigEntry:
    entry = MockConfigEntry(
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
    entry.add_to_hass(hass)
    return entry


def _standard_entry(
    hass: HomeAssistant, devices: list[dict[str, Any]]
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_ACCESS_KEY: "test_ak",
            CONF_SECRET_KEY: "test_sk",
            CONF_MODE: MODE_STANDARD,
            CONF_DEVICES: devices,
        },
        unique_id="test_ak",
    )
    entry.add_to_hass(hass)
    return entry


def _wire_entry(
    hass: HomeAssistant,
    ocean_devices: list[dict[str, Any]],
    *,
    standard_mode: bool = False,
) -> tuple[MockConfigEntry, list[EcoFlowDeviceCoordinator], EcoFlowDeviceCoordinator]:
    """Build one entry with N PowerOcean devices plus one wallbox.

    Returns the entry, the list of PowerOcean coordinators (each with its own
    connected MQTT mock), and the wallbox coordinator. Mirrors
    `test_powerpulse2_charge_action.py::_wire_entry` (kept separate rather than
    imported, per the mission brief).
    """
    devices = [*ocean_devices, POWERPULSE2_DEVICE]
    entry = (
        _standard_entry(hass, devices)
        if standard_mode
        else _enhanced_entry(hass, devices)
    )
    oceans = []
    for device in ocean_devices:
        coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
        coordinator._mqtt_client = _connected_mqtt()
        oceans.append(coordinator)
    wallbox = EcoFlowDeviceCoordinator(hass, entry, POWERPULSE2_DEVICE)
    wallbox._mqtt_client = _connected_mqtt()
    coordinators = {c.device_sn: c for c in [*oceans, wallbox]}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinators
    return entry, oceans, wallbox


def _report_descriptor(wallbox: EcoFlowDeviceCoordinator) -> None:
    wallbox.set_device_value("ev_charger_sn", DESCRIPTOR_SN)
    wallbox.set_device_value("ev_charger_dev_addr", 215)


class TestButtonCreation:
    async def test_two_buttons_created_with_one_sibling_and_descriptor(
        self, hass: HomeAssistant
    ) -> None:
        entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))

        assert len(entities) == 2
        unique_ids = {e.unique_id for e in entities}
        assert unique_ids == {
            f"{POWERPULSE2_SN}_ev_start_charging",
            f"{POWERPULSE2_SN}_ev_stop_charging",
        }
        for e in entities:
            assert e.device_info == wallbox.device_info

    async def test_zero_buttons_without_a_powerocean_sibling(
        self, hass: HomeAssistant
    ) -> None:
        entry, _oceans, wallbox = _wire_entry(hass, [])
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))

        assert entities == []

    async def test_zero_buttons_with_two_powerocean_siblings(
        self, hass: HomeAssistant
    ) -> None:
        entry, _oceans, wallbox = _wire_entry(
            hass, [POWEROCEAN_DEVICE, POWEROCEAN2_DEVICE]
        )
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))

        assert entities == []

    async def test_zero_buttons_in_standard_mode(self, hass: HomeAssistant) -> None:
        """Standard Mode has no write path for the wallbox (ADR-009)."""
        entry, _oceans, wallbox = _wire_entry(
            hass, [POWEROCEAN_DEVICE], standard_mode=True
        )
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))

        assert entities == []

    async def test_buttons_appear_when_the_descriptor_arrives_later(
        self, hass: HomeAssistant
    ) -> None:
        entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))
        assert entities == []

        _report_descriptor(wallbox)
        wallbox.async_update_listeners()

        assert len(entities) == 2


class TestButtonAvailability:
    async def test_available_follows_sibling_connection_not_charge_state(
        self, hass: HomeAssistant
    ) -> None:
        entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))
        button_entity = entities[0]

        for status in ("available", "charging", "finishing"):
            wallbox.set_device_value("ev_charge_status", status)
            assert button_entity.available is True

        _mqtt(oceans[0]).is_connected.return_value = False
        assert button_entity.available is False

        _mqtt(oceans[0]).is_connected.return_value = True
        assert button_entity.available is True


class TestButtonPress:
    async def test_press_calls_the_coordinator_action_with_start_or_stop(
        self, hass: HomeAssistant
    ) -> None:
        entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))
        start_button = next(
            e for e in entities if e.unique_id.endswith("ev_start_charging")
        )
        stop_button = next(
            e for e in entities if e.unique_id.endswith("ev_stop_charging")
        )

        mock_action = AsyncMock()
        with patch.object(wallbox, "async_set_powerpulse_charge_action", mock_action):
            await start_button.async_press()
            await stop_button.async_press()

        assert [call.args for call in mock_action.await_args_list] == [
            ("start",),
            ("stop",),
        ]

    async def test_press_error_propagates(self, hass: HomeAssistant) -> None:
        entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))
        start_button = entities[0]

        mock_action = AsyncMock(
            side_effect=HomeAssistantError("The wallbox reports charging.")
        )
        with (
            patch.object(wallbox, "async_set_powerpulse_charge_action", mock_action),
            pytest.raises(HomeAssistantError),
        ):
            await start_button.async_press()


class TestDiagnosticsDoesNotLeakTheDescriptor:
    async def test_ev_charger_serial_value_never_appears_in_diagnostics(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """The descriptor value must never leak, even if a section is added later.

        `_device_diagnostics` only ever dumps `device_data.keys()`, never
        values (see `diagnostics.py`), so this is a regression guard rather
        than evidence of a fix.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, POWERPULSE2_DEVICE
        )
        secret_sn = "C376" + "A" * 12
        coordinator.set_device_value("ev_charger_sn", secret_sn)
        hass.data.setdefault(DOMAIN, {})[standard_config_entry.entry_id] = {
            coordinator.device_sn: coordinator
        }

        result = await async_get_config_entry_diagnostics(hass, standard_config_entry)

        assert secret_sn not in json.dumps(result)
