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
    DATA_SKIPPED_DEVICES,
    DEVICE_TYPE_DELTA3,
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


class TestPartialSetupCleanup:
    async def test_failing_second_device_shuts_down_first_coordinator(
        self,
        hass: HomeAssistant,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """A failed first refresh must shut down already-created coordinators.

        Without this guarantee, each ConfigEntryNotReady retry would leak a
        live MQTT client per previously set-up device. HA core provides it:
        the coordinator registers async_shutdown as an entry on_unload
        callback, and those callbacks run when setup fails. This test pins
        that behavior so an HA change or a coordinator refactor that breaks
        it is caught here.
        """
        from homeassistant.exceptions import ConfigEntryNotReady

        second_device = {
            "sn": "DAEBK5ZZ12340002",
            "name": "Delta 2 Max B",
            "product_name": "Delta 2 Max",
            "device_type": "delta",
            "online": 1,
        }
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_ACCESS_KEY: "test_access_key",
                CONF_SECRET_KEY: "test_secret_key",
                CONF_MODE: MODE_STANDARD,
                CONF_DEVICES: [
                    {
                        "sn": "DAEBK5ZZ12340001",
                        "name": "Delta 2 Max A",
                        "product_name": "Delta 2 Max",
                        "device_type": "delta",
                        "online": 1,
                    },
                    second_device,
                ],
            },
            unique_id="test_access_key",
        )
        entry.add_to_hass(hass)

        refresh_calls = 0

        async def _refresh(self) -> None:
            nonlocal refresh_calls
            refresh_calls += 1
            if refresh_calls == 2:
                raise ConfigEntryNotReady("second device refresh failed")

        shutdown_mock = AsyncMock()

        with (
            patch(
                "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator.async_config_entry_first_refresh",
                autospec=True,
            ) as mock_refresh,
            patch(
                "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator.async_shutdown",
                shutdown_mock,
            ),
        ):
            mock_refresh.side_effect = _refresh
            result = await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        assert result is False  # setup retried later by HA
        # One shutdown per created coordinator: the first (already live)
        # one and the failing one. No coordinator survives a failed setup.
        assert shutdown_mock.await_count == 2


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


class TestUnsupportedDeviceSkip:
    async def test_unsupported_device_skipped(
        self,
        hass: HomeAssistant,
        mock_mqtt_client,
    ) -> None:
        """Devices with unknown device_type are skipped during setup."""
        unsupported_device = {
            "sn": "XY99ZZ1234500001",
            "name": "PowerGlow",
            "product_name": "PowerGlow",
            "device_type": "unknown",
            "online": 1,
        }
        supported_device = MOCK_POWEROCEAN_DEVICE

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "test@example.com",
                CONF_PASSWORD: "test_password",
                CONF_USER_ID: "uid",
                CONF_DEVICES: [supported_device, unsupported_device],
            },
            unique_id="test@example.com",
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
        coordinators = hass.data[DOMAIN][entry.entry_id]
        # Only the supported device should have a coordinator
        assert len(coordinators) == 1
        assert supported_device["sn"] in coordinators
        assert unsupported_device["sn"] not in coordinators

    async def test_all_unsupported_devices_results_in_empty_setup(
        self,
        hass: HomeAssistant,
        mock_mqtt_client,
    ) -> None:
        """If all devices are unsupported, setup succeeds with no coordinators."""
        unsupported_device = {
            "sn": "XY99ZZ1234500001",
            "name": "PowerPulse",
            "product_name": "PowerPulse",
            "device_type": "unknown",
            "online": 1,
        }

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "test@example.com",
                CONF_PASSWORD: "test_password",
                CONF_USER_ID: "uid",
                CONF_DEVICES: [unsupported_device],
            },
            unique_id="test@example.com",
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
        coordinators = hass.data[DOMAIN][entry.entry_id]
        assert len(coordinators) == 0

    async def test_device_without_device_type_skipped(
        self,
        hass: HomeAssistant,
        mock_mqtt_client,
    ) -> None:
        """Devices missing the device_type field are skipped."""
        no_type_device = {
            "sn": "AB12CD3456780001",
            "name": "Mystery Device",
            "product_name": "Mystery Device",
            "online": 1,
        }

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "test@example.com",
                CONF_PASSWORD: "test_password",
                CONF_USER_ID: "uid",
                CONF_DEVICES: [no_type_device],
            },
            unique_id="test@example.com",
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
        coordinators = hass.data[DOMAIN][entry.entry_id]
        assert len(coordinators) == 0

    async def test_unsupported_device_warns_once_and_lists_skipped(
        self,
        hass: HomeAssistant,
        mock_mqtt_client,
        caplog,
    ) -> None:
        """Unsupported device: setup succeeds, one WARNING, tracked as skipped."""
        unsupported_device = {
            "sn": "BK21TEST00000001",
            "name": "Smart Meter",
            "product_name": "Smart Meter",
            "device_type": "unknown",
            "online": 1,
        }

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "test@example.com",
                CONF_PASSWORD: "test_password",
                CONF_USER_ID: "uid",
                CONF_DEVICES: [unsupported_device],
            },
            unique_id="test@example.com",
        )
        entry.add_to_hass(hass)

        mock_app_api = MagicMock()
        mock_app_api.login = AsyncMock(return_value=True)
        mock_app_api.user_id = "uid"
        mock_app_api.get_mqtt_credentials = AsyncMock(return_value={
            "userName": "app-user",
            "password": "app-pass",
        })

        import logging
        caplog.set_level(logging.WARNING)

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
        # Exactly one WARNING for the skipped device
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "Skipping unsupported" in r.message
        ]
        assert len(warnings) == 1
        # SN prefix only (privacy) — full serial must not leak
        assert "BK21" in warnings[0].getMessage()
        assert "BK21TEST00000001" not in warnings[0].getMessage()

        # Tracked under the top-level skipped-devices namespace
        skipped = hass.data[DATA_SKIPPED_DEVICES][entry.entry_id]
        assert len(skipped) == 1
        assert skipped[0]["sn_prefix"] == "BK21"
        assert skipped[0]["product_name"] == "Smart Meter"

    async def test_skipped_devices_cleared_on_unload(
        self,
        hass: HomeAssistant,
        mock_mqtt_client,
    ) -> None:
        """Unload removes the entry from the skipped-devices namespace."""
        unsupported_device = {
            "sn": "BK21TEST00000001",
            "name": "Smart Meter",
            "product_name": "Smart Meter",
            "device_type": "unknown",
            "online": 1,
        }

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "test@example.com",
                CONF_PASSWORD: "test_password",
                CONF_USER_ID: "uid",
                CONF_DEVICES: [unsupported_device],
            },
            unique_id="test@example.com",
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
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            assert entry.entry_id in hass.data[DATA_SKIPPED_DEVICES]

            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()

        assert entry.entry_id not in hass.data.get(DATA_SKIPPED_DEVICES, {})


class TestDeltaThreeReclassification:
    async def test_stored_delta_reclassified_to_delta3(
        self,
        hass: HomeAssistant,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """A device stored as 'delta' but named 'DELTA 3 Max Plus' heals to delta3.

        This is what fixes existing #110-style installs: classification is
        re-run on setup and takes precedence over the stored device_type.
        """
        stale_device = {
            "sn": "D3M1TEST00000001",
            "name": "Delta 3 Max Plus",
            "product_name": "DELTA 3 Max Plus",
            "device_type": "delta",  # wrong type from an older install
            "online": 1,
        }

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_ACCESS_KEY: "test_access_key",
                CONF_SECRET_KEY: "test_secret_key",
                CONF_MODE: MODE_STANDARD,
                CONF_DEVICES: [stale_device],
            },
            unique_id="test_access_key",
        )
        entry.add_to_hass(hass)

        with patch(
            "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator.async_config_entry_first_refresh",
            new_callable=AsyncMock,
        ):
            result = await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        assert result is True
        coordinators = hass.data[DOMAIN][entry.entry_id]
        assert "D3M1TEST00000001" in coordinators
        assert coordinators["D3M1TEST00000001"].device_type == DEVICE_TYPE_DELTA3

    async def test_null_product_name_does_not_crash_setup(
        self,
        hass: HomeAssistant,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """A stored device with product_name None sets up without crashing.

        Both device-list producers pass a JSON-null productName through, so
        re-classification on setup must tolerate None (SN prefix still
        classifies the device).
        """
        null_name_device = {
            "sn": "D3M1TEST00000001",
            "name": "Delta 3 Max Plus",
            "product_name": None,
            "device_type": "",
            "online": 1,
        }

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_ACCESS_KEY: "test_access_key",
                CONF_SECRET_KEY: "test_secret_key",
                CONF_MODE: MODE_STANDARD,
                CONF_DEVICES: [null_name_device],
            },
            unique_id="test_access_key",
        )
        entry.add_to_hass(hass)

        with patch(
            "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator.async_config_entry_first_refresh",
            new_callable=AsyncMock,
        ):
            result = await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        assert result is True
        coordinators = hass.data[DOMAIN][entry.entry_id]
        assert "D3M1TEST00000001" in coordinators
        assert coordinators["D3M1TEST00000001"].device_type == DEVICE_TYPE_DELTA3


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
