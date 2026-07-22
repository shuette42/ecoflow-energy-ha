"""Reconfigure flow steps for the EcoFlow Energy config flow."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    AUTH_METHOD_APP,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SECRET_KEY,
    CONF_USER_ID,
    MODE_ENHANCED,
)
from .ecoflow.enhanced_auth import enhanced_login
from .ecoflow.iot_api import IoTApiClient

_LOGGER = logging.getLogger(__name__)


class ReconfigureFlowMixin:
    """Reconfigure steps, composed into EcoFlowEnergyConfigFlow."""

    # ------------------------------------------------------------------
    # Reconfigure flow (user-initiated credential update)
    # ------------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user-initiated credential update."""
        reconfigure_entry = self._get_reconfigure_entry()
        if reconfigure_entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_APP:
            return await self.async_step_reconfigure_app()
        return await self.async_step_reconfigure_confirm()

    async def async_step_reconfigure_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure step 1: update access_key + secret_key."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

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
                    if reconfigure_entry.data.get(CONF_MODE) == MODE_ENHANCED:
                        self._access_key = access_key
                        self._secret_key = secret_key
                        return await self.async_step_reconfigure_enhanced()

                    new_data = dict(reconfigure_entry.data)
                    new_data[CONF_ACCESS_KEY] = access_key
                    new_data[CONF_SECRET_KEY] = secret_key
                    self.hass.config_entries.async_update_entry(
                        reconfigure_entry, data=new_data
                    )
                    return self.async_abort(reason="reconfigure_successful")
            except (aiohttp.ClientError, TimeoutError, OSError):
                errors["base"] = "cannot_connect"
            except (KeyError, ValueError, TypeError, AttributeError):
                _LOGGER.exception("Unexpected error during reconfiguration")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reconfigure_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ACCESS_KEY,
                        default=reconfigure_entry.data.get(CONF_ACCESS_KEY, ""),
                    ): str,
                    vol.Required(CONF_SECRET_KEY): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "developer_portal_url": "https://developer.ecoflow.com",
            },
        )

    async def async_step_reconfigure_enhanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure step 2: update Enhanced Mode email + password."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

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
                        new_data = dict(reconfigure_entry.data)
                        new_data[CONF_ACCESS_KEY] = self._access_key
                        new_data[CONF_SECRET_KEY] = self._secret_key
                        new_data[CONF_EMAIL] = email
                        new_data[CONF_PASSWORD] = password
                        new_data[CONF_USER_ID] = login_result["user_id"]
                        self.hass.config_entries.async_update_entry(
                            reconfigure_entry, data=new_data
                        )
                        return self.async_abort(reason="reconfigure_successful")
                except (aiohttp.ClientError, TimeoutError, OSError):
                    errors["base"] = "cannot_connect"
                except (KeyError, ValueError, TypeError, AttributeError):
                    _LOGGER.exception("Unexpected error during Enhanced reconfiguration")
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reconfigure_enhanced",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMAIL,
                        default=reconfigure_entry.data.get(CONF_EMAIL, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_app(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure for app-auth entries: email + password only."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

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
                        new_data = dict(reconfigure_entry.data)
                        new_data[CONF_EMAIL] = email
                        new_data[CONF_PASSWORD] = password
                        new_data[CONF_USER_ID] = login_result["user_id"]
                        self.hass.config_entries.async_update_entry(
                            reconfigure_entry, data=new_data
                        )
                        return self.async_abort(reason="reconfigure_successful")
                except (aiohttp.ClientError, TimeoutError, OSError):
                    errors["base"] = "cannot_connect"
                except (KeyError, ValueError, TypeError, AttributeError):
                    _LOGGER.exception("Unexpected error during app reconfiguration")
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reconfigure_app",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMAIL,
                        default=reconfigure_entry.data.get(CONF_EMAIL, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )
