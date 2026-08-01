"""MQTT message parsing and ingest for the EcoFlow device coordinator."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..const import (
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_STREAM,
    RAW_FRAME_MAX_BYTES,
)
from ..ecoflow.parsers.delta import parse_delta_report
from ..ecoflow.parsers.delta_http import parse_delta_http_quota
from ..ecoflow.parsers.delta3_http import parse_delta3_http_quota
from ..ecoflow.parsers.delta3_proto import (
    parse_delta3_bms_heartbeat,
    parse_delta3_cms_heartbeat,
    parse_delta3_display_property,
)
from ..ecoflow.parsers.powerocean import parse_powerocean_http_quota
from ..ecoflow.parsers.powerocean_proto import (
    flatten_heartbeat,
    remap_bp_keys,
    remap_ems_state_keys,
    remap_proto_keys,
)
from ..ecoflow.parsers.smartplug import (
    parse_smartplug_http_quota,
    parse_smartplug_report,
)
from ..ecoflow.parsers.stream_http import parse_stream_quota
from ..ecoflow.parsers.stream_proto import parse_stream_proto_message
from ..ecoflow.frame_capture import (
    build_frame_entry,
    decode_cmd_headers,
    frame_key,
    is_proto_frame,
)
from ..ecoflow.proto.runtime import (
    decode_proto_runtime_frame,
    decode_proto_runtime_headers,
)

_LOGGER = logging.getLogger(__name__)


def _collect_total_increasing_keys() -> frozenset[str]:
    """Collect all total_increasing sensor keys from the entity definitions.

    Derived from const.py so the monotonic guard cannot drift from the
    sensor definitions: any new total_increasing sensor is guarded
    automatically. Built once at module import; lookups stay O(1).
    """
    from .. import const as _const

    keys: set[str] = set()
    for name in dir(_const):
        if not re.fullmatch(r"[A-Z0-9]+_SENSORS", name):
            continue
        for item in getattr(_const, name):
            if item.state_class == "total_increasing":
                keys.add(item.key)
    return frozenset(keys)


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
    # these regressions prevents HA Recorder warnings. The set is derived
    # from the const.py sensor definitions (single source of truth).
    _MONOTONIC_KEYS: frozenset[str] = _collect_total_increasing_keys()

    def _enforce_monotonic(self, parsed: dict[str, Any]) -> dict[str, Any]:
        """Drop values that would decrease a total_increasing sensor."""
        for key in self._MONOTONIC_KEYS:
            if key in parsed and key in self._device_data:
                old = self._device_data[key]
                new = parsed[key]
                if (
                    isinstance(old, (int, float))
                    and isinstance(new, (int, float))
                    and new < old
                ):
                    del parsed[key]
        return parsed

    def _on_mqtt_message(self, topic: str, payload: bytes) -> None:
        """Handle an incoming MQTT message (Paho thread).

        In Standard Mode, MQTT is only used for SET commands — data updates
        come from HTTP polling. Exception: Delta and Smart Plug subscribe
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
        self._capture_raw_frame(topic, payload, parsed)
        if parsed:
            self.hass.loop.call_soon_threadsafe(self._apply_data, parsed)

    def _capture_raw_frame(
        self,
        topic: str,
        payload: bytes,
        parsed: dict[str, Any] | None,
    ) -> None:
        """Record a protobuf frame for diagnostics (Paho thread).

        Captures what the device actually sent alongside what the parser made
        of it, so a mis-decoded device variant can be diagnosed from a
        diagnostics download alone. Only the app-auth push path is captured:
        the HTTP quota path already exposes its keys verbatim.

        The frame is truncated and the device serial is masked before storage.
        Capture never affects ingest — any failure is swallowed.
        """
        if not self._enhanced_mode or not is_proto_frame(payload):
            return
        try:
            secrets = self._frame_secrets()
            entry = build_frame_entry(
                topic,
                payload,
                secrets,
                RAW_FRAME_MAX_BYTES,
                parsed_keys=len(parsed) if parsed else 0,
            )
            entry["cmds"] = decode_cmd_headers(payload)
            # Derived before the lock: the key comes out of the payload, and
            # the Paho thread should not hold the lock across that.
            key = frame_key(entry, payload, secrets)
            with self._raw_frames_lock:
                self._raw_frames.add(key, entry)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Raw frame capture failed", exc_info=True)

    def _record_unknown_fields(self, mapped: dict[str, Any]) -> None:
        """Hand a decoded message's undeclared field numbers to the coordinator.

        Runs on the Paho thread next to the frame capture, and like it must
        never cost a message: a diagnostics aid that can break ingest is worse
        than no diagnostics aid.
        """
        try:
            fields = mapped.get("_unknown_fields")
            cmd_key = mapped.get("_cmd_key")
            if isinstance(fields, dict) and isinstance(cmd_key, str):
                self.record_unknown_proto_fields(cmd_key, fields)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Unknown proto field capture failed", exc_info=True)

    def _frame_secrets(self) -> list[str]:
        """Return the identifiers that must be masked out of a stored frame."""
        secrets = [self.device_sn]
        if self._mqtt_client is not None:
            user_id = getattr(self._mqtt_client, "user_id", None)
            if isinstance(user_id, str):
                secrets.append(user_id)
        return secrets

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
                    if self.device_type == DEVICE_TYPE_STREAM:
                        parsed = parse_stream_quota(quota_map)
                        return parsed if parsed else None
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
                # Stream in Standard mode: flat camelCase JSON, optionally
                # wrapped in `param`/`params`. Routed through the field map so
                # raw quota keys never reach _device_data (#139).
                if self.device_type == DEVICE_TYPE_STREAM:
                    payload_obj = data.get("param")
                    if not isinstance(payload_obj, dict):
                        payload_obj = data.get("params")
                    if not isinstance(payload_obj, dict):
                        payload_obj = data
                    parsed = parse_stream_quota(payload_obj)
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
                if self.device_type == DEVICE_TYPE_POWEROCEAN:
                    return self._parse_powerocean_proto_frame(payload)

                result = decode_proto_runtime_frame(payload)
                self._record_unknown_fields(result.mapped)
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
                    # The BMS heartbeat has no HTTP counterpart, so it maps
                    # onto its own `bms_` sensor keys instead of the shared
                    # quota path.
                    if result.mapped.get("_is_delta3_bms_heartbeat"):
                        parsed = parse_delta3_bms_heartbeat(raw)
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
                # Enhanced Mode: EMS state report (cmd_id=17). Narrower
                # mapping than the change report, deliberately so.
                if result.mapped.get("_is_ems_state"):
                    parsed = remap_ems_state_keys(raw)
                    return parsed if parsed else None
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
                _LOGGER.debug(
                    "Protobuf decode error for %s", self.device_sn, exc_info=True
                )
            return None

        return None

    def _parse_powerocean_proto_frame(
        self, payload: bytes
    ) -> dict[str, Any] | None:
        """Decode and merge every PowerOcean header in one MQTT envelope.

        Get-All replies contain many independent headers, while normal pushes
        contain one or two. Each header is decoded with its own command tuple,
        pdata and sequence number; unsupported and empty companion headers are
        ignored. This same path therefore covers both bundled replies and the
        existing single-header HJ31/HJ32 traffic.
        """
        merged: dict[str, Any] = {}
        # The envelope decode stays guarded because the get_reply caller has no
        # guard of its own and runs on the Paho thread.
        try:
            results = decode_proto_runtime_headers(payload)
        except Exception:
            _LOGGER.debug(
                "PowerOcean protobuf decode error for %s",
                self.device_sn,
                exc_info=True,
            )
            results = []

        # One malformed header must not cost the keys already merged from the
        # others, so every result is merged under its own guard.
        for result in results:
            try:
                self._record_unknown_fields(result.mapped)
                raw = {
                    key: value
                    for key, value in result.mapped.items()
                    if not key.startswith("_")
                }

                if result.mapped.get("_is_energy_stream"):
                    merged.update(remap_proto_keys(raw))
                    continue
                if result.mapped.get("_is_pv_inv_energy_stream"):
                    merged.update(raw)
                    continue
                if result.mapped.get("_is_ems_heartbeat"):
                    merged.update(flatten_heartbeat(raw))
                    continue
                if result.mapped.get("_is_ems_param_change"):
                    merged.update(raw)
                    continue
                if result.mapped.get("_is_ems_state"):
                    merged.update(remap_ems_state_keys(raw))
                    continue
                if (
                    result.mapped.get("_is_ems_change")
                    or result.mapped.get("_is_bp_heartbeat")
                ):
                    if raw:
                        merged.update(
                            remap_bp_keys(
                                raw,
                                self._bp_sn_to_index,
                                self.device_sn,
                            )
                        )
            except Exception:
                _LOGGER.debug(
                    "PowerOcean protobuf decode error for %s (%s)",
                    self.device_sn,
                    result.parse_path,
                    exc_info=True,
                )

        return merged or None

    def _parse_powerocean_get_reply(self, payload: bytes) -> dict[str, Any] | None:
        """Backward-compatible wrapper for existing coordinator tests."""
        return self._parse_powerocean_proto_frame(payload)

    def _parse_proto_device_data(
        self, payload: bytes, headers: list[dict[str, Any]] | None = None
    ) -> dict[str, Any] | None:
        """Parse SmartPlug/Delta protobuf heartbeat via generic wire decoder.

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
                from ..ecoflow.parsers.smartplug import (
                    parse_smartplug_proto_heartbeat,
                )

                result = parse_smartplug_proto_heartbeat(pdata)
                if result:
                    return result

        return None
