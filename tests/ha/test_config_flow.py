"""Tests for the EcoFlow Energy config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ecoflow_energy.const import (
    CONF_ACCESS_KEY,
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

from .conftest import MOCK_DELTA_DEVICE, MOCK_MQTT_CREDENTIALS, MOCK_POWEROCEAN_DEVICE


# ===========================================================================
# Step 1: User credentials
# ===========================================================================


class TestUserStep:
    async def test_form_shown(self, hass: HomeAssistant) -> None:
        """Step 1 shows a form with access_key and secret_key."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

    async def test_invalid_auth(self, hass: HomeAssistant) -> None:
        """Invalid credentials show error."""
        with (
            patch(
                "custom_components.ecoflow_energy.config_flow.IoTApiClient",
            ) as mock_cls,
        ):
            mock_cls.return_value.get_mqtt_credentials = AsyncMock(return_value=None)

            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "bad_key", CONF_SECRET_KEY: "bad_secret"},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["errors"]["base"] == "invalid_auth"

    async def test_no_devices_error(self, hass: HomeAssistant) -> None:
        """Valid auth but no devices shows error."""
        with (
            patch(
                "custom_components.ecoflow_energy.config_flow.IoTApiClient",
            ) as mock_cls,
        ):
            api = mock_cls.return_value
            api.get_mqtt_credentials = AsyncMock(return_value=MOCK_MQTT_CREDENTIALS)
            api.get_device_list = AsyncMock(return_value=[])

            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "ak", CONF_SECRET_KEY: "sk"},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["errors"]["base"] == "no_devices"

    async def test_success_advances_to_devices(self, hass: HomeAssistant) -> None:
        """Valid credentials + devices advances to step 2."""
        with (
            patch(
                "custom_components.ecoflow_energy.config_flow.IoTApiClient",
            ) as mock_cls,
        ):
            api = mock_cls.return_value
            api.get_mqtt_credentials = AsyncMock(return_value=MOCK_MQTT_CREDENTIALS)
            api.get_device_list = AsyncMock(return_value=[
                {"sn": "SN001", "productName": "Delta 2 Max", "online": 1},
            ])

            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "ak", CONF_SECRET_KEY: "sk"},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["step_id"] == "devices"


# ===========================================================================
# Step 2: Device selection
# ===========================================================================


class TestDevicesStep:
    async def _advance_to_devices(self, hass: HomeAssistant):
        """Helper: advance flow to step 2 (devices)."""
        with (
            patch(
                "custom_components.ecoflow_energy.config_flow.IoTApiClient",
            ) as mock_cls,
        ):
            api = mock_cls.return_value
            api.get_mqtt_credentials = AsyncMock(return_value=MOCK_MQTT_CREDENTIALS)
            api.get_device_list = AsyncMock(return_value=[
                {"sn": "SN001", "productName": "Delta 2 Max", "online": 1},
                {"sn": "SN002", "productName": "PowerOcean", "online": 1},
            ])

            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "ak", CONF_SECRET_KEY: "sk"},
            )
            return result

    async def test_devices_form_shown(self, hass: HomeAssistant) -> None:
        result = await self._advance_to_devices(hass)
        assert result["step_id"] == "devices"

    async def test_select_devices_advances_to_mode(self, hass: HomeAssistant) -> None:
        result = await self._advance_to_devices(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEVICES: ["SN001"]},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "mode"


# ===========================================================================
# Step 3: Mode selection + entry creation (Standard)
# ===========================================================================


class TestModeStep:
    async def _advance_to_mode(self, hass: HomeAssistant):
        """Helper: advance flow to step 3 (mode)."""
        with (
            patch(
                "custom_components.ecoflow_energy.config_flow.IoTApiClient",
            ) as mock_cls,
        ):
            api = mock_cls.return_value
            api.get_mqtt_credentials = AsyncMock(return_value=MOCK_MQTT_CREDENTIALS)
            api.get_device_list = AsyncMock(return_value=[
                {"sn": "SN001", "productName": "Delta 2 Max", "online": 1},
            ])

            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "ak", CONF_SECRET_KEY: "sk"},
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_DEVICES: ["SN001"]},
            )
            return result

    async def test_standard_mode_creates_entry(self, hass: HomeAssistant) -> None:
        """Selecting Standard Mode creates the config entry."""
        result = await self._advance_to_mode(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_MODE: MODE_STANDARD},
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "EcoFlow Energy"
        assert result["data"][CONF_MODE] == MODE_STANDARD
        assert result["data"][CONF_ACCESS_KEY] == "ak"
        assert len(result["data"][CONF_DEVICES]) == 1

    async def test_enhanced_mode_advances_to_enhanced_step(
        self, hass: HomeAssistant
    ) -> None:
        """Selecting Enhanced Mode shows the enhanced credentials form."""
        result = await self._advance_to_mode(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_MODE: MODE_ENHANCED},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "enhanced"


# ===========================================================================
# Step 3b: Enhanced credentials
# ===========================================================================


class TestEnhancedStep:
    async def _advance_to_enhanced(self, hass: HomeAssistant):
        """Helper: advance flow to enhanced step."""
        with (
            patch(
                "custom_components.ecoflow_energy.config_flow.IoTApiClient",
            ) as mock_cls,
        ):
            api = mock_cls.return_value
            api.get_mqtt_credentials = AsyncMock(return_value=MOCK_MQTT_CREDENTIALS)
            api.get_device_list = AsyncMock(return_value=[
                {"sn": "SN001", "productName": "Delta 2 Max", "online": 1},
            ])

            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "ak", CONF_SECRET_KEY: "sk"},
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_DEVICES: ["SN001"]},
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_MODE: MODE_ENHANCED},
            )
            return result

    async def test_enhanced_login_failed(self, hass: HomeAssistant) -> None:
        """Failed Enhanced login shows error."""
        result = await self._advance_to_enhanced(hass)
        with (
            patch(
                "custom_components.ecoflow_energy.config_flow.enhanced_login",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "wrong"},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["errors"]["base"] == "enhanced_login_failed"

    async def test_enhanced_success_creates_entry(self, hass: HomeAssistant) -> None:
        """Successful Enhanced login creates the config entry."""
        result = await self._advance_to_enhanced(hass)
        with patch(
            "custom_components.ecoflow_energy.config_flow.enhanced_login",
            new_callable=AsyncMock,
            return_value={"token": "jwt_token", "user_id": "uid123"},
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"},
            )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert result["data"][CONF_MODE] == MODE_ENHANCED
            assert result["data"][CONF_EMAIL] == "test@example.com"
            assert result["data"][CONF_USER_ID] == "uid123"


# ===========================================================================
# Duplicate entry abort
# ===========================================================================


class TestAbort:
    async def test_already_configured(self, hass: HomeAssistant) -> None:
        """Second config entry with same access_key is aborted at entry creation."""
        # Create first entry via flow
        with (
            patch(
                "custom_components.ecoflow_energy.config_flow.IoTApiClient",
            ) as mock_cls,
        ):
            api = mock_cls.return_value
            api.get_mqtt_credentials = AsyncMock(return_value=MOCK_MQTT_CREDENTIALS)
            api.get_device_list = AsyncMock(return_value=[
                {"sn": "SN001", "productName": "Delta 2 Max", "online": 1},
            ])

            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "same_key", CONF_SECRET_KEY: "sk"},
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_DEVICES: ["SN001"]},
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_MODE: MODE_STANDARD},
            )
            assert result["type"] is FlowResultType.CREATE_ENTRY

        # Second flow with same access_key
        with (
            patch(
                "custom_components.ecoflow_energy.config_flow.IoTApiClient",
            ) as mock_cls,
        ):
            api = mock_cls.return_value
            api.get_mqtt_credentials = AsyncMock(return_value=MOCK_MQTT_CREDENTIALS)
            api.get_device_list = AsyncMock(return_value=[
                {"sn": "SN001", "productName": "Delta 2 Max", "online": 1},
            ])

            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "same_key", CONF_SECRET_KEY: "sk"},
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_DEVICES: ["SN001"]},
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_MODE: MODE_STANDARD},
            )
            assert result["type"] is FlowResultType.ABORT
            assert result["reason"] == "already_configured"


# ===========================================================================
# Options Flow
# ===========================================================================


class TestOptionsFlow:
    """Tests for EcoFlowOptionsFlow (runtime reconfiguration)."""

    def _create_standard_entry(self, hass: HomeAssistant) -> MockConfigEntry:
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_ACCESS_KEY: "ak",
                CONF_SECRET_KEY: "sk",
                CONF_MODE: MODE_STANDARD,
                CONF_DEVICES: [
                    {"sn": "SN001", "name": "Delta 2 Max", "product_name": "Delta 2 Max",
                     "device_type": "delta", "online": 1},
                ],
            },
            unique_id="ak",
        )
        entry.add_to_hass(hass)
        return entry

    def _create_enhanced_entry(self, hass: HomeAssistant) -> MockConfigEntry:
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_ACCESS_KEY: "ak",
                CONF_SECRET_KEY: "sk",
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_USER_ID: "uid",
                CONF_DEVICES: [
                    {"sn": "SN001", "name": "Delta 2 Max", "product_name": "Delta 2 Max",
                     "device_type": "delta", "online": 1},
                ],
            },
            unique_id="ak",
        )
        entry.add_to_hass(hass)
        return entry

    async def test_options_init_shows_form(self, hass: HomeAssistant) -> None:
        """Options flow shows init form with mode and devices."""
        entry = self._create_standard_entry(hass)
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_device_list = AsyncMock(return_value=[
                {"sn": "SN001", "productName": "Delta 2 Max", "online": 1},
            ])
            result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"

    async def test_options_keep_standard_mode(self, hass: HomeAssistant) -> None:
        """Keeping Standard Mode saves options directly."""
        entry = self._create_standard_entry(hass)
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_device_list = AsyncMock(return_value=[
                {"sn": "SN001", "productName": "Delta 2 Max", "online": 1},
            ])
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {CONF_MODE: MODE_STANDARD, CONF_DEVICES: ["SN001"]},
            )
        assert result["type"] is FlowResultType.CREATE_ENTRY

    async def test_options_switch_to_enhanced_shows_credentials(
        self, hass: HomeAssistant
    ) -> None:
        """Switching from Standard to Enhanced shows enhanced credentials form."""
        entry = self._create_standard_entry(hass)
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_device_list = AsyncMock(return_value=[
                {"sn": "SN001", "productName": "Delta 2 Max", "online": 1},
            ])
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {CONF_MODE: MODE_ENHANCED, CONF_DEVICES: ["SN001"]},
            )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "enhanced"

    async def test_options_no_devices_error(self, hass: HomeAssistant) -> None:
        """Selecting no devices shows error."""
        entry = self._create_standard_entry(hass)
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_device_list = AsyncMock(return_value=[
                {"sn": "SN001", "productName": "Delta 2 Max", "online": 1},
            ])
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {CONF_MODE: MODE_STANDARD, CONF_DEVICES: []},
            )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"]["base"] == "no_devices"

    async def test_options_enhanced_to_standard_removes_credentials(
        self, hass: HomeAssistant
    ) -> None:
        """Switching from Enhanced to Standard removes email/password."""
        entry = self._create_enhanced_entry(hass)
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_device_list = AsyncMock(return_value=[
                {"sn": "SN001", "productName": "Delta 2 Max", "online": 1},
            ])
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {CONF_MODE: MODE_STANDARD, CONF_DEVICES: ["SN001"]},
            )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        # Credentials should be removed from entry data
        assert CONF_EMAIL not in entry.data
        assert CONF_PASSWORD not in entry.data
        assert CONF_USER_ID not in entry.data

    async def test_options_enhanced_login_validates(self, hass: HomeAssistant) -> None:
        """Enhanced login in options flow validates credentials."""
        entry = self._create_standard_entry(hass)
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_device_list = AsyncMock(return_value=[
                {"sn": "SN001", "productName": "Delta 2 Max", "online": 1},
            ])
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {CONF_MODE: MODE_ENHANCED, CONF_DEVICES: ["SN001"]},
            )

        with patch(
            "custom_components.ecoflow_energy.config_flow.enhanced_login",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "wrong"},
            )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"]["base"] == "enhanced_login_failed"
