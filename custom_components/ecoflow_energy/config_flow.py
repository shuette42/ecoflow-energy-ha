"""Config flow for EcoFlow Energy integration.

Step 1: User enters access_key + secret_key (EcoFlow Developer Portal)
Step 2: User selects devices from auto-discovered list
Step 3: Mode selection — Standard (default) or Enhanced (WSS real-time)
        Enhanced requires email + password and shows a disclaimer.
Step 4: Config entry created
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .const import (
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
    get_device_type,
)
from .ecoflow.enhanced_auth import enhanced_login
from .ecoflow.iot_api import IoTApiClient

logger = logging.getLogger(__name__)


class EcoFlowEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EcoFlow Energy."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EcoFlowOptionsFlow:
        """Get the options flow handler."""
        return EcoFlowOptionsFlow()

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._access_key: str = ""
        self._secret_key: str = ""
        self._devices: list[dict[str, Any]] = []
        self._selected_devices: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Step 1: Credentials
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Enter access_key and secret_key."""
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
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:
                logger.exception("Unexpected error during EcoFlow API validation")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
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
                return await self.async_step_mode()

        device_options = {
            d["sn"]: f"{d['name']} ({d['sn'][:8]}...)"
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
    # Step 3: Mode selection
    # ------------------------------------------------------------------

    async def async_step_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: Choose Standard or Enhanced mode."""
        errors: dict[str, str] = {}

        if user_input is not None:
            mode = user_input.get(CONF_MODE, MODE_STANDARD)

            if mode == MODE_ENHANCED:
                return await self.async_step_enhanced()

            # Standard mode — create entry directly
            return self._create_entry(mode=MODE_STANDARD)

        return self.async_show_form(
            step_id="mode",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODE, default=MODE_STANDARD): vol.In(
                        {
                            MODE_STANDARD: "Standard (official API, ~10-30 s updates)",
                            MODE_ENHANCED: "Enhanced (real-time WSS, ~3 s updates)",
                        }
                    ),
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 3b: Enhanced credentials (email + password + disclaimer)
    # ------------------------------------------------------------------

    async def async_step_enhanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3b: Enhanced Mode credentials.

        Login with email + password to obtain userId automatically.
        MQTT credentials come from the IoT Developer API at runtime.
        """
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
                        return self._create_entry(
                            mode=MODE_ENHANCED,
                            email=email,
                            password=password,
                            user_id=login_result["user_id"],
                        )
                except (aiohttp.ClientError, TimeoutError):
                    errors["base"] = "cannot_connect"
                except Exception:
                    logger.exception("Unexpected error during Enhanced login")
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="enhanced",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Re-authentication flow
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication trigger."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1 of re-auth: validate access_key + secret_key."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            access_key = user_input[CONF_ACCESS_KEY].strip()
            secret_key = user_input[CONF_SECRET_KEY].strip()

            session = async_get_clientsession(self.hass)
            api = IoTApiClient(session, access_key, secret_key)

            try:
                creds = await api.get_mqtt_credentials()
                if creds is None:
                    errors["base"] = "invalid_auth"
                else:
                    # Credentials valid — check if Enhanced Mode needs second step
                    if reauth_entry.data.get(CONF_MODE) == MODE_ENHANCED:
                        self._access_key = access_key
                        self._secret_key = secret_key
                        return await self.async_step_reauth_enhanced()

                    # Standard Mode — update entry and finish
                    new_data = dict(reauth_entry.data)
                    new_data[CONF_ACCESS_KEY] = access_key
                    new_data[CONF_SECRET_KEY] = secret_key
                    self.hass.config_entries.async_update_entry(
                        reauth_entry, data=new_data
                    )
                    return self.async_abort(reason="reauth_successful")
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:
                logger.exception("Unexpected error during re-authentication")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ACCESS_KEY,
                        default=reauth_entry.data.get(CONF_ACCESS_KEY, ""),
                    ): str,
                    vol.Required(CONF_SECRET_KEY): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth_enhanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2 of re-auth: validate Enhanced Mode email + password."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

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
                        new_data = dict(reauth_entry.data)
                        new_data[CONF_ACCESS_KEY] = self._access_key
                        new_data[CONF_SECRET_KEY] = self._secret_key
                        new_data[CONF_EMAIL] = email
                        new_data[CONF_PASSWORD] = password
                        new_data[CONF_USER_ID] = login_result["user_id"]
                        self.hass.config_entries.async_update_entry(
                            reauth_entry, data=new_data
                        )
                        return self.async_abort(reason="reauth_successful")
                except (aiohttp.ClientError, TimeoutError):
                    errors["base"] = "cannot_connect"
                except Exception:
                    logger.exception("Unexpected error during Enhanced re-authentication")
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_enhanced",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMAIL,
                        default=reauth_entry.data.get(CONF_EMAIL, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
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
        self._async_abort_entries_match({CONF_ACCESS_KEY: self._access_key})

        data: dict[str, Any] = {
            CONF_ACCESS_KEY: self._access_key,
            CONF_SECRET_KEY: self._secret_key,
            CONF_DEVICES: self._selected_devices,
            CONF_MODE: mode,
        }

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
            device_type = get_device_type(product_name)
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


class EcoFlowOptionsFlow(OptionsFlow):
    """Handle options for EcoFlow Energy."""

    def __init__(self) -> None:
        """Initialize options flow."""
        self._all_devices: list[dict[str, Any]] = []
        self._pending_mode: str = ""
        self._pending_devices: list[str] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Main options step — change mode and device selection."""
        errors: dict[str, str] = {}

        current_mode = self.config_entry.data.get(CONF_MODE, MODE_STANDARD)
        current_device_sns = [
            d["sn"] for d in self.config_entry.data.get(CONF_DEVICES, [])
        ]

        # Fetch current device list from API
        if not self._all_devices:
            session = async_get_clientsession(self.hass)
            api = IoTApiClient(
                session,
                self.config_entry.data[CONF_ACCESS_KEY],
                self.config_entry.data[CONF_SECRET_KEY],
            )
            try:
                raw = await api.get_device_list()
                if raw:
                    self._all_devices = EcoFlowEnergyConfigFlow._normalize_devices(raw)
            except Exception:
                logger.warning("Options flow: failed to fetch device list", exc_info=True)

        if user_input is not None:
            new_mode = user_input.get(CONF_MODE, current_mode)
            selected_sns = user_input.get(CONF_DEVICES, current_device_sns)

            if not selected_sns:
                errors["base"] = "no_devices"
            elif new_mode == MODE_ENHANCED and current_mode != MODE_ENHANCED:
                # Switching to Enhanced — need email + password
                self._pending_mode = new_mode
                self._pending_devices = selected_sns
                return await self.async_step_enhanced()
            else:
                return self._save_options(new_mode, selected_sns)

        device_options = {
            d["sn"]: f"{d['name']} ({d['sn'][:8]}...)"
            for d in self._all_devices
        } if self._all_devices else {
            sn: sn[:12] for sn in current_device_sns
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODE, default=current_mode): vol.In(
                        {
                            MODE_STANDARD: "Standard (official API, ~10-30 s)",
                            MODE_ENHANCED: "Enhanced (real-time WSS, ~3 s)",
                        }
                    ),
                    vol.Required(
                        CONF_DEVICES,
                        default=current_device_sns,
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

    async def async_step_enhanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Switch to Enhanced mode — login to obtain userId."""
        errors: dict[str, str] = {}

        current_email = self.config_entry.data.get(CONF_EMAIL, "")

        if user_input is not None:
            email = user_input.get(CONF_EMAIL, "").strip()
            password = user_input.get(CONF_PASSWORD, "")
            user_id = ""

            if email and password:
                session = async_get_clientsession(self.hass)
                try:
                    login_result = await enhanced_login(session, email, password)
                    if login_result is not None:
                        user_id = login_result["user_id"]
                except (aiohttp.ClientError, TimeoutError) as exc:
                    logger.warning("Options flow: Enhanced login failed: %s", exc)
                except Exception:
                    logger.exception("Options flow: Enhanced login error")

            if user_id:
                return self._save_options(
                    self._pending_mode,
                    self._pending_devices,
                    email=email,
                    password=password,
                    user_id=user_id,
                )
            else:
                errors["base"] = "enhanced_login_failed"

        return self.async_show_form(
            step_id="enhanced",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=current_email): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    def _save_options(
        self,
        mode: str,
        selected_sns: list[str],
        *,
        email: str = "",
        password: str = "",
        user_id: str = "",
    ) -> ConfigFlowResult:
        """Persist changes by updating config entry data."""
        existing = {d["sn"]: d for d in self.config_entry.data.get(CONF_DEVICES, [])}
        api = {d["sn"]: d for d in self._all_devices}

        selected_devices = [api.get(sn) or existing.get(sn) for sn in selected_sns]
        selected_devices = [d for d in selected_devices if d is not None]

        new_data = dict(self.config_entry.data)
        new_data[CONF_MODE] = mode
        new_data[CONF_DEVICES] = selected_devices

        if mode == MODE_ENHANCED:
            new_data[CONF_EMAIL] = email or new_data.get(CONF_EMAIL, "")
            new_data[CONF_PASSWORD] = password or new_data.get(CONF_PASSWORD, "")
            new_data[CONF_USER_ID] = user_id or new_data.get(CONF_USER_ID, "")
        else:
            new_data.pop(CONF_EMAIL, None)
            new_data.pop(CONF_PASSWORD, None)
            new_data.pop(CONF_USER_ID, None)

        self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
        return self.async_create_entry(title="", data={})
