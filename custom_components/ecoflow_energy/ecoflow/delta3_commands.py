"""Delta 3 generation SET command builders.

The Delta 3 line uses a flat JSON envelope with a fixed command id, documented
in the official IoT Developer API for the DELTA 3 MAX PLUS. Every control shares
the same envelope and differs only in the `params` payload:

    {
        "cmdId": 17, "cmdFunc": 254, "dest": 2,
        "dirDest": 1, "dirSrc": 1, "needAck": true,
        "params": {"cfgAcOutOpen": true}
    }

The transport wrapper (`sn`, `id`, `version`) is added by the coordinator, so
these builders return the command body only.

Value ranges below are the vendor's own bounds. They are enforced here rather
than only on the entity, so a command built from any caller stays inside what
the device accepts.

App-login entries have no HTTP endpoint. There the same controls travel as a
binary ConfigWrite frame on the app WebSocket channel, so every control also
carries its ConfigWrite field number here. Keeping both in one table means the
JSON key and the binary field can never drift apart.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from .energy_stream import build_delta3_config_write_payload
from .proto.decoder import decode_header_message

# Shared envelope. `dirSrc` follows the HTTP examples in the official docs; the
# MQTT example spells it `dirSoc`, which reads like a typo in the vendor docs
# (the field is a direction *source*). Verified against hardware before release.
_CMD_ID = 17
_CMD_FUNC = 254
_DEST = 2
_DIR_DEST = 1
_DIR_SRC = 1


class Delta3Switch(NamedTuple):
    """One boolean control, with both wire representations."""

    params_key: str      # JSON params key on the HTTP endpoint
    config_field: int    # ConfigWrite field number on the app channel


class Delta3Number(NamedTuple):
    """One numeric control, with its vendor bounds and both wire representations."""

    params_key: str
    minimum: int
    maximum: int
    config_field: int


# Switch controls: entity key -> wire mapping. All take a plain bool.
DELTA3_SWITCH_PARAMS: dict[str, Delta3Switch] = {
    "ac_out_switch": Delta3Switch("cfgAcOutOpen", 76),
    "ac2_out_switch": Delta3Switch("cfgAc2OutOpen", 377),
    "dc_12v_out_switch": Delta3Switch("cfgDc12vOutOpen", 18),
    "xboost_switch": Delta3Switch("cfgXboostEn", 25),
    "beeper_switch": Delta3Switch("cfgBeepEn", 9),
    "bypass_out_disable_switch": Delta3Switch("cfgBypassOutDisable", 26),
}

# Energy backup is the only nested payload: {"cfgEnergyBackup": {"energyBackupEn": bool}}
# On the binary channel it is equally nested: field 43 wraps an inner field 1.
DELTA3_ENERGY_BACKUP_KEY = "energy_backup_switch"
DELTA3_ENERGY_BACKUP_PARAMS_KEY = "cfgEnergyBackup"
DELTA3_ENERGY_BACKUP_INNER_KEY = "energyBackupEn"
DELTA3_ENERGY_BACKUP_FIELD = 43

# Number controls: entity key -> wire mapping. Bounds are vendor-documented.
DELTA3_NUMBER_PARAMS: dict[str, Delta3Number] = {
    "backup_reserve_soc": Delta3Number("cfgBackupReverseSoc", 0, 50, 102),
    "max_charge_soc": Delta3Number("cfgMaxChgSoc", 50, 100, 33),
    "min_discharge_soc": Delta3Number("cfgMinDsgSoc", 0, 30, 34),
}

# Reverse lookup for the binary path: JSON params key -> ConfigWrite field.
_PARAMS_KEY_TO_FIELD: dict[str, int] = {
    **{entry.params_key: entry.config_field for entry in DELTA3_SWITCH_PARAMS.values()},
    **{entry.params_key: entry.config_field for entry in DELTA3_NUMBER_PARAMS.values()},
}

# ConfigWriteAck: cmd_func 254 / cmd_id 18, config_ok == 1 means "applied".
ACK_CMD_FUNC = 254
ACK_CMD_ID = 18
ACK_CONFIG_OK = 1


def _envelope(params: dict[str, Any], need_ack: bool = True) -> dict[str, Any]:
    """Wrap params in the shared Delta 3 command envelope."""
    return {
        "cmdId": _CMD_ID,
        "cmdFunc": _CMD_FUNC,
        "dest": _DEST,
        "dirDest": _DIR_DEST,
        "dirSrc": _DIR_SRC,
        "needAck": need_ack,
        "params": params,
    }


def build_switch_command(entity_key: str, turn_on: bool) -> dict[str, Any] | None:
    """Build a SET command for a Delta 3 switch.

    Returns None for an unknown key so the caller can log instead of sending
    a payload the device would reject.
    """
    if entity_key == DELTA3_ENERGY_BACKUP_KEY:
        return _envelope(
            {
                DELTA3_ENERGY_BACKUP_PARAMS_KEY: {
                    DELTA3_ENERGY_BACKUP_INNER_KEY: bool(turn_on)
                }
            }
        )

    entry = DELTA3_SWITCH_PARAMS.get(entity_key)
    if entry is None:
        return None
    return _envelope({entry.params_key: bool(turn_on)})


def build_number_command(entity_key: str, value: float) -> dict[str, Any] | None:
    """Build a SET command for a Delta 3 number, clamped to the vendor range."""
    entry = DELTA3_NUMBER_PARAMS.get(entity_key)
    if entry is None:
        return None
    clamped = max(entry.minimum, min(entry.maximum, int(round(value))))
    return _envelope({entry.params_key: clamped})


def build_proto_command(
    command: dict[str, Any], device_sn: str, seq: int = 0
) -> bytes | None:
    """Translate a Delta 3 command body into a binary ConfigWrite frame.

    Used for app-login entries, which have no HTTP endpoint. The command body
    is the same one the HTTP path sends, so both modes share the value ranges
    and the control-to-parameter mapping.

    Returns None when the command carries anything other than exactly one
    known parameter, so the caller can log instead of sending a frame the
    device would reject.
    """
    params = command.get("params") or {}
    if len(params) != 1:
        return None
    params_key, raw_value = next(iter(params.items()))

    if params_key == DELTA3_ENERGY_BACKUP_PARAMS_KEY:
        if not isinstance(raw_value, dict):
            return None
        inner = raw_value.get(DELTA3_ENERGY_BACKUP_INNER_KEY)
        if inner is None:
            return None
        return build_delta3_config_write_payload(
            DELTA3_ENERGY_BACKUP_FIELD,
            int(bool(inner)),
            device_sn,
            seq=seq,
            nested=True,
        )

    config_field = _PARAMS_KEY_TO_FIELD.get(params_key)
    if config_field is None or isinstance(raw_value, (dict, list, str)):
        return None
    return build_delta3_config_write_payload(
        config_field, int(raw_value), device_sn, seq=seq
    )


class Delta3ConfigAck(NamedTuple):
    """Decoded ConfigWriteAck: which field was written and whether it took."""

    action_id: int | None    # echoes the written ConfigWrite field number
    config_ok: int | None    # 1 = applied

    @property
    def applied(self) -> bool:
        return self.config_ok == ACK_CONFIG_OK


def parse_config_write_ack(payload: bytes) -> Delta3ConfigAck | None:
    """Decode a Delta 3 ConfigWriteAck frame, or None if it is not one."""
    try:
        headers, _ = decode_header_message(payload)
    except Exception:  # noqa: BLE001 - malformed frames must not break the MQTT thread
        return None

    for header in headers:
        if header.get("cmd_func") != ACK_CMD_FUNC or header.get("cmd_id") != ACK_CMD_ID:
            continue
        try:
            pdata = bytes.fromhex(header.get("pdata") or "")
        except (TypeError, ValueError):
            continue
        return Delta3ConfigAck(
            action_id=_read_varint_field(pdata, 1),
            config_ok=_read_varint_field(pdata, 2),
        )
    return None


def _read_varint_field(pdata: bytes, field_number: int) -> int | None:
    """Return the varint value of `field_number` in pdata, skipping other wires."""
    i = 0
    length = len(pdata)
    while i < length:
        tag, i = _read_varint(pdata, i)
        if tag is None:
            return None
        wire = tag & 0x07
        if wire == 0:
            value, i = _read_varint(pdata, i)
            if value is None:
                return None
            if tag >> 3 == field_number:
                return value
        elif wire == 2:
            size, i = _read_varint(pdata, i)
            if size is None:
                return None
            i += size
        elif wire == 5:
            i += 4
        elif wire == 1:
            i += 8
        else:
            return None
    return None


def _read_varint(data: bytes, index: int) -> tuple[int | None, int]:
    value = 0
    shift = 0
    while index < len(data):
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
        if shift > 63:
            return None, index
    return None, index
