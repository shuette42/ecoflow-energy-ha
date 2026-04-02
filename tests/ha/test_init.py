"""Tests for EcoFlow Energy integration setup and unload."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    AUTH_METHOD_DEVELOPER,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SECRET_KEY,
    CONF_USER_ID,
    DOMAIN,
    MODE_ENHANCED,
    MODE_STANDARD,
)

from .conftest import MOCK_MQTT_CREDENTIALS, MOCK_POWEROCEAN_DEVICE


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
        mock_mqtt_client,
    ) -> None:
        """Enhanced mode setup creates coordinator with WSS mode.

        The enhanced_config_entry has email+password, so the auto-upgrade
        to app-auth triggers. We mock the app-auth login path.
        """
        enhanced_config_entry.add_to_hass(hass)

        mock_app_api = MagicMock()
        mock_app_api.login = AsyncMock(return_value=True)
        mock_app_api.user_id = "uid"
        mock_app_api.get_mqtt_credentials = AsyncMock(return_value={
            "userName": "app-user",
            "password": "app-pass",
        })

        with (
            patch(
                "custom_components.ecoflow_energy.ecoflow.app_api.AppApiClient",
                return_value=mock_app_api,
            ),
            patch(
                "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator.async_config_entry_first_refresh",
                new_callable=AsyncMock,
            ),
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


# ===========================================================================
# Config entry migration
# ===========================================================================


class TestMigration:
    async def test_v1_to_v3_adds_auth_method(self, hass: HomeAssistant) -> None:
        """Migration from v1 to v3 adds auth_method=developer."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_ACCESS_KEY: "old_ak",
                CONF_SECRET_KEY: "old_sk",
                CONF_MODE: MODE_STANDARD,
                CONF_DEVICES: [],
            },
            unique_id="old_ak",
            version=1,
        )
        entry.add_to_hass(hass)

        from custom_components.ecoflow_energy import async_migrate_entry

        result = await async_migrate_entry(hass, entry)
        assert result is True
        assert entry.data[CONF_AUTH_METHOD] == AUTH_METHOD_DEVELOPER
        assert entry.version == 3

    async def test_v2_to_v3_adds_auth_method(self, hass: HomeAssistant) -> None:
        """Migration from v2 to v3 adds auth_method if missing."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_ACCESS_KEY: "ak",
                CONF_SECRET_KEY: "sk",
                CONF_MODE: MODE_STANDARD,
                CONF_DEVICES: [],
            },
            unique_id="ak",
            version=2,
        )
        entry.add_to_hass(hass)

        from custom_components.ecoflow_energy import async_migrate_entry

        result = await async_migrate_entry(hass, entry)
        assert result is True
        assert entry.data[CONF_AUTH_METHOD] == AUTH_METHOD_DEVELOPER
        assert entry.version == 3

    async def test_v3_no_migration(self, hass: HomeAssistant) -> None:
        """V3 entries are not migrated."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_DEVELOPER,
                CONF_ACCESS_KEY: "ak",
                CONF_SECRET_KEY: "sk",
                CONF_MODE: MODE_STANDARD,
                CONF_DEVICES: [],
            },
            unique_id="ak",
            version=3,
        )
        entry.add_to_hass(hass)

        from custom_components.ecoflow_energy import async_migrate_entry

        result = await async_migrate_entry(hass, entry)
        assert result is True
        assert entry.data[CONF_AUTH_METHOD] == AUTH_METHOD_DEVELOPER


# ===========================================================================
# App-auth setup
# ===========================================================================


class TestAppAuthSetup:
    async def test_app_auth_setup_creates_coordinators(
        self,
        hass: HomeAssistant,
        app_auth_config_entry: MockConfigEntry,
        mock_mqtt_client,
    ) -> None:
        """App-auth setup creates coordinators without IoT API."""
        app_auth_config_entry.add_to_hass(hass)

        mock_app_api = MagicMock()
        mock_app_api.login = AsyncMock(return_value=True)
        mock_app_api.user_id = "uid"
        mock_app_api.get_mqtt_credentials = AsyncMock(return_value={
            "userName": "app-user",
            "password": "app-pass",
        })

        with (
            patch(
                "custom_components.ecoflow_energy.ecoflow.app_api.AppApiClient",
                return_value=mock_app_api,
            ),
            patch(
                "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator.async_config_entry_first_refresh",
                new_callable=AsyncMock,
            ),
        ):
            result = await hass.config_entries.async_setup(
                app_auth_config_entry.entry_id
            )
            await hass.async_block_till_done()

        assert result is True
        assert app_auth_config_entry.entry_id in hass.data[DOMAIN]
        coordinators = hass.data[DOMAIN][app_auth_config_entry.entry_id]
        assert len(coordinators) == 1

    async def test_enhanced_entry_auto_upgrades_to_app_auth(
        self,
        hass: HomeAssistant,
        mock_mqtt_client,
    ) -> None:
        """Enhanced Mode entry with email+pw auto-upgrades to auth_type=app."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_DEVELOPER,
                CONF_ACCESS_KEY: "ak",
                CONF_SECRET_KEY: "sk",
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "pass",
                CONF_USER_ID: "uid",
                CONF_DEVICES: [MOCK_POWEROCEAN_DEVICE],
            },
            unique_id="ak",
        )
        entry.add_to_hass(hass)

        mock_app_api = MagicMock()
        mock_app_api.login = AsyncMock(return_value=True)
        mock_app_api.user_id = "uid"
        mock_app_api.get_mqtt_credentials = AsyncMock(return_value={
            "userName": "app-user",
            "password": "app-pass",
        })

        with (
            patch(
                "custom_components.ecoflow_energy.ecoflow.app_api.AppApiClient",
                return_value=mock_app_api,
            ),
            patch(
                "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator.async_config_entry_first_refresh",
                new_callable=AsyncMock,
            ),
        ):
            result = await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        assert result is True
        # Entry should have been auto-upgraded to app-auth
        assert entry.data[CONF_AUTH_METHOD] == AUTH_METHOD_APP

    async def test_standard_entry_not_auto_upgraded(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """Standard Mode entries are NOT auto-upgraded to app-auth."""
        standard_config_entry.add_to_hass(hass)

        with patch(
            "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator.async_config_entry_first_refresh",
            new_callable=AsyncMock,
        ):
            await hass.config_entries.async_setup(standard_config_entry.entry_id)
            await hass.async_block_till_done()

        # Standard mode should stay as-is (no auth_type set means developer)
        assert standard_config_entry.data.get(CONF_AUTH_METHOD) != AUTH_METHOD_APP
