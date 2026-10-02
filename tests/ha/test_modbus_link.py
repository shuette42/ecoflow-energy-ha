"""The adapter onto Home Assistant's shared Modbus connection.

Local Home Assistant has no ``modbus_connection`` package and no
``async_get_unit``, so the library names the adapter uses are replaced by fakes
on the module. The fake unit records every call, which is what lets these tests
pin the address arithmetic, the allowlist, the error mapping, the serialisation
and the rule that the adapter never closes a connection that is not its own.
"""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.ecoflow_energy.coordinator import modbus_link
from custom_components.ecoflow_energy.coordinator.modbus_link import (
    SharedModbusLink,
    create_link,
    read_setup_blocks,
)
from custom_components.ecoflow_energy.ecoflow.modbus_local import (
    BACKUP_RATIO_OFFSET,
    HEARTBEAT_OFFSET,
    MODBUS_BASE_ADDRESS,
    ModbusConnectError,
    ModbusExceptionResponse,
    ModbusLocalClient,
    ModbusLocalError,
    ModbusProtocolError,
    ModbusTimeoutError,
    ModbusTransport,
    decode_f32_ws,
    decode_u16,
)


class LibError(Exception):
    """Stands in for the library's ``ModbusError`` base."""


class LibTimeout(LibError, TimeoutError):
    """The library's timeout. It is also a ``TimeoutError``, the awkward case."""


class LibConnection(LibError):
    """Stands in for ``ModbusConnectionError``."""


class LibProtocol(LibError):
    """Stands in for ``ModbusProtocolError``."""


class LibException(LibError):
    """Stands in for ``ModbusExceptionError``."""

    def __init__(self, exception_code: int | None) -> None:
        super().__init__(f"exception {exception_code}")
        self.exception_code = exception_code


class FakeUnit:
    """A unit that records its calls and answers from a register table."""

    def __init__(self, registers: dict[int, int] | None = None) -> None:
        self.registers = registers or {}
        self.calls: list[tuple[str, int, int]] = []
        # Call index (over reads and writes together) -> error to raise.
        self.errors: dict[int, Exception] = {}
        self.delay = 0.0
        self.active = 0
        self.max_active = 0
        # Anything the adapter must never touch: the connection is not its own.
        self.forbidden: list[str] = []

    async def _step(self, index: int) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if index in self.errors:
                raise self.errors[index]
        finally:
            self.active -= 1

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        index = len(self.calls)
        self.calls.append(("read", address, count))
        await self._step(index)
        return [self.registers.get(address + i, 0) for i in range(count)]

    async def write_register(self, address: int, value: int) -> None:
        index = len(self.calls)
        self.calls.append(("write", address, value))
        await self._step(index)

    async def disconnect(self) -> None:
        self.forbidden.append("disconnect")

    async def close(self) -> None:
        self.forbidden.append("close")


@pytest.fixture
def lib(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Put the fake library on the module, whether or not the real one imported."""
    names = SimpleNamespace(
        ModbusError=LibError,
        ModbusTimeoutError=LibTimeout,
        ModbusConnectionError=LibConnection,
        ModbusProtocolError=LibProtocol,
        ModbusExceptionError=LibException,
    )
    monkeypatch.setattr(modbus_link, "mc", names, raising=False)
    return names


def _word_swapped_f32(value: float) -> list[int]:
    """The two registers a device sends for ``value``: low word first."""
    high, low = struct.unpack(">HH", struct.pack(">f", value))
    return [low, high]


async def test_read_blocks_packs_the_words_and_the_real_decoders_read_them(
    lib: SimpleNamespace,
) -> None:
    low, high = _word_swapped_f32(12.5)
    unit = FakeUnit(
        {MODBUS_BASE_ADDRESS + 0x0206: low, MODBUS_BASE_ADDRESS + 0x0207: high}
    )
    unit.registers[MODBUS_BASE_ADDRESS + 0x020E] = 77
    link = SharedModbusLink(unit)

    result = await link.read_blocks([(0x0206, 2), (0x020E, 1)])

    assert unit.calls == [
        ("read", MODBUS_BASE_ADDRESS + 0x0206, 2),
        ("read", MODBUS_BASE_ADDRESS + 0x020E, 1),
    ]
    assert decode_f32_ws(result[0x0206]) == 12.5
    assert decode_u16(result[0x020E]) == 77
    assert isinstance(link, ModbusTransport)


async def test_a_short_answer_is_a_protocol_error_not_short_bytes(
    lib: SimpleNamespace,
) -> None:
    unit = FakeUnit()

    async def _short(address: int, count: int) -> list[int]:
        return [0] * (count - 1)

    unit.read_holding_registers = _short  # type: ignore[method-assign]

    with pytest.raises(ModbusProtocolError, match="registers answered"):
        await SharedModbusLink(unit).read_blocks([(0x0206, 2)])


@pytest.mark.parametrize("offset", [0x021D, 0x021F, 0x023A, 0x022D, 0x0215, 0x0000])
async def test_a_write_outside_the_allowlist_is_refused_before_the_unit_is_called(
    lib: SimpleNamespace, offset: int
) -> None:
    unit = FakeUnit()
    link = SharedModbusLink(unit)

    with pytest.raises(ValueError, match="not writable"):
        await link.write_register(offset, 1)

    assert unit.calls == []


async def test_an_allowlisted_write_reaches_the_unit_at_the_wire_address(
    lib: SimpleNamespace,
) -> None:
    """The control for the refusal above: the same call passes for a real offset."""
    unit = FakeUnit()
    link = SharedModbusLink(unit)

    await link.write_register(HEARTBEAT_OFFSET, 1)
    await link.write_register(BACKUP_RATIO_OFFSET, 30)

    assert unit.calls == [
        ("write", MODBUS_BASE_ADDRESS + HEARTBEAT_OFFSET, 1),
        ("write", MODBUS_BASE_ADDRESS + BACKUP_RATIO_OFFSET, 30),
    ]


@pytest.mark.parametrize("value", [-1, 0x10000])
async def test_a_value_outside_sixteen_bits_is_refused_before_the_unit_is_called(
    lib: SimpleNamespace, value: int
) -> None:
    unit = FakeUnit()

    with pytest.raises(ValueError, match="out of range"):
        await SharedModbusLink(unit).write_register(BACKUP_RATIO_OFFSET, value)

    assert unit.calls == []


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (LibConnection("refused"), ModbusConnectError),
        (LibProtocol("bad frame"), ModbusProtocolError),
        (LibError("something else"), ModbusProtocolError),
        (LibException(2), ModbusExceptionResponse),
    ],
)
@pytest.mark.parametrize("operation", ["read", "write"])
async def test_each_library_error_maps_onto_the_local_family(
    lib: SimpleNamespace,
    raised: Exception,
    expected: type[ModbusLocalError],
    operation: str,
) -> None:
    unit = FakeUnit()
    unit.errors[0] = raised
    link = SharedModbusLink(unit)

    with pytest.raises(ModbusLocalError) as caught:
        if operation == "read":
            await link.read_blocks([(0x0206, 2)])
        else:
            await link.write_register(HEARTBEAT_OFFSET, 1)

    assert type(caught.value) is expected
    assert caught.value.__cause__ is raised


async def test_an_exception_response_keeps_its_code_and_names_the_failing_block(
    lib: SimpleNamespace,
) -> None:
    unit = FakeUnit()
    unit.errors[1] = LibException(2)

    with pytest.raises(ModbusExceptionResponse) as caught:
        await SharedModbusLink(unit).read_blocks([(0x0206, 2), (0x0217, 18)])

    assert caught.value.code == 2
    assert caught.value.offset == 0x0217


async def test_an_exception_without_a_code_maps_to_code_zero(
    lib: SimpleNamespace,
) -> None:
    unit = FakeUnit()
    unit.errors[0] = LibException(None)

    with pytest.raises(ModbusExceptionResponse) as caught:
        await SharedModbusLink(unit).write_register(HEARTBEAT_OFFSET, 1)

    assert caught.value.code == 0


async def test_the_library_timeout_is_not_mistaken_for_the_poll_budget(
    lib: SimpleNamespace,
) -> None:
    """The library's timeout is also a TimeoutError; it must not read as the budget."""
    unit = FakeUnit()
    unit.errors[0] = LibTimeout("no answer")

    with pytest.raises(ModbusTimeoutError) as caught:
        await SharedModbusLink(unit).read_blocks([(0x0206, 2)])

    assert type(caught.value) is ModbusTimeoutError
    assert "budget" not in str(caught.value)
    assert isinstance(caught.value.__cause__, LibTimeout)


@pytest.mark.parametrize("operation", ["read", "write"])
async def test_a_call_that_outlasts_the_budget_is_our_timeout(
    lib: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setattr(modbus_link, "_BUDGET_S", 0.05)
    unit = FakeUnit()
    unit.delay = 5.0
    link = SharedModbusLink(unit)

    with pytest.raises(ModbusTimeoutError, match="budget"):
        if operation == "read":
            await link.read_blocks([(0x0206, 2)])
        else:
            await link.write_register(HEARTBEAT_OFFSET, 1)


async def test_two_concurrent_calls_on_one_link_never_overlap(
    lib: SimpleNamespace,
) -> None:
    unit = FakeUnit()
    unit.delay = 0.02
    link = SharedModbusLink(unit)

    await asyncio.gather(
        link.read_blocks([(0x0206, 2)]),
        link.write_register(HEARTBEAT_OFFSET, 1),
        link.read_blocks([(0x020E, 1)]),
    )

    assert unit.max_active == 1
    assert len(unit.calls) == 3


async def test_the_overlap_probe_sees_two_links_on_one_unit(
    lib: SimpleNamespace,
) -> None:
    """Control for the test above: without one lock per link the probe reads 2."""
    unit = FakeUnit()
    unit.delay = 0.02

    await asyncio.gather(
        SharedModbusLink(unit).read_blocks([(0x0206, 2)]),
        SharedModbusLink(unit).read_blocks([(0x020E, 1)]),
    )

    assert unit.max_active == 2


async def test_the_adapter_never_disconnects_or_closes_the_shared_connection(
    lib: SimpleNamespace,
) -> None:
    unit = FakeUnit()
    link = SharedModbusLink(unit)

    await link.read_blocks([(0x0206, 2)])
    await link.write_register(HEARTBEAT_OFFSET, 1)
    unit.errors[2] = LibConnection("dropped")
    with pytest.raises(ModbusConnectError):
        await link.read_blocks([(0x0206, 2)])
    unit.errors[3] = LibTimeout("slow")
    with pytest.raises(ModbusTimeoutError):
        await link.write_register(HEARTBEAT_OFFSET, 1)

    assert unit.forbidden == []
    assert len(unit.calls) == 4


def _fake_get_unit(
    monkeypatch: pytest.MonkeyPatch, unit: FakeUnit | Exception
) -> list[tuple[Any, ...]]:
    """Replace ``async_get_unit`` and ``ModbusTcpParams``; return the recorded calls."""
    calls: list[tuple[Any, ...]] = []

    def get_unit(hass: Any, entry: Any, params: Any, unit_id: int) -> FakeUnit:
        calls.append((hass, entry, params, unit_id))
        if isinstance(unit, Exception):
            raise unit
        return unit

    def params(**kwargs: Any) -> SimpleNamespace:
        if set(kwargs) != {"host", "port"}:
            raise TypeError("ModbusTcpParams takes host and port by keyword")
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(modbus_link, "async_get_unit", get_unit, raising=False)
    monkeypatch.setattr(modbus_link, "ModbusTcpParams", params, raising=False)
    return calls


def test_create_link_takes_a_unit_on_the_shared_connection_when_it_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = FakeUnit()
    calls = _fake_get_unit(monkeypatch, unit)
    monkeypatch.setattr(modbus_link, "SHARED_CONNECTION", True)
    hass, entry = MagicMock(), MagicMock()

    link = create_link(hass, entry, "modbus.example.test", 1502, 3)

    assert isinstance(link, SharedModbusLink)
    assert link._unit is unit
    assert len(calls) == 1
    got_hass, got_entry, params, unit_id = calls[0]
    assert (got_hass, got_entry, unit_id) == (hass, entry, 3)
    assert (params.host, params.port) == ("modbus.example.test", 1502)


def test_create_link_uses_the_own_client_when_the_shared_connection_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_get_unit(monkeypatch, FakeUnit())
    monkeypatch.setattr(modbus_link, "SHARED_CONNECTION", False)

    link = create_link(MagicMock(), MagicMock(), "modbus.example.test", 502, 1)

    assert isinstance(link, ModbusLocalClient)
    assert isinstance(link, ModbusTransport)
    assert calls == []


def test_a_refused_unit_fails_the_setup_and_never_falls_back_to_a_second_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_get_unit(monkeypatch, HomeAssistantError("held with other link settings"))
    monkeypatch.setattr(modbus_link, "SHARED_CONNECTION", True)

    with pytest.raises(HomeAssistantError, match="other link settings"):
        create_link(MagicMock(), MagicMock(), "modbus.example.test", 502, 1)


class _TemporaryUnit:
    """Stands in for ``async_get_temporary_unit``: logs the open and the close."""

    def __init__(self, unit: FakeUnit, enter_error: Exception | None = None) -> None:
        self.unit = unit
        self.enter_error = enter_error
        self.opened: list[tuple[str, int, int]] = []
        self.closed = 0

    def __call__(self, hass: Any, params: Any, unit_id: int) -> _TemporaryUnit:
        self.opened.append((params.host, params.port, unit_id))
        return self

    async def __aenter__(self) -> FakeUnit:
        if self.enter_error is not None:
            raise self.enter_error
        return self.unit

    async def __aexit__(self, *exc: object) -> None:
        self.closed += 1


def _fake_temporary_unit(
    monkeypatch: pytest.MonkeyPatch, temporary: _TemporaryUnit
) -> None:
    monkeypatch.setattr(
        modbus_link, "async_get_temporary_unit", temporary, raising=False
    )
    monkeypatch.setattr(modbus_link, "ModbusTcpParams", SimpleNamespace, raising=False)
    monkeypatch.setattr(modbus_link, "SHARED_CONNECTION", True)


async def test_setup_blocks_are_read_on_a_temporary_unit_and_packed_like_a_poll(
    lib: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = FakeUnit({MODBUS_BASE_ADDRESS + 3: 0x4849, MODBUS_BASE_ADDRESS + 4: 0x3331})
    temporary = _TemporaryUnit(unit)
    _fake_temporary_unit(monkeypatch, temporary)

    blocks = await read_setup_blocks(
        MagicMock(), "modbus.example.test", 1502, 3, [(3, 2)]
    )

    assert blocks == {3: b"HI31"}
    assert temporary.opened == [("modbus.example.test", 1502, 3)]
    assert temporary.closed == 1
    assert unit.calls == [("read", MODBUS_BASE_ADDRESS + 3, 2)]


async def test_an_error_while_opening_the_temporary_unit_maps_onto_the_local_family(
    lib: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = LibConnection("connection refused")
    _fake_temporary_unit(monkeypatch, _TemporaryUnit(FakeUnit(), refused))

    with pytest.raises(ModbusConnectError) as caught:
        await read_setup_blocks(MagicMock(), "modbus.example.test", 502, 1, [(0, 3)])

    assert caught.value.__cause__ is refused


async def test_a_refusal_by_home_assistant_is_not_mapped_and_not_retried(
    lib: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = HomeAssistantError("held with other link settings")
    temporary = _TemporaryUnit(FakeUnit(), refused)
    _fake_temporary_unit(monkeypatch, temporary)

    with pytest.raises(HomeAssistantError, match="other link settings"):
        await read_setup_blocks(MagicMock(), "modbus.example.test", 502, 1, [(0, 3)])

    assert len(temporary.opened) == 1


async def test_setup_blocks_use_the_own_client_without_the_shared_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = _TemporaryUnit(FakeUnit())
    _fake_temporary_unit(monkeypatch, temporary)
    monkeypatch.setattr(modbus_link, "SHARED_CONNECTION", False)
    built: list[tuple[Any, ...]] = []

    class OwnClient:
        def __init__(self, host: str, port: int, unit_id: int, timeout: float) -> None:
            built.append((host, port, unit_id))

        async def read_blocks(self, blocks: Any) -> dict[int, bytes]:
            return {0: b"\x00\x01"}

    monkeypatch.setattr(modbus_link, "ModbusLocalClient", OwnClient)

    blocks = await read_setup_blocks(
        MagicMock(), "modbus.example.test", 502, 1, [(0, 1)]
    )

    assert blocks == {0: b"\x00\x01"}
    assert built == [("modbus.example.test", 502, 1)]
    assert temporary.opened == []
