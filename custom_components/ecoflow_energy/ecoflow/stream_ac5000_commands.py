"""SET command builders for the STREAM AC 5000 family (`ES22` and `ES21`).

The EcoFlow Android app publishes its writes to the same
`/app/{uid}/{sn}/thing/property/set` topic the integration can subscribe to,
so these frames are not inferred: they are the app's own frames with the
payload swapped. Four of them are reproduced byte for byte in
`tests/test_stream_ac5000_commands.py`, and the SoC limit, work mode and task
power writes were each confirmed on live hardware on 2026-08-03, with the
device acknowledging and reading the new value back.

The envelope below is not specific to either model number. The fourth vector
is an app write to a live `ES21` (#231), and our own builder reproduces it
byte for byte from its recorded payload, which is what opened the control
gate to that prefix.

This device needs its own envelope rather than
`build_delta3_config_write_payload`: this family writes on `cmd_id = 38`, not
17, and its header carries a product id, a version of 4, a `from` string and
the serial in two fields, none of which the Delta 3 frame has.

The payload is always "field 1 names the config field being written, then that
field's value":

    19  backup socket {1: on/off}
    25  work mode: 0 self-powered, 1 Intelligent Mode+, 2 custom
    29  SoC limits {1: charge %, 2: discharge %}, both in one frame
    30  backup reserve {1: on/off, 2: reserve %}, both in one frame
    39  scheduled task, see build_task_payload

A write number is not a readback number. Four of the six config fields
written here are read back under the number they were written on and two are
not; the table on `CONFIG_*` below says which is which, and it is the table
that lists all six.
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
#   10  grid power       -> one field, both power limits, one subfield each.
#                           The output setpoint reads back on `254/39 f10.1`,
#                           the reading the Max Grid-tied Output Power sensor
#                           shows, and is written as `{1: watts, 4: ?, 5: ?}`;
#                           the two unnamed companions belong to the unit and
#                           are read back before every write. The input
#                           setpoint reads back on `f10.2` and is written as
#                           `{2: watts}` alone, with no companions.
CONFIG_WORK_MODE = 25
CONFIG_BACKUP_SOCKET = 19
CONFIG_SOC_LIMITS = 29
CONFIG_BACKUP_RESERVE = 30
CONFIG_TASK = 39
# One config field carries both power limits, one subfield each, so it is
# named for the field rather than for either setting: `.1` is the grid-tied
# output setpoint, `.2` the grid input one. A second constant of the same
# value for the input side would be a synonym, not a distinction.
CONFIG_GRID_POWER = 10

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
    """Wrap a config-write payload in this family's envelope.

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


def build_grid_output_power_payload(
    power_w: int,
    field_4: int,
    field_5: int,
    device_sn: str,
    seq: int = 0,
) -> bytes:
    """Build a grid-tied output power SET frame (config field 10).

    This is the setpoint the app calls the grid-tied output, and the reading
    the Max Grid-tied Output Power sensor already shows. It is the one write
    in this file confirmed on the model it is offered to rather than
    inherited: the frame it reproduces was recorded from a live `ES21`
    changing this very setting, and the device reported the new value back
    two seconds later (#231).

    ``field_4`` and ``field_5`` travel with the setpoint because the app
    sends all three together. Nothing here knows what they mean, and their
    values differ per unit, so the caller passes what this device last
    reported rather than a constant. Guessing them would send one unit's
    numbers to another.

    No upper bound is enforced here, because the device reports its own on
    `f10.6` and the control offers that instead. A constant in this builder
    would be the rated 2500 W, which is above the ceiling on at least one
    real unit and therefore not a bound at all. What the device does with a
    setpoint above its ceiling is untested: the recording only carries
    in-range writes.
    """
    if power_w < 0:
        raise ValueError(f"power_w must not be negative, got {power_w}")
    for name, value in (("field_4", field_4), ("field_5", field_5)):
        if value < 0:
            raise ValueError(f"{name} must not be negative, got {value}")

    inner = (
        encode_field_varint(1, power_w)
        + encode_field_varint(4, field_4)
        + encode_field_varint(5, field_5)
    )
    pdata = encode_field_varint(1, CONFIG_GRID_POWER) + encode_field_bytes(
        CONFIG_GRID_POWER, inner
    )
    return _build_envelope(pdata, device_sn, seq)


def build_grid_input_power_payload(
    power_w: int,
    device_sn: str,
    seq: int = 0,
) -> bytes:
    """Build a Max Grid Input Power SET frame (config field 10, subfield 2).

    The setting the app calls "netzgekoppelte Eingangsleistung", and the
    reading the Max Grid Input Power sensor already shows on `f10.2`. It is
    the ceiling the device charges under, which is what separates it from the
    scheduled charge setpoint beside it (#177).

    It shares config field 10 with the grid-tied output setpoint and is
    written differently: the app sends the watts alone on subfield 2, without
    the two companion values it sends with the output setpoint on subfield 1.
    That is not an economy taken here - it is what four recorded writes from a
    live `ES22` do, and they are the vectors in
    `tests/test_stream_ac5000_commands.py`. Sending the companions along would
    write the output setpoint at the same time.

    No upper bound is enforced here. The device carries its own and clamps a
    setpoint to it silently rather than refusing the frame, so a constant in
    this builder could only be wrong in one direction or the other; the
    control offers the bound instead. Nothing on the wire has been seen to
    report an input ceiling the way `f10.6` reports the output one, and the
    device's answer to one of these writes says only that the frame arrived:
    the four recorded acknowledgements are the same two bytes whatever value
    they answer.
    """
    if power_w < 0:
        raise ValueError(f"power_w must not be negative, got {power_w}")

    inner = encode_field_varint(2, power_w)
    pdata = encode_field_varint(1, CONFIG_GRID_POWER) + encode_field_bytes(
        CONFIG_GRID_POWER, inner
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
    task_slot: int | None = None,
) -> bytes:
    """Build a scheduled-task SET frame (config field 39).

    Grammar read off the app's own writes:

        39.1.1  operation: 1 add, 2 update, 3 remove
        39.1.2  the task's number in the app's list. Read off the app's own
                writes as 1 for charge and 2 for discharge, which is what this
                builder sends by default and what keeps the two tasks this
                integration writes from colliding. It is more likely a slot
                than a type: on 2026-08-08 the app removed the charge task
                numbered 1 and added a discharge task numbered 1 in one frame.
                `task_slot` overrides it so a removal or an update can name the
                number the device reported rather than one derived from `kind`.
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
    discharge power becomes an absolute setpoint, confirmed on hardware at
    1400 W into a house drawing far less, and again by a second owner at
    789 W against an 800 W setpoint while 1.8 kW went to the grid.

    The charge power does not follow that pattern and is unproven. The same
    owner measured a full grid charge running at about 2330 W against a
    1250 W setpoint, with an external meter in the supply line, and an earlier
    charge with no setpoint at all ran at about 2570 W for the same energy.
    The device does take the value: it reports it back on `40.1.8.3.3` within
    seconds. So it is stored and not acted on.

    What outranked it is unknown. Backup Reserve stood at 100% in that run, a
    task is only acted on in Custom mode, `8.1` and `8.2` below are unexplained
    constants, and the whole-day window is one no recorded app write uses - the
    charge fixture this builder is pinned against covers 13:00 to 16:00. That
    last one is weak: the discharge task uses the same window and does act.

    Do not close this by inventing a different frame. What is pinned here is a
    reproduction of a recorded app write, and guessing past it would lose that.
    """
    task_type = _TASK_KINDS.get(kind)
    if task_type is None:
        raise ValueError(f"kind must be charge or discharge, got {kind!r}")
    if task_slot is not None and not 0 < task_slot <= 0xFFFF:
        raise ValueError(f"task_slot must be 1..65535, got {task_slot}")
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
    task.extend(encode_field_varint(2, task_type if task_slot is None else task_slot))
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
