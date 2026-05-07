"""Proto-runtime decoder using google.protobuf.json_format.MessageToDict.

Declarative cmd_id registry replaces manual per-field extraction.
Each cmd_id maps to a CmdConfig that defines message class, flags, field renames,
and zero-fill rules. The generic decode loop handles all message types uniformly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from google.protobuf.json_format import MessageToDict

from .decoder import decode_header_message

_LOGGER = logging.getLogger(__name__)


@dataclass
class ProtoRuntimeDecodeResult:
    headers: list[dict]
    payload: bytes | None
    source: bytes
    mapped: dict[str, Any]
    parse_path: str
    parse_reason_code: str


@dataclass(frozen=True)
class CmdConfig:
    """Declarative configuration for a protobuf cmd_id."""
    msg_class: type
    parse_path: str
    flags: dict[str, bool] = field(default_factory=dict)
    rename: dict[str, str] = field(default_factory=dict)
    zero_fill: frozenset[str] = field(default_factory=frozenset)
    flatten_key: str | None = None


def _build_cmd_registry() -> dict[int, CmdConfig]:
    """Build cmd_id -> CmdConfig registry. Lazy-loaded to avoid import-time pb2 dependency."""
    try:
        from . import ecocharge_pb2 as pb2
    except ImportError:
        _LOGGER.warning("Failed to import protobuf module — Enhanced Mode will not work")
        return {}

    return {
        33: CmdConfig(
            msg_class=pb2.JTS1EnergyStreamReport,
            parse_path="typed_runtime:energy_stream_report",
            flags={"_is_energy_stream": True, "_is_energy_stream_report": True},
            rename={
                "mppt_pwr": "solar",
                "sys_load_pwr": "home_direct",
                "bp_pwr": "batt_pb",
                "sys_grid_pwr": "grid_raw_f2",
                "bp_soc": "soc",
            },
            zero_fill=frozenset({"solar", "home_direct", "batt_pb", "grid_raw_f2"}),
        ),
        39: CmdConfig(
            msg_class=pb2.JTS1EmsPVInvEnergyStreamReport,
            parse_path="typed_runtime:pv_inv_energy_stream",
        ),
        1: CmdConfig(
            msg_class=pb2.JTS1EmsHeartbeat,
            parse_path="typed_runtime:ems_heartbeat",
            flags={"_is_ems_heartbeat": True},
        ),
        7: CmdConfig(
            msg_class=pb2.JTS1BpHeartbeatReport,
            parse_path="typed_runtime:bp_heartbeat",
            flags={"_is_bp_heartbeat": True},
            flatten_key="bp_heart_beat",
        ),
        8: CmdConfig(
            msg_class=pb2.JTS1EmsChangeReport,
            parse_path="typed_runtime:ems_change",
            flags={"_is_ems_change": True},
            rename={"ems_word_mode": "ems_work_mode"},
        ),
        13: CmdConfig(
            msg_class=pb2.JTS1EmsParamChangeReport,
            parse_path="typed_runtime:ems_param_change",
            flags={"_is_ems_param_change": True},
            rename={"dev_soc": "ems_app_surplus_pct"},
        ),
    }


_CMD_REGISTRY: dict[int, CmdConfig] | None = None
_FULL_POWER_KEYS = frozenset({"solar", "home_direct", "batt_pb", "grid_raw_f2"})


def _empty_mapped() -> dict[str, Any]:
    """Returns a stable empty mapping skeleton for non-decoded runtime frames."""
    return {
        "_available_keys": set(),
        "_flat_count": 0,
        "_is_energy_stream": False,
        "_is_energy_stream_report": False,
        "_is_ems_heartbeat": False,
        "_is_full_power_frame": False,
    }


def _header_value(headers: list[dict], key: str) -> Any:
    for header in headers or []:
        if not isinstance(header, dict):
            continue
        value = header.get(key)
        if value is not None:
            return value
    return None


def _first_pdata(headers: list[dict]) -> tuple[bytes | None, bool]:
    """Extract first valid pdata from headers. Returns (pdata_bytes, had_invalid)."""
    had_invalid = False
    for header in headers or []:
        if not isinstance(header, dict):
            continue
        pdata_hex = header.get("pdata")
        if not isinstance(pdata_hex, str) or not pdata_hex:
            continue
        try:
            candidate = bytes.fromhex(pdata_hex)
        except ValueError:
            had_invalid = True
            continue
        if candidate:
            return candidate, False
    return None, had_invalid


def _typed_runtime_map(headers: list[dict], source: bytes) -> tuple[dict[str, Any], str] | None:
    """Declarative decode: cmd_id -> MessageToDict -> rename -> zero-fill -> flags."""
    global _CMD_REGISTRY
    if _CMD_REGISTRY is None:
        _CMD_REGISTRY = _build_cmd_registry()
    if not _CMD_REGISTRY:
        return None

    cmd_func = _header_value(headers, "cmd_func")
    cmd_id = _header_value(headers, "cmd_id")

    if cmd_func != 96:
        return None

    config = _CMD_REGISTRY.get(cmd_id)
    if config is None:
        return None

    # 1. Parse protobuf message
    msg = config.msg_class()
    msg.ParseFromString(source)

    # 2. Convert to dict (only present fields, proto field names preserved)
    fields = MessageToDict(msg, preserving_proto_field_name=True)

    # 3. For repeated messages, extract first element (keep all for multi-pack)
    if config.flatten_key:
        items = fields.get(config.flatten_key, [])
        if items:
            # Keep first item as before (backward compatible for existing bp_* sensors)
            first = items[0] if isinstance(items[0], dict) else {}
            # Store all items for multi-pack extraction
            first["all_packs"] = items
            fields = first
        else:
            fields = {}

    # 4. Build mapped dict with renames and available_keys tracking
    mapped: dict[str, Any] = _empty_mapped()
    mapped.update(config.flags)

    for proto_name, value in fields.items():
        key = config.rename.get(proto_name, proto_name)
        if isinstance(value, int) and not isinstance(value, bool):
            mapped[key] = float(value) if key == "soc" else value
        else:
            mapped[key] = value
        mapped["_available_keys"].add(key)

    # 5. Zero-fill: proto3 omits 0.0 values, but power fields need explicit 0.0
    for key in config.zero_fill:
        if key not in mapped["_available_keys"]:
            mapped[key] = 0.0
            mapped["_available_keys"].add(key)

    # 6. Compute full-power-frame flag
    if config.flags.get("_is_energy_stream"):
        mapped["_is_full_power_frame"] = len(
            _FULL_POWER_KEYS & mapped["_available_keys"]
        ) >= 3

    mapped["_flat_count"] = len(fields)
    return mapped, config.parse_path


def decode_proto_runtime_frame(payload_bytes: bytes) -> ProtoRuntimeDecodeResult:
    """Decode a complete EcoFlow protobuf frame into structured data."""
    headers, payload = decode_header_message(payload_bytes)

    cmd_func = _header_value(headers, "cmd_func")
    cmd_id = _header_value(headers, "cmd_id")

    global _CMD_REGISTRY
    if _CMD_REGISTRY is None:
        _CMD_REGISTRY = _build_cmd_registry()
    typed_eligible = cmd_func == 96 and cmd_id in (_CMD_REGISTRY or {})

    # Resolve inner payload source for typed decode
    if payload is not None:
        typed_source = payload
        typed_reason_code = "typed_source_payload_field"
    elif typed_eligible:
        pdata, had_invalid = _first_pdata(headers)
        if pdata is not None:
            typed_source = pdata
            typed_reason_code = "typed_source_header_pdata"
        elif had_invalid:
            typed_source = payload_bytes
            typed_reason_code = "typed_source_full_frame_invalid_pdata"
        else:
            return ProtoRuntimeDecodeResult(
                headers=headers, payload=payload, source=b"",
                mapped=_empty_mapped(),
                parse_path="typed_runtime:guarded_no_inner_payload",
                parse_reason_code="typed_inner_payload_missing",
            )
    else:
        typed_source = payload_bytes
        typed_reason_code = "typed_source_full_frame"

    typed = _typed_runtime_map(headers, typed_source)
    if typed is not None:
        mapped, parse_path = typed
        return ProtoRuntimeDecodeResult(
            headers=headers,
            payload=payload,
            source=typed_source,
            mapped=mapped,
            parse_path=parse_path,
            parse_reason_code=typed_reason_code,
        )

    return ProtoRuntimeDecodeResult(
        headers=headers, payload=payload, source=typed_source,
        mapped=_empty_mapped(),
        parse_path="typed_runtime:no_match",
        parse_reason_code="typed_no_match",
    )
