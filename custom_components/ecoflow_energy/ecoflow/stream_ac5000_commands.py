"""SET command builders for the EcoFlow STREAM AC 5000 (ES22).

The EcoFlow Android app publishes its writes to the same
`/app/{uid}/{sn}/thing/property/set` topic the integration can subscribe to,
so these frames are not inferred: they are the app's own frames with the
payload swapped. Three of them are reproduced byte for byte in
`tests/test_stream_ac5000_commands.py`, and the SoC limit, work mode and task
power writes were each confirmed on live hardware on 2026-08-03, with the
device acknowledging and reading the new value back.

This device needs its own envelope rather than
`build_delta3_config_write_payload`: the ES22 writes on `cmd_id = 38`, not 17,
and its header carries a product id, a version of 4, a `from` string and the
serial in two fields, none of which the Delta 3 frame has.

The payload is always "field 1 names the config field being written, then that
field's value":

    19  backup socket {1: on/off}
    25  work mode: 0 self-powered, 1 Intelligent Mode+, 2 custom
    29  SoC limits {1: charge %, 2: discharge %}, both in one frame
    30  backup reserve {1: on/off, 2: reserve %}, both in one frame
    39  scheduled task, see build_task_payload

A write number is not a readback number. Three of these five are read back
under the number they were written on and two are not; the table on
`CONFIG_*` below says which is which.
"""

from __future__ import annotations

import time

from .proto_encoding import encode_field_bytes, encode_field_varint, encode_varint

# Config field numbers for writes. Two of these do NOT read back under the
# number they are written on, so check the readback rather than assuming it:
#
#   25  work mode        -> `254/39 f25`
#   19  backup socket    -> `254/39 f19.1`
#   30  backup reserve   -> `254/39 f30`
#   29  SoC limits       -> `32/2 f1.7` and `f1.21`. `f29` carries them too but
#                           lags an app change by minutes, so it is unmapped.
#   39  scheduled task   -> `254/39 f40`, one level deeper on `.8`.
CONFIG_WORK_MODE = 25
CONFIG_BACKUP_SOCKET = 19
CONFIG_SOC_LIMITS = 29
CONFIG_BACKUP_RESERVE = 30
CONFIG_TASK = 39

CMD_FUNC_CONFIG = 254
CMD_ID_CONFIG_WRITE = 38

# Work mode as written on config field 25 and read back on `254/39 f25`.
WORK_MODES: dict[str, int] = {
    "self_powered": 0,
    "intelligent_plus": 1,
    "custom": 2,
}

TASK_ADD = 1
TASK_UPDATE = 2
TASK_REMOVE = 3

_TASK_KINDS: dict[str, int] = {"charge": 1, "discharge": 2}

# The model's rated power, as a constant. Not read from the device: the app's
# own limits sit at or below it, and the device clamps a setpoint to them
# silently rather than rejecting the frame, so no limit can be enforced here.
MAX_TASK_POWER_W = 2500

MINUTES_PER_DAY = 1440


def _build_envelope(
    pdata: bytes,
    device_sn: str,
    seq: int = 0,
    cmd_id: int = CMD_ID_CONFIG_WRITE,
) -> bytes:
    """Wrap a config-write payload in the ES22 envelope.

    Field order and values copied from the captured app frames:

      1  pdata            9  cmd_id = 38       16 version = 4
      2  src = 32        10  data_len          23 from = "Android"
      3  dest = 2        11  need_ack = 1      26 device serial
      4  d_src = 1       14  seq               27 device serial
      5  d_dest = 1      15  product_id
      8  cmd_func = 254

    The device answers on the same topic with src and dest swapped and the
    same payload, which the integration logs but does not parse.
    """
    if not device_sn:
        raise ValueError("device_sn is required for /app/ routing")
    if seq == 0:
        seq = int(time.time() * 1000) & 0x7FFFFFFF

    serial = device_sn.encode("ascii")
    header = bytearray()
    header.extend(encode_field_bytes(1, pdata))
    header.extend(encode_field_varint(2, 32))
    header.extend(encode_field_varint(3, 2))
    header.extend(encode_field_varint(4, 1))
    header.extend(encode_field_varint(5, 1))
    header.extend(encode_field_varint(8, CMD_FUNC_CONFIG))
    header.extend(encode_field_varint(9, cmd_id))
    header.extend(encode_field_varint(10, len(pdata)))
    header.extend(encode_field_varint(11, 1))
    header.extend(encode_field_varint(14, seq))
    # Sent as a negative number by the app, so it goes out as the 64-bit
    # two's complement the wire format uses for one.
    header.extend(encode_field_varint(15, (1 << 64) - 116))
    header.extend(encode_field_varint(16, 4))
    header.extend(encode_field_bytes(23, b"Android"))
    header.extend(encode_field_bytes(26, serial))
    header.extend(encode_field_bytes(27, serial))
    return encode_field_bytes(1, bytes(header))


def build_work_mode_payload(mode: str, device_sn: str, seq: int = 0) -> bytes:
    """Build a work-mode SET frame (config field 25)."""
    wire_value = WORK_MODES.get(mode)
    if wire_value is None:
        raise ValueError(f"unknown work mode {mode!r}")
    pdata = encode_field_varint(1, CONFIG_WORK_MODE) + encode_field_varint(
        CONFIG_WORK_MODE, wire_value
    )
    return _build_envelope(pdata, device_sn, seq)


def build_soc_limits_payload(
    charge: int, discharge: int, device_sn: str, seq: int = 0
) -> bytes:
    """Build a SoC limit SET frame (config field 29).

    Both limits travel in one frame because the field holds both. A caller
    changing one has to supply the other's current value; sending a lone limit
    was never observed from the app.
    """
    for name, value in (("charge", charge), ("discharge", discharge)):
        if not 0 <= value <= 100:
            raise ValueError(f"{name} limit must be 0..100, got {value}")
    if discharge >= charge:
        raise ValueError(
            f"discharge limit ({discharge}) must stay below the charge limit ({charge})"
        )

    inner = encode_field_varint(1, charge) + encode_field_varint(2, discharge)
    pdata = encode_field_varint(1, CONFIG_SOC_LIMITS) + encode_field_bytes(
        CONFIG_SOC_LIMITS, inner
    )
    return _build_envelope(pdata, device_sn, seq)


def build_backup_socket_payload(
    enabled: bool, device_sn: str, seq: int = 0
) -> bytes:
    """Build a backup socket SET frame (config field 19).

    The app calls this the backup socket control. It reads back on
    `254/39 f19.1`.
    """
    pdata = encode_field_varint(1, CONFIG_BACKUP_SOCKET) + encode_field_bytes(
        CONFIG_BACKUP_SOCKET, encode_field_varint(1, 1 if enabled else 0)
    )
    return _build_envelope(pdata, device_sn, seq)


def build_backup_reserve_payload(
    enabled: bool, reserve_pct: int, device_sn: str, seq: int = 0
) -> bytes:
    """Build a backup reserve SET frame (config field 30).

    Both parts travel together, the same way the SoC limits do, because the
    field holds both: `{1: on/off, 2: reserve %}` is how the device reports
    it back on `254/39 f30`.
    """
    if not 0 <= reserve_pct <= 100:
        raise ValueError(f"reserve_pct must be 0..100, got {reserve_pct}")

    inner = encode_field_varint(1, 1 if enabled else 0) + encode_field_varint(
        2, reserve_pct
    )
    pdata = encode_field_varint(1, CONFIG_BACKUP_RESERVE) + encode_field_bytes(
        CONFIG_BACKUP_RESERVE, inner
    )
    return _build_envelope(pdata, device_sn, seq)


def build_task_payload(
    kind: str,
    start_min: int,
    end_min: int,
    power_w: int,
    device_sn: str,
    enabled: bool = True,
    operation: int = TASK_UPDATE,
    charge_soc_target: int = 100,
    seq: int = 0,
) -> bytes:
    """Build a scheduled-task SET frame (config field 39).

    Grammar read off the app's own writes:

        39.1.1  operation: 1 add, 2 update, 3 remove
        39.1.2  task type: 1 charge, 2 discharge
        39.1.3  enabled, 39.1.4 its inverse, 39.1.5 always 1
        39.1.7  window: a packed varint, start in the low 16 bits and end in
                the high 16, both minutes since midnight
        39.1.8  charge settings {1: 1, 2: 0, 3: per device}, where the per
                device block is {1: serial, 2: target SoC %, 3: watts}
        39.1.9  discharge power, system wide: {1: watts}

    Written on config field 39, read back on `254/39 f40`. The grammar under
    `.1` is identical on both sides, so `40.1.8.3.3` carries exactly what
    `39.1.8.3.3` was given and only the top-level number differs. The parser
    reads the charge power from `40.1.8.3.3` and the discharge power from
    `40.1.9.1`. See the config field table above for which writes read back
    where.

    A charge task carries more than a power: the app shows it as "Charging
    source", "Max grid charging power" and "Charge limit", and the last of
    those is the target SoC in `39.1.8.3.2`. It has to be passed in and
    preserved, or every power change would silently reset the task to charge
    to 100%.

    The app calls both powers a *maximum*, and with a smart meter linked they
    behave as one: the device runs closed-loop against the meter and will not
    discharge into an export. With no meter linked it runs open-loop and the
    power becomes an absolute setpoint, confirmed on hardware at 1400 W into
    a house drawing far less.
    """
    task_type = _TASK_KINDS.get(kind)
    if task_type is None:
        raise ValueError(f"kind must be charge or discharge, got {kind!r}")
    if operation not in (TASK_ADD, TASK_UPDATE, TASK_REMOVE):
        raise ValueError(f"operation must be 1, 2 or 3, got {operation}")
    if not 0 <= start_min < MINUTES_PER_DAY or not 0 < end_min <= MINUTES_PER_DAY:
        raise ValueError("window must be minutes since midnight")
    if start_min >= end_min:
        raise ValueError(f"start ({start_min}) must be before end ({end_min})")
    if not 0 <= power_w <= MAX_TASK_POWER_W:
        raise ValueError(f"power_w must be 0..{MAX_TASK_POWER_W}, got {power_w}")
    if not 0 <= charge_soc_target <= 100:
        raise ValueError(
            f"charge_soc_target must be 0..100, got {charge_soc_target}"
        )

    task = bytearray()
    task.extend(encode_field_varint(1, operation))
    task.extend(encode_field_varint(2, task_type))
    task.extend(encode_field_varint(3, 1 if enabled else 0))
    task.extend(encode_field_varint(4, 0 if enabled else 1))
    task.extend(encode_field_varint(5, 1))
    task.extend(encode_field_bytes(7, encode_varint((end_min << 16) | start_min)))

    if kind == "charge":
        per_device = (
            encode_field_bytes(1, device_sn.encode("ascii"))
            + encode_field_varint(2, charge_soc_target)
            + encode_field_varint(3, power_w)
        )
        task.extend(
            encode_field_bytes(
                8,
                encode_field_varint(1, 1)
                + encode_field_varint(2, 0)
                + encode_field_bytes(3, per_device),
            )
        )
    else:
        task.extend(encode_field_bytes(9, encode_field_varint(1, power_w)))

    pdata = encode_field_varint(1, CONFIG_TASK) + encode_field_bytes(
        CONFIG_TASK, encode_field_bytes(1, bytes(task))
    )
    return _build_envelope(pdata, device_sn, seq)
