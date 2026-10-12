"""SET command builder for the Ocean 2 family (`RE11` and its variants).

One write so far: the Backup Reserve. The frame reproduces the app's own
write, captured from the `/app/{uid}/{sn}/thing/property/set` topic while the
setting was changed in the app, and `tests/test_ocean2_commands.py` rebuilds
it byte for byte (the serial masked to a run of `X`).

The device answers on the same topic with `is_ack` set and `cmd_ok` in the
payload. Five app writes of this shape were acknowledged with `cmd_ok = 1`
and applied.

This family does not share `stream_ac5000_commands._build_envelope`. The
STREAM AC 5000 envelope hardcodes destination 2, a product id and the app
string "Android", and carries the serial in only two fields; the Ocean 2
frame goes to destination 96, carries no product id and carries the serial
in three fields.

The payload is `{10: backup reserve %, 92: edev SoC ratio %}`. The app
always sends both fields together, so the builder does too: field 92 is the
ratio the device last reported and is re-asserted at its current value, which
is idempotent. The caller reads a fresh snapshot first and passes it in.
"""

from __future__ import annotations

import time

from .delta3_commands import _read_varint_field
from .proto.decoder import decode_header_message
from .proto_encoding import encode_field_bytes, encode_field_varint

# Header values. 254 / 38 is the generic property command; the request and
# its acknowledgement share the pair, the acknowledgement adds `is_ack`.
CMD_FUNC_PROPERTY = 254
CMD_ID_PROPERTY_CMD = 38

# Header endpoints as observed on the app's writes to this device. The
# STREAM AC 5000 sends dest 2 for the same command.
SRC_APP = 32
DEST_DEVICE = 96

# Payload field numbers (the app schema's property command message).
PROPERTY_BACKUP_RESERVE = 10
PROPERTY_EDEV_SOC_RATIO = 92

# Acknowledgement payload: `cmd_ok` is a plain bool on field 2.
ACK_CMD_OK_FIELD = 2


def build_ocean2_backup_reserve_payload(
    reserve_pct: int, edev_soc_ratio: int, device_sn: str, seq: int = 0
) -> bytes:
    """Build a Backup Reserve SET frame (payload fields 10 and 92).

    Both fields travel together because the app sends them together and the
    device acknowledged exactly that shape. ``edev_soc_ratio`` is the value
    the device last reported on field 92; writing it back unchanged leaves it
    as it was.

    Header, in the order the app sends it:

      1  pdata            9  cmd_id = 38        16 version = 4
      2  src = 32        10  data_len           23 from = "ios"
      3  dest = 96       11  need_ack = 1       25 device serial
      4  d_src = 1       14  seq                26 device serial
      5  d_dest = 1                             27 device serial
      7  check_type = 3
      8  cmd_func = 254

    There is no product id and no payload version on the request; both appear
    only on the reply. The capture cannot show that fields 25, 26 and 27 carry
    the same serial (the sanitizer masks any long upper-case run to the same
    shape), so that is the reading consistent with the sibling envelopes, not
    an observation.
    """
    for name, value in (
        ("reserve_pct", reserve_pct),
        ("edev_soc_ratio", edev_soc_ratio),
    ):
        if not 0 <= value <= 100:
            raise ValueError(f"{name} must be 0..100, got {value}")
    if not device_sn:
        raise ValueError("device_sn is required for /app/ routing")
    if seq == 0:
        seq = int(time.time() * 1000) & 0x7FFFFFFF

    pdata = encode_field_varint(
        PROPERTY_BACKUP_RESERVE, reserve_pct
    ) + encode_field_varint(PROPERTY_EDEV_SOC_RATIO, edev_soc_ratio)

    serial = device_sn.encode("ascii")
    header = bytearray()
    header.extend(encode_field_bytes(1, pdata))
    header.extend(encode_field_varint(2, SRC_APP))
    header.extend(encode_field_varint(3, DEST_DEVICE))
    header.extend(encode_field_varint(4, 1))
    header.extend(encode_field_varint(5, 1))
    header.extend(encode_field_varint(7, 3))
    header.extend(encode_field_varint(8, CMD_FUNC_PROPERTY))
    header.extend(encode_field_varint(9, CMD_ID_PROPERTY_CMD))
    header.extend(encode_field_varint(10, len(pdata)))
    header.extend(encode_field_varint(11, 1))
    header.extend(encode_field_varint(14, seq))
    header.extend(encode_field_varint(16, 4))
    header.extend(encode_field_bytes(23, b"ios"))
    header.extend(encode_field_bytes(25, serial))
    header.extend(encode_field_bytes(26, serial))
    header.extend(encode_field_bytes(27, serial))
    return encode_field_bytes(1, bytes(header))


def parse_ocean2_property_ack(payload: bytes) -> bool | None:
    """Read the device's answer to a property write.

    Returns ``None`` when the frame is not an acknowledgement of a property
    command (wrong command pair, or ``is_ack`` not set, or not a frame at
    all). Otherwise ``True`` when the payload carries ``cmd_ok = 1`` and
    ``False`` when it carries ``cmd_ok = 0`` or no ``cmd_ok`` at all: the
    device answered, and did not say it applied the write.

    The payload is read as received. A header that masks its payload
    (``enc_type``) is unmasked one layer up, before this is called.
    """
    try:
        headers, _ = decode_header_message(payload)
    except Exception:  # noqa: BLE001 - malformed frames must not break the MQTT thread
        return None

    for header in headers:
        if (
            header.get("cmd_func") != CMD_FUNC_PROPERTY
            or header.get("cmd_id") != CMD_ID_PROPERTY_CMD
            or header.get("is_ack") != 1
        ):
            continue
        try:
            pdata = bytes.fromhex(header.get("pdata") or "")
        except (TypeError, ValueError):
            continue
        return _read_varint_field(pdata, ACK_CMD_OK_FIELD) == 1
    return None
