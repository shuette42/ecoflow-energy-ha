"""EcoFlow Smart Plug parser and protobuf encoder.

Parses HTTP Quota, MQTT JSON, and Protobuf heartbeat messages, and
builds Protobuf SET command payloads for the Smart Plug (Wn511SocketSys).

HTTP Quota uses a "2_1." prefix for heartbeat data and "2_2." for tasks.
Protobuf uses Send_Header_Msg envelope with plug_heartbeat_pack payload.
"""

from __future__ import annotations

import time
from typing import Any

from . import _safe_float
from ..proto_encoding import encode_field_bytes, encode_field_varint, encode_varint


def parse_smartplug_http_quota(quota_data: dict) -> dict[str, Any]:
    """Parse a Smart Plug GET /quota/all response into flat sensor keys.

    Args:
        quota_data: The "data" dict from the API response.

    Returns:
        Dict mapping sensor keys to values.
    """
    result: dict[str, Any] = {}

    # --- Core measurements ---
    _prefix = "2_1."

    # Power (deci-W → W) — API returns value in 0.1 W units
    if f"{_prefix}watts" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}watts"])
        if v is not None:
            result["power_w"] = v / 10.0

    # Current (mA → A)
    if f"{_prefix}current" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}current"])
        if v is not None:
            result["current_a"] = v / 1000.0

    # Voltage (V)
    if f"{_prefix}volt" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}volt"])
        if v is not None:
            result["voltage_v"] = v

    # Frequency (Hz)
    if f"{_prefix}freq" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}freq"])
        if v is not None:
            result["frequency_hz"] = v

    # Temperature (°C)
    if f"{_prefix}temp" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}temp"])
        if v is not None:
            result["temperature_c"] = v

    # --- Switch state ---
    if f"{_prefix}switchSta" in quota_data:
        val = quota_data[f"{_prefix}switchSta"]
        if isinstance(val, bool):
            result["switch_state"] = 1 if val else 0
        elif isinstance(val, (int, float)):
            result["switch_state"] = 1 if val else 0

    # --- Diagnostics ---
    if f"{_prefix}brightness" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}brightness"])
        if v is not None:
            result["led_brightness"] = round(v * 100.0 / 1023.0)  # 0-1023 -> 0-100%

    if f"{_prefix}maxWatts" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}maxWatts"])
        if v is not None:
            result["max_power_w"] = v

    # Max current (deci-A → A) — API returns value in 0.1 A units
    if f"{_prefix}maxCur" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}maxCur"])
        if v is not None:
            result["max_current_a"] = v / 10.0

    if f"{_prefix}errCode" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}errCode"])
        if v is not None:
            result["error_code"] = 0 if int(v) == 65535 else int(v)

    if f"{_prefix}warnCode" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}warnCode"])
        if v is not None:
            result["warning_code"] = 0 if int(v) == 65535 else int(v)

    return result


# MQTT field → (sensor_key, scale_factor)
# Scale factors match parse_smartplug_http_quota exactly:
#   watts:   deci-W → W  (/10)
#   current: mA → A      (/1000)
#   volt:    V → V        (1)
#   maxCur:  deci-A → A   (/10)
#   maxWatts: W → W       (1)  — no scaling in HTTP parser
_MQTT_FIELD_MAP: dict[str, tuple[str, float]] = {
    "watts": ("power_w", 0.1),
    "current": ("current_a", 0.001),
    "volt": ("voltage_v", 1.0),
    "freq": ("frequency_hz", 1.0),
    "temp": ("temperature_c", 1.0),
    "brightness": ("led_brightness", 100.0 / 1023.0),  # 0-1023 -> 0-100%
    "maxWatts": ("max_power_w", 1.0),
    "maxCur": ("max_current_a", 0.1),
    "errCode": ("error_code", 1.0),
    "warnCode": ("warning_code", 1.0),
}


# Proto field_number -> (mqtt_key, wire_type)
# From Wn511SocketSys.proto: plug_heartbeat_pack
_PROTO_HEARTBEAT_FIELDS: dict[int, str] = {
    1: "errCode",
    2: "warnCode",
    5: "maxCur",
    6: "temp",
    7: "freq",
    8: "current",
    9: "volt",
    10: "watts",
    11: "switchSta",
    12: "brightness",
    13: "maxWatts",
    34: "cons_watt",
}


def parse_smartplug_proto_heartbeat(pdata: bytes) -> dict[str, Any]:
    """Parse a SmartPlug protobuf heartbeat (pdata) into sensor keys.

    Uses the same scaling as parse_smartplug_report for consistency.
    Proto wire format: varint fields from plug_heartbeat_pack.
    """
    fields: dict[str, Any] = {}
    mv = memoryview(pdata)
    i = 0
    while i < len(mv):
        # Read tag (varint)
        shift = 0
        tag = 0
        while True:
            b = mv[i]
            i += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        fn, wt = tag >> 3, tag & 0x07
        if wt == 0:
            # Varint
            shift = 0
            val = 0
            while True:
                b = mv[i]
                i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            key = _PROTO_HEARTBEAT_FIELDS.get(fn)
            if key:
                if key == "switchSta":
                    fields[key] = bool(val)
                else:
                    fields[key] = val
        elif wt == 2:
            # Length-delimited: skip
            shift = 0
            length = 0
            while True:
                b = mv[i]
                i += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            i += length
        elif wt == 5:
            i += 4  # fixed32
        elif wt == 1:
            i += 8  # fixed64
        else:
            break

    if not fields:
        return {}
    return parse_smartplug_report(fields)


def parse_smartplug_report(data: dict[str, Any]) -> dict[str, Any]:
    """Parse a Smart Plug MQTT report message.

    MQTT reports may arrive in different envelope formats:
    1. {"params": {"2_1.watts": 150, ...}} — same keys as HTTP quota
    2. {"param": {"watts": 150, ...}} — direct field names (cmdId/cmdFunc format)
    3. Direct dict with field names
    """
    # Extract inner payload from envelope
    params = data.get("params") or data.get("param") or data

    # If keys have "2_1." prefix, reuse the HTTP parser (same format)
    if any(k.startswith("2_1.") for k in params):
        return parse_smartplug_http_quota(params)

    # Direct field names — apply same scaling as HTTP parser
    result: dict[str, Any] = {}

    for api_key, (sensor_key, scale) in _MQTT_FIELD_MAP.items():
        val = params.get(api_key)
        if val is None:
            continue
        fval = _safe_float(val)
        if fval is None:
            continue
        if sensor_key in ("error_code", "warning_code"):
            raw_int = int(fval)
            result[sensor_key] = 0 if raw_int == 65535 else raw_int
        elif sensor_key == "led_brightness":
            result[sensor_key] = round(fval * scale)
        else:
            result[sensor_key] = fval * scale

    # Switch state: handle bool and int
    switch_val = params.get("switchSta")
    if switch_val is not None:
        if isinstance(switch_val, bool):
            result["switch_state"] = 1 if switch_val else 0
        elif isinstance(switch_val, (int, float)):
            result["switch_state"] = 1 if switch_val else 0

    return result


def _decode_varint_fields(data: bytes) -> dict[int, int]:
    """Decode all varint fields from a protobuf byte string."""
    from google.protobuf.internal.decoder import _DecodeVarint

    fields: dict[int, int] = {}
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _DecodeVarint(data, pos)
        except Exception:
            break
        field_num = tag >> 3
        wire_type = tag & 7
        if wire_type == 0:  # varint
            val, pos = _DecodeVarint(data, pos)
            fields[field_num] = val
        elif wire_type == 2:  # length-delimited (skip)
            try:
                length, pos = _DecodeVarint(data, pos)
                pos += length
            except Exception:
                break
        elif wire_type == 5:  # fixed32 (skip)
            pos += 4
        elif wire_type == 1:  # fixed64 (skip)
            pos += 8
        else:
            break
    return fields


def _extract_pdata(payload: bytes) -> bytes | None:
    """Extract the pdata field from a Send_Header_Msg/Header envelope.

    Wire format: Send_Header_Msg { repeated Header msg = 1 }
    Header { bytes pdata = 1; int32 cmd_func = 8; int32 cmd_id = 9; ... }

    Returns the pdata bytes (plug_heartbeat_pack content) or None.
    """
    from google.protobuf.internal.decoder import _DecodeVarint

    if not payload or payload[0:1] != b"\x0a":
        return None

    try:
        # Outer: field 1 (length-delimited) = Header
        _, pos = _DecodeVarint(payload, 0)
        header_len, pos = _DecodeVarint(payload, pos)
        header_bytes = payload[pos:pos + header_len]
        if not header_bytes or header_bytes[0:1] != b"\x0a":
            return None

        # Header: field 1 (length-delimited) = pdata
        _, pos2 = _DecodeVarint(header_bytes, 0)
        pdata_len, pos2 = _DecodeVarint(header_bytes, pos2)
        return header_bytes[pos2:pos2 + pdata_len]
    except Exception:
        return None


# plug_heartbeat_pack field mapping (from Wn511SocketSys.proto)
# Field# -> (sensor_key, scale_factor, is_bool)
_PLUG_HEARTBEAT_FIELDS: dict[int, tuple[str, float, bool]] = {
    1: ("error_code", 1.0, False),
    2: ("warning_code", 1.0, False),
    5: ("max_current_a", 0.1, False),       # deciA -> A
    6: ("temperature_c", 1.0, False),
    7: ("frequency_hz", 1.0, False),
    8: ("current_a", 0.001, False),          # mA -> A
    9: ("voltage_v", 1.0, False),
    10: ("power_w", 0.1, False),             # deciW -> W
    11: ("switch_state", 1.0, True),         # bool
    12: ("led_brightness", 100.0 / 1023.0, False),  # 0-1023 -> 0-100%
    13: ("max_power_w", 1.0, False),
}


def parse_smartplug_proto(payload: bytes) -> dict[str, Any] | None:
    """Parse a Smart Plug protobuf message from /app/device/property/{SN}.

    Envelope: Send_Header_Msg { Header { pdata = plug_heartbeat_pack } }

    The plug_heartbeat_pack contains all sensor fields directly:
        f1: err_code, f2: warn_code, f5: max_cur (deciA),
        f6: temp, f7: freq, f8: current (mA), f9: volt (V),
        f10: watts (deciW), f11: switch_sta (bool),
        f12: brightness, f13: max_watts
    """
    pdata = _extract_pdata(payload)
    if not pdata:
        return None

    try:
        fields = _decode_varint_fields(pdata)
        if not fields:
            return None

        result: dict[str, Any] = {}

        for field_num, (sensor_key, scale, is_bool) in _PLUG_HEARTBEAT_FIELDS.items():
            if field_num not in fields:
                continue
            raw = fields[field_num]
            if is_bool:
                result[sensor_key] = 1 if raw else 0
            elif sensor_key in ("error_code", "warning_code"):
                result[sensor_key] = 0 if int(raw) == 65535 else int(raw)
            elif sensor_key == "led_brightness":
                result[sensor_key] = round(raw * scale)
            else:
                result[sensor_key] = raw * scale

        # Note: Proto3 omits zero-valued fields, and the Smart Plug does NOT
        # send brightness/switch_state in every heartbeat. Only power, time,
        # and a few other fields arrive regularly. We must NOT inject defaults
        # here - the coordinator merge preserves previously received values.

        return result if result else None

    except Exception:
        return None


# ------------------------------------------------------------------
# Protobuf SET command builders (Smart Plug / Wn511SocketSys)
# ------------------------------------------------------------------
# All SET commands use the same Send_Header_Msg envelope with:
#   src=32 (App), dest=53 (Plug), cmd_func=2, need_ack=1
# Payload (pdata) is specific to each command.

_PLUG_SRC = 32    # Client/App
_PLUG_DEST = 53   # Smart Plug


def _encode_field_string(field_number: int, value: str) -> bytes:
    """Encode a string field (wire type 2)."""
    data = value.encode("utf-8")
    tag = (field_number << 3) | 2
    return encode_varint(tag) + encode_varint(len(data)) + data


def _build_plug_set_payload(
    cmd_id: int, pdata: bytes, device_sn: str = "", seq: int = 0,
) -> bytes:
    """Build a Send_Header_Msg for a SmartPlug SET command.

    Header fields match the EcoFlow app exactly:
      pdata, src=32, dest=53, seq, needAck=1, cmdId, cmdFunc=2,
      deviceSn, dataLen.

    Args:
        cmd_id: Command ID (129=switch, 130=brightness, 131=max_cur, 137=max_watts).
        pdata: Serialized protobuf payload for the specific command.
        device_sn: Device serial number (required by the cloud broker for routing).
        seq: Sequence number (0 = auto-generate from timestamp).

    Returns:
        Binary protobuf payload ready to publish on the SET topic.
    """
    if seq == 0:
        seq = int(time.time() * 1000) & 0x7FFFFFFF

    header = bytearray()
    header.extend(encode_field_bytes(1, pdata))              # pdata (field 1)
    header.extend(encode_field_varint(2, _PLUG_SRC))         # src = 32 (field 2)
    header.extend(encode_field_varint(3, _PLUG_DEST))        # dest = 53 (field 3)
    header.extend(encode_field_varint(8, 2))                 # cmdFunc = 2 (field 8)
    header.extend(encode_field_varint(9, cmd_id))            # cmdId (field 9)
    header.extend(encode_field_varint(10, len(pdata)))       # dataLen (field 10)
    header.extend(encode_field_varint(11, 1))                # needAck = 1 (field 11)
    header.extend(encode_field_varint(14, seq))              # seq (field 14)
    if device_sn:
        header.extend(_encode_field_string(25, device_sn))    # deviceSn (field 25)

    return encode_field_bytes(1, bytes(header))


def build_plug_switch_payload(
    on: bool, device_sn: str = "", seq: int = 0,
) -> bytes:
    """Build a plug_switch_message SET payload.

    Args:
        on: True to turn the plug on, False to turn off.
        device_sn: Device serial number for cloud routing.
        seq: Sequence number (0 = auto-generate).

    Returns:
        Binary protobuf payload for plug on/off control.
    """
    # plug_switch_message { optional uint32 plug_switch = 1 }
    pdata = encode_field_varint(1, 1 if on else 0)
    return _build_plug_set_payload(129, pdata, device_sn, seq)


def build_plug_brightness_payload(
    brightness: int, device_sn: str = "", seq: int = 0,
) -> bytes:
    """Build a brightness_pack SET payload.

    Args:
        brightness: LED brightness level (0-1023).
        device_sn: Device serial number for cloud routing.
        seq: Sequence number (0 = auto-generate).

    Returns:
        Binary protobuf payload for LED brightness control.
    """
    # brightness_pack { optional int32 brightness = 1 }
    brightness = max(0, min(1023, brightness))
    pdata = encode_field_varint(1, brightness)
    return _build_plug_set_payload(130, pdata, device_sn, seq)


def build_plug_max_watts_payload(
    max_watts: int, device_sn: str = "", seq: int = 0,
) -> bytes:
    """Build a max_watts_pack SET payload.

    Args:
        max_watts: Maximum power limit in watts.
        device_sn: Device serial number for cloud routing.
        seq: Sequence number (0 = auto-generate).

    Returns:
        Binary protobuf payload for max power limit control.
    """
    # max_watts_pack { optional int32 max_watts = 1 }
    pdata = encode_field_varint(1, max_watts)
    return _build_plug_set_payload(137, pdata, device_sn, seq)


def build_plug_get_all_payload() -> bytes:
    """Build a "get all data" request for the Smart Plug.

    Sends a minimal Send_Header_Msg with src=32, dest=32, from="app"
    on the /thing/property/get topic. The plug responds with a full
    heartbeat containing all fields (brightness, switch_state, etc.)
    on /app/device/property/{SN}.
    """
    header = bytearray()
    header.extend(encode_field_varint(2, 32))             # src = 32 (App)
    header.extend(encode_field_varint(3, 32))             # dest = 32
    header.extend(encode_field_varint(14, 0x1234))        # seq = 4660 (fixed)
    header.extend(_encode_field_string(23, "app"))         # from = "app"

    return encode_field_bytes(1, bytes(header))
