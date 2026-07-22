"""MQTT message parsing and ingest for the EcoFlow device coordinator."""

from __future__ import annotations

import json
import logging
from typing import Any

from ..const import (
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_STREAM,
)
from ..ecoflow.parsers.delta import parse_delta_report
from ..ecoflow.parsers.delta_http import parse_delta_http_quota
from ..ecoflow.parsers.delta3_http import parse_delta3_http_quota
from ..ecoflow.parsers.delta3_proto import (
    parse_delta3_cms_heartbeat,
    parse_delta3_display_property,
)
from ..ecoflow.parsers.powerocean_proto import (
    flatten_heartbeat,
    remap_bp_keys,
    remap_proto_keys,
)
from ..ecoflow.parsers.powerocean import parse_powerocean_http_quota
from ..ecoflow.parsers.smartplug import parse_smartplug_http_quota, parse_smartplug_report
from ..ecoflow.parsers.stream_proto import parse_stream_proto_message
from ..ecoflow.proto.runtime import decode_proto_runtime_frame

_LOGGER = logging.getLogger(__name__)


class MqttIngestMixin:
    """Mixin providing MQTT message parsing and monotonic enforcement."""

    # ------------------------------------------------------------------
    # MQTT message handling (called from Paho thread)
    # ------------------------------------------------------------------

    # Protobuf decoder output → sensor key mapping (F-001 fix)
    # Full chain: proto_field → runtime.py rename → this map → sensor key
    #   mppt_pwr    → solar       → solar_w
    #   sys_load_pwr→ home_direct → home_w
    #   bp_pwr      → batt_pb     → batt_w
    #   sys_grid_pwr→ grid_raw_f2 → grid_w
    #   bp_soc      → soc         → soc_pct
    # Keys with state_class=total_increasing must never decrease.
    # EcoFlow API occasionally returns slightly lower values (e.g. 461→460
    # for battery cycles, or 4408.259→4408.258 kWh for energy). Dropping
    # these regressions prevents HA Recorder warnings.
    _MONOTONIC_KEYS: frozenset[str] = frozenset({
        # PowerOcean
        "bp_cycles",
        "solar_energy_kwh", "home_energy_kwh",
        "grid_import_energy_kwh", "grid_export_energy_kwh",
        "batt_charge_energy_kwh", "batt_discharge_energy_kwh",
        # PowerOcean per-pack (cycles + lifetime energy are total_increasing)
        *(f"pack{n}_cycles" for n in range(1, 6)),
        *(f"pack{n}_accu_chg_energy_kwh" for n in range(1, 6)),
        *(f"pack{n}_accu_dsg_energy_kwh" for n in range(1, 6)),
        # Delta
        "bms_cycles",
        "solar2_energy_kwh", "ac_in_energy_kwh", "ac_out_energy_kwh",
        # Smart Plug
        "energy_kwh",
    })

    def _enforce_monotonic(self, parsed: dict[str, Any]) -> dict[str, Any]:
        """Drop values that would decrease a total_increasing sensor."""
        for key in self._MONOTONIC_KEYS:
            if key in parsed and key in self._device_data:
                old = self._device_data[key]
                new = parsed[key]
                if isinstance(old, (int, float)) and isinstance(new, (int, float)) and new < old:
                    del parsed[key]
        return parsed


    def _on_mqtt_message(self, topic: str, payload: bytes) -> None:
        """Handle an incoming MQTT message (Paho thread).

        In Standard Mode, MQTT is only used for SET commands — data updates
        come from HTTP polling.  Exception: Delta and Smart Plug subscribe
        to MQTT push for real-time data alongside HTTP polling (dual-source).
        In Enhanced Mode, MQTT is the primary source.
        """
        # SET reply tracking (all modes): log acknowledgement, do not process as data
        if "/set_reply" in topic:
            _LOGGER.debug("SET reply for %s: %s", self.device_sn, payload[:200])
            self._log_event("set_reply", f"topic={topic}")
            if self.device_type == DEVICE_TYPE_DELTA3:
                self._check_delta3_set_ack(payload)
            return

        if not self._enhanced_mode and self.device_type not in (
            DEVICE_TYPE_DELTA,
            DEVICE_TYPE_DELTA3,
            DEVICE_TYPE_SMARTPLUG,
            DEVICE_TYPE_STREAM,
        ):
            return  # Standard Mode (non-Delta/SmartPlug): ignore MQTT data
        parsed = self._parse_message(topic, payload)
        if parsed:
            self.hass.loop.call_soon_threadsafe(self._apply_data, parsed)

    def _check_delta3_set_ack(self, payload: bytes) -> None:
        """Report a rejected Delta 3 setting (Paho thread).

        A rejection means the user pressed a control and the device did not
        apply it, which is worth a warning. A successful write stays silent.
        """
        from ..ecoflow.delta3_commands import parse_config_write_ack

        ack = parse_config_write_ack(payload)
        if ack is None:
            return
        if ack.applied:
            _LOGGER.debug(
                "Setting applied on %s (field %s)", self.device_sn, ack.action_id
            )
            return
        _LOGGER.warning(
            "Device %s rejected a setting (field %s, status %s) - "
            "the change was not applied",
            self.device_sn,
            ack.action_id,
            ack.config_ok,
        )
        self._log_event("set_rejected", f"field={ack.action_id}")

    def _parse_message(self, topic: str, payload: bytes) -> dict[str, Any] | None:
        """Parse an MQTT message payload."""
        # get_reply topic: /app/{userId}/{sn}/thing/property/get_reply
        if "get_reply" in topic:
            try:
                data = json.loads(payload)
                quota_map = (data.get("data") or {}).get("quotaMap")
                if isinstance(quota_map, dict) and quota_map:
                    if self.device_type == DEVICE_TYPE_DELTA:
                        return parse_delta_http_quota(quota_map)
                    if self.device_type == DEVICE_TYPE_DELTA3:
                        # Route through the community-researched field map;
                        # unmapped keys are dropped so raw quota keys never
                        # leak into the device data store.
                        parsed = parse_delta3_http_quota(quota_map)
                        return parsed if parsed else None
                    if self.device_type == DEVICE_TYPE_SMARTPLUG:
                        return parse_smartplug_http_quota(quota_map)
                    if self.device_type == DEVICE_TYPE_POWEROCEAN:
                        return parse_powerocean_http_quota(quota_map)
                    return quota_map
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            # Proto get_reply: binary protobuf
            if b"\x0a" in payload[:4]:
                if self.device_type == DEVICE_TYPE_POWEROCEAN:
                    return self._parse_powerocean_get_reply(payload)
                if self.device_type == DEVICE_TYPE_STREAM:
                    return parse_stream_proto_message(payload)
                return self._parse_proto_device_data(payload)
            return None

        # JSON topic: /open/{account}/{sn}/quota
        if topic.endswith("/quota"):
            try:
                data = json.loads(payload)
                if not isinstance(data, dict):
                    return None
                # Delta devices send {"typeCode": "pdStatus", "params": {...}}
                if self.device_type == DEVICE_TYPE_DELTA and data.get("typeCode"):
                    parsed = parse_delta_report(data)
                    return parsed if parsed else None
                # Smart Plug MQTT reports: may use params/param envelope
                if self.device_type == DEVICE_TYPE_SMARTPLUG:
                    parsed = parse_smartplug_report(data)
                    return parsed if parsed else None
                # Delta 3 generation push: top-level cmdId/cmdFunc plus a
                # `param` object (sometimes `params`) with the same flat
                # camelCase keys. Prefer `param`, fall back to `params`, then
                # the flat dict. Always route through the field map so
                # unmapped keys never leak into _device_data.
                if self.device_type == DEVICE_TYPE_DELTA3:
                    payload_obj = data.get("param")
                    if not isinstance(payload_obj, dict):
                        payload_obj = data.get("params")
                    if not isinstance(payload_obj, dict):
                        payload_obj = data
                    parsed = parse_delta3_http_quota(payload_obj)
                    return parsed if parsed else None
                # PowerOcean sends flat {"params": {...}} or flat dicts
                if data.get("params"):
                    return data["params"]
                return data
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            return None

        # /app/device/property/{sn} - JSON (Delta) or Protobuf (PowerOcean/SmartPlug)
        if payload[:1] == b"{":
            try:
                data = json.loads(payload)
                if isinstance(data, dict):
                    if self.device_type == DEVICE_TYPE_DELTA:
                        if data.get("typeCode"):
                            parsed = parse_delta_report(data)
                            return parsed if parsed else None
                        # Dot-notation format: {"params": {"pd.soc": 85, ...}}
                        params = data.get("params")
                        if isinstance(params, dict) and params:
                            return parse_delta_http_quota(params)
                    if self.device_type == DEVICE_TYPE_SMARTPLUG:
                        parsed = parse_smartplug_report(data)
                        return parsed if parsed else None
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            return None

        if b"\x0a" in payload[:4]:
            try:
                # Device-type routing comes first: (cmd_func, cmd_id) pairs are
                # not unique across device classes. The Stream AC Pro uses the
                # very same (254, 21) main status frame as the Delta 3
                # generation, so a generic registry lookup would hand a Stream
                # frame to the Delta 3 parser and drop the Stream telemetry.
                if self.device_type == DEVICE_TYPE_STREAM:
                    return parse_stream_proto_message(payload)
                result = decode_proto_runtime_frame(payload)
                raw = {
                    k: v
                    for k, v in result.mapped.items()
                    if not k.startswith("_")
                }
                # Delta 3 generation: status frame and battery heartbeat.
                # Both feed the same parser as the HTTP path, so the sensor
                # keys are identical in Standard and Enhanced Mode.
                if self.device_type == DEVICE_TYPE_DELTA3:
                    if result.mapped.get("_is_delta3_display"):
                        parsed = parse_delta3_display_property(raw)
                        return parsed if parsed else None
                    if result.mapped.get("_is_delta3_cms_heartbeat"):
                        parsed = parse_delta3_cms_heartbeat(raw)
                        return parsed if parsed else None
                if result.mapped.get("_is_energy_stream"):
                    return remap_proto_keys(raw)
                # Enhanced Mode: heartbeat with nested extraction
                if result.mapped.get("_is_ems_heartbeat"):
                    return flatten_heartbeat(raw)
                # Enhanced Mode: param change report (cmd_id=13) carries
                # only `ems_app_surplus_pct` (renamed from `dev_soc`). This
                # field has no entry in the BP/EMS-change rename tables and
                # would be dropped by remap_bp_keys, so pass it through
                # unchanged.
                if result.mapped.get("_is_ems_param_change"):
                    return raw or None
                # Enhanced Mode: change reports and battery heartbeat
                if (
                    result.mapped.get("_is_ems_change")
                    or result.mapped.get("_is_bp_heartbeat")
                ):
                    if not raw:
                        return None
                    return remap_bp_keys(raw, self._bp_sn_to_index, self.device_sn)
                # Non-PowerOcean protobuf: SmartPlug heartbeats. The headers
                # are already decoded above, so hand them over instead of
                # decoding the same frame a second time.
                return self._parse_proto_device_data(payload, result.headers)
            except Exception:
                _LOGGER.debug("Protobuf decode error for %s", self.device_sn, exc_info=True)
            return None

        return None

    def _parse_powerocean_get_reply(self, payload: bytes) -> dict[str, Any] | None:
        """Parse PowerOcean proto get_reply by extracting EmsChangeReport
        and EmsParamChangeReport sub-messages.

        The get_reply contains multiple sub-messages, each with its own
        cmd_func/cmd_id and pdata. We extract cmd_func=96 cmd_id=8
        (EmsChangeReport, connectivity + enum fields) and cmd_id=13
        (EmsParamChangeReport, the app-side surplus mirror).
        """
        from ..ecoflow.proto.decoder import decode_header_message
        from ..ecoflow.parsers.powerocean_proto import remap_bp_keys

        try:
            headers, _ = decode_header_message(payload)
            from ..ecoflow.proto import ecocharge_pb2 as pb2
            from google.protobuf.json_format import MessageToDict

            merged: dict[str, Any] = {}
            for hdr in headers or []:
                if hdr.get("cmd_func") != 96:
                    continue
                cmd_id = hdr.get("cmd_id")
                pdata_hex = hdr.get("pdata")
                if cmd_id not in (8, 13):
                    continue
                if not isinstance(pdata_hex, str) or not pdata_hex:
                    continue
                try:
                    pdata = bytes.fromhex(pdata_hex)
                except ValueError:
                    continue
                # Generated _pb2 classes are registered via _descriptor_pool
                # at runtime, which Pyright/Pylance cannot resolve statically.
                msg_class = (
                    pb2.JTS1EmsChangeReport if cmd_id == 8  # type: ignore[attr-defined]
                    else pb2.JTS1EmsParamChangeReport  # type: ignore[attr-defined]
                )
                msg = msg_class()
                msg.ParseFromString(pdata)
                fields = MessageToDict(msg, preserving_proto_field_name=True)
                if not fields:
                    continue
                if cmd_id == 8 and "ems_word_mode" in fields:
                    fields["ems_work_mode"] = fields.pop("ems_word_mode")
                if cmd_id == 13 and "dev_soc" in fields:
                    fields["ems_app_surplus_pct"] = fields.pop("dev_soc")
                merged.update(fields)
            if merged:
                # remap_bp_keys filters via BP/EMS-change rename tables and
                # drops anything not listed there. Pull out fields that are
                # already in sensor-key form (e.g. ems_app_surplus_pct from
                # cmd_id=13) before remap, then re-add them.
                passthrough = {}
                for key in ("ems_app_surplus_pct",):
                    if key in merged:
                        passthrough[key] = merged.pop(key)
                remapped = remap_bp_keys(merged, self._bp_sn_to_index, self.device_sn)
                remapped.update(passthrough)
                return remapped
        except Exception:
            _LOGGER.debug("PowerOcean get_reply decode error", exc_info=True)

        return None

    def _parse_proto_device_data(
        self, payload: bytes, headers: list[dict[str, Any]] | None = None
    ) -> dict[str, Any] | None:
        """Parse SmartPlug/Delta protobuf heartbeat via generic wire-format decoder.

        `headers` may be supplied by a caller that already decoded the frame
        so the header decode does not run twice per message.
        """
        if self.device_type == DEVICE_TYPE_STREAM:
            return parse_stream_proto_message(payload)

        if headers is None:
            from ..ecoflow.proto.decoder import decode_header_message

            headers, _ = decode_header_message(payload)
        for hdr in headers or []:
            pdata_hex = hdr.get("pdata")
            if not pdata_hex:
                continue
            try:
                pdata = bytes.fromhex(pdata_hex)
            except Exception:
                continue

            if self.device_type == DEVICE_TYPE_SMARTPLUG:
                from ..ecoflow.parsers.smartplug import parse_smartplug_proto_heartbeat
                result = parse_smartplug_proto_heartbeat(pdata)
                if result:
                    return result

        return None

