"""Initial setup flow steps for the EcoFlow Energy config flow."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
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
    CONF_SECRET_KEY,
    CONF_UNIT_ID,
    CONF_USER_ID,
    DEVICE_TYPE_DISPLAY_NAMES,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_POWERSTREAM,
    DEVICE_TYPE_UNKNOWN,
    DOMAIN,
    ENHANCED_ONLY_DEVICE_TYPES,
    LOCAL_MODBUS_TIMEOUT_S,
    MODE_ENHANCED,
    MODE_LOCAL,
    MODE_STANDARD,
    get_device_name,
    get_device_type,
)
from .ecoflow.enhanced_auth import enhanced_login, get_app_device_list
from .ecoflow.iot_api import IoTApiClient
from .ecoflow.modbus_local import (
    MODBUS_DEFAULT_PORT,
    ModbusConnectError,
    ModbusLocalClient,
    ModbusLocalError,
    ModbusTimeoutError,
)
from .ecoflow.parsers.powerocean_modbus import (
    SETUP_BLOCKS,
    is_supported_device,
    parse_device_info,
)

_LOGGER = logging.getLogger(__name__)

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def short_serial(sn: str) -> str:
    """Prefix and tail, never the middle.

    The picker is the owner's own screen, so it may carry more than a log
    does, and it has to let two devices of one model be told apart. The
    middle is what the vendor's own device names never show either
    (PLAN-124).
    """
    return f"{sn[:4]}...{sn[-4:]}" if len(sn) > 8 else sn


def unsupported_suffix(device_type: str | None) -> str:
    """Return the marker for a device this integration has no parser for.

    Picking such a device is deliberately still allowed. The raw data
    capture only runs on devices that were selected, and that capture is
    the one route by which an unmapped model ever becomes a supported one.
    Hiding the entry would close it. So the entry stays and says what it
    is, rather than presenting a device that will produce two diagnostic
    sensors and nothing else as if it were working.

    Only an explicit unknown classification is marked. An absent or empty
    device type means nobody classified this entry, which is not the same
    as having classified it as unsupported: an `HJ36` was refused for
    exactly that confusion once (#267), and a wrong marker here would
    tell an owner their working device is not supported.

    The marker names the consequence, not only the status. The reporter
    who asked for the marker in the first place read the finished one and
    said that "not supported" gave him the state without telling him what
    it costs him, which is the part he could not infer before (#296).
    """
    return (
        " - not supported yet (no data exposed)"
        if device_type == DEVICE_TYPE_UNKNOWN
        else ""
    )


def _device_label(device: dict[str, Any]) -> str:
    """Build a human-readable label for a device selection checkbox."""
    name = (
        device.get("name")
        or device.get("product_name")
        or get_device_name("", device.get("sn", ""))
        or DEVICE_TYPE_DISPLAY_NAMES.get(device.get("device_type", ""), "")
    )
    sn = device.get("sn", "")
    sn_short = short_serial(sn)
    status = "" if device.get("online", 0) else " (offline)"
    status += unsupported_suffix(device.get("device_type"))
    if device.get("device_type") == DEVICE_TYPE_POWERSTREAM:
        status += " - requires Standard Mode"
    elif device.get("device_type") in ENHANCED_ONLY_DEVICE_TYPES:
        status += " - requires Enhanced Mode"
    return f"{name} ({sn_short}){status}" if name else f"{sn_short}{status}"


class LocalDeviceError(Exception):
    """A local read the flow reports as a form error; ``reason`` is the key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def read_local_device(host: str, port: int, unit_id: int) -> dict[str, Any]:
    """Read the identity registers and return the device info.

    Raises ``LocalDeviceError`` with the error key the form shows: an
    unreachable or silent device is ``cannot_connect``, an exception or a
    malformed answer is ``modbus_exception``, and a reachable device that is
    not the supported PowerOcean is ``unsupported_device``. Nothing here
    writes: the client only issues read requests.
    """
    client = ModbusLocalClient(host, port, unit_id, timeout=LOCAL_MODBUS_TIMEOUT_S)
    try:
        blocks = await client.read_blocks(SETUP_BLOCKS)
    except (ModbusConnectError, ModbusTimeoutError) as err:
        raise LocalDeviceError("cannot_connect") from err
    except ModbusLocalError as err:
        raise LocalDeviceError("modbus_exception") from err
    info = parse_device_info(blocks)
    if not info.get("serial"):
        raise LocalDeviceError("modbus_exception")
    if not is_supported_device(info):
        raise LocalDeviceError("unsupported_device")
    return info


def serial_in_other_entries(
    hass: HomeAssistant, serial: str, exclude_entry_id: str | None = None
) -> bool:
    """True when another entry of this domain already carries ``serial``.

    Cloud and local entries both list their devices under ``CONF_DEVICES``,
    so one scan covers both. No cloud entry sets a ``unique_id`` from the
    serial, so the unique-id check alone would only catch local against
    local; two entries emitting the same entity ``unique_id`` would have
    Home Assistant silently refuse the second.
    """
    return any(
        device.get("sn") == serial
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.entry_id != exclude_entry_id
        for device in entry.data.get(CONF_DEVICES, [])
    )


def local_schema(host: str, port: int, unit_id: int) -> vol.Schema:
    """The host / port / unit id form, shared by setup and reconfigure."""
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=host): str,
            vol.Required(CONF_PORT, default=port): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(CONF_UNIT_ID, default=unit_id): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=255)
            ),
        }
    )


if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigFlow as _Base
else:
    _Base = object


class SetupFlowMixin(_Base):
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
            if mode == MODE_LOCAL:
                return await self.async_step_local()
            return await self.async_step_developer()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODE, default=MODE_STANDARD): vol.In(
                        {
                            MODE_STANDARD: "Standard - Official EcoFlow API",
                            MODE_ENHANCED: "Enhanced - Real-time (~3 s)",
                            MODE_LOCAL: "Local - Modbus/TCP, no cloud (read-only)",
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
                        if self.source != SOURCE_RECONFIGURE:
                            return await self.async_step_devices()
                        reason = self._switch_target_error(MODE_STANDARD)
                        if reason is None:
                            return self._finish_switch(MODE_STANDARD)
                        errors["base"] = reason
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
                    vol.Required(CONF_SECRET_KEY): _PASSWORD_SELECTOR,
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
                            elif self.source != SOURCE_RECONFIGURE:
                                return await self.async_step_devices()
                            else:
                                reason = self._switch_target_error(MODE_ENHANCED)
                                if reason is None:
                                    return self._finish_switch(MODE_ENHANCED)
                                errors["base"] = reason
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
                    vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2c: Local Modbus/TCP (host, port, unit id)
    # ------------------------------------------------------------------

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2c: Read the device identity over Modbus/TCP and create the entry."""
        errors: dict[str, str] = {}
        host, port, unit_id = "", MODBUS_DEFAULT_PORT, 1

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            unit_id = user_input[CONF_UNIT_ID]
            try:
                info = await read_local_device(host, port, unit_id)
            except LocalDeviceError as err:
                errors["base"] = err.reason
            else:
                serial = info["serial"]
                # The unique id also stops two flows for one device running
                # side by side; entries already holding it are caught below.
                await self.async_set_unique_id(serial)
                if serial_in_other_entries(self.hass, serial):
                    return self.async_abort(reason="already_configured")
                display_name = DEVICE_TYPE_DISPLAY_NAMES[DEVICE_TYPE_POWEROCEAN]
                return self.async_create_entry(
                    title=f"EcoFlow Energy Local ({serial[:4]})",
                    data={
                        CONF_MODE: MODE_LOCAL,
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_UNIT_ID: unit_id,
                        CONF_DEVICES: [
                            {
                                "sn": serial,
                                "name": display_name,
                                "product_name": display_name,
                                "device_type": DEVICE_TYPE_POWEROCEAN,
                                "online": 1,
                                "sw_version": info.get("firmware", ""),
                            }
                        ],
                    },
                )

        return self.async_show_form(
            step_id="local",
            data_schema=local_schema(host, port, unit_id),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Reconfigure: Local -> Standard / Enhanced
    #
    # These live next to the credential steps because they reuse them: the
    # same `developer` / `app_credentials` forms run, and a flow whose source
    # is reconfigure finishes by updating the entry instead of listing devices.
    # ------------------------------------------------------------------

    async def async_step_reconfigure_to_standard(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Switch a local entry to Standard Mode: ask for Developer keys."""
        self._auth_type = AUTH_METHOD_DEVELOPER
        return await self.async_step_developer()

    async def async_step_reconfigure_to_enhanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Switch a local entry to Enhanced Mode: ask for the account sign-in."""
        return await self.async_step_app_credentials()

    def _switch_target_error(self, mode: str) -> str | None:
        """Error key when the entry's device cannot move to ``mode``, else None."""
        entry = self._get_reconfigure_entry()
        serial = entry.data[CONF_DEVICES][0]["sn"]
        device = next((d for d in self._devices if d["sn"] == serial), None)
        if device is None:
            return "device_not_on_account"
        if mode == MODE_STANDARD and device.get("device_type") in (
            ENHANCED_ONLY_DEVICE_TYPES
        ):
            return "device_requires_enhanced"
        return None

    def _finish_switch(self, mode: str) -> ConfigFlowResult:
        """Rewrite the local entry as a cloud entry for its one device."""
        entry = self._get_reconfigure_entry()
        serial = entry.data[CONF_DEVICES][0]["sn"]
        if serial_in_other_entries(self.hass, serial, entry.entry_id):
            return self.async_abort(reason="already_configured")
        self._selected_devices = [d for d in self._devices if d["sn"] == serial]
        return self.async_update_reload_and_abort(
            entry,
            unique_id=None,
            data=self._entry_data(
                mode=mode,
                email=self._email,
                password=self._password,
                user_id=self._user_id,
            ),
            reason="reconfigure_successful",
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
            elif self._auth_type == AUTH_METHOD_APP and any(
                d["sn"] in selected_sns
                and d.get("device_type") == DEVICE_TYPE_POWERSTREAM
                for d in self._devices
            ):
                errors["base"] = "powerstream_requires_standard"
            elif self._auth_type != AUTH_METHOD_APP and any(
                d["sn"] in selected_sns
                and d.get("device_type") in ENHANCED_ONLY_DEVICE_TYPES
                for d in self._devices
            ):
                # The mirror of the PowerStream case. These devices report
                # on the app channel only, so a developer-key entry would
                # set one up and then never receive a reading.
                errors["base"] = "device_requires_enhanced"
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

        device_options = {d["sn"]: _device_label(d) for d in self._devices}

        # Two translation keys for one form - see the note in
        # `config_flow_options.py`. `_auth_type` is set before either path
        # reaches this step.
        return self.async_show_form(
            step_id=(
                "devices_app" if self._auth_type == AUTH_METHOD_APP else "devices"
            ),
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICES,
                        default=[
                            sn
                            for sn in device_options
                            if next(d for d in self._devices if d["sn"] == sn).get(
                                "device_type"
                            )
                            not in (
                                (DEVICE_TYPE_POWERSTREAM,)
                                if self._auth_type == AUTH_METHOD_APP
                                else ENHANCED_ONLY_DEVICE_TYPES
                            )
                        ],
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=sn, label=label)
                                for sn, label in device_options.items()
                            ],
                            multiple=True,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_devices_app(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The account sign-in rendering of `devices`."""
        return await self.async_step_devices(user_input)

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

        data = self._entry_data(
            mode=mode, email=email, password=password, user_id=user_id
        )
        return self.async_create_entry(title="EcoFlow Energy", data=data)

    def _entry_data(
        self,
        *,
        mode: str,
        email: str = "",
        password: str = "",
        user_id: str = "",
    ) -> dict[str, Any]:
        """Build the cloud entry data from what the steps collected."""
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
        return data

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
            # Measured 2026-08-04 against a five-device account: the Developer
            # API device list returns sn, deviceName, productName and online -
            # no revision field under any name. The lookup stays because it
            # costs nothing and would pick the value up if EcoFlow ever adds
            # it, but nothing may assume it is populated. The quota carries
            # revisions on some device families; see ecoflow/firmware.py.
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
            device_type = dev.get("device_type")
            if not device_type or device_type == DEVICE_TYPE_UNKNOWN:
                device_type = get_device_type(product_name, sn)
            devices.append(
                {
                    "sn": sn,
                    "name": product_name,
                    "product_name": product_name,
                    "device_type": device_type,
                    "online": 1 if online else 0,
                    # The app device list carries createTime, deviceFlag,
                    # model, productSkuId and productType - no revision field
                    # either (measured 2026-08-04). Account sign-in therefore
                    # has no firmware source at all: Enhanced Mode never polls
                    # the quota, and the protobuf stream does not carry one.
                    "sw_version": "",
                }
            )
        return devices
