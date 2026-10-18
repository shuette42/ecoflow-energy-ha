"""SET command builder for the Ocean 2 family (`RE11` and its variants).

Two writes so far: the Backup Reserve (`254/38`, a property command) and the
Storm Guard switch (`254/37`, an action command). Each frame reproduces the
app's own write, captured from the `/app/{uid}/{sn}/thing/property/set` topic
while the setting was changed in the app, and `tests/test_ocean2_commands.py`
rebuilds it byte for byte (the serial masked to a run of `X`).

The device answers on the same topic with `is_ack` set and `cmd_ok` in the
payload. Five app writes of the reserve shape were acknowledged with
`cmd_ok = 1` and applied. Five of the storm guard shape were acknowledged
with `cmd_ok = 1` too; a full snapshot reported the written state after
four of them (the fifth, 2026-10-13 on, was followed by the off write
before any snapshot).

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
# 254 / 37 is the generic action command: the payload names an `action_id` and
# carries that action's own parameter message.
CMD_ID_ACTION_CMD = 37

# Header endpoints as observed on the app's writes to this device. The
# STREAM AC 5000 sends dest 2 for the same command.
SRC_APP = 32
DEST_DEVICE = 96

# Payload field numbers of the property command.
PROPERTY_BACKUP_RESERVE = 10
PROPERTY_EDEV_SOC_RATIO = 92

# Payload field numbers of the action command, and the one action built here.
ACTION_ID_FIELD = 1
ACTION_STORM = 17
ACTION_STORM_PARAM = 25
# StormParam: `storm_pattern_enable` is the switch, `storm_pattern_open_flag`
# is sent as an explicit 0 next to it when the app switches the guard off.
STORM_PATTERN_ENABLE = 1
STORM_PATTERN_OPEN_FLAG = 2
# Field 3 is sent as an explicit 0 only by the RE41's app (full_param form);
# what it means as a write is unconfirmed, so it is named by its number (the
# parser calls the read side, 21.3, the end time).
STORM_PARAM_FIELD_3 = 3

# Acknowledgement payload: `cmd_ok` is a plain bool on field 2. A property
# reply (254/38) has no field 1 of its own; an action reply (254/37) echoes
# the `action_id` there.
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
    pdata = encode_field_varint(
        PROPERTY_BACKUP_RESERVE, reserve_pct
    ) + encode_field_varint(PROPERTY_EDEV_SOC_RATIO, edev_soc_ratio)
    return _build_envelope(pdata, CMD_ID_PROPERTY_CMD, device_sn, seq)


def build_ocean2_storm_guard_payload(
    enable: bool, device_sn: str, seq: int = 0, full_param: bool = False
) -> bytes:
    """Build a Storm Guard SET frame (action 17, StormParam on field 25).

    Two shapes, each the one a model's own app sent. The StormParam fields
    are presence-tracked, so the two are different messages to a reader that
    checks presence, and each model gets the bytes it was seen to accept.

    With ``full_param=False`` (the RE11): ``{1: 17, 25: {1: 1}}`` switches
    the guard on and ``{1: 17, 25: {1: 0, 2: 0}}`` switches it off. The zeros
    are sent explicitly, as that app sends them, and the on form omits
    field 2. Both were acknowledged with ``cmd_ok = 1``, and the unit then
    reported the new state on field 21.1 of its next full snapshot.

    With ``full_param=True`` (the RE41): all three StormParam fields are
    sent, ``{1: x, 2: 0, 3: 0}``, on and off alike, which is what the RE41's
    own app sent for both switches. The unit acknowledged the off write with
    ``cmd_ok = 1`` and reported the on state on field 21.1.

    The envelope is the Backup Reserve one with ``cmd_id = 37``. The app's
    own storm guard writes carry the same header field for field (source 32,
    destination 96, ``from`` "ios", the serial in three fields); only the
    sequence number and the serial differ between requests. The header fields
    and their order are listed in :func:`build_ocean2_backup_reserve_payload`.
    """
    if not isinstance(enable, bool):
        raise ValueError(f"enable must be a bool, got {type(enable).__name__}")

    storm_param = encode_field_varint(STORM_PATTERN_ENABLE, 1 if enable else 0)
    if full_param:
        storm_param += encode_field_varint(STORM_PATTERN_OPEN_FLAG, 0)
        storm_param += encode_field_varint(STORM_PARAM_FIELD_3, 0)
    elif not enable:
        storm_param += encode_field_varint(STORM_PATTERN_OPEN_FLAG, 0)
    pdata = encode_field_varint(ACTION_ID_FIELD, ACTION_STORM) + encode_field_bytes(
        ACTION_STORM_PARAM, storm_param
    )
    return _build_envelope(pdata, CMD_ID_ACTION_CMD, device_sn, seq)


def _build_envelope(pdata: bytes, cmd_id: int, device_sn: str, seq: int) -> bytes:
    """Wrap ``pdata`` in the request header the app sends to this family."""
    if not device_sn:
        raise ValueError("device_sn is required for /app/ routing")
    if seq == 0:
        seq = int(time.time() * 1000) & 0x7FFFFFFF

    serial = device_sn.encode("ascii")
    header = bytearray()
    header.extend(encode_field_bytes(1, pdata))
    header.extend(encode_field_varint(2, SRC_APP))
    header.extend(encode_field_varint(3, DEST_DEVICE))
    header.extend(encode_field_varint(4, 1))
    header.extend(encode_field_varint(5, 1))
    header.extend(encode_field_varint(7, 3))
    header.extend(encode_field_varint(8, CMD_FUNC_PROPERTY))
    header.extend(encode_field_varint(9, cmd_id))
    header.extend(encode_field_varint(10, len(pdata)))
    header.extend(encode_field_varint(11, 1))
    header.extend(encode_field_varint(14, seq))
    header.extend(encode_field_varint(16, 4))
    header.extend(encode_field_bytes(23, b"ios"))
    header.extend(encode_field_bytes(25, serial))
    header.extend(encode_field_bytes(26, serial))
    header.extend(encode_field_bytes(27, serial))
    return encode_field_bytes(1, bytes(header))


def _ack_pdata(payload: bytes, cmd_id: int) -> bytes | None:
    """Return the readable payload of the first acknowledgement of ``cmd_id``.

    ``None`` when the frame is not one (wrong command pair, ``is_ack`` not
    set, not a frame at all) or when the header masks its payload (non-zero
    ``enc_type``): nothing on this path unmasks it, so its payload cannot be
    told from a refusal.
    """
    try:
        headers, _ = decode_header_message(payload)
    except Exception:  # noqa: BLE001 - malformed frames must not break the MQTT thread
        return None

    for header in headers:
        if (
            header.get("cmd_func") != CMD_FUNC_PROPERTY
            or header.get("cmd_id") != cmd_id
            or header.get("is_ack") != 1
        ):
            continue
        if header.get("enc_type"):
            continue
        try:
            return bytes.fromhex(header.get("pdata") or "")
        except (TypeError, ValueError):
            continue
    return None


def parse_ocean2_property_ack(payload: bytes) -> bool | None:
    """Read the device's answer to a property write.

    Returns ``None`` when the frame is not an acknowledgement of a property
    command (wrong command pair, or ``is_ack`` not set, or not a frame at
    all) and also when the header masks its payload (non-zero ``enc_type``):
    nothing on this path unmasks it, so its payload cannot be told from a
    refusal. Otherwise ``True`` when the payload carries ``cmd_ok = 1`` and
    ``False`` when it carries ``cmd_ok = 0`` or no ``cmd_ok`` at all: the
    device answered, and did not say it applied the write.

    The payload is read as received.
    """
    pdata = _ack_pdata(payload, CMD_ID_PROPERTY_CMD)
    if pdata is None:
        return None
    return _read_varint_field(pdata, ACK_CMD_OK_FIELD) == 1


def parse_ocean2_action_ack(payload: bytes, action_id: int) -> bool | None:
    """Read the device's answer to an action write (``254/37``).

    A sibling of :func:`parse_ocean2_property_ack` rather than a command id
    switch inside it: the action reply echoes the ``action_id`` on field 1, and
    that echo is what tells this caller's answer from the answer to another
    action sent in the same minute. A property reply has no such field, so one
    function with one signature would have to guess which of the two it read.

    Returns ``None`` when the frame is not an acknowledgement of an action
    command, is masked (see ``_ack_pdata``), or answers a different
    ``action_id`` than the one asked about; ``True`` for ``cmd_ok = 1``;
    ``False`` for ``cmd_ok = 0`` or no ``cmd_ok`` at all.
    """
    pdata = _ack_pdata(payload, CMD_ID_ACTION_CMD)
    if pdata is None or _read_varint_field(pdata, ACTION_ID_FIELD) != action_id:
        return None
    return _read_varint_field(pdata, ACK_CMD_OK_FIELD) == 1
