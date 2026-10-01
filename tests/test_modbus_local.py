"""Tests for the read-only Modbus/TCP client and its register decoders."""

import asyncio
import contextlib
import socket
import struct
import time

import pytest
from ecoflow_energy.ecoflow.modbus_local import (
    ModbusConnectError,
    ModbusExceptionResponse,
    ModbusLocalClient,
    ModbusProtocolError,
    ModbusTimeoutError,
    decode_ascii,
    decode_f32_ws,
    decode_i32_ws,
    decode_u16,
    decode_u32_ws,
)

# The HA test harness blocks sockets; these tests talk to a fake device on loopback.
pytestmark = pytest.mark.usefixtures("socket_enabled")

_REQUEST_LEN = 12  # MBAP (7) + function (1) + address (2) + count (2)


@contextlib.asynccontextmanager
async def _serve(handler):
    """Run a fake Modbus device on a free loopback port and yield the port."""
    writers = []
    handlers = []

    async def on_connect(reader, writer):
        writers.append(writer)
        handlers.append(asyncio.current_task())
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            await handler(reader, writer)

    server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        for writer in writers:
            writer.close()
        # A handler may still sleep when the client gave up; end it here so
        # no task outlives the test.
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        server.close()
        await asyncio.wait_for(server.wait_closed(), 2)


def _unpack_request(request):
    return struct.unpack(">HHHBBHH", request)


def _register_bytes(address, count):
    """Register i of a block answers its own wire address, so slicing is checkable."""
    return b"".join(struct.pack(">H", address + i) for i in range(count))


async def _answer_reads(reader, writer, seen):
    while True:
        request = await reader.readexactly(_REQUEST_LEN)
        tid, _proto, _length, unit, function, address, count = _unpack_request(request)
        seen.append((unit, function, address, count))
        data = _register_bytes(address, count)
        header = struct.pack(
            ">HHHBBB", tid, 0, 3 + len(data), unit, function, len(data)
        )
        writer.write(header + data)
        await writer.drain()


def _closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_u32_and_i32_swap_the_two_words():
    # 0x12345678 travels as the words 5678 1234
    assert decode_u32_ws(bytes.fromhex("56781234")) == 0x12345678
    # 0x00010000 travels as 0000 0001
    assert decode_u32_ws(bytes.fromhex("00000001")) == 0x00010000
    # -2 is 0xFFFFFFFE, sent as FFFE FFFF; read unswapped it would be -65537
    assert decode_i32_ws(bytes.fromhex("fffeffff")) == -2


def test_f32_swaps_the_two_words():
    # struct.pack(">f", 49.99).hex() == "4247f5c3"; on the wire the words trade places
    assert decode_f32_ws(bytes.fromhex("f5c34247")) == pytest.approx(49.99, abs=1e-3)


def test_u16_is_big_endian_and_ascii_drops_padding():
    assert decode_u16(bytes.fromhex("0102")) == 0x0102
    assert decode_ascii(b"HJ31\x00\x00  ") == "HJ31"


@pytest.mark.asyncio
async def test_read_blocks_sends_function_03_at_base_plus_offset_on_one_connection():
    seen: list[tuple[int, int, int, int]] = []
    connections = []

    async def handler(reader, writer):
        connections.append(writer)
        await _answer_reads(reader, writer, seen)

    async with _serve(handler) as port:
        client = ModbusLocalClient("127.0.0.1", port, unit_id=1, timeout=2.0)
        result = await client.read_blocks([(0x0206, 9), (0x0251, 12)])

    # 40001 + 0x0206 = 40519, 40001 + 0x0251 = 40594
    assert seen == [(1, 0x03, 40519, 9), (1, 0x03, 40594, 12)]
    assert all(function == 0x03 for _unit, function, _address, _count in seen)
    assert len(connections) == 1
    assert result == {
        0x0206: _register_bytes(40519, 9),
        0x0251: _register_bytes(40594, 12),
    }


@pytest.mark.asyncio
async def test_exception_response_carries_the_code():
    async def handler(reader, writer):
        request = await reader.readexactly(_REQUEST_LEN)
        tid, _proto, _length, unit, _function, _address, _count = _unpack_request(
            request
        )
        writer.write(struct.pack(">HHHBBB", tid, 0, 3, unit, 0x83, 0x02))
        await writer.drain()
        await reader.read()

    async with _serve(handler) as port:
        client = ModbusLocalClient("127.0.0.1", port, timeout=2.0)
        with pytest.raises(ModbusExceptionResponse) as excinfo:
            await client.read_blocks([(0x0206, 9)])

    assert excinfo.value.code == 2
    # The block the device rejected is named, for the log and last_error.
    assert excinfo.value.offset == 0x0206
    assert "0x0206" in str(excinfo.value)


def _answer(tid, unit, address, count):
    """The fields of a valid read answer, for a test to damage one of."""
    data = _register_bytes(address, count)
    return {
        "tid": tid,
        "protocol": 0,
        "length": 3 + len(data),
        "unit": unit,
        "function": 0x03,
        "byte_count": len(data),
        "data": data,
    }


def _pack_answer(fields):
    header = struct.pack(
        ">HHHBBB",
        fields["tid"],
        fields["protocol"],
        fields["length"],
        fields["unit"],
        fields["function"],
        fields["byte_count"],
    )
    return header + fields["data"]


@pytest.mark.parametrize(
    ("field", "damage", "message"),
    [
        ("tid", lambda value: value + 1, "transaction id mismatch"),
        ("protocol", lambda value: 1, "protocol id"),
        ("unit", lambda value: value + 1, "unit id mismatch"),
        # Below the smallest possible PDU: the frame cannot be read to its end.
        ("length", lambda value: 1, "implausible length"),
        # Frame length consistent, byte count short by one register.
        ("byte_count", lambda value: value - 2, "byte count"),
    ],
)
@pytest.mark.asyncio
async def test_an_answer_that_does_not_match_the_request_is_a_protocol_error(
    field, damage, message
):
    async def handler(reader, writer):
        request = await reader.readexactly(_REQUEST_LEN)
        tid, _proto, _length, unit, _function, address, count = _unpack_request(request)
        fields = _answer(tid, unit, address, count)
        fields[field] = damage(fields[field])
        writer.write(_pack_answer(fields))
        await writer.drain()
        await reader.read()

    async with _serve(handler) as port:
        client = ModbusLocalClient("127.0.0.1", port, unit_id=1, timeout=2.0)
        with pytest.raises(ModbusProtocolError, match=message):
            await client.read_blocks([(0x0206, 9)])


@pytest.mark.parametrize("answer", ["registers", "exception"])
@pytest.mark.asyncio
async def test_the_connection_is_closed_after_success_and_after_an_exception(
    answer, monkeypatch
):
    closed = asyncio.Event()
    seen_by_server: list[bytes] = []
    # Keep the client's writer alive: an unreferenced writer closes itself when
    # it is garbage collected, which would hide a missing close.
    held: list[asyncio.StreamWriter] = []
    real_open_connection = asyncio.open_connection

    async def open_and_hold(*args, **kwargs):
        reader, writer = await real_open_connection(*args, **kwargs)
        held.append(writer)
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", open_and_hold)

    async def handler(reader, writer):
        request = await reader.readexactly(_REQUEST_LEN)
        tid, _proto, _length, unit, _function, address, count = _unpack_request(request)
        if answer == "registers":
            writer.write(_pack_answer(_answer(tid, unit, address, count)))
        else:
            writer.write(struct.pack(">HHHBBB", tid, 0, 3, unit, 0x83, 0x02))
        await writer.drain()
        # Reads to the end of the stream: b"" means the client hung up.
        seen_by_server.append(await reader.read())
        closed.set()

    async with _serve(handler) as port:
        client = ModbusLocalClient("127.0.0.1", port, timeout=2.0)
        if answer == "registers":
            await client.read_blocks([(0x0206, 9)])
        else:
            with pytest.raises(ModbusExceptionResponse):
                await client.read_blocks([(0x0206, 9)])
        assert held[0].transport.is_closing()
        # The server's own cleanup also closes the socket, so the wait has to
        # finish before the context ends: it proves the client closed first.
        await asyncio.wait_for(closed.wait(), 2)

    assert seen_by_server == [b""]


@pytest.mark.asyncio
async def test_a_slow_device_cannot_hold_a_poll_past_the_shared_budget():
    async def handler(reader, writer):
        while True:
            request = await reader.readexactly(_REQUEST_LEN)
            tid, _proto, _length, unit, _function, address, count = _unpack_request(
                request
            )
            # Every answer arrives inside the 0.3 s single timeout.
            await asyncio.sleep(0.15)
            writer.write(_pack_answer(_answer(tid, unit, address, count)))
            await writer.drain()

    blocks = [(0x0206 + number, 1) for number in range(12)]
    async with _serve(handler) as port:
        client = ModbusLocalClient("127.0.0.1", port, timeout=0.3)

        # Control: four slow answers (0.6 s) fit the 1.2 s budget and succeed.
        assert len(await client.read_blocks(blocks[:4])) == 4

        # Twelve take 1.8 s: no single request is late, the poll as a whole is.
        started = time.monotonic()
        with pytest.raises(ModbusTimeoutError, match="budget"):
            await asyncio.wait_for(client.read_blocks(blocks), 5)
        elapsed = time.monotonic() - started

    assert elapsed < 1.7


@pytest.mark.asyncio
async def test_a_device_that_accepts_but_never_answers_times_out():
    async def handler(reader, writer):
        await reader.readexactly(_REQUEST_LEN)
        await reader.read()  # holds the connection until the client hangs up

    async with _serve(handler) as port:
        client = ModbusLocalClient("127.0.0.1", port, timeout=0.3)
        started = time.monotonic()
        with pytest.raises(ModbusTimeoutError):
            await asyncio.wait_for(client.read_blocks([(0x0206, 9)]), 5)
        elapsed = time.monotonic() - started

    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_a_closed_port_is_a_connect_error_not_a_timeout():
    client = ModbusLocalClient("127.0.0.1", _closed_port(), timeout=2.0)

    with pytest.raises(ModbusConnectError) as excinfo:
        await client.read_blocks([(0x0206, 9)])

    assert not isinstance(excinfo.value, ModbusTimeoutError)
