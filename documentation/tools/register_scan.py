#!/usr/bin/env python3
"""Read-only Modbus/TCP register scan for EcoFlow PowerOcean-style inverters.

Run it on any computer that can reach the inverter and attach the output to a
GitHub issue. It shows whether the register map of the EcoFlow Energy
integration fits your model, so a new model can be added from a real measurement.

    python3 register_scan.py ADDRESS [--port 502] [--unit 1] [--json]

Standard library only, Python 3.10 or newer. The scan never writes: the only
Modbus function code it can send is 0x03 (read holding registers), and a guard
raises before any other code reaches the socket. The serial number is never
printed in full, and the address you scan is not part of the report. For a
model whose identity registers do not have the known layout, those registers
are withheld altogether.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import socket
import struct
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone

SCRIPT_VERSION = "2"
MIN_PYTHON = (3, 10)
DEFAULT_PORT = 502
MODBUS_BASE_ADDRESS = 40001  # wire address = base + zero-based register offset
READ_HOLDING_REGISTERS = 0x03
MAX_REGISTERS_PER_READ = 125
_EXCEPTION_FLAG = 0x80
_MBAP_LEN = 7
_FALLBACK_TIMEOUT_LIMIT = 5  # stop a single-register retry after this many timeouts

# ---------------------------------------------------------------------------
# REGISTER MAP. A copy of the integration's table (offsets are zero-based wire
# offsets, the wire address is 40001 + offset). A test keeps this section equal
# to the integration, so edit both together.
# ---------------------------------------------------------------------------

# (offset, register count): protocol version + product category + product
# number, serial (16 ASCII bytes), firmware package (4 bytes).
SETUP_BLOCKS = ((0x0000, 3), (0x0003, 8), (0x000B, 2))
SERIAL_OFFSET = 0x0003
SERIAL_REGISTER_COUNT = 8

POLL_BLOCKS = (
    (0x0206, 15),
    (0x0217, 18),
    (0x023D, 6),
    (0x0251, 17),
    (0x0800, 1),
    (0x0820, 1),
    (0x0870, 98),
)

# 20 fault events of one 16-bit word each, 0 = empty slot.
FAULT_EVENT_OFFSET = 0x0801
FAULT_EVENT_COUNT = 20

# Product category and number of the model the integration's Local mode serves.
SUPPORTED_PRODUCT = (1, 1)
# Only for that model is it known that the serial ends before 0x000B. For any
# other model the raw words of these two blocks are never printed.
IDENTITY_BLOCK_OFFSETS = (0x0003, 0x000B)
WITHHELD_TEXT = "withheld, unknown layout"

_COUNTER = (0, math.inf)
_PERCENT = (0, 100)
_POWER_W = (-100_000, 100_000)
_VOLTAGE = (0, 1000)
_CURRENT = (-1000, 1000)

# (key, offset, register count, decoder, lifetime counter, plausible range).
# Decoders: u16, u32_ws and f32_ws (two registers, low word first).
REGISTERS = (
    ("home_w", 0x0206, 2, "f32_ws", False, _POWER_W),
    ("grid_w", 0x0208, 2, "f32_ws", False, _POWER_W),
    ("solar_w", 0x020A, 2, "f32_ws", False, _POWER_W),
    ("batt_w", 0x020C, 2, "f32_ws", False, _POWER_W),
    ("soc_pct", 0x020E, 1, "u16", False, _PERCENT),
    ("local_system_status", 0x0211, 2, "u32_ws", False, None),
    ("local_system_state2", 0x0213, 2, "u32_ws", False, None),
    ("ems_backup_ratio_pct", 0x0217, 1, "u16", False, _PERCENT),
    ("local_indicator_brightness_pct", 0x021C, 1, "u16", False, _PERCENT),
    ("ems_total_battery_capacity_wh", 0x0227, 2, "u32_ws", False, (0, 1_000_000)),
    ("batt_voltage_v", 0x023D, 2, "f32_ws", False, _VOLTAGE),
    ("batt_current_a", 0x023F, 2, "f32_ws", False, _CURRENT),
    ("batt_temp_c", 0x0241, 2, "f32_ws", False, (-50, 100)),
    ("pcs_ac_freq_hz", 0x0251, 2, "f32_ws", False, (45, 65)),
    ("mppt_pv1_voltage_v", 0x0253, 2, "f32_ws", False, _VOLTAGE),
    ("mppt_pv2_voltage_v", 0x0255, 2, "f32_ws", False, _VOLTAGE),
    ("mppt_pv1_current_a", 0x0259, 2, "f32_ws", False, _CURRENT),
    ("mppt_pv2_current_a", 0x025B, 2, "f32_ws", False, _CURRENT),
    ("ems_feed_power_limit_w", 0x0260, 2, "u32_ws", False, (0, 100_000)),
    ("fault_count", 0x0800, 1, "u16", False, None),
    ("bp_online_sum", 0x0820, 1, "u16", False, None),
    ("grid_import_lifetime_energy_kwh", 0x0870, 2, "f32_ws", True, _COUNTER),
    ("grid_export_lifetime_energy_kwh", 0x0880, 2, "f32_ws", True, _COUNTER),
    ("batt_charge_energy_kwh", 0x08B0, 2, "f32_ws", True, _COUNTER),
    ("batt_discharge_energy_kwh", 0x08C0, 2, "f32_ws", True, _COUNTER),
    ("solar_lifetime_energy_kwh", 0x08D0, 2, "f32_ws", True, _COUNTER),
)

BLOCK_NAMES = {
    0x0000: "identity: protocol version, product category, product number",
    0x0003: "serial number (masked)",
    0x000B: "firmware package",
    0x0206: "load, grid, solar, battery power, SoC, system status",
    0x0217: "backup ratio ... battery capacity",
    0x023D: "battery voltage, current, temperature",
    0x0251: "frequency, PV voltage and current, feed limit",
    0x0800: "fault count",
    0x0801: "fault events",
    0x0820: "batteries online",
    0x0870: "lifetime energy counters",
}


def decode_u16(words: list[int]) -> int:
    return words[0]


def decode_u32_ws(words: list[int]) -> int:
    return (words[1] << 16) | words[0]


def decode_f32_ws(words: list[int]) -> float:
    return float(struct.unpack(">f", struct.pack(">HH", words[1], words[0]))[0])


_DECODERS = {"u16": decode_u16, "u32_ws": decode_u32_ws, "f32_ws": decode_f32_ws}

# ---------------------------------------------------------------------------
# Modbus link. Read-only by construction.
# ---------------------------------------------------------------------------


class ReadOnlyViolation(RuntimeError):
    """Raised before anything but a read request could reach the socket."""


class ScanError(Exception):
    """The connection could not be made, broke twice, or the reply made no sense."""


class Refused(Exception):
    """The inverter answered with a Modbus exception."""

    def __init__(self, code: int) -> None:
        super().__init__(f"exception code {code}")
        self.code = code


class NoAnswer(Exception):
    """Connected, but no reply within the timeout."""


def build_request(tid: int, unit: int, function: int, offset: int, count: int) -> bytes:
    """Build one request frame. Only function code 0x03 is ever allowed."""
    if function != READ_HOLDING_REGISTERS:
        raise ReadOnlyViolation(
            f"refusing to send function code {function:#04x}: this tool only reads"
        )
    if not 1 <= count <= MAX_REGISTERS_PER_READ:
        raise ValueError(f"register count out of range: {count}")
    address = MODBUS_BASE_ADDRESS + offset
    return struct.pack(">HHHBBHH", tid, 0, 6, unit, function, address, count)


class Link:
    """One reused TCP connection, reconnected once on a reset."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        unit: int = 1,
        timeout: float = 3.0,
        pause: float = 0.2,
    ) -> None:
        self.host, self.port, self.unit = host, port, unit
        self.timeout, self.pause = timeout, pause
        self._sock: socket.socket | None = None
        self._tid = 0
        self._first = True

    def connect(self) -> None:
        self.close()
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            )
        except (ValueError, OverflowError) as err:  # UnicodeError is a ValueError
            raise ScanError("the inverter address you gave is not valid") from err
        except OSError as err:
            # The address you typed is left out of the message on purpose.
            reason = (
                "timed out"
                if isinstance(err, TimeoutError)
                else (err.strerror or type(err).__name__)
            )
            raise ScanError(
                f"cannot connect to the inverter address you gave (port {self.port}): "
                f"{reason}"
            ) from err

    def close(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None

    def _send(self, frame: bytes) -> None:
        # Second guard, on the bytes themselves: the function code is byte 7.
        if len(frame) < 8 or frame[7] != READ_HOLDING_REGISTERS:
            raise ReadOnlyViolation("refusing to send a frame that is not a read")
        assert self._sock is not None
        self._sock.sendall(frame)

    def _recv(self, size: int) -> bytes:
        assert self._sock is not None
        data = b""
        while len(data) < size:
            chunk = self._sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("connection closed by the inverter")
            data += chunk
        return data

    def _exchange(self, offset: int, count: int) -> list[int]:
        self._tid = self._tid % 0xFFFF + 1
        self._send(
            build_request(self._tid, self.unit, READ_HOLDING_REGISTERS, offset, count)
        )
        tid, protocol, length, _unit = struct.unpack(">HHHB", self._recv(_MBAP_LEN))
        if tid != self._tid or protocol != 0 or not 3 <= length <= 254:
            raise ScanError("the reply header does not match the request")
        pdu = self._recv(length - 1)
        if pdu[0] == READ_HOLDING_REGISTERS | _EXCEPTION_FLAG:
            raise Refused(pdu[1])
        if pdu[0] != READ_HOLDING_REGISTERS or pdu[1] != 2 * count:
            raise ScanError("malformed reply")
        if len(pdu) != 2 + 2 * count:
            raise ScanError("reply length does not match the register count")
        return list(struct.unpack(f">{count}H", pdu[2:]))

    def read(self, offset: int, count: int) -> list[int]:
        """Read ``count`` registers. Raises Refused, NoAnswer or ScanError."""
        if not self._first:
            time.sleep(self.pause)
        self._first = False
        for attempt in (1, 2):
            if self._sock is None:
                self.connect()
            try:
                return self._exchange(offset, count)
            except TimeoutError:
                self.close()  # a late reply would be taken for the next answer
                raise NoAnswer from None
            except ScanError:
                self.close()  # out of sync: the next request starts clean
                raise
            except (OSError, struct.error) as err:  # reset or closed
                self.close()
                if attempt == 2:
                    raise ScanError(f"connection lost: {err}") from err
        raise ScanError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


NULLED_WORD = "****"


def _hex(words: list[int | None]) -> list[str | None]:
    return [None if w is None else f"{w:04x}" for w in words]


def read_block(link: Link, offset: int, count: int) -> dict:
    """Read one block; on a refusal or timeout retry it register by register."""
    block: dict = {
        "name": BLOCK_NAMES.get(offset, ""),
        "offset": offset,
        "count": count,
        "address": MODBUS_BASE_ADDRESS + offset,
        "block_result": "ok",
        "exception_code": None,
        "read_mode": "block",
        "status": "ok",
        "detail": "",
        "withheld": False,
    }
    words: list[int | None]
    try:
        words = list(link.read(offset, count))
        block["words"] = words
        return block
    except Refused as err:
        block["block_result"] = f"refused (exception code {err.code})"
        block["exception_code"] = err.code
    except NoAnswer:
        block["block_result"] = "timeout"
    except ScanError as err:
        block.update(block_result="error", status="error", detail=str(err))
        block["words"] = [None] * count
        return block

    block["read_mode"] = "single"
    words = [None] * count
    timeouts = 0
    for index in range(count):
        try:
            words[index] = link.read(offset + index, 1)[0]
            timeouts = 0
        except Refused:
            timeouts = 0
        except NoAnswer:
            timeouts += 1
            if timeouts >= _FALLBACK_TIMEOUT_LIMIT:
                block["detail"] = "stopped after repeated timeouts"
                break
        except ScanError as err:
            block["detail"] = str(err)
            break
    block["words"] = words
    answered = sum(w is not None for w in words)
    block["status"] = "ok" if answered == count else "partial" if answered else "failed"
    return block


def _serial_text(offset: int, words: list[int | None]) -> str | None:
    """The serial in clear, when this block holds all of it; else None."""
    low = SERIAL_OFFSET - offset
    part = words[low : low + SERIAL_REGISTER_COUNT]
    if low < 0 or len(part) != SERIAL_REGISTER_COUNT or None in part:
        return None
    raw = struct.pack(f">{SERIAL_REGISTER_COUNT}H", *part)  # type: ignore[arg-type]
    return raw.decode("ascii", errors="replace").replace("\x00", "").strip()


def mask_serial(serial: str) -> str:
    """First four characters, the rest hidden."""
    return serial[:4] + "*" * max(len(serial) - 4, 0) if serial else "(empty)"


def _hex_without_serial(offset: int, words: list[int | None]) -> list[str | None]:
    """Hex words for the report; a word of the serial range becomes ``****``."""
    start, end = SERIAL_OFFSET, SERIAL_OFFSET + SERIAL_REGISTER_COUNT
    shown = _hex(words)
    return [
        NULLED_WORD if (start <= offset + i < end and w is not None) else w
        for i, w in enumerate(shown)
    ]


def plausibility(value: float, bounds: tuple[float, float] | None) -> str:
    if isinstance(value, float) and not math.isfinite(value):
        return "implausible"
    if bounds is None:
        return "no range check"
    return "plausible" if bounds[0] <= value <= bounds[1] else "implausible"


def scan(
    link: Link, progress: Callable[[int, int, int, int], None] | None = None
) -> dict:
    """Run the whole scan and return the report as a dict.

    ``progress(index, total, offset, count)`` is called before each block.
    """
    plan = [*SETUP_BLOCKS, *POLL_BLOCKS, (FAULT_EVENT_OFFSET, FAULT_EVENT_COUNT)]
    blocks: list[dict] = []
    known: dict[int, int] = {}
    serial = None
    for index, (offset, count) in enumerate(plan, 1):
        if progress is not None:
            progress(index, len(plan), offset, count)
        block = read_block(link, offset, count)
        words = block["words"]
        found = _serial_text(offset, words)
        if found is not None:
            serial = found
        known.update({offset + i: w for i, w in enumerate(words) if w is not None})
        block["words"] = _hex_without_serial(offset, words)  # clear serial not kept
        blocks.append(block)

    def window(offset: int, count: int) -> list[int] | None:
        part = [known.get(offset + i) for i in range(count)]
        return None if None in part else part  # type: ignore[return-value]

    registers = []
    for key, offset, count, decoder, counter, bounds in REGISTERS:
        part = window(offset, count)
        decoded = None if part is None else _DECODERS[decoder](part)
        # NaN and infinity are not valid JSON: they are reported as null and
        # the plausibility flag still marks them.
        finite = not (isinstance(decoded, float) and not math.isfinite(decoded))
        registers.append(
            {
                "key": key,
                "offset": offset,
                "answered": decoded is not None,
                "value": decoded if finite else None,
                "counter": counter,
                "plausibility": None
                if decoded is None
                else plausibility(decoded, bounds),
            }
        )

    category, number = known.get(1), known.get(2)
    matches = (category, number) == SUPPORTED_PRODUCT
    firmware = window(0x000B, 2)
    fw_text = None
    if firmware is not None and matches:
        raw = struct.pack(">HH", *firmware)
        fw_text = ".".join(str(b) for b in raw[2:4] + raw[0:2])
    if not matches:
        # Fail closed: an unknown or unreadable model may keep its serial anywhere
        # in these registers, so their words are not printed.
        for block in blocks:
            if block["offset"] in IDENTITY_BLOCK_OFFSETS:
                block["words"] = None
                block["withheld"] = True
        serial = None
    events = window(FAULT_EVENT_OFFSET, FAULT_EVENT_COUNT)
    answered = [r["key"] for r in registers if r["answered"]]
    return {
        "script_version": SCRIPT_VERSION,
        "scan_time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),  # noqa: UP017
        "port": link.port,
        "unit": link.unit,
        "device": {
            "protocol_version": known.get(0),
            "product_category": category,
            "product_number": number,
            "matches_local_mode": matches,
            "identity_withheld": not matches,
            "firmware": fw_text,
            "serial_masked": None if serial is None else mask_serial(serial),
        },
        "blocks": blocks,
        "registers": registers,
        "fault_events": None if events is None else [w for w in events if w],
        "summary": {
            "expected": len(registers),
            "answered": answered,
            "missing": [r["key"] for r in registers if not r["answered"]],
        },
        "note": "Only read requests (function code 0x03) were sent. "
        "Nothing was written to the inverter.",
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def render_text(report: dict) -> str:
    dev = report["device"]
    label = (
        "three-phase PowerOcean (the model the Local mode serves)"
        if dev["matches_local_mode"]
        else "not a model the Local mode serves yet"
    )
    firmware = WITHHELD_TEXT if dev["identity_withheld"] else dev["firmware"]
    serial = WITHHELD_TEXT if dev["identity_withheld"] else dev["serial_masked"]
    lines = [
        f"register_scan.py version {report['script_version']}",
        f"scan time (UTC): {report['scan_time_utc']}",
        f"target: port {report['port']}, unit {report['unit']}",
        "",
        "Device (as reported)",
        f"  protocol version: {dev['protocol_version']}",
        f"  product category: {dev['product_category']}",
        f"  product number:   {dev['product_number']}",
        f"  model:            {label}",
        f"  firmware:         {firmware}",
        f"  serial:           {serial}",
        "",
        "Blocks",
    ]
    for b in report["blocks"]:
        end = b["offset"] + b["count"] - 1
        lines.append(
            f"  [{b['status']}] {b['offset']:#06x}-{end:#06x} ({b['count']} registers)"
            f" read_mode={b['read_mode']}  {b['name']}"
        )
        if b["block_result"] != "ok":
            lines.append(f"      block read: {b['block_result']}")
        if b["detail"]:
            lines.append(f"      note: {b['detail']}")
        if b["withheld"]:
            lines.append(f"      {WITHHELD_TEXT}")
            continue
        words = [w or "----" for w in b["words"]]
        for start in range(0, len(words), 16):
            lines.append("      " + " ".join(words[start : start + 16]))
    lines += ["", "Registers of the integration's map"]
    for r in report["registers"]:
        if not r["answered"]:
            shown = "no answer"
        else:
            shown = "not finite" if r["value"] is None else repr(r["value"])
        flag = r["plausibility"] or ""
        lines.append(f"  {r['key']:<34} {r['offset']:#06x}  {shown:<14} {flag}")
    if report["fault_events"] is not None:
        lines.append(f"  fault events (non-zero words): {report['fault_events']}")
    s = report["summary"]
    lines += [
        "",
        f"{len(s['answered'])} of {s['expected']} expected registers answered."
        + (f" No answer: {', '.join(s['missing'])}." if s["missing"] else ""),
        "",
        "Please attach this output to a new GitHub issue. " + report["note"],
    ]
    return "\n".join(lines)


def _int_between(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{text!r} is not a whole number"
            ) from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return value

    return parse


def _seconds(*, allow_zero: bool) -> Callable[[str], float]:
    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
        if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
            raise argparse.ArgumentTypeError(
                "must be 0 or more" if allow_zero else "must be more than 0"
            )
        return value

    return parse


def _address(text: str) -> str:
    if not text.strip():
        raise argparse.ArgumentTypeError("the inverter address is empty")
    return text


def _progress(index: int, total: int, offset: int, count: int) -> None:
    end = offset + count - 1
    sys.stderr.write(
        f"reading block {index}/{total}: {offset:#06x}-{end:#06x} ({count} registers)\n"
    )
    sys.stderr.flush()


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        sys.stderr.write(
            "error: register_scan.py needs Python "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer "
            f"(this is {sys.version_info[0]}.{sys.version_info[1]})\n"
        )
        return 2
    parser = argparse.ArgumentParser(
        description="Read-only Modbus register scan for EcoFlow PowerOcean inverters."
    )
    parser.add_argument(
        "host", type=_address, help="IP address or name of the inverter"
    )
    parser.add_argument(
        "--port", type=_int_between(1, 65535), default=DEFAULT_PORT, help="TCP port"
    )
    parser.add_argument(
        "--unit", type=_int_between(0, 255), default=1, help="Modbus unit id"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--timeout",
        type=_seconds(allow_zero=False),
        default=3.0,
        help="seconds per request",
    )
    parser.add_argument(
        "--pause",
        type=_seconds(allow_zero=True),
        default=0.2,
        help="seconds between requests",
    )
    args = parser.parse_args(argv)

    # In JSON mode stdout stays pure JSON, so the notice goes to stderr.
    notice = "read-only scan: nothing is written to the inverter"
    if args.json:
        sys.stderr.write(notice + "\n")
    else:
        print(notice)
    link = Link(args.host, args.port, args.unit, args.timeout, args.pause)
    try:
        link.connect()
        report = scan(link, progress=_progress)
    except ScanError as err:
        sys.stderr.write(f"error: {err}\n")
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("interrupted: nothing was written to the inverter\n")
        return 130
    finally:
        link.close()
    print(
        json.dumps(report, indent=2, allow_nan=False)
        if args.json
        else render_text(report)
    )
    return 0 if report["summary"]["answered"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
