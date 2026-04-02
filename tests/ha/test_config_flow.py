"""Tests for the EcoFlow Energy config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

import aiohttp
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE

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
    DOMAIN,
    MODE_ENHANCED,
    MODE_STANDARD,
)

from .conftest import MOCK_DELTA_DEVICE, MOCK_MQTT_CREDENTIALS, MOCK_POWEROCEAN_DEVICE  # noqa: F401


# ===========================================================================
# Helper: advance through mode selection
# ===========================================================================


async def _select_mode(hass: HomeAssistant, mode: str = MODE_STANDARD):
    """Init the config flow and select connection mode, return the result."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MODE: mode},
    )
    return result


# ===========================================================================
# Step 1: Mode selection
# ===========================================================================


class TestUserStep:
    async def test_form_shown(self, hass: HomeAssistant) -> None:
        """Step 1 shows a form with mode selector."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

    async def test_standard_selected_shows_developer_form(
        self, hass: HomeAssistant
    ) -> None:
        """Selecting Standard mode advances to the developer credentials form."""
        result = await _select_mode(hass, MODE_STANDARD)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "developer"

    async def test_enhanced_selected_shows_app_form(self, hass: HomeAssistant) -> None:
        """Selecting Enhanced mode advances to the app credentials form."""
        result = await _select_mode(hass, MODE_ENHANCED)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "app_credentials"


# ===========================================================================
# Step 2a: Developer credentials
# ===========================================================================


class TestDeveloperStep:
    async def test_invalid_auth(self, hass: HomeAssistant) -> None:
        """Invalid credentials show error."""
        with (
            patch(
                "custom_components.ecoflow_energy.config_flow.IoTApiClient",
            ) as mock_cls,
        ):
            mock_cls.return_value.get_mqtt_credentials = AsyncMock(return_value=None)

            result = await _select_mode(hass, MODE_STANDARD)
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

            result = await _select_mode(hass, MODE_STANDARD)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "ak", CONF_SECRET_KEY: "sk"},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["errors"]["base"] == "no_devices"

    async def test_success_advances_to_devices(self, hass: HomeAssistant) -> None:
        """Valid credentials + devices advances to device selection."""
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

            result = await _select_mode(hass, MODE_STANDARD)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "ak", CONF_SECRET_KEY: "sk"},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["step_id"] == "devices"


# ===========================================================================
# Step 2b: App credentials
# ===========================================================================


class TestAppCredentialsStep:
    async def test_login_failed(self, hass: HomeAssistant) -> None:
        """Failed app login shows error."""
        result = await _select_mode(hass, MODE_ENHANCED)
        with patch(
            "custom_components.ecoflow_energy.config_flow.enhanced_login",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "wrong"},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["errors"]["base"] == "enhanced_login_failed"

    async def test_connection_error(self, hass: HomeAssistant) -> None:
        """Connection error during app login shows cannot_connect."""
        result = await _select_mode(hass, MODE_ENHANCED)
        with patch(
            "custom_components.ecoflow_energy.config_flow.enhanced_login",
            new_callable=AsyncMock,
            side_effect=aiohttp.ClientError("Connection failed"),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["errors"]["base"] == "cannot_connect"

    async def test_no_devices_shows_error(self, hass: HomeAssistant) -> None:
        """Successful login but no devices shows error."""
        result = await _select_mode(hass, MODE_ENHANCED)
        with (
            patch(
                "custom_components.ecoflow_energy.config_flow.enhanced_login",
                new_callable=AsyncMock,
                return_value={"token": "jwt_token", "user_id": "uid123"},
            ),
            patch(
                "custom_components.ecoflow_energy.config_flow.get_app_device_list",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["errors"]["base"] == "no_devices"

    async def test_success_advances_to_devices(self, hass: HomeAssistant) -> None:
        """Successful login + devices advances to device selection."""
        result = await _select_mode(hass, MODE_ENHANCED)
        with (
            patch(
                "custom_components.ecoflow_energy.config_flow.enhanced_login",
                new_callable=AsyncMock,
                return_value={"token": "jwt_token", "user_id": "uid123"},
            ),
            patch(
                "custom_components.ecoflow_energy.config_flow.get_app_device_list",
                new_callable=AsyncMock,
                return_value=[
                    {"sn": "HJ31TEST00000001", "product_name": "PowerOcean", "online": 1, "device_type": "powerocean"},
                ],
            ),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["step_id"] == "devices"

    async def test_full_path_creates_entry(self, hass: HomeAssistant) -> None:
        """Full app-auth path: login -> devices -> entry created (no mode step)."""
        result = await _select_mode(hass, MODE_ENHANCED)
        with (
            patch(
                "custom_components.ecoflow_energy.config_flow.enhanced_login",
                new_callable=AsyncMock,
                return_value={"token": "jwt_token", "user_id": "uid123"},
            ),
            patch(
                "custom_components.ecoflow_energy.config_flow.get_app_device_list",
                new_callable=AsyncMock,
                return_value=[
                    {"sn": "HJ31TEST00000001", "product_name": "PowerOcean", "online": 1, "device_type": "powerocean"},
                ],
            ),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"},
            )
            # Should be on devices step now
            assert result["step_id"] == "devices"

            # Select the device - should create entry directly (no mode step)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_DEVICES: ["HJ31TEST00000001"]},
            )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert result["data"][CONF_AUTH_METHOD] == AUTH_METHOD_APP
            assert result["data"][CONF_MODE] == MODE_ENHANCED
            assert result["data"][CONF_EMAIL] == "test@example.com"
            assert result["data"][CONF_USER_ID] == "uid123"
            assert CONF_ACCESS_KEY not in result["data"]
            assert len(result["data"][CONF_DEVICES]) == 1


# ===========================================================================
# Step 3: Device selection
# ===========================================================================


class TestDevicesStep:
    async def _advance_to_devices(self, hass: HomeAssistant):
        """Helper: advance flow to device selection step."""
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

            result = await _select_mode(hass, MODE_STANDARD)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "ak", CONF_SECRET_KEY: "sk"},
            )
            return result

    async def test_devices_form_shown(self, hass: HomeAssistant) -> None:
        result = await self._advance_to_devices(hass)
        assert result["step_id"] == "devices"

    async def test_select_devices_creates_entry(self, hass: HomeAssistant) -> None:
        """Selecting devices with developer auth creates the entry directly."""
        result = await self._advance_to_devices(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEVICES: ["SN001"]},
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_MODE] == MODE_STANDARD
        assert result["data"][CONF_ACCESS_KEY] == "ak"
        assert len(result["data"][CONF_DEVICES]) == 1


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

            result = await _select_mode(hass, MODE_STANDARD)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "same_key", CONF_SECRET_KEY: "sk"},
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_DEVICES: ["SN001"]},
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

            result = await _select_mode(hass, MODE_STANDARD)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "same_key", CONF_SECRET_KEY: "sk"},
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_DEVICES: ["SN001"]},
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

    def _create_app_auth_entry(self, hass: HomeAssistant) -> MockConfigEntry:
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_USER_ID: "uid",
                CONF_MODE: MODE_STANDARD,
                CONF_DEVICES: [
                    {"sn": "SN001", "name": "Delta 2 Max", "product_name": "Delta 2 Max",
                     "device_type": "delta", "online": 1},
                ],
            },
            unique_id="uid",
        )
        entry.add_to_hass(hass)
        return entry

    async def test_options_app_auth_skips_api_fetch(self, hass: HomeAssistant) -> None:
        """App-auth entries skip IoTApiClient and use stored devices."""
        entry = self._create_app_auth_entry(hass)
        # No IoTApiClient mock needed - it should not be called
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            result = await hass.config_entries.options.async_init(entry.entry_id)
            mock_cls.assert_not_called()
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"

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


# ===========================================================================
# Re-authentication Flow
# ===========================================================================


class TestReauthFlow:
    """Tests for the re-authentication flow."""

    def _create_standard_entry(self, hass: HomeAssistant):
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_ACCESS_KEY: "old_ak",
                CONF_SECRET_KEY: "old_sk",
                CONF_MODE: MODE_STANDARD,
                CONF_DEVICES: [
                    {"sn": "SN001", "name": "Delta 2 Max", "product_name": "Delta 2 Max",
                     "device_type": "delta", "online": 1},
                ],
            },
            unique_id="old_ak",
        )
        entry.add_to_hass(hass)
        return entry

    def _create_enhanced_entry(self, hass: HomeAssistant):
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_ACCESS_KEY: "old_ak",
                CONF_SECRET_KEY: "old_sk",
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "old@example.com",
                CONF_PASSWORD: "old_pass",
                CONF_USER_ID: "old_uid",
                CONF_DEVICES: [
                    {"sn": "SN001", "name": "PowerOcean", "product_name": "PowerOcean",
                     "device_type": "powerocean", "online": 1},
                ],
            },
            unique_id="old_ak",
        )
        entry.add_to_hass(hass)
        return entry

    async def test_reauth_shows_confirm_form(self, hass: HomeAssistant) -> None:
        """Reauth flow shows the confirm form."""
        entry = self._create_standard_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

    async def test_reauth_invalid_credentials(self, hass: HomeAssistant) -> None:
        """Invalid credentials show error on reauth."""
        entry = self._create_standard_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_mqtt_credentials = AsyncMock(return_value=None)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "bad_key", CONF_SECRET_KEY: "bad_secret"},
            )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"]["base"] == "invalid_auth"

    async def test_reauth_success_standard(self, hass: HomeAssistant) -> None:
        """Successful reauth for Standard Mode updates entry and aborts."""
        entry = self._create_standard_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_mqtt_credentials = AsyncMock(
                return_value=MOCK_MQTT_CREDENTIALS
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "new_ak", CONF_SECRET_KEY: "new_sk"},
            )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert entry.data[CONF_ACCESS_KEY] == "new_ak"
        assert entry.data[CONF_SECRET_KEY] == "new_sk"

    async def test_reauth_enhanced_shows_second_step(self, hass: HomeAssistant) -> None:
        """Reauth for Enhanced Mode shows enhanced credentials form after API validation."""
        entry = self._create_enhanced_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_mqtt_credentials = AsyncMock(
                return_value=MOCK_MQTT_CREDENTIALS
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "new_ak", CONF_SECRET_KEY: "new_sk"},
            )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_enhanced"

    async def test_reauth_enhanced_full_flow(self, hass: HomeAssistant) -> None:
        """Complete Enhanced Mode reauth updates all credentials."""
        entry = self._create_enhanced_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_mqtt_credentials = AsyncMock(
                return_value=MOCK_MQTT_CREDENTIALS
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "new_ak", CONF_SECRET_KEY: "new_sk"},
            )

        with patch(
            "custom_components.ecoflow_energy.config_flow.enhanced_login",
            new_callable=AsyncMock,
            return_value={"token": "new_jwt", "user_id": "new_uid"},
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "new@example.com", CONF_PASSWORD: "new_pass"},
            )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert entry.data[CONF_ACCESS_KEY] == "new_ak"
        assert entry.data[CONF_SECRET_KEY] == "new_sk"
        assert entry.data[CONF_EMAIL] == "new@example.com"
        assert entry.data[CONF_PASSWORD] == "new_pass"
        assert entry.data[CONF_USER_ID] == "new_uid"

    async def test_reauth_enhanced_login_failed(self, hass: HomeAssistant) -> None:
        """Failed Enhanced login during reauth shows error."""
        entry = self._create_enhanced_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_mqtt_credentials = AsyncMock(
                return_value=MOCK_MQTT_CREDENTIALS
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "new_ak", CONF_SECRET_KEY: "new_sk"},
            )

        with patch(
            "custom_components.ecoflow_energy.config_flow.enhanced_login",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "wrong"},
            )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"]["base"] == "enhanced_login_failed"

    async def test_reauth_connection_error(self, hass: HomeAssistant) -> None:
        """Connection error during reauth shows cannot_connect."""
        entry = self._create_standard_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_mqtt_credentials = AsyncMock(
                side_effect=aiohttp.ClientError("Connection failed")
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "ak", CONF_SECRET_KEY: "sk"},
            )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"]["base"] == "cannot_connect"


# ===========================================================================
# Reconfigure Flow (user-initiated credential update)
# ===========================================================================


class TestReconfigureFlow:
    """Tests for the user-initiated reconfigure flow."""

    def _create_standard_entry(self, hass: HomeAssistant):
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_ACCESS_KEY: "old_ak",
                CONF_SECRET_KEY: "old_sk",
                CONF_MODE: MODE_STANDARD,
                CONF_DEVICES: [
                    {"sn": "SN001", "name": "Delta 2 Max", "product_name": "Delta 2 Max",
                     "device_type": "delta", "online": 1},
                ],
            },
            unique_id="old_ak",
        )
        entry.add_to_hass(hass)
        return entry

    def _create_enhanced_entry(self, hass: HomeAssistant):
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_ACCESS_KEY: "old_ak",
                CONF_SECRET_KEY: "old_sk",
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "old@example.com",
                CONF_PASSWORD: "old_pass",
                CONF_USER_ID: "old_uid",
                CONF_DEVICES: [
                    {"sn": "SN001", "name": "PowerOcean", "product_name": "PowerOcean",
                     "device_type": "powerocean", "online": 1},
                ],
            },
            unique_id="old_ak",
        )
        entry.add_to_hass(hass)
        return entry

    async def test_reconfigure_shows_form(self, hass: HomeAssistant) -> None:
        """Reconfigure flow shows the credentials form."""
        entry = self._create_standard_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reconfigure_confirm"

    async def test_reconfigure_invalid_credentials(self, hass: HomeAssistant) -> None:
        """Invalid credentials show error."""
        entry = self._create_standard_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_mqtt_credentials = AsyncMock(return_value=None)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "bad_key", CONF_SECRET_KEY: "bad_secret"},
            )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"]["base"] == "invalid_auth"

    async def test_reconfigure_success_standard(self, hass: HomeAssistant) -> None:
        """Successful reconfigure for Standard Mode updates entry."""
        entry = self._create_standard_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_mqtt_credentials = AsyncMock(
                return_value=MOCK_MQTT_CREDENTIALS
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "new_ak", CONF_SECRET_KEY: "new_sk"},
            )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_ACCESS_KEY] == "new_ak"
        assert entry.data[CONF_SECRET_KEY] == "new_sk"

    async def test_reconfigure_enhanced_full_flow(self, hass: HomeAssistant) -> None:
        """Reconfigure Enhanced Mode updates all credentials."""
        entry = self._create_enhanced_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_mqtt_credentials = AsyncMock(
                return_value=MOCK_MQTT_CREDENTIALS
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "new_ak", CONF_SECRET_KEY: "new_sk"},
            )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reconfigure_enhanced"

        with patch(
            "custom_components.ecoflow_energy.config_flow.enhanced_login",
            new_callable=AsyncMock,
            return_value={"token": "new_jwt", "user_id": "new_uid"},
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "new@example.com", CONF_PASSWORD: "new_pass"},
            )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_ACCESS_KEY] == "new_ak"
        assert entry.data[CONF_EMAIL] == "new@example.com"
        assert entry.data[CONF_USER_ID] == "new_uid"

    async def test_reconfigure_connection_error(self, hass: HomeAssistant) -> None:
        """Connection error shows cannot_connect."""
        entry = self._create_standard_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        with patch(
            "custom_components.ecoflow_energy.config_flow.IoTApiClient",
        ) as mock_cls:
            mock_cls.return_value.get_mqtt_credentials = AsyncMock(
                side_effect=aiohttp.ClientError("Connection failed")
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_ACCESS_KEY: "ak", CONF_SECRET_KEY: "sk"},
            )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"]["base"] == "cannot_connect"


# ===========================================================================
# App-Auth Re-authentication Flow
# ===========================================================================


class TestAppAuthReauthFlow:
    """Tests for re-authentication flow with app-auth entries."""

    def _create_app_auth_entry(self, hass: HomeAssistant):
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "old@example.com",
                CONF_PASSWORD: "old_pass",
                CONF_USER_ID: "old_uid",
                CONF_DEVICES: [
                    {"sn": "SN001", "name": "PowerOcean", "product_name": "PowerOcean",
                     "device_type": "powerocean", "online": 1},
                ],
            },
            unique_id="old@example.com",
        )
        entry.add_to_hass(hass)
        return entry

    async def test_reauth_shows_app_form(self, hass: HomeAssistant) -> None:
        """App-auth reauth shows email/password form directly (no Developer Keys)."""
        entry = self._create_app_auth_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_app"

    async def test_reauth_app_success(self, hass: HomeAssistant) -> None:
        """Successful app-auth reauth updates credentials."""
        entry = self._create_app_auth_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        with patch(
            "custom_components.ecoflow_energy.config_flow.enhanced_login",
            new_callable=AsyncMock,
            return_value={"token": "new_jwt", "user_id": "new_uid"},
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "new@example.com", CONF_PASSWORD: "new_pass"},
            )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert entry.data[CONF_EMAIL] == "new@example.com"
        assert entry.data[CONF_PASSWORD] == "new_pass"
        assert entry.data[CONF_USER_ID] == "new_uid"

    async def test_reauth_app_login_failed(self, hass: HomeAssistant) -> None:
        """Failed app-auth reauth shows error."""
        entry = self._create_app_auth_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        with patch(
            "custom_components.ecoflow_energy.config_flow.enhanced_login",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "wrong"},
            )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"]["base"] == "enhanced_login_failed"


# ===========================================================================
# App-Auth Reconfigure Flow
# ===========================================================================


class TestAppAuthReconfigureFlow:
    """Tests for user-initiated reconfigure flow with app-auth entries."""

    def _create_app_auth_entry(self, hass: HomeAssistant):
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "old@example.com",
                CONF_PASSWORD: "old_pass",
                CONF_USER_ID: "old_uid",
                CONF_DEVICES: [
                    {"sn": "SN001", "name": "PowerOcean", "product_name": "PowerOcean",
                     "device_type": "powerocean", "online": 1},
                ],
            },
            unique_id="old@example.com",
        )
        entry.add_to_hass(hass)
        return entry

    async def test_reconfigure_shows_app_form(self, hass: HomeAssistant) -> None:
        """App-auth reconfigure shows email/password form directly."""
        entry = self._create_app_auth_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reconfigure_app"

    async def test_reconfigure_app_success(self, hass: HomeAssistant) -> None:
        """Successful app-auth reconfigure updates credentials."""
        entry = self._create_app_auth_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        with patch(
            "custom_components.ecoflow_energy.config_flow.enhanced_login",
            new_callable=AsyncMock,
            return_value={"token": "new_jwt", "user_id": "new_uid"},
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "new@example.com", CONF_PASSWORD: "new_pass"},
            )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_EMAIL] == "new@example.com"
        assert entry.data[CONF_USER_ID] == "new_uid"

    async def test_reconfigure_app_login_failed(self, hass: HomeAssistant) -> None:
        """Failed app-auth reconfigure shows error."""
        entry = self._create_app_auth_entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        with patch(
            "custom_components.ecoflow_energy.config_flow.enhanced_login",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "wrong"},
            )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"]["base"] == "enhanced_login_failed"
