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
    ``bad_frame_once`` answers a request once with a wrong transaction id,
    ``truncate_once`` / ``truncate_always`` cut the reply short and close the
    connection, ``close_after_reply`` closes after every reply and ``silent``
    never answers. ``units`` collects the unit ids it was addressed with.
    """

    def __init__(
        self,
        registers,
        refuse=None,
        drop=None,
        bad_frame_once=None,
        truncate_once=None,
        truncate_always=None,
        close_after_reply=False,
        silent=False,
    ):
        self.registers = registers
        self.refuse = dict(refuse or {})
        self.drop = set(drop or ())
        self.bad_frame_once = set(bad_frame_once or ())
        self.truncate_once = set(truncate_once or ())
        self.truncate_always = set(truncate_always or ())
        self.close_after_reply = close_after_reply
        self.silent = silent
        self.units: set[int] = set()
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
                self.units.add(unit)
                self.function_codes.add(function)
                if function != 0x03:
                    conn.sendall(self._frame(tid, unit, bytes([function | 0x80, 1])))
                    continue
                address, count = struct.unpack(">HH", pdu[1:5])
                key = (address - BASE, count)
                self.requests.append(key)
                if self.silent or key in self.drop:
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
                frame = self._frame(tid, unit, bytes([0x03, 2 * count]) + body)
                if key in self.bad_frame_once:
                    self.bad_frame_once.discard(key)
                    frame = self._frame(tid + 1000, unit, frame[7:])
                elif key in self.truncate_always or key in self.truncate_once:
                    self.truncate_once.discard(key)
                    conn.sendall(frame[: len(frame) // 2])
                    return
                conn.sendall(frame)
                if self.close_after_reply:
                    return


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
    assert serial_block["words"] == ["****"] * 8
    assert " ".join(["****"] * 8) in text_out
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
    assert lines[0].startswith("error: cannot connect to the inverter address you gave")
    assert "127.0.0.1" not in result.stderr  # the typed address is never echoed


def _scan_args(fake: FakeInverter, *extra: str) -> list[str]:
    return [
        "127.0.0.1",
        "--port",
        str(fake.port),
        "--timeout",
        "1",
        "--pause",
        "0",
        *extra,
    ]


def test_the_typed_address_is_not_in_the_report(fake_inverter, capsys):
    fake = fake_inverter()
    assert script.main(_scan_args(fake)) == 0
    text_out = capsys.readouterr().out
    assert script.main(_scan_args(fake, "--json")) == 0
    json_out = capsys.readouterr().out
    for out in (text_out, json_out):
        assert "127.0.0.1" not in out
        assert "localhost" not in out
    assert "host" not in json.loads(json_out)
    assert f"target: port {fake.port}, unit 1" in text_out


def test_unit_id_reaches_the_inverter(fake_inverter, capsys):
    fake = fake_inverter()
    assert script.main(_scan_args(fake, "--unit", "7", "--json")) == 0
    assert fake.units == {7}
    assert json.loads(capsys.readouterr().out)["unit"] == 7


def test_json_stdout_is_pure_and_progress_goes_to_stderr(fake_inverter, capsys):
    fake = fake_inverter()
    assert script.main(_scan_args(fake, "--json")) == 0
    captured = capsys.readouterr()
    json.loads(captured.out)  # nothing but the report on stdout
    progress = [
        ln for ln in captured.err.splitlines() if ln.startswith("reading block")
    ]
    assert len(progress) == 11
    assert progress[0].startswith("reading block 1/11: 0x0000-0x0002")
    assert progress[-1].startswith("reading block 11/11: 0x0801")
    assert "reading block" not in captured.out


def test_a_bad_frame_does_not_spoil_the_blocks_after_it(fake_inverter):
    fake = fake_inverter(bad_frame_once={(0x0217, 18)})
    report = run_scan(fake, timeout=0.5)

    by_offset = {b["offset"]: b for b in report["blocks"]}
    assert by_offset[0x0217]["status"] == "error"
    assert "does not match" in by_offset[0x0217]["detail"]
    later = [b for o, b in by_offset.items() if o > 0x0217]
    assert later and all(b["status"] == "ok" for b in later)
    assert values_of(report)["batt_voltage_v"] == 650.0
    assert values_of(report)["grid_export_lifetime_energy_kwh"] == 6789.25


def test_scan_survives_a_connection_closed_after_every_reply(fake_inverter):
    report = run_scan(fake_inverter(close_after_reply=True))
    assert all(b["status"] == "ok" for b in report["blocks"])
    assert all(b["read_mode"] == "block" for b in report["blocks"])
    assert report["summary"]["missing"] == []


def test_a_truncated_reply_is_retried_once_and_then_reported(fake_inverter):
    once = run_scan(fake_inverter(truncate_once={(0x0870, 98)}))
    block = next(b for b in once["blocks"] if b["offset"] == 0x0870)
    assert block["status"] == "ok" and block["read_mode"] == "block"

    always = run_scan(fake_inverter(truncate_always={(0x0870, 98)}))
    block = next(b for b in always["blocks"] if b["offset"] == 0x0870)
    assert block["status"] == "error"
    assert block["detail"].startswith("connection lost")
    others = [b for b in always["blocks"] if b["offset"] != 0x0870]
    assert all(b["status"] == "ok" for b in others)


def test_an_exception_code_other_than_2_is_reported_with_its_number(fake_inverter):
    report = run_scan(fake_inverter(refuse={(0x0206, 15): 3}))
    block = next(b for b in report["blocks"] if b["offset"] == 0x0206)
    assert block["block_result"] == "refused (exception code 3)"
    assert block["exception_code"] == 3
    assert block["read_mode"] == "single"
    assert block["status"] == "ok"


def test_nothing_answering_exits_with_code_1(fake_inverter, capsys):
    fake = fake_inverter(registers={})  # every address is refused
    assert script.main(_scan_args(fake)) == 1
    out = capsys.readouterr().out
    assert "0 of" in out and "expected registers answered" in out


def test_a_silent_inverter_ends_the_scan_with_exit_code_1(
    fake_inverter, monkeypatch, capsys
):
    monkeypatch.setattr(script, "_FALLBACK_TIMEOUT_LIMIT", 1)  # keep the test fast
    fake = fake_inverter(silent=True)
    args = ["127.0.0.1", "--port", str(fake.port), "--timeout", "0.2", "--pause", "0"]
    assert script.main(args) == 1
    assert "0 of" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("bad", "fragment"),
    [
        (["--unit", "256"], "--unit"),
        (["--unit", "-1"], "--unit"),
        (["--port", "0"], "--port"),
        (["--port", "65536"], "--port"),
        (["--timeout", "0"], "--timeout"),
        (["--timeout", "nan"], "--timeout"),
        (["--pause", "-1"], "--pause"),
        (["--unit", "abc"], "--unit"),
    ],
)
def test_bad_arguments_exit_2_without_a_traceback(bad, fragment, capsys):
    with pytest.raises(SystemExit) as raised:
        script.main(["inverter.example", *bad])
    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert fragment in err
    assert "Traceback" not in err
    assert "inverter.example" not in err


def test_an_invalid_address_is_a_one_line_error_with_exit_code_2():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), ".."],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert result.stderr.strip().splitlines() == [
        "error: the inverter address you gave is not valid"
    ]


def test_ctrl_c_prints_one_line_and_returns_130(fake_inverter, monkeypatch, capsys):
    def interrupted(link, progress=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(script, "scan", interrupted)
    assert script.main(_scan_args(fake_inverter())) == 130
    err = capsys.readouterr().err.strip().splitlines()
    assert err == ["interrupted: nothing was written to the inverter"]


def test_python_before_3_10_is_refused_with_exit_code_2(monkeypatch, capsys):
    monkeypatch.setattr(script.sys, "version_info", (3, 9, 18, "final", 0))
    assert script.main(["127.0.0.1"]) == 2
    err = capsys.readouterr().err
    assert err.strip().splitlines() == [
        "error: register_scan.py needs Python 3.10 or newer (this is 3.9)"
    ]


def test_non_finite_floats_are_null_in_strict_json(fake_inverter, capsys):
    regs = default_registers()
    for i, word in enumerate(f32_words(float("nan"))):
        regs[0x0251 + i] = word  # frequency NaN
    for i, word in enumerate(f32_words(float("inf"))):
        regs[0x0206 + i] = word  # home power +inf
    fake = fake_inverter(regs)
    assert script.main(_scan_args(fake, "--json")) == 0
    out = capsys.readouterr().out
    report = json.loads(out, parse_constant=_reject_constant)  # strict parse
    rows = {r["key"]: r for r in report["registers"]}
    for key in ("pcs_ac_freq_hz", "home_w"):
        assert rows[key]["value"] is None
        assert rows[key]["answered"] is True
        assert rows[key]["plausibility"] == "implausible"
    assert rows["grid_w"]["value"] == -200.0
    assert report["summary"]["missing"] == []


def _reject_constant(name):
    raise AssertionError(f"non-standard JSON constant {name}")


def test_serial_block_refused_or_incomplete_leaks_nothing(fake_inverter, capsys):
    refused = fake_inverter(refuse={(0x0003, 8): 2})
    one_missing = fake_inverter(refuse={(0x0003, 8): 2}, drop={(0x0005, 1)})
    for fake in (refused, one_missing):
        assert script.main(_scan_args(fake, "--timeout", "0.3")) == 0
        text_out = capsys.readouterr().out
        assert script.main(_scan_args(fake, "--timeout", "0.3", "--json")) == 0
        json_out = capsys.readouterr().out
        for out in (text_out, json_out):
            assert SERIAL[4:].lower() not in out.lower()
            for word in struct.unpack(">8H", SERIAL.encode("ascii")):
                assert f"{word:04x}" not in out
        block = next(b for b in json.loads(json_out)["blocks"] if b["offset"] == 0x0003)
        assert block["read_mode"] == "single"
    # One serial register missing: the masked serial cannot be built either.
    assert json.loads(json_out)["device"]["serial_masked"] is None
    assert "----" in text_out and "****" in text_out


def test_unknown_model_withholds_the_identity_registers(fake_inverter, capsys):
    regs = default_registers()
    regs[0x0002] = 2  # product number is not the known one
    spill = "QWXY"  # a longer serial runs on into 0x000B and 0x000C
    regs[0x000B], regs[0x000C] = struct.unpack(">2H", spill.encode("ascii"))
    fake = fake_inverter(regs)
    assert script.main(_scan_args(fake)) == 0
    text_out = capsys.readouterr().out
    assert script.main(_scan_args(fake, "--json")) == 0
    json_out = capsys.readouterr().out

    for out in (text_out, json_out):
        assert spill.lower() not in out.lower()
        for word in struct.unpack(">2H", spill.encode("ascii")):
            assert f"{word:04x}" not in out
        assert SERIAL[:4].lower() not in out.lower()
        assert SERIAL[4:].lower() not in out.lower()
    assert text_out.count("withheld, unknown layout") == 4  # 2 blocks + 2 device rows
    report = json.loads(json_out)
    assert report["device"]["product_number"] == 2
    assert report["device"]["identity_withheld"] is True
    assert report["device"]["serial_masked"] is None
    assert report["device"]["firmware"] is None
    withheld = [b for b in report["blocks"] if b["withheld"]]
    assert {b["offset"] for b in withheld} == {0x0003, 0x000B}
    assert all(b["words"] is None for b in withheld)
    assert values_of(report)["home_w"] == 1234.5  # the rest is still decoded


def test_unreadable_model_numbers_also_withhold_the_identity_registers(
    fake_inverter, capsys
):
    fake = fake_inverter(refuse={(0x0000, 3): 2, (0x0001, 1): 2})
    assert script.main(_scan_args(fake, "--json")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["device"]["identity_withheld"] is True
    assert {b["offset"] for b in report["blocks"] if b["withheld"]} == {0x0003, 0x000B}


def test_known_model_keeps_firmware_and_masked_serial(fake_inverter, capsys):
    assert script.main(_scan_args(fake_inverter(), "--json")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["device"]["identity_withheld"] is False
    assert report["device"]["firmware"] == "5.1.37.10"
    assert not any(b["withheld"] for b in report["blocks"])
