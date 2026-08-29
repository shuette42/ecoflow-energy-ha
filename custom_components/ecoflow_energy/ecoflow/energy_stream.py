"""EcoFlow Protobuf encoder for EnergyStreamSwitch activation.

Builds the binary payload that activates / deactivates the energy_stream_report
on EcoFlow devices.  Must be sent after every MQTT connect and periodically
every 15-25 s to keep the stream alive.

Reverse-engineered from EcoFlow Portal JavaScript bundle.
"""

import time

from .parsers.powerocean_proto import SCHEDULE_MAX_INDEX
from .proto_encoding import encode_field_bytes, encode_field_varint, encode_varint


def build_energy_stream_activate_payload(seq: int = 0) -> bytes:
    """Build the portal-exact Send_Header_Msg protobuf payload.

    Activates energy_stream_report on the EcoFlow device.
    Must be repeated every 10-25 s.

    Args:
        seq: Sequence number for the protobuf header.  Default 0 generates
             a value from the current timestamp.

    Returns:
        33-byte protobuf payload.
    """
    if seq == 0:
        seq = int(time.time() * 1000) & 0x7FFFFFFF

    # EnergyStreamSwitch: field 1 = true (emsOpenEnergyStream)
    switch_bytes = encode_field_varint(1, 1)

    # Header - portal-exact field order:
    header = bytearray()
    header.extend(encode_field_bytes(1, switch_bytes))   # pdata as field 1 (nested)
    header.extend(encode_field_varint(2, 32))            # src = 32 (Client/App)
    header.extend(encode_field_varint(3, 96))            # dest = 96 (EMS)
    header.extend(encode_field_varint(4, 1))             # dSrc = 1
    header.extend(encode_field_varint(5, 1))             # dDest = 1
    header.extend(encode_field_varint(8, 96))            # cmdFunc = 96 (EMS)
    header.extend(encode_field_varint(9, 97))            # cmdId = 97 (EnergyStreamSwitch)
    header.extend(encode_field_varint(10, len(switch_bytes)))  # dataLen
    header.extend(encode_field_varint(11, 1))            # needAck = 1
    header.extend(encode_field_varint(14, seq))          # seq (timestamp)
    header.extend(encode_field_varint(16, 3))            # version = 3
    header.extend(encode_field_varint(17, 1))            # payloadVer = 1

    # Send_Header_Msg: field 1 = Header (length-delimited)
    return encode_field_bytes(1, bytes(header))


def build_powerocean_soc_set_payload(
    backup_reserve_pct: int,
    solar_surplus_pct: int,
    seq: int = 0,
    device_sn: str = "",
    surplus_field: str = "both",
) -> bytes:
    """Build SysBatChgDsgSet (cmd_id=112) replicating the EcoFlow app frame.

    Field mapping (verified against live cloud quota + device echo):

        field 1 = max_charge_soc       (sys_bat_chg_up_limit, always 100)
        field 2 = backup_reserve_pct   (sys_bat_dsg_down_limit, "Backup-Reserve" slider)
        field 3 = solar_surplus_pct    (sys_bat_backup_ratio, EMS internal state)
        field 4 = solar_surplus_pct    (dev_soc / socDev, App-UI state)

    Field 3 and field 4 are two separate views of the same logical slider.
    The device's EMS controller reads field 3 (sys_bat_backup_ratio) and
    publishes it via JTS1EmsChangeReport. The EcoFlow app reads field 4
    (dev_soc / socDev) from the cloud quota cache. Writing only one of
    them desynchronizes app and device:

        only field 3 -> EMS controls correctly, but the app shows the
            previous value because socDev is never updated.
        only field 4 -> the cloud cache reflects the new value, but the
            EMS keeps running with the old threshold.

    Setting both keeps app, cloud quota, and EMS aligned.

    Args:
        surplus_field: "both" (default) writes field 3 + field 4. "field3"
            and "field4" exist only as diagnostic shortcuts for probe scripts.
    """
    if not 0 <= backup_reserve_pct <= 100:
        raise ValueError(
            f"backup_reserve_pct must be 0..100, got {backup_reserve_pct}"
        )
    if not 0 <= solar_surplus_pct <= 100:
        raise ValueError(
            f"solar_surplus_pct must be 0..100, got {solar_surplus_pct}"
        )
    if backup_reserve_pct > solar_surplus_pct:
        raise ValueError(
            f"backup_reserve_pct ({backup_reserve_pct}) must be <= "
            f"solar_surplus_pct ({solar_surplus_pct})"
        )
    if surplus_field not in ("field3", "field4", "both"):
        raise ValueError(
            f"surplus_field must be field3|field4|both, got {surplus_field!r}"
        )

    pdata = (
        encode_field_varint(1, 100)
        + encode_field_varint(2, backup_reserve_pct)
    )
    if surplus_field in ("field3", "both"):
        pdata += encode_field_varint(3, solar_surplus_pct)
    if surplus_field in ("field4", "both"):
        pdata += encode_field_varint(4, solar_surplus_pct)
    return _build_powerocean_set_envelope(pdata, cmd_id=112, seq=seq, device_sn=device_sn)


def build_soc_limit_set_payload(
    max_charge_soc: int,
    min_discharge_soc: int,
    seq: int = 0,
) -> bytes:
    """Build SysBatChgDsgSet protobuf payload for PowerOcean SoC limits.

    Sets battery charge upper limit and discharge lower limit via the WSS
    Protobuf protocol (Enhanced Mode only).  Same header pattern as
    EnergyStreamSwitch but with cmd_id=112.

    Only fields 1+2 are sent.  The proto definition (SysBatChgDsgSet) defines
    4 fields but live testing shows the device rejects charge limit changes
    when fields 3+4 are included.  Sending only 2 fields matches the
    original working implementation (v1.6.0).

    Note: Only min_discharge_soc (field 2) is confirmed working via live
    testing.  max_charge_soc (field 1) is sent as pass-through for protocol
    completeness, but the device does not reliably accept charge limit
    changes through this command.

    Args:
        max_charge_soc: Max charge SoC (50-100).  Sent as pass-through;
            not reliably accepted by the device.
        min_discharge_soc: Min discharge SoC (0-30).  Confirmed working
            via live testing.
        seq: Sequence number.  Default 0 generates from timestamp.

    Returns:
        Binary protobuf payload (Send_Header_Msg).
    """
    if seq == 0:
        seq = int(time.time() * 1000) & 0x7FFFFFFF

    # SysBatChgDsgSet: field 1 = sys_bat_chg_up_limit, field 2 = sys_bat_dsg_down_limit
    # Only 2 fields - firmware does not reliably accept the payload with 4 fields.
    payload_bytes = (
        encode_field_varint(1, max_charge_soc)
        + encode_field_varint(2, min_discharge_soc)
    )

    # Header - portal-exact field order (same as EnergyStreamSwitch, cmd_id=112):
    header = bytearray()
    header.extend(encode_field_bytes(1, payload_bytes))          # pdata
    header.extend(encode_field_varint(2, 32))                    # src = 32 (Client/App)
    header.extend(encode_field_varint(3, 96))                    # dest = 96 (EMS)
    header.extend(encode_field_varint(4, 1))                     # dSrc = 1
    header.extend(encode_field_varint(5, 1))                     # dDest = 1
    header.extend(encode_field_varint(8, 96))                    # cmdFunc = 96 (EMS)
    header.extend(encode_field_varint(9, 112))                   # cmdId = 112 (SysBatChgDsgSet)
    header.extend(encode_field_varint(10, len(payload_bytes)))   # dataLen
    header.extend(encode_field_varint(11, 1))                    # needAck = 1
    header.extend(encode_field_varint(14, seq))                  # seq (timestamp)
    header.extend(encode_field_varint(16, 3))                    # version = 3
    header.extend(encode_field_varint(17, 1))                    # payloadVer = 1

    # Send_Header_Msg: field 1 = Header (length-delimited)
    return encode_field_bytes(1, bytes(header))


def _build_powerocean_set_envelope(
    pdata: bytes,
    cmd_id: int,
    seq: int = 0,
    device_sn: str = "",
    *,
    check_type: int | None = 3,
    product_id: int | None = None,
    version: int = 3,
    source: str = "ios",
) -> bytes:
    """Build the PowerOcean SET envelope around a pre-encoded inner pdata.

    Common header for all `cmd_func=96` SET commands. Replicates the byte
    layout the official EcoFlow Android/iOS app uses on
    `/app/{userId}/{sn}/thing/property/set`.

    Sniffed app-frame fields (verified 2026-05-06 against live app traffic):
      1  pdata (length-delimited)
      2  src = 32
      3  dest = 96
      4  d_src = 1
      5  d_dest = 1
      7  check_type = 3                  (NEW: app sends this)
      8  cmd_func = 96
      9  cmd_id
      10 data_len
      11 need_ack = 1
      14 seq
      16 version = 3
      17 payload_ver = 1
      23 from = "ios"                    (NEW: client identifier)
      25 device_sn = SN                  (NEW: target device)

    The earlier short envelope (without 7/23/25) worked for some commands
    but not for SoC-limit changes. The full envelope replicates the app
    payload byte-for-byte and is accepted reliably.

    Args:
        pdata: Pre-encoded inner protobuf payload bytes.
        cmd_id: Command ID (98, 99, 112, 115, ...).
        seq: Sequence number. Default 0 generates from timestamp.
        device_sn: Device serial number. If empty, the SN field is omitted
            (same as the older short envelope).
        check_type: Field 7. None omits it. The app does not send it on every
            command: the scheduled-task frames carry no field 7 at all.
        product_id: Field 15. None omits it, which is what the SoC and feed
            commands were captured doing. The scheduled-task frames carry 1.
        version: Field 16. 3 on the frames this envelope was first written
            for, 19 on the scheduled-task frames.
        source: Field 23, the client identifier. The frames this envelope was
            first written for came from an iOS client, the scheduled-task
            capture from an Android one. Nothing shows the device reading it,
            but matching the capture removes a variable.
    """
    if seq == 0:
        seq = int(time.time() * 1000) & 0x7FFFFFFF

    header = bytearray()
    header.extend(encode_field_bytes(1, pdata))                  # pdata
    header.extend(encode_field_varint(2, 32))                    # src
    header.extend(encode_field_varint(3, 96))                    # dest
    header.extend(encode_field_varint(4, 1))                     # d_src
    header.extend(encode_field_varint(5, 1))                     # d_dest
    if check_type is not None:
        header.extend(encode_field_varint(7, check_type))        # check_type
    header.extend(encode_field_varint(8, 96))                    # cmd_func
    header.extend(encode_field_varint(9, cmd_id))                # cmd_id
    header.extend(encode_field_varint(10, len(pdata)))           # data_len
    header.extend(encode_field_varint(11, 1))                    # need_ack
    header.extend(encode_field_varint(14, seq))                  # seq
    if product_id is not None:
        header.extend(encode_field_varint(15, product_id))       # product_id
    header.extend(encode_field_varint(16, version))              # version
    header.extend(encode_field_varint(17, 1))                    # payload_ver
    header.extend(encode_field_bytes(23, source.encode("ascii")))  # from
    if device_sn:
        header.extend(encode_field_bytes(25, device_sn.encode("ascii")))

    return encode_field_bytes(1, bytes(header))


def build_work_mode_set_payload(work_mode: int, seq: int = 0) -> bytes:
    """Build SysWorkModeSet (cmd_id=98) for PowerOcean work mode selection.

    Sends only field 1 (`ems_word_mode` enum varint). The proto defines
    additional oneof fields for TouParam / BackupParam but those are
    out of scope - the EcoFlow app sends field 1 alone for plain mode
    switches.

    WorkMode enum (verified against `_WORK_MODE_MAP` in parsers/powerocean.py):
        0 = SELFUSE, 1 = TOU, 2 = BACKUP, 3 = DBG, 4 = AC_MAKEUP,
        5 = DRM, 6 = REMOTE_SCHED, 7 = STANDBY, 8 = SOC_CALIB,
        9 = TIMER, 10 = FCR, 11 = THIRD_PARTY, 12 = AI_SCHEDULE, 13 = KRAKEN

    User-exposed subset (HA select): SELFUSE, TOU, BACKUP, AI_SCHEDULE.

    Args:
        work_mode: WorkMode enum value (0-13).
        seq: Sequence number. Default 0 generates from timestamp.
    """
    if not 0 <= work_mode <= 13:
        raise ValueError(f"work_mode must be 0-13, got {work_mode}")

    pdata = encode_field_varint(1, work_mode)
    return _build_powerocean_set_envelope(pdata, cmd_id=98, seq=seq)


def build_feed_mode_set_payload(feed_mode: int, seq: int = 0) -> bytes:
    """Build SysFeedPowerSet (cmd_id=115) with field 2 only - mode selector.

    Field 2 (`ems_feed_mode` uint32) is the discrete mode enum. Field 1
    (`ems_max_feed_pwr` float) is in the same oneof and skipped here -
    field 2 is the canonical path the official app uses.

    Feed mode enum (verified against `_FEED_MODE_MAP` in parsers/powerocean.py):
        0 = off (feed disabled)
        1 = no_limit (feed everything available)
        2 = zero (zero-feed, RegEnergie 0% compliance)
        3 = limit (limited by `ems_feed_pwr` set separately)

    Args:
        feed_mode: Feed mode enum (0-3).
        seq: Sequence number. Default 0 generates from timestamp.
    """
    if not 0 <= feed_mode <= 3:
        raise ValueError(f"feed_mode must be 0-3, got {feed_mode}")

    pdata = encode_field_varint(2, feed_mode)
    return _build_powerocean_set_envelope(pdata, cmd_id=115, seq=seq)


def build_feed_power_set_payload(feed_power_w: int, seq: int = 0) -> bytes:
    """Build SysFeedPowerSet (cmd_id=115) with field 4 only - power cap.

    Field 4 (`ems_feed_pwr` uint32, watts) sets the absolute feed-in cap.
    Only effective when feed_mode is 3 (limit). Sending this alone does
    not change the mode - that requires `build_feed_mode_set_payload`.

    Args:
        feed_power_w: Feed power cap in watts (0-10000 typical).
        seq: Sequence number. Default 0 generates from timestamp.
    """
    if not 0 <= feed_power_w <= 100000:
        raise ValueError(f"feed_power_w must be 0-100000, got {feed_power_w}")

    pdata = encode_field_varint(4, feed_power_w)
    return _build_powerocean_set_envelope(pdata, cmd_id=115, seq=seq)


def build_feed_mode_and_power_set_payload(
    feed_mode: int, feed_power_w: int, seq: int = 0,
) -> bytes:
    """Build SysFeedPowerSet (cmd_id=115) with mode (field 2) AND power (field 4).

    Combined SET for Mode=Limit (3) which requires a power cap to be set
    in the same message. Field 1 (float) is skipped via the oneof rule -
    only field 2 (uint32 mode) is set, plus field 4 (uint32 power-cap watts).

    Args:
        feed_mode: Feed mode enum (0-3).
        feed_power_w: Feed power cap in watts (0-100000).
        seq: Sequence number. Default 0 generates from timestamp.
    """
    if not 0 <= feed_mode <= 3:
        raise ValueError(f"feed_mode must be 0-3, got {feed_mode}")
    if not 0 <= feed_power_w <= 100000:
        raise ValueError(f"feed_power_w must be 0-100000, got {feed_power_w}")

    pdata = bytearray()
    pdata.extend(encode_field_varint(2, feed_mode))     # field 2 = mode
    pdata.extend(encode_field_varint(4, feed_power_w))  # field 4 = power cap
    return _build_powerocean_set_envelope(bytes(pdata), cmd_id=115, seq=seq)


def build_backup_event_set_payload(
    enable: bool, start_ts: int, end_ts: int, seq: int = 0,
) -> bytes:
    """Build SysBackupEventSet (cmd_id=99) - storm-watch / backup window.

    Triggers a pre-charge to 100% before the event window, then maintains
    backup state through the window. Used by the EcoFlow app for the
    "Storm Watch" / scheduled backup feature.

    Fields:
        2: ems_backup_enable_disenabl (bool) - enable=true, disable=false
        3: ems_backup_start_time (uint32 unix ts)
        4: ems_backup_end_time (uint32 unix ts)

    Args:
        enable: True to start/enable, False to cancel.
        start_ts: Backup window start (unix epoch seconds).
        end_ts: Backup window end (unix epoch seconds).
        seq: Sequence number. Default 0 generates from timestamp.
    """
    if start_ts < 0 or end_ts < 0:
        raise ValueError("timestamps must be non-negative")
    if enable and start_ts >= end_ts:
        raise ValueError(f"start_ts ({start_ts}) must be < end_ts ({end_ts})")

    pdata = bytearray()
    pdata.extend(encode_field_varint(2, 1 if enable else 0))   # bool as varint
    pdata.extend(encode_field_varint(3, start_ts))
    pdata.extend(encode_field_varint(4, end_ts))

    return _build_powerocean_set_envelope(bytes(pdata), cmd_id=99, seq=seq)


# Scheduled charge tasks (cmd_func 96, cmd_id 125). The device holds a list of
# timer tasks; each one arms a charge window with a power setting. Only slot 1
# has ever been seen on the wire.
TIMER_TASK_CMD_ID = 125

# `is_cfg`, field 2. The write path here only ever modifies an existing task.
# 1 (create) and 3 (delete) exist on the wire and are deliberately unreachable
# from this builder, see `build_timer_task_set_payload`.
_TIMER_TASK_MODIFY = 2

# Watts, and the bound is ours rather than the device's. Every write in the
# capture succeeded, so no rejection was ever seen and the ceiling the firmware
# enforces is unknown. The only two values on file are 1000 and 1500. Nothing
# else in this repo carries a PowerOcean grid-charge ceiling either: the 2600 W
# on `max_grid_input_power_w` belongs to a different device family. So this
# refuses what cannot be a watt setting on this hardware at all and leaves the
# real limit to the device, which is the side that knows it. It moves the day a
# capture shows a higher figure accepted or a lower one refused.
TIMER_TASK_POWER_MIN_W = 0
TIMER_TASK_POWER_MAX_W = 30000

_TIMER_TASK_OPERATIONS = ("arm", "disarm", "power")

# The fields a power change has to hand back to the device untouched. Their
# encoding is only partly resolved, so they are echoed from the last read and
# never composed here.
_TIMER_TASK_ECHOED = ("task_type", "time_mode", "time_param", "time_table")


def _timer_task_int(name: str, value: object, minimum: int, maximum: int) -> int:
    """Check one varint argument, refusing bools and out-of-range values."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be {minimum}..{maximum}, got {value}")
    return value


def build_timer_task_set_payload(
    operation: str,
    task_index: int,
    *,
    device_sn: str,
    power_w: int | None = None,
    task_type: int | None = None,
    time_mode: int | None = None,
    time_param: int | None = None,
    time_table: int | None = None,
    armed: bool | None = None,
    seq: int = 0,
) -> bytes:
    """Build an EmsTimerTaskCfg write for one existing PowerOcean schedule.

    Three operations, all of them `is_cfg=2` (modify) against a slot the owner
    already created in the app:

    ``arm``
        Six-byte frame, fields 2, 3 and 4. Arms the schedule.
    ``disarm``
        Four-byte frame, fields 2 and 3. Disarms it. Field 4 is **omitted**,
        never sent as zero: a false bool is not serialised in proto3, both
        sides of the wire express "off" as the missing field, and an explicit
        `4=0` is a no-op that leaves the schedule armed.
    ``power``
        Full body, fields 2, 3, 6, 7, 8, 9, 10 and optionally 4. Changes the
        charge power while handing the schedule back unchanged.

    Create and delete are refused rather than left reachable. A create has to
    send `time_mode` and `time_param`, and each has exactly one sample with no
    reading that survives falsification, so building one means copying a
    recurrence off a stranger's device and hoping it means "every day". Delete
    is four trivial bytes and a one-way door: with create impossible, an owner
    who deletes from here can only get the schedule back in the app.

    `armed` and the two-frame sequence
        A full-body write clears the enable flag as a side effect. The app
        handles that by sending the power change without field 4 and re-arming
        1.5 s later with a separate six-byte frame. This builder can produce
        either shape: `armed=True` folds the enable flag into the one frame and
        closes the window where a lost re-arm silently disarms the schedule,
        while `armed=False` reproduces the app's frame exactly, so the two-frame
        sequence is a second call to this same function rather than a rewrite.
        The single frame is composition, not observation - every one of its
        fields is observed on `is_cfg=2` and the whole union is observed on
        `is_cfg=1`, but that exact union on a modify is not - so it is the shape
        the hardware round trip has to confirm.

    The echoed fields
        `task_type`, `time_mode`, `time_param` and `time_table` belong to the
        device, not to the caller. They go out exactly as the last read reported
        them. `time_table` is the decoded varint the read path publishes, and it
        is re-encoded here into the length-delimited block the wire carries.

    Args:
        operation: One of ``arm``, ``disarm``, ``power``.
        task_index: Device slot, 1-based.
        device_sn: Target serial for envelope field 25. Every captured frame
            carries it and no frame without it was tried.
        power_w: Charge power in watts. ``power`` only.
        task_type: Field 6, echoed. ``power`` only.
        time_mode: Field 8, echoed. ``power`` only.
        time_param: Field 9, echoed. ``power`` only.
        time_table: Field 10 as the decoded varint, echoed. ``power`` only.
        armed: Whether the schedule is armed and should stay armed.
            ``power`` only, and required there.
        seq: Sequence number. Default 0 generates from timestamp.

    Returns:
        Binary protobuf payload ready to publish on the SET topic.

    Raises:
        ValueError: unknown or refused operation, or a value out of range.
        TypeError: an argument of the wrong type, or one that does not belong
            to the requested operation.
    """
    if operation in ("create", "delete"):
        raise ValueError(
            f"timer task operation {operation!r} is not built: create needs two "
            "recurrence fields whose meaning is unresolved, and delete cannot "
            "be undone without a create"
        )
    if operation not in _TIMER_TASK_OPERATIONS:
        raise ValueError(
            f"timer task operation must be one of "
            f"{', '.join(_TIMER_TASK_OPERATIONS)}, got {operation!r}"
        )
    if not device_sn:
        raise ValueError("device_sn is required for a timer task write")

    _timer_task_int("task_index", task_index, 1, SCHEDULE_MAX_INDEX)

    supplied = {
        "power_w": power_w,
        "task_type": task_type,
        "time_mode": time_mode,
        "time_param": time_param,
        "time_table": time_table,
    }

    if operation in ("arm", "disarm"):
        # A short frame carries none of these. Accepting and dropping them
        # would let a caller believe a power change went out.
        extra = sorted(name for name, value in supplied.items() if value is not None)
        if extra:
            raise TypeError(
                f"{operation} carries no {', '.join(extra)}; "
                "the short frame holds only the slot"
            )
        if armed is not None:
            raise TypeError(
                f"{operation} sets the enable flag itself; armed does not apply"
            )

        pdata = encode_field_varint(2, _TIMER_TASK_MODIFY)
        pdata += encode_field_varint(3, task_index)
        if operation == "arm":
            pdata += encode_field_varint(4, 1)
        # disarm omits field 4 entirely. Not `encode_field_varint(4, 0)`.
    else:
        missing = sorted(name for name, value in supplied.items() if value is None)
        if missing:
            raise TypeError(
                f"power needs {', '.join(missing)}; the echoed fields come from "
                "the last read and are never composed here"
            )
        if not isinstance(armed, bool):
            raise TypeError(
                "power needs armed=True or armed=False: a full body clears the "
                "enable flag, so the caller has to say whether to carry it"
            )

        watts = _timer_task_int(
            "power_w", power_w, TIMER_TASK_POWER_MIN_W, TIMER_TASK_POWER_MAX_W
        )
        # All four are declared uint32 in the message definition, and every
        # value the read path has produced fits. A wider one would mean the
        # field is not what the read assumes, which is worth a refusal rather
        # than a silently reshaped write.
        echoed = {
            name: _timer_task_int(name, supplied[name], 0, 2**32 - 1)
            for name in _TIMER_TASK_ECHOED
        }

        pdata = encode_field_varint(2, _TIMER_TASK_MODIFY)
        pdata += encode_field_varint(3, task_index)
        if armed:
            pdata += encode_field_varint(4, 1)
        pdata += encode_field_varint(6, echoed["task_type"])
        pdata += encode_field_varint(7, watts)
        pdata += encode_field_varint(8, echoed["time_mode"])
        pdata += encode_field_varint(9, echoed["time_param"])
        pdata += encode_field_bytes(10, encode_varint(echoed["time_table"]))

    # The scheduled-task frames carry a different envelope from the SoC and
    # feed commands: no check_type, product_id 1, version 19, and an Android
    # client identifier. Reproduced as captured rather than normalised.
    return _build_powerocean_set_envelope(
        pdata,
        cmd_id=TIMER_TASK_CMD_ID,
        seq=seq,
        device_sn=device_sn,
        check_type=None,
        product_id=1,
        version=19,
        source="android",
    )


def build_device_get_all_payload(seq: int = 0) -> bytes:
    """Build a protobuf get-all request for non-Enhanced devices (SmartPlug, Delta).

    Requests a full state dump from the device. The response arrives as a
    protobuf heartbeat on the /thing/property/get_reply topic.

    Based on observed EcoFlow Portal network traffic.

    Args:
        seq: Sequence number. Default 0 generates from timestamp.

    Returns:
        Binary protobuf payload (Send_Header_Msg).
    """
    if seq == 0:
        seq = int(time.time() * 1000) & 0x7FFFFFFF

    # Header: no pdata, no cmdId, no cmdFunc
    header = bytearray()
    header.extend(encode_field_varint(2, 32))        # src = 32 (App)
    header.extend(encode_field_varint(3, 32))        # dest = 32
    header.extend(encode_field_varint(14, seq))      # seq
    header.extend(encode_field_bytes(23, b"app"))    # from = "app"

    return encode_field_bytes(1, bytes(header))


# The backup reserve floor is not the fixed 3 % the entity declares. The device
# holds the reserve at least three points above the discharge limit and carries
# it upwards whenever that limit rises, so the floor travels with the limit.
#
# Measured on a BK31 (#264, and on the same hardware in #98 against
# v1.17.0-beta.16 and v1.17.0-beta.20): raising the discharge limit raised the
# reserve with it and kept the distance, and a reserve below the floor was
# corrected by the device with live telemetry restoring the real value within
# about 30 seconds.
STREAM_BACKUP_RESERVE_MARGIN = 3


def stream_backup_reserve_floor(
    min_discharge_soc: int | None, declared_floor: int, declared_ceiling: int
) -> int:
    """Return the lowest backup reserve the device accepts, in percent.

    While the discharge limit has not been reported the declared floor stands.
    A slider pinned shut before the first telemetry frame is worse than one
    that is briefly too permissive, and a value the device will not take is
    corrected by the device anyway.

    The result never passes the declared ceiling. A discharge limit at the top
    of its own range would otherwise put the floor above the ceiling, and an
    inverted range makes the entity unusable in Home Assistant - a worse
    failure than the over-permissive point it trades for.

    This derives what is *shown*, never what is sent: the value the user picks
    travels to the device untouched, so the device stays the authority on what
    it accepts.
    """
    if not isinstance(min_discharge_soc, int):
        return declared_floor
    return min(declared_ceiling, min_discharge_soc + STREAM_BACKUP_RESERVE_MARGIN)


def build_stream_backup_reserve_payload(
    backup_soc: int, device_sn: str, seq: int = 0,
) -> bytes:
    """Build the Stream AC Pro (BkSeries) Backup-Reserve SET frame.

    ConfigWrite { cfg_backup_reverse_soc = 102 } on the /app/ WSS topic. The
    Stream family uses the same ConfigWrite path as the Delta 3: the SET is
    cmd_func=254 / cmd_id=17, while cmd_id=18 is the device's reply/ack (its
    field 102 read-back is mapped in stream_proto.py). Observed app MQTT
    traffic confirms the write goes out on cmd_id=17; the earlier cmd_id=18
    frame was the reply id and the device silently ignored it.

    The frame is byte-for-byte the Delta 3 ConfigWrite frame with the single
    changed setting (field 102) as the pdata, so this delegates to the
    hardware-verified builder to keep the two in lockstep:
      1  pdata            8  cmd_func = 254   14 seq
      2  src = 32         9  cmd_id = 17      16 version = 3
      3  dest = 2        10  data_len         17 payload_ver = 1
      4  d_src = 1       11  need_ack = 1     25 device_sn (string)
      5  d_dest = 1
      7  check_type = 3

    Args:
        backup_soc: Backup reserve SoC percentage (0-100).
        device_sn: Device serial number (required for /app/ routing).
        seq: Sequence number (0 = auto-generate from timestamp).

    Returns:
        Binary protobuf payload ready to publish on the SET topic.
    """
    if not 0 <= backup_soc <= 100:
        raise ValueError(f"backup_soc must be 0..100, got {backup_soc}")

    return build_delta3_config_write_payload(
        config_field=102,       # cfg_backup_reverse_soc
        value=backup_soc,
        device_sn=device_sn,
        seq=seq,
        nested=False,
    )


def build_stream_soc_limits_payload(
    max_charge_soc: int,
    min_discharge_soc: int,
    backup_soc: int,
    device_sn: str,
    seq: int = 0,
    timestamp: int | None = None,
) -> bytes:
    """Build the grouped Stream AC Pro charge/discharge-limit SET frame.

    A live app capture showed these values travelling as one ConfigWrite
    pdata: field 6 is the Unix timestamp, 33 is the upper charge limit, 34 is
    the lower discharge limit and 102 is backup reserve. The same capture
    carried an `ios` source header, which is reproduced here.

    Only what the wire format itself requires is rejected: percentages outside
    0..100, a discharge limit above the charge limit, and a timestamp that is
    not a uint32. Any further relation between the three values would be a
    claim about device behaviour that no capture supports.
    """
    for name, value in (
        ("max_charge_soc", max_charge_soc),
        ("min_discharge_soc", min_discharge_soc),
        ("backup_soc", backup_soc),
    ):
        if not 0 <= value <= 100:
            raise ValueError(f"{name} must be 0..100, got {value}")
    if min_discharge_soc > max_charge_soc:
        raise ValueError(
            f"min_discharge_soc ({min_discharge_soc}) must be <= "
            f"max_charge_soc ({max_charge_soc})"
        )
    if timestamp is None:
        timestamp = int(time.time())
    if not 0 <= timestamp <= 0xFFFFFFFF:
        raise ValueError(f"timestamp must be a uint32, got {timestamp}")

    return build_delta3_config_write_payload(
        config_field=33,
        value=max_charge_soc,
        device_sn=device_sn,
        seq=seq,
        companions=(
            (6, timestamp),
            (34, min_discharge_soc),
            (102, backup_soc),
        ),
        source="ios",
    )


def build_stream_led_brightness_payload(
    brightness_pct: int,
    device_sn: str,
    seq: int = 0,
) -> bytes:
    """Build the hardware-confirmed Stream AC Pro LED ConfigWrite frame."""
    if not 0 <= brightness_pct <= 100:
        raise ValueError(f"brightness_pct must be 0..100, got {brightness_pct}")
    return build_delta3_config_write_payload(
        config_field=384,
        value=brightness_pct,
        device_sn=device_sn,
        seq=seq,
        source="ios",
    )


def build_delta3_config_write_payload(
    config_field: int,
    value: int,
    device_sn: str,
    seq: int = 0,
    nested: bool = False,
    companions: tuple[tuple[int, int], ...] = (),
    submessage: bytes | None = None,
    source: str | None = None,
) -> bytes:
    """Build a Delta 3 ConfigWrite SET frame for the app WebSocket channel.

    A frame normally carries exactly one changed setting: the pdata holds only
    the field being written, the device keeps everything else untouched. Some
    settings are the exception and are only processed as a group; `companions`
    carries the remaining members of such a group. A group frame missing a
    member is dropped by the device **without any answer at all**, so silence
    rather than a rejection is the symptom to look for.

    The header replicates the app frame so the device routes it on the /app/
    topic - verified against hardware (ack plus readback of the new value).

      1  pdata            8  cmd_func = 254   14 seq
      2  src = 32         9  cmd_id = 17      16 version = 3
      3  dest = 2        10  data_len         17 payload_ver = 1
      4  d_src = 1       11  need_ack = 1     25 device_sn (string)
      5  d_dest = 1
      7  check_type = 3

    Args:
        config_field: ConfigWrite field number of the setting.
        value: Varint value to write (booleans as 0/1).
        device_sn: Device serial number (required for /app/ routing).
        seq: Sequence number (0 = auto-generate from timestamp).
        nested: True for settings wrapped in a submessage (inner field 1).
        companions: further (field, value) pairs that belong in the same frame.
            Not combinable with `nested`.
        submessage: pre-encoded body for settings whose value is a message
            rather than a scalar, written as the length-delimited value of
            `config_field`. `value` is ignored when this is given. Not
            combinable with `nested` or `companions`.
        source: optional app identifier for header field 23, for example
            ``ios``. The Stream AC Pro frames carry it because the captures
            they were reproduced from did; the Delta 3 frames omit it.

    Returns:
        Binary protobuf payload ready to publish on the SET topic.
    """
    if seq == 0:
        seq = int(time.time() * 1000) & 0x7FFFFFFF

    if submessage is not None:
        pdata = encode_field_bytes(config_field, submessage)
    elif nested:
        pdata = encode_field_bytes(config_field, encode_field_varint(1, value))
    else:
        # Ascending field order, which is what the app's protobuf runtime
        # emits. Whether the device cares is unproven; matching the app costs
        # nothing and removes one variable.
        fields = sorted([(config_field, value), *companions])
        pdata = b"".join(encode_field_varint(f, v) for f, v in fields)

    header = bytearray()
    header.extend(encode_field_bytes(1, pdata))              # pdata
    header.extend(encode_field_varint(2, 32))                # src = 32 (App)
    header.extend(encode_field_varint(3, 2))                 # dest = 2
    header.extend(encode_field_varint(4, 1))                 # d_src
    header.extend(encode_field_varint(5, 1))                 # d_dest
    header.extend(encode_field_varint(7, 3))                 # check_type
    header.extend(encode_field_varint(8, 254))               # cmd_func
    header.extend(encode_field_varint(9, 17))                # cmd_id = ConfigWrite
    header.extend(encode_field_varint(10, len(pdata)))       # data_len
    header.extend(encode_field_varint(11, 1))                # need_ack
    header.extend(encode_field_varint(14, seq))              # seq
    header.extend(encode_field_varint(16, 3))                # version
    header.extend(encode_field_varint(17, 1))                # payload_ver
    if source is not None:
        header.extend(encode_field_bytes(23, source.encode("ascii")))  # from
    header.extend(encode_field_bytes(25, device_sn.encode("ascii")))  # deviceSn

    return encode_field_bytes(1, bytes(header))


def build_stream_ac_outlet_payload(
    outlet: int,
    enabled: bool,
    device_sn: str,
    seq: int = 0,
) -> bytes:
    """Build a Stream AC Pro AC outlet ConfigWrite frame."""
    if outlet not in (1, 2):
        raise ValueError(f"outlet must be 1 or 2, got {outlet}")
    return build_delta3_config_write_payload(
        config_field=380 if outlet == 1 else 381,
        value=int(enabled),
        device_sn=device_sn,
        seq=seq,
        source="ios",
    )


def build_energy_stream_deactivate_payload(seq: int = 0) -> bytes:
    """Build the payload to deactivate energy_stream_report."""
    if seq == 0:
        seq = int(time.time() * 1000) & 0x7FFFFFFF

    switch_bytes = encode_field_varint(1, 0)  # emsOpenEnergyStream = false

    header = bytearray()
    header.extend(encode_field_bytes(1, switch_bytes))
    header.extend(encode_field_varint(2, 32))
    header.extend(encode_field_varint(3, 96))
    header.extend(encode_field_varint(4, 1))
    header.extend(encode_field_varint(5, 1))
    header.extend(encode_field_varint(8, 96))
    header.extend(encode_field_varint(9, 97))
    header.extend(encode_field_varint(10, len(switch_bytes)))
    header.extend(encode_field_varint(11, 1))
    header.extend(encode_field_varint(14, seq))
    header.extend(encode_field_varint(16, 3))
    header.extend(encode_field_varint(17, 1))

    return encode_field_bytes(1, bytes(header))
