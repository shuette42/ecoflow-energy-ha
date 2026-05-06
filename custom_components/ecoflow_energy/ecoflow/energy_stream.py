"""EcoFlow Protobuf encoder for EnergyStreamSwitch activation.

Builds the binary payload that activates / deactivates the energy_stream_report
on EcoFlow devices.  Must be sent after every MQTT connect and periodically
every 15-25 s to keep the stream alive.

Reverse-engineered from EcoFlow Portal JavaScript bundle.
"""

import time

from .proto_encoding import encode_field_bytes, encode_field_varint


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

    # Header — portal-exact field order:
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
    header.extend(encode_field_varint(16, 3))            # isRwCmd = 3
    header.extend(encode_field_varint(17, 1))            # isQueue = 1

    # Send_Header_Msg: field 1 = Header (length-delimited)
    return encode_field_bytes(1, bytes(header))


def build_powerocean_soc_set_payload(
    backup_reserve_pct: int,
    solar_surplus_pct: int,
    seq: int = 0,
    device_sn: str = "",
) -> bytes:
    """Build SysBatChgDsgSet (cmd_id=112) replicating the EcoFlow app frame.

    The app sends THREE fields in the inner pdata for every SoC-limit
    change (verified 2026-05-06 against live app traffic):

        field 1 = max_charge_soc       (always 100 in observed app traffic)
        field 2 = backup_reserve_pct   (the "Backup-Reserve" slider)
        field 4 = solar_surplus_pct    (the "Überschüssige Solarenergie" slider)

    The older 2-field builder (`build_soc_limit_set_payload`) sent only
    fields 1+2, which the device sometimes silently ignored. Using the
    full 3-field payload plus the extended app envelope (check_type,
    from="ios", device_sn) is accepted reliably.

    Constraint enforced by the device UI: backup_reserve_pct <= solar_surplus_pct.
    Sending values that violate this constraint can be rejected with
    SetAck result=1.

    Args:
        backup_reserve_pct: 0..100, the Backup-Reserve floor SoC.
        solar_surplus_pct: 0..100, the Überschüssige-Solarenergie threshold.
        seq: Sequence number. Default 0 generates from timestamp.
        device_sn: Device serial number for envelope field 25.

    Returns:
        Binary protobuf payload (Send_Header_Msg).
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

    pdata = (
        encode_field_varint(1, 100)                       # max_charge_soc, constant
        + encode_field_varint(2, backup_reserve_pct)      # backup reserve floor
        + encode_field_varint(4, solar_surplus_pct)       # solar surplus threshold
    )
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
    # Only 2 fields — firmware does not reliably accept the payload with 4 fields.
    payload_bytes = (
        encode_field_varint(1, max_charge_soc)
        + encode_field_varint(2, min_discharge_soc)
    )

    # Header — portal-exact field order (same as EnergyStreamSwitch, cmd_id=112):
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
    header.extend(encode_field_varint(16, 3))                    # isRwCmd = 3
    header.extend(encode_field_varint(17, 1))                    # isQueue = 1

    # Send_Header_Msg: field 1 = Header (length-delimited)
    return encode_field_bytes(1, bytes(header))


def _build_powerocean_set_envelope(
    pdata: bytes,
    cmd_id: int,
    seq: int = 0,
    device_sn: str = "",
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
      16 payload_ver = 3 (was: is_rw_cmd)
      17 is_queue = 1
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
    """
    if seq == 0:
        seq = int(time.time() * 1000) & 0x7FFFFFFF

    header = bytearray()
    header.extend(encode_field_bytes(1, pdata))                  # pdata
    header.extend(encode_field_varint(2, 32))                    # src
    header.extend(encode_field_varint(3, 96))                    # dest
    header.extend(encode_field_varint(4, 1))                     # d_src
    header.extend(encode_field_varint(5, 1))                     # d_dest
    header.extend(encode_field_varint(7, 3))                     # check_type
    header.extend(encode_field_varint(8, 96))                    # cmd_func
    header.extend(encode_field_varint(9, cmd_id))                # cmd_id
    header.extend(encode_field_varint(10, len(pdata)))           # data_len
    header.extend(encode_field_varint(11, 1))                    # need_ack
    header.extend(encode_field_varint(14, seq))                  # seq
    header.extend(encode_field_varint(16, 3))                    # payload_ver
    header.extend(encode_field_varint(17, 1))                    # is_queue
    header.extend(encode_field_bytes(23, b"ios"))                # from
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
