"""Reconfigure flow steps for the EcoFlow Energy config flow."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .config_flow_setup import (
    LocalDeviceError,
    local_schema,
    read_local_device,
    retitle_for_mode,
    serial_in_other_entries,
    valid_local_host,
)
from .const import (
    AUTH_METHOD_APP,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SECRET_KEY,
    CONF_UNIT_ID,
    CONF_USER_ID,
    DEVICE_TYPE_POWEROCEAN,
    MODE_ENHANCED,
    MODE_LOCAL,
)
from .ecoflow.enhanced_auth import enhanced_login
from .ecoflow.iot_api import IoTApiClient
from .ecoflow.modbus_local import MODBUS_DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _single_powerocean(entry: ConfigEntry) -> bool:
    """True for an entry that holds exactly one PowerOcean and nothing else.

    A cloud entry is account-wide: switching a multi-device entry to Local
    would drop its other devices, so only the one-device case may switch.
    """
    devices = entry.data.get(CONF_DEVICES, [])
    return len(devices) == 1 and devices[0].get("device_type") == DEVICE_TYPE_POWEROCEAN


if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigFlow as _Base
else:
    _Base = object


class ReconfigureFlowMixin(_Base):
    """Reconfigure steps, composed into EcoFlowEnergyConfigFlow."""

    # ------------------------------------------------------------------
    # Reconfigure flow (user-initiated credential update)
    # ------------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user-initiated reconfiguration.

        A one-device PowerOcean entry first asks which way to connect; every
        other cloud entry goes straight to its credentials as before.
        """
        reconfigure_entry = self._get_reconfigure_entry()
        if reconfigure_entry.data.get(CONF_MODE) == MODE_LOCAL or _single_powerocean(
            reconfigure_entry
        ):
            return await self.async_step_reconfigure_menu()
        return await self.async_step_reconfigure_credentials()

    async def async_step_reconfigure_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask how the entry should connect: a local entry may go back to the cloud."""
        if self._get_reconfigure_entry().data.get(CONF_MODE) == MODE_LOCAL:
            options = [
                "reconfigure_local",
                "reconfigure_to_standard",
                "reconfigure_to_enhanced",
            ]
        else:
            options = ["reconfigure_credentials", "reconfigure_local"]
        return self.async_show_menu(step_id="reconfigure_menu", menu_options=options)

    async def async_step_reconfigure_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update the credentials of a cloud entry (Developer keys or account)."""
        reconfigure_entry = self._get_reconfigure_entry()
        if reconfigure_entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_APP:
            return await self.async_step_reconfigure_app()
        return await self.async_step_reconfigure_confirm()

    async def async_step_reconfigure_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Point the entry at the device's Modbus/TCP port.

        For a cloud entry this is the switch to Local, for a local entry it
        changes host, port or unit id. Either way the serial read from the
        device must equal the entry's, so the entities keep their identity.
        The new data is built from scratch, not copied: a local entry holds
        no credentials, and the two modes are never mixed in one entry.
        """
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()
        device = reconfigure_entry.data[CONF_DEVICES][0]
        host = reconfigure_entry.data.get(CONF_HOST, "")
        port = reconfigure_entry.data.get(CONF_PORT, MODBUS_DEFAULT_PORT)
        unit_id = reconfigure_entry.data.get(CONF_UNIT_ID, 1)

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            unit_id = user_input[CONF_UNIT_ID]
            try:
                if not valid_local_host(host):
                    raise LocalDeviceError("invalid_host")
                info = await read_local_device(self.hass, host, port, unit_id)
            except LocalDeviceError as err:
                errors["base"] = err.reason
            else:
                serial = info["serial"]
                if serial != device["sn"]:
                    errors["base"] = "serial_mismatch"
                elif serial_in_other_entries(
                    self.hass, serial, reconfigure_entry.entry_id
                ):
                    return self.async_abort(reason="already_configured")
                else:
                    switching = reconfigure_entry.data.get(CONF_MODE) != MODE_LOCAL
                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        unique_id=serial,
                        title=retitle_for_mode(
                            reconfigure_entry, serial, to_local=True
                        ),
                        data={
                            CONF_MODE: MODE_LOCAL,
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_UNIT_ID: unit_id,
                            CONF_DEVICES: [device],
                        },
                        reason="mode_switched" if switching else "local_updated",
                    )

        return self.async_show_form(
            step_id="reconfigure_local",
            data_schema=local_schema(host, port, unit_id),
            errors=errors,
        )

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
                    vol.Required(CONF_SECRET_KEY): _PASSWORD_SELECTOR,
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
                    _LOGGER.exception(
                        "Unexpected error during Enhanced reconfiguration"
                    )
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reconfigure_enhanced",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMAIL,
                        default=reconfigure_entry.data.get(CONF_EMAIL, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
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
                    vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )
