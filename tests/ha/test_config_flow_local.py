"""Config flow for the local Modbus/TCP connection (read-only PowerOcean).

The Modbus client is stubbed with the identity registers a device answers, so
these tests cover what the flow does with an answer: which entry it writes,
which errors it names, and that a switch between cloud and local never mixes
credentials with a host. Entries carry the current ``ConfigFlow.VERSION``,
because a lower one makes ``async_migrate_entry`` add an ``auth_method`` that
a local entry must not have.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_DEVELOPER,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_MODE,
    CONF_SECRET_KEY,
    CONF_UNIT_ID,
    DEVICE_TYPE_POWEROCEAN,
    DOMAIN,
    MODE_LOCAL,
    MODE_STANDARD,
)
from custom_components.ecoflow_energy.ecoflow.modbus_local import (
    ModbusConnectError,
    ModbusExceptionResponse,
    ModbusLocalError,
    ModbusProtocolError,
    ModbusTimeoutError,
)

SERIAL = "HJ31DUMMY0000001"
OTHER_SERIAL = "HJ31DUMMY0000002"
HOST = "modbus.example.test"
SETUP_CLIENT = "custom_components.ecoflow_energy.config_flow_setup.ModbusLocalClient"
SETUP_IOT = "custom_components.ecoflow_energy.config_flow_setup.IoTApiClient"
CONFIG_VERSION = 3  # ConfigFlow.VERSION

CLOUD_DEVICE: dict[str, Any] = {
    "sn": SERIAL,
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}


def _registers(
    serial: str = SERIAL, category: int = 1, number: int = 1
) -> dict[int, bytes]:
    """The identity blocks a device answers, as raw wire bytes."""
    return {
        0x0000: struct.pack(">3H", 1, category, number),
        0x0003: serial.encode("ascii").ljust(16, b"\x00"),
        0x000B: bytes([37, 10, 5, 1]),
    }


class _StubClient:
    """Stands in for ModbusLocalClient: answers with `reply` or raises it."""

    def __init__(self, reply: dict[int, bytes] | ModbusLocalError) -> None:
        self._reply = reply
        self.calls: list[tuple[str, int, int]] = []

    def factory(
        self, host: str, port: int, unit_id: int, timeout: float = 3.0
    ) -> _StubClient:
        self.calls.append((host, port, unit_id))
        return self

    async def read_blocks(self, blocks: Any) -> dict[int, bytes]:
        if isinstance(self._reply, ModbusLocalError):
            raise self._reply
        return self._reply


def _patch_client(reply: dict[int, bytes] | ModbusLocalError) -> Any:
    stub = _StubClient(reply)
    return patch(SETUP_CLIENT, side_effect=stub.factory)


@pytest.fixture(autouse=True)
def _no_real_setup() -> Any:
    """A created or reloaded entry must not start the real coordinator."""
    with patch(
        "custom_components.ecoflow_energy.async_setup_entry",
        new_callable=AsyncMock,
        return_value=True,
    ):
        yield


def _cloud_entry(
    hass: HomeAssistant, devices: list[dict[str, Any]] | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        version=CONFIG_VERSION,
        data={
            CONF_ACCESS_KEY: "test_ak",
            CONF_SECRET_KEY: "test_sk",
            CONF_AUTH_METHOD: AUTH_METHOD_DEVELOPER,
            CONF_MODE: MODE_STANDARD,
            CONF_DEVICES: [CLOUD_DEVICE] if devices is None else devices,
        },
        unique_id="test_ak",
    )
    entry.add_to_hass(hass)
    return entry


def _local_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy Local (HJ31)",
        version=CONFIG_VERSION,
        data={
            CONF_MODE: MODE_LOCAL,
            CONF_HOST: HOST,
            CONF_PORT: 502,
            CONF_UNIT_ID: 1,
            CONF_DEVICES: [CLOUD_DEVICE],
        },
        unique_id=SERIAL,
    )
    entry.add_to_hass(hass)
    return entry


async def _start_local_setup(hass: HomeAssistant) -> Any:
    """Open the user step, pick Local, return the local form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODE: MODE_LOCAL}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "local"
    return result


def _address(host: str = HOST, port: int = 502, unit_id: int = 1) -> dict[str, Any]:
    return {CONF_HOST: host, CONF_PORT: port, CONF_UNIT_ID: unit_id}


async def _start_reconfigure(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )


# ===========================================================================
# C1: setup
# ===========================================================================


async def test_setup_creates_a_credential_free_local_entry(
    hass: HomeAssistant,
) -> None:
    """A valid read ends in an entry with exactly the keys the local setup reads."""
    result = await _start_local_setup(hass)
    with _patch_client(_registers()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _address(port=1502, unit_id=7)
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["version"] == CONFIG_VERSION
    # Exact equality: no access key, no password, no auth_method.
    assert result["data"] == {
        CONF_MODE: MODE_LOCAL,
        CONF_HOST: HOST,
        CONF_PORT: 1502,
        CONF_UNIT_ID: 7,
        CONF_DEVICES: [
            {
                "sn": SERIAL,
                "name": "PowerOcean",
                "product_name": "PowerOcean",
                "device_type": DEVICE_TYPE_POWEROCEAN,
                "online": 1,
                "sw_version": "37.10.5.1",
            }
        ],
    }
    # The title carries the serial prefix and nothing more of it.
    assert result["title"] == "EcoFlow Energy Local (HJ31)"
    assert SERIAL not in result["title"]
    assert result["result"].unique_id == SERIAL


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (_registers(number=3), "unsupported_device"),
        (ModbusConnectError("refused"), "cannot_connect"),
        (ModbusTimeoutError("silent"), "cannot_connect"),
        (ModbusExceptionResponse(2, 0x0206), "modbus_exception"),
        (ModbusProtocolError("transaction"), "modbus_exception"),
    ],
    ids=["product-number-3", "refused", "silent", "exception", "protocol"],
)
async def test_setup_names_the_error_and_creates_nothing(
    hass: HomeAssistant, reply: Any, error: str
) -> None:
    """Each failure maps to one error key, and the form can be submitted again."""
    result = await _start_local_setup(hass)
    with _patch_client(reply):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _address()
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "local"
    assert result["errors"] == {"base": error}
    assert hass.config_entries.async_entries(DOMAIN) == []

    with _patch_client(_registers()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _address()
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


# ===========================================================================
# C2: collision guard
# ===========================================================================


@pytest.mark.parametrize("holder", ["cloud", "local"])
async def test_setup_aborts_when_the_serial_is_already_in_an_entry(
    hass: HomeAssistant, holder: str
) -> None:
    """A serial held by a cloud or a local entry is never added a second time."""
    if holder == "cloud":
        _cloud_entry(hass)
    else:
        _local_entry(hass)
    result = await _start_local_setup(hass)
    with _patch_client(_registers()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _address()
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


# ===========================================================================
# C3: reconfigure cloud -> local
# ===========================================================================


async def test_reconfigure_single_powerocean_switches_to_local(
    hass: HomeAssistant,
) -> None:
    """The same entry becomes a local one: same id, no credentials left."""
    entry = _cloud_entry(hass)
    entry_id = entry.entry_id

    result = await _start_reconfigure(hass, entry)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reconfigure_menu"
    assert result["menu_options"] == ["reconfigure_credentials", "reconfigure_local"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_local"}
    )
    assert result["step_id"] == "reconfigure_local"
    with _patch_client(_registers()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _address()
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.entry_id == entry_id
    assert entry.data == {
        CONF_MODE: MODE_LOCAL,
        CONF_HOST: HOST,
        CONF_PORT: 502,
        CONF_UNIT_ID: 1,
        CONF_DEVICES: [CLOUD_DEVICE],
    }
    assert entry.unique_id == SERIAL


async def test_reconfigure_to_local_refuses_another_serial(
    hass: HomeAssistant,
) -> None:
    """The device read must be the entry's own, and the entry stays untouched."""
    entry = _cloud_entry(hass)
    before = dict(entry.data)

    result = await _start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_local"}
    )
    with _patch_client(_registers(serial=OTHER_SERIAL)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _address()
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "serial_mismatch"}
    assert dict(entry.data) == before


@pytest.mark.parametrize(
    "devices",
    [
        [CLOUD_DEVICE, {**CLOUD_DEVICE, "sn": OTHER_SERIAL}],
        [{**CLOUD_DEVICE, "device_type": "delta"}],
    ],
    ids=["two-devices", "not-a-powerocean"],
)
async def test_reconfigure_offers_local_only_for_one_powerocean(
    hass: HomeAssistant, devices: list[dict[str, Any]]
) -> None:
    """An account entry with other devices keeps its credentials form only."""
    entry = _cloud_entry(hass, devices)
    result = await _start_reconfigure(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_confirm"


# ===========================================================================
# Reconfigure local -> cloud, and changing the address
# ===========================================================================


def _iot_api(devices: list[dict[str, Any]]) -> Any:
    """Patch the Developer API client the credential step builds."""
    patcher = patch(SETUP_IOT)
    mock_cls = patcher.start()
    mock_cls.return_value.get_mqtt_credentials = AsyncMock(
        return_value={"certificateAccount": "a", "certificatePassword": "p"}
    )
    mock_cls.return_value.get_device_list = AsyncMock(return_value=devices)
    return patcher


async def test_local_entry_returns_to_standard_with_only_its_own_device(
    hass: HomeAssistant,
) -> None:
    """Back to the cloud: same entry, the one device, and no local keys."""
    entry = _local_entry(hass)
    result = await _start_reconfigure(hass, entry)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == [
        "reconfigure_local",
        "reconfigure_to_standard",
        "reconfigure_to_enhanced",
    ]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_to_standard"}
    )
    assert result["step_id"] == "developer"

    patcher = _iot_api(
        [
            {"sn": OTHER_SERIAL, "productName": "PowerOcean", "online": 1},
            {"sn": SERIAL, "productName": "PowerOcean", "online": 1},
        ]
    )
    try:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ACCESS_KEY: "new_ak", CONF_SECRET_KEY: "new_sk"},
        )
    finally:
        patcher.stop()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_MODE] == MODE_STANDARD
    assert entry.data[CONF_ACCESS_KEY] == "new_ak"
    assert [d["sn"] for d in entry.data[CONF_DEVICES]] == [SERIAL]
    for key in (CONF_HOST, CONF_PORT, CONF_UNIT_ID):
        assert key not in entry.data


async def test_return_to_cloud_refuses_an_account_without_the_device(
    hass: HomeAssistant,
) -> None:
    """The serial has to be on the account, otherwise the entry stays local."""
    entry = _local_entry(hass)
    result = await _start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_to_standard"}
    )
    patcher = _iot_api([{"sn": OTHER_SERIAL, "productName": "PowerOcean", "online": 1}])
    try:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ACCESS_KEY: "new_ak", CONF_SECRET_KEY: "new_sk"},
        )
    finally:
        patcher.stop()

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "device_not_on_account"}
    assert entry.data[CONF_MODE] == MODE_LOCAL
    assert entry.data[CONF_HOST] == HOST


async def test_local_entry_host_can_be_changed(hass: HomeAssistant) -> None:
    """Changing host, port and unit id keeps the device and the entry shape."""
    entry = _local_entry(hass)
    result = await _start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_local"}
    )
    with _patch_client(_registers()) as client:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            _address(host="modbus-new.example.test", port=1502, unit_id=7),
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert client.call_args.args[:3] == ("modbus-new.example.test", 1502, 7)
    assert entry.data[CONF_HOST] == "modbus-new.example.test"
    assert entry.data[CONF_PORT] == 1502
    assert entry.data[CONF_UNIT_ID] == 7
    assert entry.data[CONF_DEVICES] == [CLOUD_DEVICE]


async def test_options_flow_of_a_local_entry_points_to_reconfigure(
    hass: HomeAssistant,
) -> None:
    """The options form would offer cloud modes a local entry has no keys for."""
    entry = _local_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "local_use_reconfigure"
    assert entry.data[CONF_MODE] == MODE_LOCAL


# ===========================================================================
# Text: the schema gate cannot resolve the Home Assistant host/port constants
# ===========================================================================

_PACKAGE = Path("custom_components/ecoflow_energy")


@pytest.mark.parametrize(
    "path",
    [_PACKAGE / "strings.json", _PACKAGE / "translations/en.json"]
    + [_PACKAGE / "translations/de.json"],
    ids=["strings", "en", "de"],
)
def test_local_flow_has_text_for_every_field_menu_option_and_error(
    path: Path,
) -> None:
    """Fields, menu options and errors the local flow shows all have a text."""
    config = json.loads(path.read_text())["config"]
    for step in ("local", "reconfigure_local"):
        assert set(config["step"][step]["data"]) == {"host", "port", "unit_id"}
    assert set(config["step"]["local"]["data_description"]) == {
        "host",
        "port",
        "unit_id",
    }
    assert set(config["step"]["reconfigure_menu"]["menu_options"]) == {
        "reconfigure_credentials",
        "reconfigure_local",
        "reconfigure_to_standard",
        "reconfigure_to_enhanced",
    }
    for error in (
        "unsupported_device",
        "serial_mismatch",
        "modbus_exception",
        "device_not_on_account",
    ):
        assert config["error"][error]
