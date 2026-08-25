"""Options flow steps for the EcoFlow Energy config flow."""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .config_flow_setup import (
    SetupFlowMixin,
    _device_label,
    unsupported_suffix,
)
from .const import (
    AUTH_METHOD_APP,
    AUTH_METHOD_DEVELOPER,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_RAW_CAPTURE,
    CONF_RAW_CAPTURE_UNTIL,
    CONF_SECRET_KEY,
    CONF_USER_ID,
    DEVICE_TYPE_DISPLAY_NAMES,
    DEVICE_TYPE_UNKNOWN,
    MODE_ENHANCED,
    MODE_STANDARD,
    RAW_CAPTURE_DURATION_S,
    get_device_name,
    get_device_type,
    raw_capture_window_open,
)
from .ecoflow.enhanced_auth import enhanced_login, get_app_device_list
from .ecoflow.iot_api import IoTApiClient

_LOGGER = logging.getLogger(__name__)


async def _async_fetch_app_devices(
    hass: Any, email: str, password: str
) -> list[dict[str, Any]]:
    """Log in to the app API and return the normalized device list.

    Mirrors the discovery performed by
    :meth:`EcoFlowEnergyConfigFlow.async_step_app_credentials`. Returns an
    empty list when the login fails or the account exposes no devices.
    Network and parsing errors are raised to the caller.
    """
    session = async_get_clientsession(hass)
    login_result = await enhanced_login(session, email, password)
    if login_result is None:
        return []
    raw_devices = await get_app_device_list(session, login_result["token"])
    if not raw_devices:
        return []
    return SetupFlowMixin._normalize_app_devices(raw_devices)


class OptionsFlowMixin:
    """Options flow steps, composed into EcoFlowOptionsFlow."""

    @staticmethod
    def _stored_device_type(stored: dict[str, dict[str, Any]], sn: str) -> str:
        """Classify a stored device the way setup does on every start.

        `async_setup_entry` re-runs the classification and only falls back
        to what the entry recorded, so a serial prefix added in a later
        release makes a device supported without anything in the entry
        changing. Reading the recorded type alone would keep this branch
        calling such a device unsupported until a device-list fetch
        happens to succeed - the same "working device labelled
        unsupported" confusion that #267 was about.
        """
        device = stored.get(sn, {})
        device_type = get_device_type(device.get("product_name") or "", sn)
        if device_type == DEVICE_TYPE_UNKNOWN:
            device_type = device.get("device_type", "")
        return device_type

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Main options step - change mode and device selection."""
        errors: dict[str, str] = {}

        current_mode = self.config_entry.data.get(CONF_MODE, MODE_STANDARD)
        current_device_sns = [
            d["sn"] for d in self.config_entry.data.get(CONF_DEVICES, [])
        ]

        # Fetch current device list from API (developer auth only)
        if not self._all_devices:
            auth_method = self.config_entry.data.get(CONF_AUTH_METHOD, AUTH_METHOD_DEVELOPER)
            if auth_method == AUTH_METHOD_DEVELOPER:
                ak = self.config_entry.data.get(CONF_ACCESS_KEY)
                sk = self.config_entry.data.get(CONF_SECRET_KEY)
                if ak and sk:
                    session = async_get_clientsession(self.hass)
                    api = IoTApiClient(session, ak, sk)
                    try:
                        raw = await api.get_device_list()
                        if raw:
                            self._all_devices = SetupFlowMixin._normalize_devices(raw)
                    except (aiohttp.ClientError, TimeoutError, OSError, KeyError, ValueError, TypeError):
                        _LOGGER.warning("Options flow: failed to fetch device list", exc_info=True)
            elif auth_method == AUTH_METHOD_APP:
                email = self.config_entry.data.get(CONF_EMAIL)
                password = self.config_entry.data.get(CONF_PASSWORD)
                if email and password:
                    try:
                        devices = await _async_fetch_app_devices(
                            self.hass, email, password
                        )
                    except (
                        aiohttp.ClientError,
                        TimeoutError,
                        OSError,
                        KeyError,
                        ValueError,
                        TypeError,
                        AttributeError,
                    ):
                        # Fall back to the stored device list - the options
                        # flow stays usable without a fresh discovery.
                        _LOGGER.debug(
                            "Options flow: failed to fetch app device list",
                            exc_info=True,
                        )
                    else:
                        if devices:
                            self._all_devices = devices
                        else:
                            _LOGGER.debug(
                                "Options flow: app device list empty, "
                                "using stored devices"
                            )

        if user_input is not None:
            new_mode = user_input.get(CONF_MODE, current_mode)
            selected_sns = user_input.get(CONF_DEVICES, current_device_sns)
            if CONF_RAW_CAPTURE in user_input:
                self._pending_raw_capture = user_input[CONF_RAW_CAPTURE]

            if not selected_sns:
                errors["base"] = "no_devices"
            elif new_mode == MODE_ENHANCED and current_mode != MODE_ENHANCED:
                # Switching to Enhanced - need email + password
                self._pending_mode = new_mode
                self._pending_devices = selected_sns
                return await self.async_step_enhanced()
            elif new_mode != MODE_ENHANCED and not self.config_entry.data.get(CONF_ACCESS_KEY):
                # Switching to Standard but no Developer API keys stored
                self._pending_mode = new_mode
                self._pending_devices = selected_sns
                return await self.async_step_developer()
            else:
                return self._save_options(new_mode, selected_sns)

        if self._all_devices:
            device_options = {
                d["sn"]: _device_label(d)
                for d in self._all_devices
            }
        else:
            stored = {
                d["sn"]: d
                for d in self.config_entry.data.get(CONF_DEVICES, [])
            }
            # Prefix-derived name first, same order as _device_label: the
            # type table alone would label an ES21 "STREAM AC 5000" and a
            # P231 "Delta 3 Series" whenever this fallback branch renders.
            # The marker comes from the same helper the fresh-list branch
            # uses, so which of the two branches rendered cannot change
            # what a device is called.
            device_options = {
                sn: (
                    f"{get_device_name('', sn) or DEVICE_TYPE_DISPLAY_NAMES.get(self._stored_device_type(stored, sn), sn[:12])}"
                    f" ({sn[:12]})"
                    f"{unsupported_suffix(self._stored_device_type(stored, sn))}"
                )
                for sn in current_device_sns
            }

        schema: dict[Any, Any] = {
            vol.Required(CONF_MODE, default=current_mode): vol.In(
                {
                    MODE_STANDARD: "Standard - Official EcoFlow API",
                    MODE_ENHANCED: "Enhanced - Real-time (~3 s)",
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

        # Only offered with account login, because that is the only mode the
        # capture works in - it records the protobuf push stream, and Standard
        # Mode has none. In that mode it always does something: it deepens the
        # frame buffer of every supported device as well as recording the ones
        # with no parser. Deliberately the last field: it is a help-us-out
        # switch, not a setting anyone needs.
        if self.config_entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_APP:
            schema[
                vol.Required(
                    CONF_RAW_CAPTURE,
                    default=self._raw_capture_currently_on(),
                )
            ] = bool

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    def _raw_capture_currently_on(self) -> bool:
        """Return whether the capture is on AND still inside its window.

        An expired window must show as off, otherwise the checkbox claims
        something is running when nothing is. Shares its definition with
        setup and with the coordinator's buffer depth, so the checkbox cannot
        drift away from what is actually recording.
        """
        return raw_capture_window_open(self.config_entry.data)

    async def async_step_developer(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Switch to Standard mode - collect Developer API keys."""
        errors: dict[str, str] = {}

        if user_input is not None:
            access_key = user_input.get(CONF_ACCESS_KEY, "").strip()
            secret_key = user_input.get(CONF_SECRET_KEY, "").strip()

            if access_key and secret_key:
                session = async_get_clientsession(self.hass)
                api = IoTApiClient(session, access_key, secret_key)
                try:
                    devices = await api.get_device_list()
                    if devices is not None:
                        return self._save_options(
                            self._pending_mode,
                            self._pending_devices,
                            access_key=access_key,
                            secret_key=secret_key,
                        )
                    errors["base"] = "invalid_auth"
                except (aiohttp.ClientError, TimeoutError, OSError):
                    errors["base"] = "cannot_connect"
            else:
                errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="developer",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCESS_KEY): str,
                    vol.Required(CONF_SECRET_KEY): str,
                }
            ),
            errors=errors,
        )

    async def async_step_enhanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Switch to Enhanced mode - login to obtain userId."""
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
                    _LOGGER.warning("Options flow: Enhanced login failed: %s", exc)
                except (KeyError, ValueError, TypeError, AttributeError):
                    _LOGGER.exception("Options flow: Enhanced login error")

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
        access_key: str = "",
        secret_key: str = "",
    ) -> ConfigFlowResult:
        """Persist changes by updating config entry data."""
        existing = {d["sn"]: d for d in self.config_entry.data.get(CONF_DEVICES, [])}
        api = {d["sn"]: d for d in self._all_devices}

        selected_devices = [api.get(sn) or existing.get(sn) for sn in selected_sns]
        selected_devices = [d for d in selected_devices if d is not None]

        new_data = dict(self.config_entry.data)
        new_data[CONF_MODE] = mode
        new_data[CONF_DEVICES] = selected_devices
        self._apply_raw_capture(new_data)

        if mode == MODE_ENHANCED:
            new_data[CONF_AUTH_METHOD] = AUTH_METHOD_APP
            new_data[CONF_EMAIL] = email or new_data.get(CONF_EMAIL, "")
            new_data[CONF_PASSWORD] = password or new_data.get(CONF_PASSWORD, "")
            new_data[CONF_USER_ID] = user_id or new_data.get(CONF_USER_ID, "")
        else:
            new_data[CONF_AUTH_METHOD] = AUTH_METHOD_DEVELOPER
            if access_key:
                new_data[CONF_ACCESS_KEY] = access_key
            if secret_key:
                new_data[CONF_SECRET_KEY] = secret_key
            new_data.pop(CONF_EMAIL, None)
            new_data.pop(CONF_PASSWORD, None)
            new_data.pop(CONF_USER_ID, None)

        self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
        return self.async_create_entry(title="", data={})

    def _apply_raw_capture(self, new_data: dict[str, Any]) -> None:
        """Write the raw capture flag and, when it is switched on, its deadline.

        A running window is left alone: reopening the options for an unrelated
        change and saving must not silently extend the capture. Turning it off
        and on again does start a fresh window, which is a deliberate act.
        """
        wanted = getattr(self, "_pending_raw_capture", None)
        if wanted is None:
            return

        if not wanted:
            new_data[CONF_RAW_CAPTURE] = False
            new_data.pop(CONF_RAW_CAPTURE_UNTIL, None)
            return

        new_data[CONF_RAW_CAPTURE] = True
        if not self._raw_capture_currently_on():
            new_data[CONF_RAW_CAPTURE_UNTIL] = time.time() + RAW_CAPTURE_DURATION_S
