"""Config flow for the local Modbus/TCP connection (PowerOcean).

The Modbus client is stubbed with the identity registers a device answers, so
these tests cover what the flow does with an answer: which entry it writes,
which errors it names, and that a switch between cloud and local never mixes
credentials with a host. Entries carry the current ``ConfigFlow.VERSION``,
because a lower one makes ``async_migrate_entry`` add an ``auth_method`` that
a local entry must not have.
"""

from __future__ import annotations

import json
import logging
import struct
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.config_flow import EcoFlowEnergyConfigFlow
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
    CONF_UNIT_ID,
    CONF_USER_ID,
    DEVICE_TYPE_POWEROCEAN,
    DOMAIN,
    MODE_ENHANCED,
    MODE_LOCAL,
    MODE_STANDARD,
)
from custom_components.ecoflow_energy.coordinator import modbus_link
from custom_components.ecoflow_energy.ecoflow.modbus_local import (
    MODBUS_BASE_ADDRESS,
    ModbusConnectError,
    ModbusExceptionResponse,
    ModbusLocalError,
    ModbusProtocolError,
    ModbusTimeoutError,
)

SERIAL = "HJ31DUMMY0000001"
OTHER_SERIAL = "HJ31DUMMY0000002"
HOST = "modbus.example.test"
SETUP_CLIENT = (
    "custom_components.ecoflow_energy.coordinator.modbus_link.ModbusLocalClient"
)
SETUP_IOT = "custom_components.ecoflow_energy.config_flow_setup.IoTApiClient"
SETUP_LOGIN = "custom_components.ecoflow_energy.config_flow_setup.enhanced_login"
SETUP_APP_DEVICES = (
    "custom_components.ecoflow_energy.config_flow_setup.get_app_device_list"
)
OPTIONS_IOT = "custom_components.ecoflow_energy.config_flow_options.IoTApiClient"
CONFIG_VERSION = 3  # ConfigFlow.VERSION

CLOUD_DEVICE: dict[str, Any] = {
    "sn": SERIAL,
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}
OTHER_DEVICE: dict[str, Any] = {**CLOUD_DEVICE, "sn": OTHER_SERIAL}


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
def _own_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe with the own client unless a test asks for the shared connection."""
    monkeypatch.setattr(modbus_link, "SHARED_CONNECTION", False)


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
                "sw_version": "5.1.37.10",
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
        (_registers(category=2), "unsupported_device"),
        (_registers(serial=""), "modbus_exception"),
        (_registers(serial="HJ31SHORT"), "modbus_exception"),
        (_registers(serial="hj31dummy0000001"), "modbus_exception"),
        ({**_registers(), 0x0003: b"\xff\xfe\x01\x02" * 4}, "modbus_exception"),
        (ModbusConnectError("refused"), "cannot_connect"),
        (ModbusTimeoutError("silent"), "cannot_connect"),
        (ModbusExceptionResponse(2, 0x0206), "modbus_exception"),
        (ModbusProtocolError("transaction"), "modbus_exception"),
    ],
    ids=[
        "product-number-3",
        "category-2",
        "empty-serial",
        "short-serial",
        "lower-case-serial",
        "garbage-serial",
        "refused",
        "silent",
        "exception",
        "protocol",
    ],
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


class _SharedUnit:
    """A unit on the shared connection, answering from the identity blocks."""

    def __init__(self, blocks: dict[int, bytes]) -> None:
        self._blocks = blocks

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        raw = self._blocks[address - MODBUS_BASE_ADDRESS]
        return list(struct.unpack(f">{count}H", raw))


@pytest.fixture
def shared(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Home Assistant's shared connection with a temporary unit that logs its use."""
    state = SimpleNamespace(opened=[], closed=0, refusal=None, blocks=_registers())

    @asynccontextmanager
    async def temporary_unit(hass: Any, params: Any, unit_id: int) -> Any:
        if state.refusal is not None:
            raise state.refusal
        state.opened.append((params.host, params.port, unit_id))
        try:
            yield _SharedUnit(state.blocks)
        finally:
            state.closed += 1

    class LibError(Exception):
        """Stands in for the library's ``ModbusError`` base."""

    names = SimpleNamespace(ModbusError=LibError)
    monkeypatch.setattr(modbus_link, "SHARED_CONNECTION", True)
    monkeypatch.setattr(modbus_link, "mc", names, raising=False)
    monkeypatch.setattr(
        modbus_link, "async_get_temporary_unit", temporary_unit, raising=False
    )
    monkeypatch.setattr(modbus_link, "ModbusTcpParams", SimpleNamespace, raising=False)
    return state


async def test_setup_probes_through_a_temporary_unit_on_the_shared_connection(
    hass: HomeAssistant, shared: SimpleNamespace
) -> None:
    """With the shared connection the flow opens no socket of its own."""
    result = await _start_local_setup(hass)
    with patch(SETUP_CLIENT, side_effect=AssertionError("own client built")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _address(port=1502, unit_id=7)
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICES][0]["sn"] == SERIAL
    assert shared.opened == [(HOST, 1502, 7)]
    assert shared.closed == 1


async def test_reconfigure_probes_through_a_temporary_unit_on_the_shared_connection(
    hass: HomeAssistant, shared: SimpleNamespace
) -> None:
    """The reconfigure probe takes the same route as the setup probe."""
    entry = _cloud_entry(hass)
    result = await _start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_local"}
    )
    with patch(SETUP_CLIENT, side_effect=AssertionError("own client built")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _address(port=1502, unit_id=7)
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "mode_switched"
    assert shared.opened == [(HOST, 1502, 7)]
    assert shared.closed == 1


async def test_a_device_held_with_other_link_settings_is_a_form_error_without_the_host(
    hass: HomeAssistant, shared: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """Home Assistant's refusal reads as an unreachable device and logs no host."""
    shared.refusal = HomeAssistantError(f"{HOST}:502 is held with other settings")
    caplog.set_level(logging.DEBUG, logger="custom_components.ecoflow_energy")
    result = await _start_local_setup(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _address()
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert hass.config_entries.async_entries(DOMAIN) == []
    # Positive control: the refusal was logged, only not with the endpoint.
    assert "refused by the shared connection" in caplog.text
    assert HOST not in caplog.text

    shared.refusal = None
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
    assert result["reason"] == "mode_switched"
    assert entry.entry_id == entry_id
    assert entry.title == "EcoFlow Energy Local (HJ31)"
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
    assert result["reason"] == "mode_switched"
    assert entry.title == "EcoFlow Energy"
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
    assert result["reason"] == "local_updated"
    assert entry.title == "EcoFlow Energy Local (HJ31)"
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
# Collisions: a device or an account another entry already holds
# ===========================================================================


def _extra_entry(hass: HomeAssistant, data: dict[str, Any]) -> MockConfigEntry:
    """A second cloud entry, on its own account, with whatever data it needs."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="EcoFlow Energy", version=CONFIG_VERSION, data=data
    )
    entry.add_to_hass(hass)
    return entry


def _developer_data(devices: list[dict[str, Any]], access_key: str) -> dict[str, Any]:
    return {
        CONF_ACCESS_KEY: access_key,
        CONF_SECRET_KEY: "other_sk",
        CONF_AUTH_METHOD: AUTH_METHOD_DEVELOPER,
        CONF_MODE: MODE_STANDARD,
        CONF_DEVICES: devices,
    }


async def _local_to_standard(
    hass: HomeAssistant, entry: MockConfigEntry, access_key: str = "new_ak"
) -> Any:
    """Run the Local -> Standard switch with an account that lists the device."""
    result = await _start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_to_standard"}
    )
    patcher = _iot_api([{"sn": SERIAL, "productName": "PowerOcean", "online": 1}])
    try:
        return await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ACCESS_KEY: access_key, CONF_SECRET_KEY: "new_sk"},
        )
    finally:
        patcher.stop()


async def test_cloud_setup_refuses_a_device_a_local_entry_already_holds(
    hass: HomeAssistant,
) -> None:
    """Two entries listing one device would emit the same entity ids."""
    local = _local_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODE: MODE_STANDARD}
    )
    patcher = _iot_api(
        [
            {"sn": SERIAL, "productName": "PowerOcean", "online": 1},
            {"sn": OTHER_SERIAL, "productName": "PowerOcean", "online": 1},
        ]
    )
    try:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_KEY: "new_ak", CONF_SECRET_KEY: "new_sk"}
        )
        assert result["step_id"] == "devices"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICES: [SERIAL, OTHER_SERIAL]}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "device_in_other_entry"}
        assert hass.config_entries.async_entries(DOMAIN) == [local]

        # Positive control: leaving the held device out lets the entry through.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICES: [OTHER_SERIAL]}
        )
    finally:
        patcher.stop()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [d["sn"] for d in result["data"][CONF_DEVICES]] == [OTHER_SERIAL]


async def test_options_flow_refuses_a_device_another_entry_holds(
    hass: HomeAssistant,
) -> None:
    """The entry's own devices stay selectable, another entry's do not."""
    _local_entry(hass)  # holds SERIAL
    entry = _cloud_entry(hass, [OTHER_DEVICE])
    with patch(OPTIONS_IOT) as client:
        client.return_value.get_device_list = AsyncMock(
            return_value=[
                {"sn": SERIAL, "productName": "PowerOcean", "online": 1},
                {"sn": OTHER_SERIAL, "productName": "PowerOcean", "online": 1},
            ]
        )
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_MODE: MODE_STANDARD, CONF_DEVICES: [SERIAL, OTHER_SERIAL]},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "device_in_other_entry"}

        # Positive control: the entry's own device alone is accepted.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_MODE: MODE_STANDARD, CONF_DEVICES: [OTHER_SERIAL]},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [d["sn"] for d in entry.data[CONF_DEVICES]] == [OTHER_SERIAL]


async def test_two_cloud_entries_sharing_a_device_still_save_their_options(
    hass: HomeAssistant,
) -> None:
    """Only a Local holder is a collision; a cloud pair predates this mode."""
    _extra_entry(hass, _developer_data([OTHER_DEVICE], "first_ak"))
    entry = _cloud_entry(hass, [OTHER_DEVICE])
    with patch(OPTIONS_IOT) as client:
        client.return_value.get_device_list = AsyncMock(
            return_value=[
                {"sn": OTHER_SERIAL, "productName": "PowerOcean", "online": 1}
            ]
        )
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_MODE: MODE_STANDARD, CONF_DEVICES: [OTHER_SERIAL]},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("route", ["address", "standard"])
async def test_another_entry_holding_the_serial_blocks_a_change_of_a_local_entry(
    hass: HomeAssistant, route: str
) -> None:
    """Changing the address or leaving Local both stop on a foreign holder."""
    entry = _local_entry(hass)
    _extra_entry(hass, _developer_data([CLOUD_DEVICE], "other_ak"))
    before = dict(entry.data)

    if route == "standard":
        result = await _local_to_standard(hass, entry)
    else:
        result = await _start_reconfigure(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "reconfigure_local"}
        )
        with _patch_client(_registers()):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], _address(host="modbus-new.example.test")
            )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert dict(entry.data) == before


@pytest.mark.parametrize("route", ["standard", "enhanced"])
async def test_switching_to_an_account_another_entry_uses_aborts(
    hass: HomeAssistant, route: str
) -> None:
    """A normal setup refuses a known account, and so does the switch."""
    entry = _local_entry(hass)
    before = dict(entry.data)
    if route == "standard":
        _extra_entry(hass, _developer_data([OTHER_DEVICE], "dup_ak"))
        result = await _local_to_standard(hass, entry, access_key="dup_ak")
    else:
        _extra_entry(
            hass,
            {
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "dup@example.test",
                CONF_PASSWORD: "test_password",
                CONF_USER_ID: "u2",
                CONF_DEVICES: [OTHER_DEVICE],
            },
        )
        result = await _start_reconfigure(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "reconfigure_to_enhanced"}
        )
        with (
            patch(
                SETUP_LOGIN,
                AsyncMock(return_value={"token": "t", "user_id": "u1"}),
            ),
            patch(
                SETUP_APP_DEVICES,
                AsyncMock(
                    return_value=[
                        {"sn": SERIAL, "product_name": "PowerOcean", "online": 1}
                    ]
                ),
            ),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "dup@example.test", CONF_PASSWORD: "test_password"},
            )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert dict(entry.data) == before


async def test_local_entry_switches_to_enhanced_with_only_its_own_device(
    hass: HomeAssistant,
) -> None:
    """The second direction back to the cloud: account sign-in, one device."""
    entry = _local_entry(hass)
    result = await _start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_to_enhanced"}
    )
    assert result["step_id"] == "app_credentials"
    with (
        patch(SETUP_LOGIN, AsyncMock(return_value={"token": "t", "user_id": "u1"})),
        patch(
            SETUP_APP_DEVICES,
            AsyncMock(
                return_value=[
                    {"sn": OTHER_SERIAL, "product_name": "PowerOcean", "online": 1},
                    {"sn": SERIAL, "product_name": "PowerOcean", "online": 1},
                ]
            ),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "test_password"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "mode_switched"
    assert entry.title == "EcoFlow Energy"
    assert entry.data[CONF_MODE] == MODE_ENHANCED
    assert entry.data[CONF_AUTH_METHOD] == AUTH_METHOD_APP
    assert entry.data[CONF_EMAIL] == "test@example.com"
    assert entry.data[CONF_USER_ID] == "u1"
    assert [d["sn"] for d in entry.data[CONF_DEVICES]] == [SERIAL]
    for key in (CONF_HOST, CONF_PORT, CONF_UNIT_ID, CONF_ACCESS_KEY):
        assert key not in entry.data


async def test_a_title_the_owner_chose_survives_a_mode_switch(
    hass: HomeAssistant,
) -> None:
    """Only a title the setup steps would have given is replaced."""
    entry = _local_entry(hass)
    hass.config_entries.async_update_entry(entry, title="Cellar PowerOcean")

    result = await _local_to_standard(hass, entry)

    assert result["reason"] == "mode_switched"
    assert entry.data[CONF_MODE] == MODE_STANDARD
    assert entry.title == "Cellar PowerOcean"


# ===========================================================================
# The host field, and the options button
# ===========================================================================


@pytest.mark.parametrize("route", ["setup", "reconfigure"])
@pytest.mark.parametrize(
    "host",
    [
        "http://modbus.example.test",
        "modbus.example.test/status",
        "modbus example.test",
        "   ",
    ],
    ids=["scheme", "path", "space", "blank"],
)
async def test_a_value_that_is_not_a_host_never_reaches_the_device(
    hass: HomeAssistant, route: str, host: str
) -> None:
    """The form refuses it before connecting, and an address still works."""
    if route == "setup":
        result = await _start_local_setup(hass)
    else:
        result = await _start_reconfigure(hass, _local_entry(hass))
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "reconfigure_local"}
        )
    with _patch_client(_registers()) as client:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _address(host=host)
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_host"}
        client.assert_not_called()

        # Positive control: a plain IP address passes the same form.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _address(host="192.0.2.10")
        )
    assert result["type"] is not FlowResultType.FORM
    assert client.call_args.args[0] == "192.0.2.10"


async def test_the_mode_choice_says_local_is_for_the_three_phase_powerocean(
    hass: HomeAssistant,
) -> None:
    """The picker line is what an owner of another model reads first."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    data_schema = result["data_schema"]
    assert data_schema is not None
    schema = data_schema.schema
    choices = next(v for k, v in schema.items() if k == CONF_MODE).container
    assert "three-phase PowerOcean" in choices[MODE_LOCAL]


async def test_the_options_button_is_offered_to_cloud_entries_only(
    hass: HomeAssistant,
) -> None:
    """A Local entry has nothing to change there, so the button stays away."""
    local = _local_entry(hass)
    cloud = _cloud_entry(hass)
    assert EcoFlowEnergyConfigFlow.async_supports_options_flow(local) is False
    assert EcoFlowEnergyConfigFlow.async_supports_options_flow(cloud) is True


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
        "device_in_other_entry",
        "invalid_host",
    ):
        assert config["error"][error]
    for reason in ("mode_switched", "local_updated"):
        assert config["abort"][reason]
    description = config["step"]["user"]["description"]
    assert "three-phase" in description or "dreiphasige" in description


@pytest.mark.parametrize(
    "path",
    [_PACKAGE / "strings.json", _PACKAGE / "translations/en.json"]
    + [_PACKAGE / "translations/de.json"],
    ids=["strings", "en", "de"],
)
def test_options_text_for_a_local_entry_sits_in_the_abort_block(path: Path) -> None:
    """An abort reason nested under `error` is never looked up as an abort."""
    options = json.loads(path.read_text())["options"]
    assert options["abort"]["local_use_reconfigure"]
    assert "abort" not in options["error"]
    assert options["error"]["device_in_other_entry"]
