"""Tests for EcoFlow Energy integration setup and unload."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import DOMAIN

from .conftest import MOCK_MQTT_CREDENTIALS


# ===========================================================================
# async_setup_entry
# ===========================================================================


class TestSetupEntry:
    async def test_setup_creates_coordinators(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """Setup creates one coordinator per device."""
        standard_config_entry.add_to_hass(hass)

        with patch(
            "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator.async_config_entry_first_refresh",
            new_callable=AsyncMock,
        ):
            result = await hass.config_entries.async_setup(standard_config_entry.entry_id)
            await hass.async_block_till_done()

        assert result is True
        assert DOMAIN in hass.data
        assert standard_config_entry.entry_id in hass.data[DOMAIN]
        coordinators = hass.data[DOMAIN][standard_config_entry.entry_id]
        assert len(coordinators) == 1
        assert "DAEBK5ZZ12340001" in coordinators

    async def test_setup_enhanced_mode(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
        mock_enhanced_auth,
    ) -> None:
        """Enhanced mode setup creates coordinator with WSS mode."""
        enhanced_config_entry.add_to_hass(hass)

        with patch(
            "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator.async_config_entry_first_refresh",
            new_callable=AsyncMock,
        ):
            result = await hass.config_entries.async_setup(enhanced_config_entry.entry_id)
            await hass.async_block_till_done()

        assert result is True
        coordinators = hass.data[DOMAIN][enhanced_config_entry.entry_id]
        coordinator = list(coordinators.values())[0]
        assert coordinator.enhanced_mode is True


# ===========================================================================
# async_unload_entry
# ===========================================================================


class TestUnloadEntry:
    async def test_unload_cleans_up(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """Unload shuts down coordinators and removes from hass.data."""
        standard_config_entry.add_to_hass(hass)

        with patch(
            "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator.async_config_entry_first_refresh",
            new_callable=AsyncMock,
        ):
            await hass.config_entries.async_setup(standard_config_entry.entry_id)
            await hass.async_block_till_done()

        assert standard_config_entry.entry_id in hass.data[DOMAIN]

        result = await hass.config_entries.async_unload(standard_config_entry.entry_id)
        await hass.async_block_till_done()

        assert result is True
        # After unload, entry_id should be removed from hass.data[DOMAIN]
        assert standard_config_entry.entry_id not in hass.data.get(DOMAIN, {})
