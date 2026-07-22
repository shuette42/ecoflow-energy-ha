"""Initial setup flow steps for the EcoFlow Energy config flow."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .const import (
    AUTH_METHOD_APP,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SECRET_KEY,
    CONF_USER_ID,
    DEVICE_TYPE_DISPLAY_NAMES,
    MODE_ENHANCED,
    MODE_STANDARD,
    get_device_type,
)
from .ecoflow.enhanced_auth import enhanced_login, get_app_device_list
from .ecoflow.iot_api import IoTApiClient

_LOGGER = logging.getLogger(__name__)


def _device_label(device: dict[str, Any]) -> str:
    """Build a human-readable label for a device selection checkbox."""
    name = device.get("name") or device.get("product_name") or ""
    if not name:
        name = DEVICE_TYPE_DISPLAY_NAMES.get(device.get("device_type", ""), "")
    sn = device.get("sn", "")
    sn_short = f"{sn[:8]}..." if len(sn) > 8 else sn
    status = "" if device.get("online", 0) else " (offline)"
    return f"{name} ({sn_short}){status}" if name else f"{sn_short}{status}"


class SetupFlowMixin:
    """Initial setup steps, composed into EcoFlowEnergyConfigFlow."""

    # ------------------------------------------------------------------
    # Step 1: Mode selection (Standard vs Enhanced)
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Select connection mode."""
        if user_input is not None:
            mode = user_input[CONF_MODE]
            if mode == MODE_ENHANCED:
                return await self.async_step_app_credentials()
            return await self.async_step_developer()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODE, default=MODE_STANDARD): vol.In(
                        {
                            MODE_STANDARD: "Standard - Official EcoFlow API",
                            MODE_ENHANCED: "Enhanced - Real-time (~3 s)",
                        }
                    ),
                }
            ),
        )

    # ------------------------------------------------------------------
    # Step 2a: Developer API credentials
    # ------------------------------------------------------------------

    async def async_step_developer(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2a: Enter access_key and secret_key."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._access_key = user_input[CONF_ACCESS_KEY].strip()
            self._secret_key = user_input[CONF_SECRET_KEY].strip()

            session = async_get_clientsession(self.hass)
            api = IoTApiClient(session, self._access_key, self._secret_key)

            try:
                creds = await api.get_mqtt_credentials()
                if creds is None:
                    errors["base"] = "invalid_auth"
                else:
                    devices = await api.get_device_list()
                    if devices is None or len(devices) == 0:
                        errors["base"] = "no_devices"
                    else:
                        self._devices = self._normalize_devices(devices)
                        return await self.async_step_devices()
            except (aiohttp.ClientError, TimeoutError, OSError):
                errors["base"] = "cannot_connect"
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.exception("Unexpected error during EcoFlow API validation")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="developer",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCESS_KEY): str,
                    vol.Required(CONF_SECRET_KEY): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "developer_portal_url": "https://developer.ecoflow.com",
            },
        )

    # ------------------------------------------------------------------
    # Step 2b: App credentials (email + password)
    # ------------------------------------------------------------------

    async def async_step_app_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2b: Enter EcoFlow app email and password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input.get(CONF_EMAIL, "").strip()
            password = user_input.get(CONF_PASSWORD, "")

            if not email or not password:
                errors["base"] = "enhanced_login_failed"
            else:
                session = async_get_clientsession(self.hass)
                try:
                    login_result = await enhanced_login(session, email, password)
                    if login_result is None:
                        errors["base"] = "enhanced_login_failed"
                    else:
                        token = login_result["token"]
                        self._email = email
                        self._password = password
                        self._user_id = login_result["user_id"]
                        self._auth_type = AUTH_METHOD_APP

                        # Fetch device list via app API (Bearer token)
                        raw_devices = await get_app_device_list(session, token)
                        if not raw_devices:
                            errors["base"] = "no_devices"
                        else:
                            self._devices = self._normalize_app_devices(raw_devices)
                            if not self._devices:
                                errors["base"] = "no_devices"
                            else:
                                return await self.async_step_devices()
                except (aiohttp.ClientError, TimeoutError, OSError):
                    errors["base"] = "cannot_connect"
                except (KeyError, ValueError, TypeError, AttributeError):
                    _LOGGER.exception("Unexpected error during EcoFlow app login")
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="app_credentials",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2: Device selection
    # ------------------------------------------------------------------

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Select devices."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_sns = user_input.get(CONF_DEVICES, [])
            if not selected_sns:
                errors["base"] = "no_devices"
            else:
                self._selected_devices = [
                    d for d in self._devices if d["sn"] in selected_sns
                ]
                if self._auth_type == AUTH_METHOD_APP:
                    return self._create_entry(
                        mode=MODE_ENHANCED,
                        email=self._email,
                        password=self._password,
                        user_id=self._user_id,
                    )
                return self._create_entry(mode=MODE_STANDARD)

        device_options = {
            d["sn"]: _device_label(d)
            for d in self._devices
        }

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICES,
                        default=list(device_options.keys()),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": sn, "label": label}
                                for sn, label in device_options.items()
                            ],
                            multiple=True,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_entry(
        self,
        *,
        mode: str,
        email: str = "",
        password: str = "",
        user_id: str = "",
    ) -> ConfigFlowResult:
        """Create the config entry with all collected data."""
        if self._auth_type == AUTH_METHOD_APP:
            self._async_abort_entries_match({CONF_EMAIL: self._email})
        else:
            self._async_abort_entries_match({CONF_ACCESS_KEY: self._access_key})

        data: dict[str, Any] = {
            CONF_AUTH_METHOD: self._auth_type,
            CONF_DEVICES: self._selected_devices,
            CONF_MODE: mode,
        }

        if self._auth_type == AUTH_METHOD_APP:
            data[CONF_EMAIL] = email
            data[CONF_PASSWORD] = password
            data[CONF_USER_ID] = user_id
        else:
            data[CONF_ACCESS_KEY] = self._access_key
            data[CONF_SECRET_KEY] = self._secret_key
            if mode == MODE_ENHANCED:
                data[CONF_EMAIL] = email
                data[CONF_PASSWORD] = password
                data[CONF_USER_ID] = user_id

        return self.async_create_entry(title="EcoFlow Energy", data=data)

    @staticmethod
    def _normalize_devices(
        raw_devices: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Normalize the device list from the IoT API response."""
        devices = []
        for dev in raw_devices:
            sn = dev.get("sn", "")
            if not sn:
                continue
            product_name = dev.get("productName", dev.get("deviceName", "Unknown"))
            online = dev.get("online", 0)
            device_type = get_device_type(product_name, sn)
            sw_version = dev.get("firmwareVersion", dev.get("softwareVersion", ""))
            devices.append(
                {
                    "sn": sn,
                    "name": product_name,
                    "product_name": product_name,
                    "device_type": device_type,
                    "online": online,
                    "sw_version": str(sw_version) if sw_version else "",
                }
            )
        return devices

    @staticmethod
    def _normalize_app_devices(
        raw_devices: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Normalize the device list from the app API response.

        The app API returns {sn, product_name, online, device_type} per device
        (same format as app_api._parse_device_response).
        """
        devices = []
        for dev in raw_devices:
            sn = dev.get("sn", "")
            if not sn:
                continue
            product_name = dev.get("product_name", "Unknown")
            online = dev.get("online", 0)
            device_type = dev.get("device_type", get_device_type(product_name, sn))
            devices.append(
                {
                    "sn": sn,
                    "name": product_name,
                    "product_name": product_name,
                    "device_type": device_type,
                    "online": 1 if online else 0,
                    "sw_version": "",
                }
            )
        return devices
