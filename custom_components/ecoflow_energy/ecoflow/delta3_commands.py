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
from .proto_encoding import encode_field_bytes, encode_field_varint

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


class Delta3Select(NamedTuple):
    """One stepped control whose options are fixed by the vendor app."""

    params_key: str
    config_field: int


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

# --- AC charging: two fields, one setting -----------------------------------
#
# The charge power and the charge mode are a single setting on the wire. The
# app never writes one without the other, and the device drops a frame that
# carries only one of them - without an ack, without a rejection, silently.
# Measured on a D3M1 on 2026-08-03: field 54 alone and field 125 alone both
# produce no answer and no change; the two together are applied and read back
# within a second.
#
# Only the power has an entity. The mode is written along with it and never
# displayed, because its read-back arrives only when the mode changes - five
# minutes of listening produced none - and a control that cannot show the
# device's setting is worse than no control.
AC_CHARGE_POWER_KEY = "ac_charge_power_limit"
AC_CHARGE_MODE_PARAMS_KEY = "cfgAcInChgMode"
AC_CHARGE_MODE_FIELD = 125
# The custom-power mode, the only one in which a requested wattage means
# anything. Moving the slider in the app selects it too.
AC_CHARGE_MODE_CUSTOM = 0


# Number controls: entity key -> wire mapping. Bounds are vendor-documented.
DELTA3_NUMBER_PARAMS: dict[str, Delta3Number] = {
    "backup_reserve_soc": Delta3Number("cfgBackupReverseSoc", 0, 50, 102),
    "max_charge_soc": Delta3Number("cfgMaxChgSoc", 50, 100, 33),
    "min_discharge_soc": Delta3Number("cfgMinDsgSoc", 0, 30, 34),
    # AC charge power. The bounds are the app slider's own, read off a D3M1
    # (#111), and confirmed by the read-back sitting at exactly 200 on a second
    # unit. They are not treated as hard truth: a different firmware may allow
    # more, so a device rejection (ConfigWriteAck) stays the authority.
    AC_CHARGE_POWER_KEY: Delta3Number("cfgPlugInInfoAcInChgPowMax", 200, 2400, 54),
}


# --- LCD screen timeout ------------------------------------------------------
#
# ConfigWrite field 12, read back as status-frame field 18, in seconds. Verified
# on a D3M1 on 2026-08-05: a write of 30 was acknowledged with config_ok=1 after
# 0.23 s and reported back on field 18 as 30 after 2.16 s; restoring 10 behaved
# the same way.
#
# Only the app's own six steps are offered. Whether the device accepts an
# arbitrary value such as 47 has never been measured, and a ConfigWrite it will
# not process is dropped without any answer at all - so an unmeasured value
# would fail silently rather than visibly.
SCREEN_TIMEOUT_KEY = "screen_timeout"
SCREEN_TIMEOUT_PARAMS_KEY = "cfgScreenOffTime"
SCREEN_TIMEOUT_FIELD = 12


# --- Idle shutdowns ----------------------------------------------------------
#
# Four settings from the same app page, all in minutes, all writable the same
# way. They switch an output - or the whole unit - off after the configured span
# with no load connected and no activity, which is the vendor's own description
# and the reason they are called idle shutdowns here rather than timeouts.
#
# The field numbers are close together and easy to transpose, which is why each
# one has its own payload test asserting the exact number rather than a shared
# parametrized one over the table below.
DELTA3_SELECT_FIELDS: dict[str, Delta3Select] = {
    SCREEN_TIMEOUT_KEY: Delta3Select(SCREEN_TIMEOUT_PARAMS_KEY, SCREEN_TIMEOUT_FIELD),
    "device_idle_shutdown": Delta3Select("cfgDevStandbyTime", 13),
    "ac1_idle_shutdown": Delta3Select("cfgAcStandbyTime", 10),
    "ac2_idle_shutdown": Delta3Select("cfgAc2StandbyTime", 572),
    "dc_idle_shutdown": Delta3Select("cfgDcStandbyTime", 11),
}


# --- Port priority: one item per write ---------------------------------------
#
# ConfigWrite field 376 carries a PowerOutagesList, whose only field is a
# repeated PowerOutageItem. The app writes exactly one item per user action
# even though the device reports all three ports back, and this follows it:
# sending an item per port would be an untested shape, and the read-back
# refreshes every port anyway.
#
# Item fields: 1 = non-essential flag, 2 = port type, 3 = cutoff SoC.
PORT_PRIORITY_FIELD = 376
PORT_PRIORITY_PARAMS_KEY = "cfgPowerOutagesList"
_PORT_ITEM_FIELD = 1
_PORT_ITEM_ENABLE = 1
_PORT_ITEM_TYPE = 2
_PORT_ITEM_MIN_SOC = 3

# entity stem -> POWER_OUTAGE_PORT enum value, from the app's own port list.
PORT_PRIORITY_PORT_TYPES: dict[str, int] = {"dc": 1, "ac1": 2, "ac2": 3}

# The cutoff bounds are not fixed. The app derives them from the battery's own
# charge and discharge limits:
#
#     lower = min(min_discharge_soc, 30) + 5
#     upper = max(max_charge_soc, 50) - 5
#
# Confirmed on a D3M1 on 2026-08-04: with the limits at 0 and 100 the slider
# stopped at 5 and 95 respectively, while its scale still read 0 % to 100 %.
# Whether the device enforces the same range or only the app does is unknown,
# so the integration clamps identically rather than finding out on hardware.
PORT_PRIORITY_SOC_MARGIN = 5
PORT_PRIORITY_LOWER_ANCHOR = 30
PORT_PRIORITY_UPPER_ANCHOR = 50


def port_priority_soc_bounds(
    max_charge_soc: int | None, min_discharge_soc: int | None
) -> tuple[int, int]:
    """Return the (lower, upper) cutoff bounds for the given battery limits.

    Falls back to the widest range the anchors allow when a limit has not been
    reported yet, which keeps a slider usable instead of pinning it shut.

    The anchors keep the range from ever inverting, whatever the device
    reports: the lower end can never exceed 30 + 5 and the upper end can never
    fall below 50 - 5, so upper > lower by at least ten points. No clamp is
    needed here, and adding one would hide an anchor change that broke it.
    """
    lower_source = min_discharge_soc if isinstance(min_discharge_soc, int) else 0
    upper_source = max_charge_soc if isinstance(max_charge_soc, int) else 100
    lower = min(lower_source, PORT_PRIORITY_LOWER_ANCHOR) + PORT_PRIORITY_SOC_MARGIN
    upper = max(upper_source, PORT_PRIORITY_UPPER_ANCHOR) - PORT_PRIORITY_SOC_MARGIN
    return lower, upper


def encode_port_priority_item(port_type: int, limited: bool, cutoff_soc: int) -> bytes:
    """Encode one PowerOutagesList entry, ready for ConfigWrite field 376."""
    item = encode_field_varint(_PORT_ITEM_ENABLE, int(bool(limited)))
    item += encode_field_varint(_PORT_ITEM_TYPE, int(port_type))
    item += encode_field_varint(_PORT_ITEM_MIN_SOC, int(cutoff_soc))
    return encode_field_bytes(_PORT_ITEM_FIELD, item)


def build_port_priority_command(
    stem: str, limited: bool, cutoff_soc: int
) -> dict[str, Any] | None:
    """Build a SET command for one port of the port priority list.

    Both values always travel together: the wire item carries the flag and the
    cutoff in one message, so writing either one means sending both.
    """
    port_type = PORT_PRIORITY_PORT_TYPES.get(stem)
    if port_type is None:
        return None
    return _envelope(
        {
            PORT_PRIORITY_PARAMS_KEY: {
                "portType": port_type,
                "limited": bool(limited),
                "cutoffSoc": int(cutoff_soc),
            }
        }
    )


# Reverse lookup for the binary path: JSON params key -> ConfigWrite field.
_PARAMS_KEY_TO_FIELD: dict[str, int] = {
    **{entry.params_key: entry.config_field for entry in DELTA3_SWITCH_PARAMS.values()},
    **{entry.params_key: entry.config_field for entry in DELTA3_NUMBER_PARAMS.values()},
    AC_CHARGE_MODE_PARAMS_KEY: AC_CHARGE_MODE_FIELD,
    **{entry.params_key: entry.config_field for entry in DELTA3_SELECT_FIELDS.values()},
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
    if entity_key == AC_CHARGE_POWER_KEY:
        # A wattage only means anything in the custom mode, and the device
        # ignores the frame unless the mode travels with it.
        return _envelope(
            {
                entry.params_key: clamped,
                AC_CHARGE_MODE_PARAMS_KEY: AC_CHARGE_MODE_CUSTOM,
            }
        )
    return _envelope({entry.params_key: clamped})


def build_select_command(entity_key: str, wire_value: int) -> dict[str, Any] | None:
    """Build a SET command for a Delta 3 select.

    The caller resolves the option label to its wire value, because the option
    list and the values behind it belong together in one place.
    """
    entry = DELTA3_SELECT_FIELDS.get(entity_key)
    if entry is None:
        return None
    return _envelope({entry.params_key: int(wire_value)})


# Parameter sets that legitimately travel in one frame. Anything not listed
# here stays a single-setting write: sending two unrelated settings together
# has never been observed and would be guesswork.
_MULTI_FIELD_GROUPS: tuple[frozenset[str], ...] = (
    frozenset(
        {
            DELTA3_NUMBER_PARAMS[AC_CHARGE_POWER_KEY].params_key,
            AC_CHARGE_MODE_PARAMS_KEY,
        }
    ),
)


def _build_multi_field_frame(
    params: dict[str, Any], device_sn: str, seq: int
) -> bytes | None:
    """Encode a known group of settings into one ConfigWrite frame."""
    if frozenset(params) not in _MULTI_FIELD_GROUPS:
        return None

    encoded: list[tuple[int, int]] = []
    for params_key, raw_value in params.items():
        config_field = _PARAMS_KEY_TO_FIELD.get(params_key)
        if config_field is None or isinstance(raw_value, (dict, list, str)):
            return None
        encoded.append((config_field, int(raw_value)))

    lead_field, lead_value = encoded[0]
    return build_delta3_config_write_payload(
        lead_field,
        lead_value,
        device_sn,
        seq=seq,
        companions=tuple(encoded[1:]),
    )


def build_proto_command(
    command: dict[str, Any], device_sn: str, seq: int = 0
) -> bytes | None:
    """Translate a Delta 3 command body into a binary ConfigWrite frame.

    Used for app-login entries, which have no HTTP endpoint. The command body
    is the same one the HTTP path sends, so both modes share the value ranges
    and the control-to-parameter mapping.

    Most commands carry exactly one parameter. The AC charging pair is the
    exception and carries two, because the device processes them only
    together. Anything else returns None, so the caller can log instead of
    sending a frame the device would reject.
    """
    params = command.get("params") or {}
    if len(params) > 1:
        return _build_multi_field_frame(params, device_sn, seq)
    if len(params) != 1:
        return None
    params_key, raw_value = next(iter(params.items()))

    if params_key == PORT_PRIORITY_PARAMS_KEY:
        if not isinstance(raw_value, dict):
            return None
        port_type = raw_value.get("portType")
        cutoff = raw_value.get("cutoffSoc")
        if not isinstance(port_type, int) or not isinstance(cutoff, int):
            return None
        return build_delta3_config_write_payload(
            PORT_PRIORITY_FIELD,
            0,
            device_sn,
            seq=seq,
            submessage=encode_port_priority_item(
                port_type, bool(raw_value.get("limited")), cutoff
            ),
        )

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
