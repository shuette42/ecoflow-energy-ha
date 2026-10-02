"""Connection adapter for the local Modbus coordinator.

Home Assistant 2026.9 added a shared Modbus connection to its ``modbus``
integration: one connection per device endpoint, used by every integration that
asks for the same endpoint. A PowerOcean serves one Modbus client at a time, so
a second socket opened beside that connection would be exactly the second client
the device does not answer. Whether the shared connection exists is therefore
decided once, when this module is imported, and never revisited: an entry either
takes a unit on the shared connection or uses the integration's own client.
There is no switch at runtime and no fallback from one to the other.

``SharedModbusLink`` presents a shared unit through the same two calls as the
own client (``ModbusTransport``) and maps the library's errors onto the
``ecoflow.modbus_local`` family, so the coordinator handles one error type.
The shared connection belongs to the ``modbus`` integration: this adapter never
disconnects or closes it.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Sequence
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import LOCAL_MODBUS_TIMEOUT_S
from ..ecoflow.modbus_local import (
    MODBUS_BASE_ADDRESS,
    WRITABLE_OFFSETS,
    ModbusConnectError,
    ModbusExceptionResponse,
    ModbusLocalClient,
    ModbusLocalError,
    ModbusProtocolError,
    ModbusTimeoutError,
    ModbusTransport,
)

# The library names are imported under an alias: three of them share a name
# with the ``ecoflow.modbus_local`` family the adapter maps them onto.
try:
    import modbus_connection as mc
    from homeassistant.components.modbus import (  # type: ignore[attr-defined,unused-ignore]
        async_get_temporary_unit,
        async_get_unit,
    )
    from modbus_connection import ModbusTcpParams

    SHARED_CONNECTION = True
    SHARED_CONNECTION_REASON = ""
except ImportError as err:
    SHARED_CONNECTION = False
    SHARED_CONNECTION_REASON = str(err)

# One budget for the whole call sequence, as in the own client: a few single
# timeouts, well short of the sum of one per request.
_BUDGET_S = LOCAL_MODBUS_TIMEOUT_S * 4


def _translate(err: BaseException, offset: int) -> ModbusLocalError:
    """Map a library error, or the budget timeout, onto the local family.

    Order matters: the library's own timeout is tested before the plain
    ``TimeoutError`` of the budget, which a library timeout may also match.
    """
    if isinstance(err, mc.ModbusTimeoutError):
        return ModbusTimeoutError("the shared Modbus connection timed out")
    if isinstance(err, TimeoutError):
        return ModbusTimeoutError(f"request exceeded its {_BUDGET_S:.1f}s budget")
    if isinstance(err, mc.ModbusConnectionError):
        return ModbusConnectError(f"shared Modbus connection failed: {err}")
    if isinstance(err, mc.ModbusExceptionError):
        return ModbusExceptionResponse(int(err.exception_code or 0), offset)
    return ModbusProtocolError(f"shared Modbus connection: {err}")


def _os_error(err: OSError) -> ModbusLocalError:
    """Map a raw socket error that escaped the library onto the local family.

    Only the type is named: the text of a socket error may carry the device
    address. Reached after the clause for the library's errors and for the
    budget timeout, because ``TimeoutError`` is itself an ``OSError``.
    """
    return ModbusConnectError(f"shared Modbus connection failed: {type(err).__name__}")


class SharedModbusLink:
    """``ModbusTransport`` on a unit of Home Assistant's shared connection."""

    def __init__(self, unit: Any) -> None:
        self._unit = unit
        # One call sequence at a time, so a poll, a beat and a write never
        # interleave their requests on the shared connection.
        self._lock = asyncio.Lock()

    async def read_blocks(self, blocks: Sequence[tuple[int, int]]) -> dict[int, bytes]:
        """Read ``(offset, register_count)`` blocks; bytes as the own client returns."""
        result: dict[int, bytes] = {}
        offset = -1
        async with self._lock:
            try:
                async with asyncio.timeout(_BUDGET_S):
                    for offset, count in blocks:
                        words = await self._unit.read_holding_registers(
                            MODBUS_BASE_ADDRESS + offset, count
                        )
                        if len(words) != count:
                            raise ModbusProtocolError(
                                f"{len(words)} registers answered for {count} "
                                f"at offset {offset:#06x}"
                            )
                        result[offset] = struct.pack(f">{len(words)}H", *words)
            except (mc.ModbusError, TimeoutError) as err:
                raise _translate(err, offset) from err
            except OSError as err:
                raise _os_error(err) from err
        return result

    async def write_register(self, offset: int, value: int) -> None:
        """Write one holding register (function 0x06); only allowlisted offsets."""
        if offset not in WRITABLE_OFFSETS:
            raise ValueError(f"register offset {offset:#06x} is not writable")
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"register value out of range: {value}")
        async with self._lock:
            try:
                async with asyncio.timeout(_BUDGET_S):
                    await self._unit.write_register(MODBUS_BASE_ADDRESS + offset, value)
            except (mc.ModbusError, TimeoutError) as err:
                raise _translate(err, offset) from err
            except OSError as err:
                raise _os_error(err) from err


def create_link(
    hass: HomeAssistant,
    entry: ConfigEntry,
    host: str,
    port: int,
    unit_id: int,
) -> ModbusTransport:
    """Return the link this process uses for a local Modbus entry.

    A ``HomeAssistantError`` from Home Assistant (the same device already held
    with different link settings) propagates: setup fails with that reason.
    """
    if SHARED_CONNECTION:
        unit = async_get_unit(
            hass, entry, ModbusTcpParams(host=host, port=port), unit_id
        )
        return SharedModbusLink(unit)
    return ModbusLocalClient(host, port, unit_id, timeout=LOCAL_MODBUS_TIMEOUT_S)


async def read_setup_blocks(
    hass: HomeAssistant,
    host: str,
    port: int,
    unit_id: int,
    blocks: Sequence[tuple[int, int]],
) -> dict[int, bytes]:
    """Read ``(offset, register_count)`` blocks for a flow with no config entry.

    A device that serves one client refuses a socket of its own while another
    integration holds it on the shared connection, so on that connection the
    probe takes a temporary unit instead: a connection an entry already holds
    is shared and stays up, one opened here is closed on exit. The reads go
    through ``SharedModbusLink``, so the words are packed and the errors mapped
    exactly as in a running entry. A ``HomeAssistantError`` (the device is held
    with other link settings) propagates for the caller to report.
    """
    if not SHARED_CONNECTION:
        client = ModbusLocalClient(host, port, unit_id, timeout=LOCAL_MODBUS_TIMEOUT_S)
        return await client.read_blocks(blocks)
    try:
        async with async_get_temporary_unit(
            hass, ModbusTcpParams(host=host, port=port), unit_id
        ) as unit:
            return await SharedModbusLink(unit).read_blocks(blocks)
    except mc.ModbusError as err:
        # Only what happens outside the reads reaches here (opening or closing
        # the temporary connection); the reads map their own errors.
        raise _translate(err, 0) from err
    except OSError as err:
        raise _os_error(err) from err
