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
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_DISPLAY_NAMES,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_STREAM,
    DEVICE_TYPE_UNKNOWN,
    DOMAIN,
    HTTP_FALLBACK_INTERVAL_S,
    POWEROCEAN_ENERGY_FROM_API,
    POWEROCEAN_POWER_TO_ENERGY,
    SMARTPLUG_ENERGY_FROM_API,
    SMARTPLUG_POWER_TO_ENERGY,
    STREAM_ENERGY_FROM_API,
    STREAM_POWER_TO_ENERGY,
    get_delta_profile,
)
from ..ecoflow.energy_integrator import EnergyIntegrator
from .availability import AvailabilityMixin
from .credentials import CredentialsMixin
from .http_poll import HttpPollMixin
from .keepalive import KeepaliveMixin
from .mqtt_ingest import MqttIngestMixin
from .set_commands import SetCommandsMixin
from .setup import SetupMixin
from .state_apply import StateApplyMixin

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
    MqttIngestMixin,
    StateApplyMixin,
    SetCommandsMixin,
    HttpPollMixin,
    AvailabilityMixin,
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
        # Battery charge/discharge state: rolling-average derivation (#63, #50).
        # State is derived from a short moving average of signed batt_w,
        # not the instantaneous value. This filters short oscillations that
        # occur when solar production and house load balance (morning/evening),
        # where instantaneous power swings from +1000W to -300W within seconds.
        # A confirmation window requires a diverging candidate state to persist
        # before the transition is committed; min hold time additionally blocks
        # a new transition right after a commit.
        self._batt_w_samples: list[tuple[float, float]] = []  # (monotonic_ts, batt_w)
        self._batt_state_changed_at: float = 0.0  # monotonic timestamp
        self._batt_pending_state: str | None = None  # candidate awaiting confirmation
        self._batt_pending_since: float = 0.0  # monotonic ts when candidate appeared

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
