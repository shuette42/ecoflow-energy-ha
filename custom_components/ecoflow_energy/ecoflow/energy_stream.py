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
