"""The public read-only register scan, run against an in-process fake inverter.

The script lives in ``documentation/tools`` and cannot import the integration,
so it carries a copy of the register map. These tests run the real script over
a real socket, prove that it only ever sends reads, that the serial number never
reaches the output, and that its copy of the map equals the integration's.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from ecoflow_energy.ecoflow import modbus_local
from ecoflow_energy.ecoflow.parsers import powerocean_modbus as pm

# The HA test harness blocks sockets; these tests talk to a fake inverter on loopback.
pytestmark = pytest.mark.usefixtures("socket_enabled")

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "documentation"
    / "tools"
    / "register_scan.py"
)
_spec = importlib.util.spec_from_file_location("register_scan_under_test", SCRIPT)
assert _spec is not None and _spec.loader is not None
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

SERIAL = "ZZ99FAKESERIAL42"  # fictional, 16 characters
BASE = 40001


def f32_words(value: float) -> list[int]:
    """A float as two registers, low word first (the device's word order)."""
    high, low = struct.unpack(">HH", struct.pack(">f", value))
    return [low, high]


def u32_words(value: int) -> list[int]:
    return [value & 0xFFFF, value >> 16]


def default_registers() -> dict[int, int]:
    """Every register of every scanned block, with plausible values."""
    blocks = (
        *script.SETUP_BLOCKS,
        *script.POLL_BLOCKS,
        (script.FAULT_EVENT_OFFSET, script.FAULT_EVENT_COUNT),
    )
    regs = {offset + i: 0 for offset, count in blocks for i in range(count)}

    def put(offset: int, words: list[int]) -> None:
        for i, word in enumerate(words):
            regs[offset + i] = word

    put(0x0000, [1, 1, 1])
    put(0x0003, list(struct.unpack(">8H", SERIAL.encode("ascii"))))
    put(0x000B, [0x250A, 0x0501])  # firmware 5.1.37.10
    for offset, value in (
        (0x0206, 1234.5),
        (0x0208, -200.0),
        (0x020A, 3000.0),
        (0x020C, 500.0),
        (0x023D, 650.0),
        (0x023F, 1.5),
        (0x0241, 25.0),
        (0x0251, 50.0),
        (0x0253, 400.0),
        (0x0255, 410.0),
        (0x0259, 5.0),
        (0x025B, 4.0),
        (0x0870, 12345.5),
        (0x0880, 6789.25),
        (0x08B0, 100.5),
        (0x08C0, 90.5),
        (0x08D0, 20000.0),
    ):
        put(offset, f32_words(value))
    put(0x020E, [55])
    put(0x0211, u32_words(0x0800))
    put(0x0217, [20])
    put(0x021C, [80])
    put(0x0227, u32_words(10240))
    put(0x0260, u32_words(10000))
    put(0x0800, [1])
    put(0x0801, [305])
    put(0x0820, [2])
    return regs


def _recv_exact(conn: socket.socket, size: int) -> bytes | None:
    data = b""
    while len(data) < size:
        try:
            chunk = conn.recv(size - len(data))
        except OSError:
            return None
        if not chunk:
            return None
        data += chunk
    return data


class FakeInverter:
    """A tiny Modbus/TCP server on 127.0.0.1 that records what it is asked.

    ``refuse`` maps a ``(offset, count)`` request to an exception code, ``drop``
    lists requests that get no answer at all. An address outside ``registers``
    is refused with exception 2, like a gap in a real register table.
    """

    def __init__(self, registers, refuse=None, drop=None):
        self.registers = registers
        self.refuse = dict(refuse or {})
        self.drop = set(drop or ())
        self.function_codes: set[int] = set()
        self.requests: list[tuple[int, int]] = []
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._listener.close()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _frame(tid: int, unit: int, pdu: bytes) -> bytes:
        return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            while True:
                header = _recv_exact(conn, 7)
                if header is None:
                    return
                tid, _protocol, length, unit = struct.unpack(">HHHB", header)
                pdu = _recv_exact(conn, length - 1)
                if pdu is None:
                    return
                function = pdu[0]
                self.function_codes.add(function)
                if function != 0x03:
                    conn.sendall(self._frame(tid, unit, bytes([function | 0x80, 1])))
                    continue
                address, count = struct.unpack(">HH", pdu[1:5])
                key = (address - BASE, count)
                self.requests.append(key)
                if key in self.drop:
                    continue
                code = self.refuse.get(key)
                if code is None and any(
                    key[0] + i not in self.registers for i in range(count)
                ):
                    code = 2
                if code is not None:
                    conn.sendall(self._frame(tid, unit, bytes([0x83, code])))
                    continue
                words = [self.registers[key[0] + i] for i in range(count)]
                body = struct.pack(f">{count}H", *words)
                conn.sendall(self._frame(tid, unit, bytes([0x03, 2 * count]) + body))


@pytest.fixture
def fake_inverter():
    made: list[FakeInverter] = []

    def factory(registers=None, **kwargs) -> FakeInverter:
        fake = FakeInverter(
            default_registers() if registers is None else registers, **kwargs
        )
        made.append(fake)
        return fake

    yield factory
    for fake in made:
        fake.close()


def run_scan(fake: FakeInverter, timeout: float = 1.0) -> dict:
    link = script.Link("127.0.0.1", fake.port, 1, timeout=timeout, pause=0)
    try:
        link.connect()
        return script.scan(link)
    finally:
        link.close()


def values_of(report: dict) -> dict:
    return {r["key"]: r["value"] for r in report["registers"]}


def test_happy_scan_reads_every_block_and_decodes_the_map(fake_inverter):
    report = run_scan(fake_inverter())

    assert len(report["blocks"]) == 11
    assert all(b["status"] == "ok" for b in report["blocks"])
    assert all(b["read_mode"] == "block" for b in report["blocks"])
    values = values_of(report)
    assert values["home_w"] == 1234.5
    assert values["grid_w"] == -200.0
    assert values["soc_pct"] == 55
    assert values["local_system_status"] == 0x0800
    assert values["ems_total_battery_capacity_wh"] == 10240
    assert values["pcs_ac_freq_hz"] == 50.0
    assert values["grid_export_lifetime_energy_kwh"] == 6789.25
    assert report["fault_events"] == [305]
    assert report["device"]["firmware"] == "5.1.37.10"
    assert report["summary"]["missing"] == []

    text = script.render_text(report)
    assert "three-phase PowerOcean" in text
    assert "product category: 1" in text
    assert "firmware:         5.1.37.10" in text
    total = len(script.REGISTERS)
    assert f"{total} of {total} expected registers answered" in text
    assert "attach this output to a new GitHub issue" in text
    assert "Nothing was written to the inverter" in text
    json.dumps(report)


@pytest.mark.parametrize("failure", ["refuse", "drop"])
def test_refused_or_dropped_block_falls_back_to_single_registers(
    fake_inverter, failure
):
    key = (0x0206, 15)
    fake = fake_inverter(
        refuse={key: 2} if failure == "refuse" else None,
        drop={key} if failure == "drop" else None,
    )
    report = run_scan(fake, timeout=0.3)

    by_offset = {b["offset"]: b for b in report["blocks"]}
    hit = by_offset[0x0206]
    assert hit["read_mode"] == "single"
    assert hit["status"] == "ok"
    expected = "refused (exception code 2)" if failure == "refuse" else "timeout"
    assert hit["block_result"] == expected
    assert [b["read_mode"] for o, b in by_offset.items() if o != 0x0206] == [
        "block"
    ] * 10
    assert values_of(report)["home_w"] == 1234.5
    assert report["summary"]["missing"] == []
    # Fifteen single reads followed the one refused block read.
    assert sum(1 for r in fake.requests if r[1] == 1 and 0x0206 <= r[0] < 0x0215) == 15


def test_scan_only_sends_function_code_read_holding_registers(fake_inverter):
    fake = fake_inverter(refuse={(0x0870, 98): 2})
    run_scan(fake)
    assert fake.function_codes == {0x03}
    assert any(count == 1 for _, count in fake.requests)  # the fallback ran too


def test_guard_refuses_every_other_function_code():
    with pytest.raises(script.ReadOnlyViolation):
        script.build_request(1, 1, 0x06, 0x0217, 1)
    with pytest.raises(script.ReadOnlyViolation):
        script.build_request(1, 1, 0x10, 0x0217, 1)
    link = script.Link("127.0.0.1", 1)
    write = struct.pack(">HHHBBHH", 1, 0, 6, 1, 0x06, BASE + 0x0217, 5)
    with pytest.raises(script.ReadOnlyViolation):
        link._send(write)
    assert script.build_request(1, 1, 0x03, 0x0206, 15)[7] == 0x03


def test_serial_number_never_reaches_text_or_json(fake_inverter, capsys):
    fake = fake_inverter()
    args = ["127.0.0.1", "--port", str(fake.port), "--timeout", "1", "--pause", "0"]
    assert script.main(args) == 0
    text_out = capsys.readouterr().out
    assert script.main([*args, "--json"]) == 0
    json_out = capsys.readouterr().out

    for out in (text_out, json_out):
        assert SERIAL not in out
        assert SERIAL[-12:] not in out
        assert SERIAL[4:].lower() not in out.lower()
    assert SERIAL[:4] + "*" * 12 in text_out
    report = json.loads(json_out)
    assert report["device"]["serial_masked"] == SERIAL[:4] + "*" * 12
    serial_block = next(b for b in report["blocks"] if b["offset"] == 0x0003)
    assert serial_block["words"] == ["0000"] * 8
    assert " ".join(["0000"] * 8) in text_out
    assert "read-only scan: nothing is written to the inverter" in text_out


def test_implausible_values_are_flagged(fake_inverter):
    regs = default_registers()
    regs[0x020E] = 250  # SoC 250 %
    for i, word in enumerate(f32_words(float("nan"))):
        regs[0x0251 + i] = word  # frequency NaN
    for i, word in enumerate(f32_words(-1.0)):
        regs[0x0870 + i] = word  # lifetime counter below zero
    report = run_scan(fake_inverter(regs))

    flags = {r["key"]: r["plausibility"] for r in report["registers"]}
    assert flags["soc_pct"] == "implausible"
    assert flags["pcs_ac_freq_hz"] == "implausible"
    assert flags["grid_import_lifetime_energy_kwh"] == "implausible"
    assert flags["home_w"] == "plausible"
    assert flags["local_system_status"] == "no range check"


def test_register_map_equals_the_integrations():
    assert script.SETUP_BLOCKS == pm.SETUP_BLOCKS
    assert script.POLL_BLOCKS == pm.POLL_BLOCKS
    assert script.SERIAL_OFFSET == pm.SERIAL_OFFSET
    assert script.SERIAL_REGISTER_COUNT == pm.SERIAL_REGISTER_COUNT
    assert script.FAULT_EVENT_OFFSET == pm.FAULT_EVENT_OFFSET
    assert script.FAULT_EVENT_COUNT == pm.FAULT_EVENT_COUNT
    assert script.SUPPORTED_PRODUCT == (
        pm.SUPPORTED_PRODUCT_CATEGORY,
        pm.SUPPORTED_PRODUCT_NUMBER,
    )
    ours = [(k, o, c, d, counter) for k, o, c, d, counter, _ in script.REGISTERS]
    theirs = [
        (r.key, r.offset, r.count, r.decode.__name__.removeprefix("decode_"), r.counter)
        for r in pm._REGISTERS
    ]
    assert ours == theirs
    assert script.MODBUS_BASE_ADDRESS == modbus_local.MODBUS_BASE_ADDRESS


def test_decoders_equal_the_integrations():
    for words in ([0x1234, 0x5678], [0, 0x3F80], [0xFFFF, 0x0001], [7, 0]):
        raw = struct.pack(">HH", *words)
        assert script.decode_u32_ws(words) == modbus_local.decode_u32_ws(raw)
        assert script.decode_f32_ws(words) == modbus_local.decode_f32_ws(raw)
    assert script.decode_u16([513]) == modbus_local.decode_u16(b"\x02\x01")


def test_unreachable_host_gives_one_line_error_and_exit_code_2():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # closed again: nothing listens here
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "127.0.0.1", "--port", str(port)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("error: cannot connect to 127.0.0.1:")
