"""DataUpdateCoordinator for EcoFlow devices.

Standard Mode: HTTP polling via IoT Developer API (POST /iot-open/sign/device/quota).
  - Primary data source is HTTP polling (update_interval=30s).
  - MQTT is used for SET commands (switches, numbers) only.
  - Exception: Delta devices additionally subscribe to MQTT push for real-time data.

Enhanced Mode: MQTT push via WSS (port 8084).
  - Primary data source is MQTT push (update_interval=None).
  - EnergyStreamSwitch keep-alive every 20s.
  - Falls back to HTTP polling when the MQTT stream is stale.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from ..const import (
    AUTH_METHOD_APP,
    AUTH_METHOD_DEVELOPER,
    CONF_AUTH_METHOD,
    DELTA_ENERGY_FROM_API,
    DELTA_POWER_TO_ENERGY,
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_DISPLAY_NAMES,
    APP_SURPLUS_SYNC_MIN_INTERVAL_S,
    APP_SURPLUS_SYNC_USER_GRACE_S,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_STREAM,
    DEVICE_TYPE_UNKNOWN,
    DOMAIN,
    HARD_UNAVAILABLE_S,
    HTTP_FALLBACK_INTERVAL_S,
    MQTT_HEALTH_CHECK_INTERVAL_S,
    POWEROCEAN_ENERGY_FROM_API,
    POWEROCEAN_POWER_TO_ENERGY,
    SMARTPLUG_ENERGY_FROM_API,
    SMARTPLUG_HARD_UNAVAILABLE_S,
    SMARTPLUG_POWER_TO_ENERGY,
    SMARTPLUG_SOFT_UNAVAILABLE_S,
    SOFT_UNAVAILABLE_S,
    STREAM_ENERGY_FROM_API,
    STREAM_POWER_TO_ENERGY,
    STALE_THRESHOLD_S,
    SMARTPLUG_STALE_THRESHOLD_S,
    get_delta_profile,
)
from ..ecoflow.energy_integrator import EnergyIntegrator
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
from .credentials import CredentialsMixin
from .keepalive import KeepaliveMixin
from .set_commands import SetCommandsMixin
from .setup import SetupMixin

if TYPE_CHECKING:
    from ..ecoflow.cloud_http import EcoFlowHTTPQuota
    from ..ecoflow.cloud_mqtt import EcoFlowMQTTClient
    from ..ecoflow.iot_api import IoTApiClient

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceSnapshot:
    """Immutable snapshot of device state at a point in time."""

    data: dict[str, Any] = field(default_factory=dict)
    captured_at: float = 0.0
    source: str = ""
    key_count: int = 0


class EcoFlowDeviceCoordinator(
    SetupMixin,
    CredentialsMixin,
    KeepaliveMixin,
    SetCommandsMixin,
    DataUpdateCoordinator[dict[str, Any]],
):
    """Coordinator for a single EcoFlow device."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: dict[str, Any],
    ) -> None:
        """Initialize the coordinator."""
        self.device_sn: str = device_info["sn"]
        # Re-classify device type from product_name if stored as "unknown"
        stored_type = device_info.get("device_type", "")
        if stored_type == DEVICE_TYPE_UNKNOWN:
            from ..const import get_device_type
            stored_type = get_device_type(device_info.get("product_name", ""), self.device_sn)
        self.device_type: str = stored_type
        display_name = DEVICE_TYPE_DISPLAY_NAMES.get(self.device_type, "")
        self.device_name: str = (
            device_info.get("name") or display_name or "EcoFlow Device"
        )
        self.product_name: str = (
            device_info.get("product_name") or display_name or "Unknown"
        )
        self._sw_version: str = device_info.get("sw_version", "")
        self.delta_profile: str = (
            get_delta_profile(self.product_name, self.device_sn)
            if self.device_type == DEVICE_TYPE_DELTA
            else ""
        )

        # App-auth (Enhanced Mode): WSS MQTT push, no HTTP polling.
        # Developer-auth (Standard Mode): HTTP polling + TCP MQTT.
        enhanced_mode = entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_APP

        # Standard Mode: HTTP polling every 30s (primary data source)
        # Enhanced Mode: MQTT push only — protobuf carries all sensor data
        #   (power, battery, MPPT, grid phases, EMS state).
        #   HTTP fallback activates only when MQTT is stale (>35s).
        poll_interval = (
            None if enhanced_mode
            else timedelta(seconds=HTTP_FALLBACK_INTERVAL_S)
        )

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"EcoFlow {self.device_name} ({self.device_sn[:8]})",
            update_interval=poll_interval,
        )

        self._entry = entry
        self._mqtt_client: EcoFlowMQTTClient | None = None
        self._http_client: EcoFlowHTTPQuota | None = None
        self._iot_api: IoTApiClient | None = None

        self._last_mqtt_ts: float = 0.0
        self._device_data: dict[str, Any] = {}
        self._snapshot = DeviceSnapshot()
        # Raw HTTP quota snapshot (Delta 3 only): the field map is
        # community-researched but not yet hardware-verified for every key,
        # so diagnostics expose the raw key/value pairs to let beta dumps
        # confirm existing mappings and surface keys still to be added.
        self._raw_quota: dict[str, Any] = {}
        self._raw_quota_captured_at: float = 0.0
        self._keepalive_unsub: asyncio.TimerHandle | None = None
        self._stale_check_unsub: asyncio.TimerHandle | None = None
        self._quotas_unsub: asyncio.TimerHandle | None = None
        self._ping_unsub: asyncio.TimerHandle | None = None
        self._enhanced_mode: bool = enhanced_mode
        self._auth_method: str = AUTH_METHOD_DEVELOPER
        self._shutdown: bool = False
        self._last_flush_ts: float = 0.0
        self._last_mqtt_event_ts: float = 0.0
        self._consecutive_http_failures: int = 0
        self._device_available: bool = True
        self._last_stale_reconnect_ts: float = 0.0
        self._last_smartplug_get_all_ts: float = 0.0
        # Surplus auto-sync state (PowerOcean Enhanced Mode):
        # the EcoFlow app sets the slider via cmd_id=112 wire field 4 only,
        # which leaves the EMS-side `sys_bat_backup_ratio` unchanged. The
        # device mirrors the app's value back via cmd_id=13 EmsParamChangeReport
        # field 10 (`dev_soc`). When that diverges from the EMS value, the
        # coordinator schedules a corrective both-field SET. The throttle
        # avoids redundant SETs and the user-grace gives the device echo time.
        self._last_app_surplus_sync_ts: float = 0.0
        self._last_user_surplus_set_ts: float = 0.0
        # Timestamp of the most recent EmsParamChangeReport (cmd_id=13) that
        # carried a `dev_soc` field. The auto-sync only acts on frames newer
        # than the last user SET — stale ParamChange frames (e.g. an EMS
        # echo of a value the user has since superseded in HA) would
        # otherwise pull HA back to the obsolete app-side value.
        self._last_ems_param_change_ts: float = 0.0
        # Debounce state for the PowerOcean SoC SET. HA Number-Entity sliders
        # send one SET per 5%-step during a mouse drag, which arrives at the
        # device at ~100 ms cadence. The device cannot keep all SETs in sync
        # between Field 3 (EMS) and Field 4 (App-Layer), so the two fields
        # desync. The debouncer coalesces all SET requests inside
        # POWEROCEAN_SOC_DEBOUNCE_S to a single frame carrying the most
        # recent (backup, solar) pair.
        self._powerocean_soc_pending: tuple[int, int] | None = None
        self._powerocean_soc_debounce_unsub: asyncio.TimerHandle | None = None
        self._credential_obtained_ts: float = 0.0
        self._credential_refresh_unsub: asyncio.TimerHandle | None = None
        self._event_log: deque[dict[str, Any]] = deque(maxlen=50)
        # Stable SN → pack index mapping for proto heartbeats (cmd_id=7).
        # Each heartbeat contains only one pack; this map ensures the same
        # physical pack always maps to the same pack{n}_* sensor keys.
        self._bp_sn_to_index: dict[str, int] = {}
        # Battery charge/discharge state: rolling-average derivation (#63).
        # State is derived from a 3-minute moving average of signed batt_w,
        # not the instantaneous value. This filters short oscillations that
        # occur when solar production and house load balance (morning/evening),
        # where instantaneous power swings from +1000W to -300W within seconds.
        # Min hold time prevents rapid flipping even if the average crosses
        # a threshold right after a transition.
        self._batt_w_samples: list[tuple[float, float]] = []  # (monotonic_ts, batt_w)
        self._batt_state_changed_at: float = 0.0  # monotonic timestamp

        # Energy integrator for power → kWh Riemann sum (all device types)
        state_path = hass.config.path(f".storage/ecoflow_energy_{self.device_sn}.json")
        self._energy_integrator = EnergyIntegrator(state_path)

        # Device-specific power → energy mappings
        if self.device_type == DEVICE_TYPE_POWEROCEAN:
            self._power_to_energy = POWEROCEAN_POWER_TO_ENERGY
            self._energy_from_api = POWEROCEAN_ENERGY_FROM_API
        elif self.device_type == DEVICE_TYPE_DELTA:
            self._power_to_energy = DELTA_POWER_TO_ENERGY
            self._energy_from_api = DELTA_ENERGY_FROM_API
        elif self.device_type == DEVICE_TYPE_SMARTPLUG:
            self._power_to_energy = SMARTPLUG_POWER_TO_ENERGY
            self._energy_from_api = SMARTPLUG_ENERGY_FROM_API
        elif self.device_type == DEVICE_TYPE_STREAM:
            self._power_to_energy = STREAM_POWER_TO_ENERGY
            self._energy_from_api = STREAM_ENERGY_FROM_API
        else:
            self._power_to_energy = {}
            self._energy_from_api = []

    @property
    def device_available(self) -> bool:
        """Return whether the device is considered reachable."""
        return self._device_available

    @property
    def device_data(self) -> dict[str, Any]:
        """Return the current device data dict."""
        return self._device_data

    @property
    def snapshot(self) -> DeviceSnapshot:
        """Return the latest device data snapshot."""
        return self._snapshot

    @property
    def raw_quota(self) -> dict[str, Any]:
        """Return the raw HTTP quota snapshot (Delta 3 only, else empty)."""
        return self._raw_quota

    @property
    def raw_quota_captured_at(self) -> float:
        """Return monotonic timestamp of the raw quota capture (0 = never)."""
        return self._raw_quota_captured_at

    def set_device_value(self, key: str, value: Any) -> None:
        """Set a single value in the persistent device data store.

        Used by entity platforms (e.g. number) for optimistic updates that
        must survive coordinator refresh cycles.
        """
        self._device_data[key] = value

    @property
    def enhanced_mode(self) -> bool:
        """Return whether Enhanced Mode is active."""
        return self._enhanced_mode

    @property
    def last_mqtt_ts(self) -> float:
        """Return the timestamp of the last MQTT message."""
        return self._last_mqtt_ts

    @property
    def mqtt_client(self) -> EcoFlowMQTTClient | None:
        """Return the MQTT client (or None if not set up)."""
        return self._mqtt_client

    @property
    def availability_stage(self) -> str:
        """Return graduated availability stage based on data age.

        Stages (app-auth MQTT-only path):
        - "healthy": data flowing within stale threshold
        - "stale": data age > stale threshold, reconnect active, entities still available
        - "degraded": data age > soft_unavailable, entities available with old values
        - "unavailable": data age > hard_unavailable, entities go unavailable in HA

        Developer-auth with HTTP fallback: uses _device_available flag (HTTP failures
        control availability), not data age.
        """
        # Developer-auth: HTTP fallback controls availability
        if self._http_client is not None:
            if not self._device_available:
                return "unavailable"
            age = self._mqtt_data_age()
            if age > self._stale_threshold_s():
                return "stale"
            return "healthy"

        # App-auth: graduated degradation based purely on data age.
        # This is independent of MQTT connection state - a disconnected
        # client with recent data is still "healthy" (data is fresh).
        age = self._mqtt_data_age()
        if age <= self._stale_threshold_s():
            return "healthy"
        if age <= self._soft_unavailable_s():
            return "stale"
        if age <= self._hard_unavailable_s():
            return "degraded"
        return "unavailable"

    @property
    def mqtt_status(self) -> str:
        """Return MQTT connection status for diagnostic sensor.

        Reports transport-level state, independent of availability stage.
        """
        if self._mqtt_client is None:
            return "not_configured"
        if not self._mqtt_client.is_connected():
            return "disconnected"
        if self.data_receiving:
            return "receiving"
        return "connected_stale"

    @property
    def data_receiving(self) -> bool:
        """Return whether MQTT is actively delivering data (not just TCP-connected)."""
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            return False
        if self._last_mqtt_ts <= 0:
            return False
        age = time.monotonic() - self._last_mqtt_ts
        return age <= self._stale_threshold_s()

    def _mqtt_data_age(self) -> float:
        """Return the age of the last MQTT data in seconds."""
        if self._last_mqtt_ts <= 0:
            return float("inf")
        return time.monotonic() - self._last_mqtt_ts

    def _soft_unavailable_s(self) -> float:
        """Return the soft-unavailable threshold for this device."""
        if self._enhanced_mode and self.device_type == DEVICE_TYPE_SMARTPLUG:
            return SMARTPLUG_SOFT_UNAVAILABLE_S
        return SOFT_UNAVAILABLE_S

    def _hard_unavailable_s(self) -> float:
        """Return the hard-unavailable threshold for this device."""
        if self._enhanced_mode and self.device_type == DEVICE_TYPE_SMARTPLUG:
            return SMARTPLUG_HARD_UNAVAILABLE_S
        return HARD_UNAVAILABLE_S

    @property
    def connection_mode(self) -> str:
        """Return current connection mode for diagnostic sensor."""
        if not self._enhanced_mode:
            return "standard"
        if self.update_interval is not None:
            return "enhanced_fallback"
        return "enhanced"

    @property
    def event_log(self) -> list[dict[str, Any]]:
        """Return the event history for diagnostics export."""
        return list(self._event_log)

    def _log_event(self, event_type: str, detail: str) -> None:
        """Record an event for diagnostics (bounded FIFO, max 50 entries)."""
        self._event_log.append({
            "ts": time.time(),
            "type": event_type,
            "detail": detail,
        })

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for all entities of this device."""
        auth_method = self._auth_method
        config_url = (
            "https://ecoflow.com"
            if auth_method == AUTH_METHOD_APP
            else "https://developer.ecoflow.com"
        )
        info = DeviceInfo(
            identifiers={(DOMAIN, self.device_sn)},
            manufacturer="EcoFlow",
            model=self.product_name,
            name=f"EcoFlow {self.device_name}",
            configuration_url=config_url,
        )
        if self._sw_version:
            info["sw_version"] = self._sw_version
        return info


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

        headers, _ = decode_header_message(payload)

        try:
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
            except (ValueError, Exception):
                continue

            if self.device_type == DEVICE_TYPE_SMARTPLUG:
                from ..ecoflow.parsers.smartplug import parse_smartplug_proto_heartbeat
                result = parse_smartplug_proto_heartbeat(pdata)
                if result:
                    return result

        return None

    def _apply_data(self, parsed: dict[str, Any]) -> None:
        """Apply parsed data and notify listeners (HA event loop)."""
        now = time.monotonic()
        self._last_mqtt_ts = now
        self._device_available = True
        # MQTT data proves credentials are valid — prevent false reauth (#2)
        self._consecutive_http_failures = 0
        # Rate-limited event log: at most once per 60s to avoid flooding the deque
        if now - self._last_mqtt_event_ts > 60:
            self._last_mqtt_event_ts = now
            self._log_event("mqtt_data", f"keys={len(parsed)}")
        self._enforce_monotonic(parsed)
        # Remove EMS raw battery state before update: bp_chg_dsg_sta reports the
        # controller MODE ("discharging" even at 0W/100% SoC), not the physical
        # state. The derivation below sets the correct value from actual power.
        # Without this, the parser overwrites the derived state on every EMS
        # report, causing ~250 false transitions/day (#50).
        parsed.pop("batt_charge_discharge_state", None)
        # Track the arrival of a fresh EmsParamChangeReport so the auto-sync
        # below can distinguish a real app-side change from a stale frame
        # whose value the user has since superseded.
        if "ems_app_surplus_pct" in parsed:
            self._last_ems_param_change_ts = now
        self._device_data.update(parsed)

        # Re-aggregate bp_remain_watth from accumulated device_data (#10).
        # Each proto heartbeat may only contain a subset of battery packs.
        # Computing the sum from _device_data (not the current message) ensures
        # all known packs contribute even if only one pack reported this tick.
        if any(k.endswith("_remain_watth") and k.startswith("pack") for k in parsed):
            self._device_data["bp_remain_watth"] = sum(
                v for k, v in self._device_data.items()
                if k.startswith("pack") and k.endswith("_remain_watth")
                and isinstance(v, (int, float))
            )

        # Derive battery charge/discharge state from actual power (#50).
        self._derive_battery_state()

        # Integrate power → energy via Riemann sum
        self._integrate_energy(parsed)
        # Throttle flush scheduling: at most once per 60s (matches integrator's SAVE_INTERVAL_S)
        if now - self._last_flush_ts > 60:
            self._last_flush_ts = now
            self.hass.async_create_task(self._async_flush_energy_state())

        self._snapshot = DeviceSnapshot(
            data=dict(self._device_data),
            captured_at=time.monotonic(),
            source="mqtt",
            key_count=len(self._device_data),
        )
        self.async_set_updated_data(dict(self._device_data))

        # PowerOcean Enhanced Mode: detect cloud-only app changes to the
        # solar-surplus slider via the EmsParamChangeReport.dev_soc echo
        # (cmd_id=13 wire field 10) and push a corrective both-field SET so
        # the EMS-side sys_bat_backup_ratio catches up to what the app set.
        if self._enhanced_mode and self.device_type == DEVICE_TYPE_POWEROCEAN:
            self._maybe_schedule_surplus_sync()

    def _maybe_schedule_surplus_sync(self) -> None:
        """Schedule an auto-sync SET if the app's dev_soc echo diverges from
        the EMS internal sys_bat_backup_ratio.

        The EcoFlow app sets the surplus slider via cmd_id=112 with only
        wire field 4 (`dev_soc`). The device acknowledges (result=0) but
        does not propagate it into the EMS-side `sys_bat_backup_ratio`
        (field 3). The device does, however, echo the app-set value back
        via the cmd_id=13 EmsParamChangeReport message (`dev_soc`, mapped
        here to `ems_app_surplus_pct`). When that value diverges from
        `ems_backup_ratio_pct`, this method schedules a corrective
        both-field SET that brings the EMS in line.
        """
        app_val = self._device_data.get("ems_app_surplus_pct")
        ems_val = self._device_data.get("ems_backup_ratio_pct")
        if app_val is None or ems_val is None:
            return
        try:
            app_int = int(app_val)
            ems_int = int(ems_val)
        except (TypeError, ValueError):
            return
        if app_int == ems_int:
            return

        # Edge-case suppress: at app_int == 100 (and likely 0), the EMS
        # internally clamps `sys_bat_backup_ratio` to ~90 by design even
        # though dev_soc / socDev hold the user value. Reissuing a SET
        # would never reconcile the two - it would just generate periodic
        # write traffic. The user-side mirror (ems_app_surplus_pct) is
        # the source of truth for the slider; the EMS-side divergence at
        # the boundaries is expected device behaviour.
        if app_int in (0, 100):
            return

        now = time.monotonic()
        # Suppress sync if the latest EmsParamChangeReport carrying the
        # `dev_soc` value is older than the user's most recent SET. The
        # ParamChange echo is event-driven and lags the EmsChangeReport
        # echo - if the user just pushed a new value in HA, the
        # ParamChange we still see may be the obsolete app-side mirror
        # of a value the user has now superseded. Without this guard the
        # auto-sync would reissue the *old* app value as a both-field
        # SET, dragging HA back to the value the user just left.
        if self._last_ems_param_change_ts <= self._last_user_surplus_set_ts:
            return
        if now - self._last_app_surplus_sync_ts < APP_SURPLUS_SYNC_MIN_INTERVAL_S:
            return
        if now - self._last_user_surplus_set_ts < APP_SURPLUS_SYNC_USER_GRACE_S:
            return

        backup_val = self._device_data.get("ems_discharge_lower_limit_pct", 0)
        try:
            backup_int = int(backup_val)
        except (TypeError, ValueError):
            backup_int = 0
        target_backup = min(backup_int, app_int)

        self._last_app_surplus_sync_ts = now
        _LOGGER.info(
            "PowerOcean surplus auto-sync (%s): app=%d ems=%d -> SET both=%d",
            self.device_sn, app_int, ems_int, app_int,
        )
        self._log_event(
            "surplus_auto_sync",
            f"app={app_int} ems={ems_int}",
        )
        self.hass.async_create_task(
            self.async_set_powerocean_soc(target_backup, app_int)
        )

    def mark_user_surplus_set(self) -> None:
        """Record a user-initiated surplus/backup change.

        The surplus auto-sync uses this timestamp to suppress stale
        app-side echoes.
        """
        self._last_user_surplus_set_ts = time.monotonic()

    async def _async_flush_energy_state(self) -> None:
        """Flush energy integrator state to disk (non-blocking)."""
        await self.hass.async_add_executor_job(self._energy_integrator.flush)

    # Battery state derivation parameters (#63).
    # These are class-level so tests can override without touching instance state.
    BATT_WINDOW_S = 180       # 3-minute rolling window
    BATT_MIN_SAMPLES = 10     # minimum samples before derivation is trusted
    BATT_OUTER_W = 150        # |avg| > 150W -> charging/discharging
    BATT_INNER_W = 50         # |avg| < 50W  -> standby
    BATT_MIN_HOLD_S = 120     # min seconds a state must be held before it can change

    def _derive_battery_state(self) -> None:
        """Derive battery charge/discharge state from a rolling-average power (#63).

        The raw EMS field bp_chg_dsg_sta reports the controller MODE, not the
        physical state, so we override it from signed batt_w. Using the
        instantaneous value causes rapid flipping when solar and house load
        balance (morning/evening): batt_w swings between +1000W and -300W
        within seconds, and any threshold check flips with each sample.

        Strategy:
          1. Append current batt_w to a rolling buffer (timestamp, value).
          2. Drop samples older than BATT_WINDOW_S seconds.
          3. Compute the mean over the buffer.
          4. Apply thresholds to the mean, not the raw sample.
          5. A deadband between BATT_INNER_W and BATT_OUTER_W keeps prev state.
          6. A transition requires the previous state to have been held for
             at least BATT_MIN_HOLD_S seconds.

        Prefers signed `batt_w` when available, falls back to the derived
        charge/discharge power split for HTTP-only paths that never expose
        signed power directly.
        """
        batt_w = self._device_data.get("batt_w")
        if batt_w is None:
            charge_w = self._device_data.get("batt_charge_power_w")
            discharge_w = self._device_data.get("batt_discharge_power_w")
            if charge_w is None or discharge_w is None:
                return
            batt_w = charge_w - discharge_w

        now_mono = time.monotonic()
        self._batt_w_samples.append((now_mono, float(batt_w)))
        cutoff = now_mono - self.BATT_WINDOW_S
        self._batt_w_samples = [
            (t, v) for t, v in self._batt_w_samples if t >= cutoff
        ]

        if len(self._batt_w_samples) < self.BATT_MIN_SAMPLES:
            return

        avg = sum(v for _, v in self._batt_w_samples) / len(self._batt_w_samples)
        prev = self._device_data.get("batt_charge_discharge_state")

        if avg > self.BATT_OUTER_W:
            derived = "charging"
        elif avg < -self.BATT_OUTER_W:
            derived = "discharging"
        elif abs(avg) < self.BATT_INNER_W:
            derived = "standby"
        else:
            return  # deadband: keep previous state

        if derived == prev:
            return

        hold_elapsed = now_mono - self._batt_state_changed_at
        if prev is not None and hold_elapsed < self.BATT_MIN_HOLD_S:
            return

        self._device_data["batt_charge_discharge_state"] = derived
        self._batt_state_changed_at = now_mono
        _LOGGER.debug(
            "Battery state for %s: avg(%ds)=%.1fW -> %s (was %s, held %.0fs, n=%d)",
            self.device_sn,
            self.BATT_WINDOW_S,
            avg,
            derived,
            prev,
            hold_elapsed,
            len(self._batt_w_samples),
        )


    # ------------------------------------------------------------------
    # HTTP fallback (called when MQTT is stale)
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """HTTP polling — primary source in Standard Mode, fallback in Enhanced.

        PowerOcean: POST /iot-open/sign/device/quota (with quotas array)
        Delta:      GET  /iot-open/sign/device/quota/all?sn=...
        """
        if self._http_client is None:
            return self._device_data

        # All device types use GET /quota/all — returns the most complete data
        raw = await self._http_client.get_quota_all()
        if not raw:
            error_code = self._http_client.last_error_code

            # Error 1006 = device not linked to API key — config issue, not auth (#2)
            if error_code == "1006":
                self._log_event("http_1006", "device not linked to API key")
                return dict(self._device_data)

            self._consecutive_http_failures += 1
            self._log_event("http_fail", f"consecutive={self._consecutive_http_failures}")
            if self._consecutive_http_failures >= 3:
                self._device_available = False
                self._snapshot = DeviceSnapshot(
                    data={},
                    captured_at=self._snapshot.captured_at,
                    source=self._snapshot.source,
                    key_count=0,
                )

            # In Enhanced Mode, only trigger reauth if MQTT has never delivered data.
            # When MQTT is active, HTTP failures are expected fallback noise (#2).
            mqtt_active = self._enhanced_mode and self._last_mqtt_ts > 0.0
            if self._consecutive_http_failures == 5 and not mqtt_active:
                _LOGGER.warning(
                    "HTTP quota failed %d consecutive times for %s — triggering re-authentication",
                    self._consecutive_http_failures, self.device_sn,
                )
                self._entry.async_start_reauth(self.hass)
            return dict(self._device_data)

        self._consecutive_http_failures = 0
        self._device_available = True
        self._log_event("http_ok", f"keys={len(raw)}")

        if self.device_type == DEVICE_TYPE_POWEROCEAN:
            parsed = parse_powerocean_http_quota(raw)
        elif self.device_type == DEVICE_TYPE_DELTA:
            parsed = parse_delta_http_quota(raw)
        elif self.device_type == DEVICE_TYPE_DELTA3:
            # Keep the raw quota snapshot for diagnostics: the Delta 3 field
            # map is community-researched but not yet hardware-verified for
            # every key, so the raw key names let beta dumps confirm and
            # extend the mapping.
            self._raw_quota = dict(raw)
            self._raw_quota_captured_at = time.monotonic()
            parsed = parse_delta3_http_quota(raw)
        elif self.device_type == DEVICE_TYPE_SMARTPLUG:
            parsed = parse_smartplug_http_quota(raw)
        else:
            parsed = raw
        self._enforce_monotonic(parsed)
        # Same pop as in _apply_data: prevent EMS raw battery state from
        # overwriting the power-derived value (#50).
        parsed.pop("batt_charge_discharge_state", None)
        self._device_data.update(parsed)

        # Derive battery state from power (same logic as MQTT path, #50)
        self._derive_battery_state()

        # Riemann sum: integrate power → energy
        self._integrate_energy(parsed)
        # Flush state to disk periodically (non-blocking)
        await self.hass.async_add_executor_job(self._energy_integrator.flush)

        self._snapshot = DeviceSnapshot(
            data=dict(self._device_data),
            captured_at=time.monotonic(),
            source="http",
            key_count=len(self._device_data),
        )
        return dict(self._device_data)

    # ------------------------------------------------------------------
    # Energy integration (Riemann sum)
    # ------------------------------------------------------------------

    def _integrate_energy(self, parsed: dict[str, Any]) -> None:
        """Integrate power readings into energy totals via Riemann sum.

        Uses device-specific power → energy mappings from const.py.
        For API-provided energy totals, prefer those over Riemann sum.
        """

        for power_key, energy_key in self._power_to_energy.items():
            power_w = parsed.get(power_key)
            if power_w is not None:
                total = self._energy_integrator.integrate(energy_key, abs(power_w))
                if total is not None:
                    self._device_data[energy_key] = round(total, 2)

        # API totals: prefer over Riemann sum (more accurate when available)
        for power_key, energy_key in self._energy_from_api:
            if energy_key in parsed:
                # API provided a total — use it (already set by parser)
                self._energy_integrator.set_total(energy_key, parsed[energy_key])
            else:
                # No API total — integrate from power
                power_w = parsed.get(power_key)
                if power_w is not None:
                    total = self._energy_integrator.integrate(energy_key, abs(power_w))
                    if total is not None:
                        self._device_data[energy_key] = round(total, 2)

    # ------------------------------------------------------------------
    # Stale detection + fallback switching (4-tier reconnect tier 4)
    # ------------------------------------------------------------------

    def _schedule_stale_check(self) -> None:
        """Schedule a periodic check for stale MQTT data."""
        stale_threshold_s = min(self._stale_threshold_s(), MQTT_HEALTH_CHECK_INTERVAL_S)
        self._stale_check_unsub = self.hass.loop.call_later(
            stale_threshold_s, self._check_stale,
        )

    def _stale_threshold_s(self) -> float:
        """Return the MQTT stale threshold for this device."""
        if self._enhanced_mode and self.device_type == DEVICE_TYPE_SMARTPLUG:
            return SMARTPLUG_STALE_THRESHOLD_S
        return STALE_THRESHOLD_S

    def _check_stale(self) -> None:
        """Check MQTT data freshness and manage graduated availability.

        Reconnect strategy (unchanged):
        1. Paho auto-reconnect (immediate, same ClientID) - handled by Paho
        2. Force-reconnect with new ClientID - on connected-but-silent
        3. Counter-reset every 5 min (never give up) - handled by cloud_mqtt
        4. HTTP fallback - developer-auth only

        Graduated availability (new):
        - healthy: data within stale threshold
        - stale: data age > stale threshold, reconnect active, entities available
        - degraded: data age > soft_unavailable, entities available with old values
        - unavailable: data age > hard_unavailable, entities go unavailable in HA
        """
        if self._shutdown:
            return

        stale_threshold_s = self._stale_threshold_s()
        age = self._mqtt_data_age()
        mqtt_connected = self._mqtt_client is not None and self._mqtt_client.is_connected()

        if self._http_client is not None:
            # Developer-auth: HTTP fallback available
            if age > stale_threshold_s and self.update_interval is None:
                _LOGGER.info(
                    "MQTT stale for %s (%.0fs) - switching to HTTP fallback (tier 4)",
                    self.device_sn, age,
                )
                self._log_event("stale_detected", f"age={age:.0f}s, http_fallback")
                self.update_interval = timedelta(seconds=HTTP_FALLBACK_INTERVAL_S)
            elif age <= stale_threshold_s and self.update_interval is not None:
                _LOGGER.info("MQTT recovered for %s - disabling HTTP fallback", self.device_sn)
                self._log_event("stale_recovered", "http_fallback_disabled")
                self.update_interval = None
        else:
            # App-auth: graduated degradation based on data age.
            # Force-reconnect is decoupled from entity availability.
            hard_unavailable_s = self._hard_unavailable_s()

            if age > stale_threshold_s:
                # Reconnect attempts: force-reconnect on connected-but-silent
                now = time.monotonic()
                if (
                    mqtt_connected
                    and self._mqtt_client is not None
                    and self._last_mqtt_ts > 0.0
                    and (now - self._last_stale_reconnect_ts) >= stale_threshold_s
                ):
                    self._last_stale_reconnect_ts = now
                    _LOGGER.info(
                        "MQTT stale for %s [%s] (%.0fs) while connected - forcing reconnect",
                        self.device_name,
                        self.device_sn,
                        age,
                    )
                    self._log_event("stale_force_reconnect", f"age={age:.0f}s")
                    self.hass.async_add_executor_job(self._mqtt_client.force_reconnect)

                # Graduated availability: only mark unavailable after hard threshold
                if self._device_available and age >= hard_unavailable_s:
                    reconnect_attempts = (
                        self._mqtt_client.reconnect_attempts
                        if self._mqtt_client is not None
                        else 0
                    )
                    _LOGGER.warning(
                        "MQTT stream interrupted for %s [%s] (%.0fs, reconnect_attempts=%d) - "
                        "marking device unavailable",
                        self.device_name,
                        self.device_sn,
                        age,
                        reconnect_attempts,
                    )
                    self._log_event("stale_unavailable", f"age={age:.0f}s, attempts={reconnect_attempts}")
                    self._device_available = False
                    self._snapshot = DeviceSnapshot(
                        data={},
                        captured_at=self._snapshot.captured_at,
                        source=self._snapshot.source,
                        key_count=0,
                    )
            else:
                if not self._device_available:
                    _LOGGER.info(
                        "MQTT recovered for %s [%s] - device available again",
                        self.device_name,
                        self.device_sn,
                    )
                    self._log_event("stale_recovered", "device_available")
                    self._device_available = True
                self._last_stale_reconnect_ts = 0.0

        # Tier 2+3: try MQTT reconnect if disconnected
        if self._mqtt_client is not None and not mqtt_connected:
            self.hass.async_add_executor_job(self._mqtt_client.try_reconnect)

        # Re-schedule unless shutting down
        self._stale_check_unsub = self.hass.loop.call_later(
            min(stale_threshold_s, MQTT_HEALTH_CHECK_INTERVAL_S), self._check_stale,
        )
