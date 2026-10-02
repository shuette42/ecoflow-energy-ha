"""Modbus/TCP client for the PowerOcean local interface.

Function code 0x03 (read holding registers) carries every poll. The one write
is function code 0x06 (write single register), and it is guarded:
``write_register`` refuses any offset outside ``WRITABLE_OFFSETS`` before a
socket is opened, and it accepts the device's answer only as an exact echo of
the request. One connection carries all requests of a poll or one write, every
network step runs under a timeout and the whole poll under a shared budget,
failures are typed, and there is no retry here (the caller owns the retry
policy). The device serves one client at a time, so a lock per client instance
keeps a poll, a heartbeat beat and a write from holding two sockets at once.
No Home Assistant imports: this is core library code.

Register offsets from the device's protocol table are added to
``MODBUS_BASE_ADDRESS`` on the wire (measured: without it the device answers
exception 0x02). Multi-register values are word-swapped: 0x12345678 is sent as
the words 5678 1234.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import Sequence

MODBUS_BASE_ADDRESS = 40001
MODBUS_DEFAULT_PORT = 502
READ_HOLDING_REGISTERS = 0x03
WRITE_SINGLE_REGISTER = 0x06
MAX_REGISTERS_PER_READ = 125

# The only registers this client writes: the control heartbeat, the Backup
# Reserve ratio and the indicator brightness. Anything else is refused before a
# socket opens, whatever the entity layer asks for.
HEARTBEAT_OFFSET = 0x025F
BACKUP_RATIO_OFFSET = 0x0217
BRIGHTNESS_OFFSET = 0x021C
WRITABLE_OFFSETS = frozenset({HEARTBEAT_OFFSET, BACKUP_RATIO_OFFSET, BRIGHTNESS_OFFSET})

_EXCEPTION_FLAG = 0x80
_MBAP_LEN = 7
_MAX_PDU_LEN = 253

# A poll is up to nine requests on one connection. Each has its own timeout, so
# a device that answers every request just inside it could hold the poll for
# nine times that. The whole request loop shares one budget of this many
# single timeouts: room for a slow but working device, well short of the sum.
_POLL_BUDGET_FACTOR = 4


class ModbusLocalError(Exception):
    """Base class for every failure of the local Modbus client."""


class ModbusConnectError(ModbusLocalError):
    """The TCP connection could not be made or broke (refused, DNS, reset)."""


class ModbusTimeoutError(ModbusLocalError):
    """Connected, but no answer within the timeout (a second client is held off)."""


class ModbusExceptionResponse(ModbusLocalError):
    """The device answered with a Modbus exception (function 0x83 or 0x86)."""

    def __init__(self, code: int, offset: int) -> None:
        super().__init__(
            f"Modbus exception response, code {code}, block at offset {offset:#06x}"
        )
        self.code = code
        self.offset = offset


class ModbusProtocolError(ModbusLocalError):
    """The answer does not match the request (transaction, unit, length, echo)."""


def decode_u16(raw: bytes) -> int:
    """Decode one register as an unsigned 16-bit integer."""
    return int(struct.unpack(">H", raw[0:2])[0])


def decode_u32_ws(raw: bytes) -> int:
    """Decode two word-swapped registers as an unsigned 32-bit integer."""
    return int(struct.unpack(">I", raw[2:4] + raw[0:2])[0])


def decode_i32_ws(raw: bytes) -> int:
    """Decode two word-swapped registers as a signed 32-bit integer."""
    return int(struct.unpack(">i", raw[2:4] + raw[0:2])[0])


def decode_f32_ws(raw: bytes) -> float:
    """Decode two word-swapped registers as an IEEE 754 single-precision float."""
    return float(struct.unpack(">f", raw[2:4] + raw[0:2])[0])


def decode_ascii(raw: bytes) -> str:
    """Decode registers as text, bytes in wire order, NUL and blanks stripped."""
    return raw.decode("ascii", errors="replace").replace("\x00", "").strip()


class ModbusLocalClient:
    """Asyncio Modbus/TCP client: polls holding registers, writes a guarded few."""

    def __init__(
        self,
        host: str,
        port: int = MODBUS_DEFAULT_PORT,
        unit_id: int = 1,
        timeout: float = 3.0,
    ) -> None:
        self._host = host
        self._port = port
        self._unit_id = unit_id
        self._timeout = timeout
        self._transaction_id = 0
        # Shared state: one lock per client, held for a whole connection, so
        # a poll, a heartbeat and a write never hold two sockets at once.
        self._lock = asyncio.Lock()

    async def read_blocks(self, blocks: Sequence[tuple[int, int]]) -> dict[int, bytes]:
        """Read ``(offset, register_count)`` blocks over one connection.

        Returns ``{offset: raw bytes}`` with two bytes per register, in wire
        order. Raises a ``ModbusLocalError`` subclass on any failure.
        """
        for offset, count in blocks:
            if not 0 <= offset <= 0xFFFF - MODBUS_BASE_ADDRESS:
                raise ValueError(f"register offset out of range: {offset:#06x}")
            if not 1 <= count <= MAX_REGISTERS_PER_READ:
                raise ValueError(f"register count out of range: {count}")

        async with self._lock:
            reader, writer = await self._connect()
            try:
                budget = self._timeout * _POLL_BUDGET_FACTOR
                result: dict[int, bytes] = {}
                try:
                    async with asyncio.timeout(budget):
                        for offset, count in blocks:
                            result[offset] = await self._read(
                                reader, writer, offset, count
                            )
                except TimeoutError as err:
                    # Only the shared budget lands here: a single request's own
                    # timeout is already a ModbusTimeoutError inside ``_read``.
                    raise ModbusTimeoutError(
                        f"poll exceeded its {budget:.1f}s budget"
                    ) from err
                return result
            finally:
                await self._close(writer)

    async def write_register(self, offset: int, value: int) -> None:
        """Write one holding register (function 0x06) and check the echo.

        Only offsets in ``WRITABLE_OFFSETS`` are accepted, and a refusal
        (``ValueError``) happens before any socket is opened. The device
        acknowledges a write with an exact echo of the request; any other
        answer is a ``ModbusProtocolError``. Range limits of the value beyond
        the 16-bit register belong to the entity layer. Raises a
        ``ModbusLocalError`` subclass on any failure.
        """
        if offset not in WRITABLE_OFFSETS:
            raise ValueError(f"register offset {offset:#06x} is not writable")
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"register value out of range: {value}")

        async with self._lock:
            reader, writer = await self._connect()
            try:
                await self._write(reader, writer, offset, value)
            finally:
                await self._close(writer)

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            async with asyncio.timeout(self._timeout):
                return await asyncio.open_connection(self._host, self._port)
        except TimeoutError as err:
            raise ModbusConnectError(
                f"connect timed out after {self._timeout:.1f}s"
            ) from err
        except OSError as err:
            raise ModbusConnectError(f"connect failed: {err}") from err

    def _next_transaction_id(self) -> int:
        self._transaction_id = self._transaction_id % 0xFFFF + 1
        return self._transaction_id

    async def _exchange(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        tid: int,
        request: bytes,
    ) -> bytes:
        """Send one request frame and return the PDU of its answer."""
        try:
            async with asyncio.timeout(self._timeout):
                writer.write(request)
                await writer.drain()
                header = await reader.readexactly(_MBAP_LEN)
                r_tid, r_protocol, r_length, r_unit = struct.unpack(">HHHB", header)
                if r_tid != tid:
                    raise ModbusProtocolError(
                        f"transaction id mismatch: sent {tid}, got {r_tid}"
                    )
                if r_protocol != 0:
                    raise ModbusProtocolError(f"protocol id {r_protocol}, expected 0")
                if r_unit != self._unit_id:
                    raise ModbusProtocolError(
                        f"unit id mismatch: sent {self._unit_id}, got {r_unit}"
                    )
                if not 3 <= r_length <= _MAX_PDU_LEN + 1:
                    raise ModbusProtocolError(f"implausible length field {r_length}")
                return bytes(await reader.readexactly(r_length - 1))
        except TimeoutError as err:
            raise ModbusTimeoutError(f"no answer within {self._timeout:.1f}s") from err
        except asyncio.IncompleteReadError as err:
            raise ModbusProtocolError("connection closed mid-frame") from err
        except OSError as err:
            raise ModbusConnectError(f"connection lost: {err}") from err

    async def _read(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        offset: int,
        count: int,
    ) -> bytes:
        tid = self._next_transaction_id()
        request = struct.pack(
            ">HHHBBHH",
            tid,
            0,
            6,
            self._unit_id,
            READ_HOLDING_REGISTERS,
            MODBUS_BASE_ADDRESS + offset,
            count,
        )
        pdu = await self._exchange(reader, writer, tid, request)

        function = pdu[0]
        if function == READ_HOLDING_REGISTERS | _EXCEPTION_FLAG:
            raise ModbusExceptionResponse(pdu[1], offset)
        if function != READ_HOLDING_REGISTERS:
            raise ModbusProtocolError(f"unexpected function code {function:#04x}")
        if pdu[1] != 2 * count or len(pdu) != 2 + 2 * count:
            raise ModbusProtocolError(
                f"byte count {pdu[1]} does not match {count} registers"
            )
        return bytes(pdu[2:])

    async def _write(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        offset: int,
        value: int,
    ) -> None:
        tid = self._next_transaction_id()
        address = MODBUS_BASE_ADDRESS + offset
        request = struct.pack(
            ">HHHBBHH",
            tid,
            0,
            6,
            self._unit_id,
            WRITE_SINGLE_REGISTER,
            address,
            value,
        )
        pdu = await self._exchange(reader, writer, tid, request)

        function = pdu[0]
        if function == WRITE_SINGLE_REGISTER | _EXCEPTION_FLAG:
            raise ModbusExceptionResponse(pdu[1], offset)
        if function != WRITE_SINGLE_REGISTER:
            raise ModbusProtocolError(f"unexpected function code {function:#04x}")
        # The device acknowledges a write by echoing function, address and
        # value; a different echo means the write did not land as asked.
        if pdu != struct.pack(">BHH", WRITE_SINGLE_REGISTER, address, value):
            raise ModbusProtocolError(
                f"write echo does not match the request at offset {offset:#06x}"
            )

    async def _close(self, writer: asyncio.StreamWriter) -> None:
        writer.close()
        with contextlib.suppress(OSError):
            async with asyncio.timeout(self._timeout):
                await writer.wait_closed()
