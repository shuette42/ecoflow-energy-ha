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
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    AUTH_METHOD_APP,
    AUTH_METHOD_DEVELOPER,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_SECRET_KEY,
    CONF_USER_ID,
    DELTA_ENERGY_FROM_API,
    DELTA_POWER_TO_ENERGY,
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_DISPLAY_NAMES,
    APP_SURPLUS_SYNC_MIN_INTERVAL_S,
    APP_SURPLUS_SYNC_USER_GRACE_S,
    POWEROCEAN_SOC_DEBOUNCE_S,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_UNKNOWN,
    DOMAIN,
    ENERGY_STREAM_KEEPALIVE_S,
    HARD_UNAVAILABLE_S,
    HTTP_FALLBACK_INTERVAL_S,
    MQTT_HEALTH_CHECK_INTERVAL_S,
    PING_KEEPALIVE_S,
    POWEROCEAN_ENERGY_FROM_API,
    POWEROCEAN_POWER_TO_ENERGY,
    QUOTAS_KEEPALIVE_S,
    SMARTPLUG_GET_ALL_KEEPALIVE_S,
    CREDENTIAL_REFRESH_CHECK_S,
    CREDENTIAL_MAX_AGE_S,
    SMARTPLUG_ENERGY_FROM_API,
    SMARTPLUG_HARD_UNAVAILABLE_S,
    SMARTPLUG_POWER_TO_ENERGY,
    SMARTPLUG_SOFT_UNAVAILABLE_S,
    SOFT_UNAVAILABLE_S,
    STALE_THRESHOLD_S,
    SMARTPLUG_STALE_THRESHOLD_S,
    get_delta_profile,
)
from .ecoflow.cloud_http import EcoFlowHTTPQuota
from .ecoflow.cloud_mqtt import EcoFlowMQTTClient
from .ecoflow.energy_integrator import EnergyIntegrator
from .ecoflow.iot_api import IoTApiClient
from .ecoflow.parsers.delta import parse_delta_report
from .ecoflow.parsers.delta_http import parse_delta_http_quota
from .ecoflow.parsers.powerocean_proto import (
    flatten_heartbeat,
    remap_bp_keys,
    remap_proto_keys,
)
from .ecoflow.parsers.powerocean import parse_powerocean_http_quota
from .ecoflow.parsers.smartplug import parse_smartplug_http_quota, parse_smartplug_report
from .ecoflow.proto.runtime import decode_proto_runtime_frame

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceSnapshot:
    """Immutable snapshot of device state at a point in time."""

    data: dict[str, Any] = field(default_factory=dict)
    captured_at: float = 0.0
    source: str = ""
    key_count: int = 0


class EcoFlowDeviceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
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
            from .const import get_device_type
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
    # Setup / teardown
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Set up the data source for this device."""
        self._auth_method = self._entry.data.get(CONF_AUTH_METHOD, AUTH_METHOD_DEVELOPER)
        session = async_get_clientsession(self.hass)

        # Load energy integrator state from disk (non-blocking)
        await self.hass.async_add_executor_job(self._energy_integrator.load_state)

        if self._auth_method == AUTH_METHOD_APP:
            await self._setup_app_auth(session)
        else:
            await self._setup_developer_auth(session)

    async def _setup_app_auth(self, session: Any) -> None:
        """Set up using app authentication (email/password, no Developer API keys).

        App-auth always uses WSS MQTT. No HTTP client or IoT API.
        """
        from .ecoflow.app_api import AppApiClient

        email = self._entry.data.get(CONF_EMAIL, "")
        password = self._entry.data.get(CONF_PASSWORD, "")

        if not email or not password:
            _LOGGER.error("App-auth: missing credentials for %s", self.device_sn)
            self._entry.async_start_reauth(self.hass)
            return

        app_api = AppApiClient(session, email, password)
        if not await app_api.login():
            _LOGGER.warning("App-auth: login failed for %s - triggering re-authentication", self.device_sn)
            self._entry.async_start_reauth(self.hass)
            return

        user_id = app_api.user_id or self._entry.data.get(CONF_USER_ID, "")

        # No IoT API, no HTTP client for app-auth
        self._iot_api = None
        self._http_client = None

        # Fetch portal MQTT credentials (AES-decrypted app-* creds)
        creds = await app_api.get_mqtt_credentials()
        if creds is None:
            _LOGGER.error("App-auth: failed to fetch MQTT credentials for %s", self.device_sn)
            self._entry.async_start_reauth(self.hass)
            return

        cert_account = creds.get("certificateAccount") or creds.get("userName", "")
        cert_password = creds.get("certificatePassword") or creds.get("password", "")

        self._mqtt_client = EcoFlowMQTTClient(
            certificate_account=cert_account,
            certificate_password=cert_password,
            device_sn=self.device_sn,
            message_handler=self._on_mqtt_message,
            user_id=user_id,
            wss_mode=True,
            enhanced_mode=(self._enhanced_mode and self.device_type == DEVICE_TYPE_POWEROCEAN),
            auth_error_handler=self._on_mqtt_auth_error,
        )

        self._credential_obtained_ts = time.monotonic()
        await self.hass.async_add_executor_job(self._start_mqtt)

        if self._enhanced_mode:
            if self.device_type == DEVICE_TYPE_POWEROCEAN:
                self._schedule_keepalive()
            self._schedule_quotas_poll()
        self._schedule_ping()
        self._schedule_stale_check()
        self._schedule_credential_refresh()

        _LOGGER.debug(
            "App-auth setup complete for %s (enhanced=%s)",
            self.device_sn, self._enhanced_mode,
        )

    async def _setup_developer_auth(self, session: Any) -> None:
        """Set up using Developer API keys (existing flow, unchanged)."""
        access_key = self._entry.data.get(CONF_ACCESS_KEY)
        secret_key = self._entry.data.get(CONF_SECRET_KEY)

        if not access_key or not secret_key:
            _LOGGER.error("Developer API keys missing for %s - triggering re-authentication", self.device_sn)
            self._entry.async_start_reauth(self.hass)
            return

        self._iot_api = IoTApiClient(session, access_key, secret_key)

        self._http_client = EcoFlowHTTPQuota(
            session, access_key, secret_key, self.device_sn,
        )

        # Standard Mode: HTTP polling is the primary data source.
        # MQTT is for SET commands only - except Delta and Smart Plug,
        # which also subscribe to the IoT MQTT /quota topic for
        # real-time push alongside HTTP polling.
        subscribe_mqtt = self.device_type in (
            DEVICE_TYPE_DELTA,
            DEVICE_TYPE_SMARTPLUG,
        )
        creds = await self._iot_api.get_mqtt_credentials()
        if creds is not None:
            cert_account = creds.get("certificateAccount", "")
            cert_password = creds.get("certificatePassword", "")
            self._mqtt_client = EcoFlowMQTTClient(
                certificate_account=cert_account,
                certificate_password=cert_password,
                device_sn=self.device_sn,
                message_handler=self._on_mqtt_message,
                user_id="",
                wss_mode=False,
                subscribe_data=subscribe_mqtt,
                auth_error_handler=(
                    self._on_mqtt_auth_error if subscribe_mqtt else None
                ),
            )
            self._credential_obtained_ts = time.monotonic()
            await self.hass.async_add_executor_job(self._start_mqtt)
        if subscribe_mqtt:
            _LOGGER.debug(
                "Standard Mode + MQTT push: HTTP every %ds + MQTT real-time for %s",
                HTTP_FALLBACK_INTERVAL_S, self.device_sn,
            )
        else:
            _LOGGER.debug(
                "Standard Mode: HTTP polling every %ds for %s",
                HTTP_FALLBACK_INTERVAL_S, self.device_sn,
            )

    def _start_mqtt(self) -> None:
        """Start the MQTT client (runs in executor thread)."""
        if self._mqtt_client is None:
            return
        if self._mqtt_client.create_client():
            if self._mqtt_client.connect():
                self._mqtt_client.start_loop()
                mode_label = "WSS Enhanced" if self._enhanced_mode else "TCP Standard"
                _LOGGER.info("MQTT started for %s (%s)", self.device_sn, mode_label)
                self._log_event("mqtt_connect", mode_label)
            else:
                _LOGGER.error("MQTT connect failed for %s", self.device_sn)
                self._log_event("mqtt_disconnect", "connect failed")
        else:
            _LOGGER.error("MQTT client creation failed for %s", self.device_sn)
            self._log_event("mqtt_disconnect", "client creation failed")

    async def async_shutdown(self) -> None:
        """Stop the MQTT client and cancel timers."""
        self._shutdown = True
        for handle in (
            self._keepalive_unsub, self._quotas_unsub, self._ping_unsub,
            self._stale_check_unsub, self._credential_refresh_unsub,
            self._powerocean_soc_debounce_unsub,
        ):
            if handle is not None:
                handle.cancel()
        self._keepalive_unsub = None
        self._quotas_unsub = None
        self._ping_unsub = None
        self._stale_check_unsub = None
        self._credential_refresh_unsub = None
        if self._mqtt_client is not None:
            await self.hass.async_add_executor_job(self._mqtt_client.disconnect)
            self._mqtt_client = None
        await self.hass.async_add_executor_job(self._energy_integrator.force_flush)
        await super().async_shutdown()

    # ------------------------------------------------------------------
    # EnergyStreamSwitch keep-alive (Enhanced Mode only)
    # ------------------------------------------------------------------

    def _schedule_keepalive(self) -> None:
        """Schedule the next EnergyStreamSwitch keep-alive."""
        self._keepalive_unsub = self.hass.loop.call_later(
            ENERGY_STREAM_KEEPALIVE_S,
            self._send_keepalive,
        )

    def _send_keepalive(self) -> None:
        """Send EnergyStreamSwitch and re-schedule (HA event loop)."""
        if self._shutdown:
            return
        if self._mqtt_client is not None and self._mqtt_client.is_connected():
            self.hass.async_add_executor_job(
                self._mqtt_client.send_energy_stream_switch,
            )
            _LOGGER.debug("EnergyStreamSwitch keepalive sent for %s", self.device_sn)
        else:
            _LOGGER.debug("EnergyStreamSwitch skipped for %s (not connected)", self.device_sn)
        if not self._shutdown:
            self._keepalive_unsub = self.hass.loop.call_later(
                ENERGY_STREAM_KEEPALIVE_S,
                self._send_keepalive,
            )

    # ------------------------------------------------------------------
    # latestQuotas poll (Enhanced Mode — app-level keepalive, every 30s)
    # ------------------------------------------------------------------

    def _schedule_quotas_poll(self) -> None:
        """Schedule the next latestQuotas poll."""
        self._quotas_unsub = self.hass.loop.call_later(
            QUOTAS_KEEPALIVE_S, self._send_quotas_poll,
        )

    def _send_quotas_poll(self) -> None:
        """Send latestQuotas request and re-schedule."""
        if self._shutdown:
            return
        if self._mqtt_client is not None and self._mqtt_client.is_connected():
            self.hass.async_add_executor_job(
                self._mqtt_client.send_latest_quotas,
            )
            # Smart Plug app-auth: periodically pull a full snapshot in addition
            # to latestQuotas. This keeps control-state fields fresh and adds
            # resilience against sparse telemetry bursts.
            if self.device_type == DEVICE_TYPE_SMARTPLUG:
                now = time.monotonic()
                if (
                    self._last_smartplug_get_all_ts <= 0.0
                    or (now - self._last_smartplug_get_all_ts) >= SMARTPLUG_GET_ALL_KEEPALIVE_S
                ):
                    self._last_smartplug_get_all_ts = now
                    self.hass.async_add_executor_job(
                        self._mqtt_client.send_get_all,
                    )
        if not self._shutdown:
            self._quotas_unsub = self.hass.loop.call_later(
                QUOTAS_KEEPALIVE_S, self._send_quotas_poll,
            )

    # ------------------------------------------------------------------
    # Ping heartbeat (Enhanced Mode — MQTT-level keepalive, every 60s)
    # ------------------------------------------------------------------

    def _schedule_ping(self) -> None:
        """Schedule the next ping heartbeat."""
        self._ping_unsub = self.hass.loop.call_later(
            PING_KEEPALIVE_S, self._send_ping,
        )

    def _send_ping(self) -> None:
        """Send ping and re-schedule."""
        if self._shutdown:
            return
        if self._mqtt_client is not None and self._mqtt_client.is_connected():
            self.hass.async_add_executor_job(
                self._mqtt_client.send_ping,
            )
        if not self._shutdown:
            self._ping_unsub = self.hass.loop.call_later(
                PING_KEEPALIVE_S, self._send_ping,
            )

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

    # ------------------------------------------------------------------
    # Delta MQTT auth error handling (credential refresh on rc=5)
    # ------------------------------------------------------------------

    def _on_mqtt_auth_error(self) -> None:
        """Handle MQTT AUTH error (rc=5) - schedule credential refresh."""
        _LOGGER.warning("MQTT AUTH error for %s - scheduling credential refresh", self.device_sn)
        self._log_event("reauth", "mqtt_auth_error")
        self.hass.loop.call_soon_threadsafe(
            self.hass.async_create_task,
            self._refresh_mqtt_credentials(),
        )

    async def _refresh_mqtt_credentials(self) -> None:
        """Refresh MQTT credentials after AUTH failure."""
        if self._mqtt_client is None:
            return

        auth_method = self._auth_method

        if auth_method == AUTH_METHOD_APP:
            # App-auth: re-login and re-fetch portal credentials
            session = async_get_clientsession(self.hass)
            from .ecoflow.app_api import AppApiClient

            email = self._entry.data.get(CONF_EMAIL, "")
            password = self._entry.data.get(CONF_PASSWORD, "")
            if not email or not password:
                _LOGGER.warning("App-auth credential refresh failed for %s - no credentials", self.device_sn)
                self._entry.async_start_reauth(self.hass)
                return

            app_api = AppApiClient(session, email, password)
            if not await app_api.login():
                _LOGGER.warning("App-auth credential refresh failed for %s - login failed", self.device_sn)
                self._entry.async_start_reauth(self.hass)
                return

            creds = await app_api.get_mqtt_credentials()
            if creds is not None:
                cert_account = creds.get("certificateAccount") or creds.get("userName", "")
                cert_password = creds.get("certificatePassword") or creds.get("password", "")
                self._mqtt_client.update_credentials(cert_account, cert_password)
                self._credential_obtained_ts = time.monotonic()
                self._log_event("credential_refresh_ok", "app-auth")
                _LOGGER.debug("App-auth MQTT credentials refreshed for %s", self.device_sn)
            else:
                self._log_event("credential_refresh_fail", "app-auth, no credentials")
                _LOGGER.warning("App-auth credential refresh failed for %s - triggering re-authentication", self.device_sn)
                self._entry.async_start_reauth(self.hass)
        else:
            # Developer-auth: use IoT API
            if self._iot_api is None:
                return
            creds = await self._iot_api.refresh_credentials()
            if creds is not None:
                self._mqtt_client.update_credentials(
                    creds.get("certificateAccount", ""),
                    creds.get("certificatePassword", ""),
                )
                self._credential_obtained_ts = time.monotonic()
                self._log_event("credential_refresh_ok", "developer-auth")
                _LOGGER.debug("MQTT credentials refreshed for %s", self.device_sn)
            else:
                self._log_event("credential_refresh_fail", "developer-auth")
                _LOGGER.warning("MQTT credential refresh failed for %s - triggering re-authentication", self.device_sn)
                self._entry.async_start_reauth(self.hass)

    # ------------------------------------------------------------------
    # Proactive credential refresh (before expiry)
    # ------------------------------------------------------------------

    def _schedule_credential_refresh(self) -> None:
        """Schedule periodic credential age check."""
        if self._shutdown:
            return
        self._credential_refresh_unsub = self.hass.loop.call_later(
            CREDENTIAL_REFRESH_CHECK_S, self._check_credential_age,
        )

    def _check_credential_age(self) -> None:
        """Check if credentials are old enough to warrant proactive refresh."""
        if self._shutdown:
            return

        if self._credential_obtained_ts > 0:
            age = time.monotonic() - self._credential_obtained_ts
            if age >= CREDENTIAL_MAX_AGE_S:
                _LOGGER.debug(
                    "Credentials for %s are %.0fh old - proactive refresh",
                    self.device_sn, age / 3600,
                )
                self._log_event("credential_proactive_refresh", f"age={age / 3600:.0f}h")
                self.hass.async_create_task(self._proactive_credential_refresh())
            else:
                _LOGGER.debug(
                    "Credentials for %s are %.0fh old - still fresh",
                    self.device_sn, age / 3600,
                )

        # Re-schedule
        if not self._shutdown:
            self._credential_refresh_unsub = self.hass.loop.call_later(
                CREDENTIAL_REFRESH_CHECK_S, self._check_credential_age,
            )

    async def _proactive_credential_refresh(self) -> None:
        """Proactively refresh credentials before they expire."""
        if self._mqtt_client is None:
            return

        auth_method = self._auth_method
        old_account = self._mqtt_client.cert_account

        if auth_method == AUTH_METHOD_APP:
            session = async_get_clientsession(self.hass)
            from .ecoflow.app_api import AppApiClient

            email = self._entry.data.get(CONF_EMAIL, "")
            password = self._entry.data.get(CONF_PASSWORD, "")
            if not email or not password:
                return

            app_api = AppApiClient(session, email, password)
            if not await app_api.login():
                _LOGGER.debug("Proactive credential refresh: login failed for %s", self.device_sn)
                self._log_event("credential_proactive_fail", "login failed")
                return

            creds = await app_api.get_mqtt_credentials()
            if creds is not None:
                cert_account = creds.get("certificateAccount") or creds.get("userName", "")
                cert_password = creds.get("certificatePassword") or creds.get("password", "")
                self._mqtt_client.update_credentials(cert_account, cert_password)
                self._credential_obtained_ts = time.monotonic()
                self._log_event("credential_proactive_ok", "app-auth")
                if cert_account != old_account:
                    _LOGGER.debug("Proactive refresh: credentials changed for %s - force reconnect", self.device_sn)
                    self.hass.async_add_executor_job(self._mqtt_client.force_reconnect)
            else:
                self._log_event("credential_proactive_fail", "no credentials")
        else:
            if self._iot_api is None:
                return
            creds = await self._iot_api.refresh_credentials()
            if creds is not None:
                cert_account = creds.get("certificateAccount", "")
                cert_password = creds.get("certificatePassword", "")
                self._mqtt_client.update_credentials(cert_account, cert_password)
                self._credential_obtained_ts = time.monotonic()
                self._log_event("credential_proactive_ok", "developer-auth")
                if cert_account != old_account:
                    _LOGGER.debug("Proactive refresh: credentials changed for %s - force reconnect", self.device_sn)
                    self.hass.async_add_executor_job(self._mqtt_client.force_reconnect)
            else:
                self._log_event("credential_proactive_fail", "api failed")

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
            return

        if not self._enhanced_mode and self.device_type not in (DEVICE_TYPE_DELTA, DEVICE_TYPE_SMARTPLUG):
            return  # Standard Mode (non-Delta/SmartPlug): ignore MQTT data
        parsed = self._parse_message(topic, payload)
        if parsed:
            self.hass.loop.call_soon_threadsafe(self._apply_data, parsed)

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
                result = decode_proto_runtime_frame(payload)
                if result.mapped.get("_is_energy_stream"):
                    raw = {
                        k: v
                        for k, v in result.mapped.items()
                        if not k.startswith("_")
                    }
                    return remap_proto_keys(raw)
                # Enhanced Mode: heartbeat with nested extraction
                if result.mapped.get("_is_ems_heartbeat"):
                    raw = {
                        k: v
                        for k, v in result.mapped.items()
                        if not k.startswith("_")
                    }
                    return flatten_heartbeat(raw)
                # Enhanced Mode: param change report (cmd_id=13) carries
                # only `ems_app_surplus_pct` (renamed from `dev_soc`). This
                # field has no entry in the BP/EMS-change rename tables and
                # would be dropped by remap_bp_keys, so pass it through
                # unchanged.
                if result.mapped.get("_is_ems_param_change"):
                    raw = {
                        k: v
                        for k, v in result.mapped.items()
                        if not k.startswith("_")
                    }
                    return raw or None
                # Enhanced Mode: change reports and battery heartbeat
                if (
                    result.mapped.get("_is_ems_change")
                    or result.mapped.get("_is_bp_heartbeat")
                ):
                    raw = {
                        k: v
                        for k, v in result.mapped.items()
                        if not k.startswith("_")
                    }
                    if not raw:
                        return None
                    return remap_bp_keys(raw, self._bp_sn_to_index, self.device_sn)
                # Non-PowerOcean protobuf: SmartPlug heartbeats
                return self._parse_proto_device_data(payload)
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
        from .ecoflow.proto.decoder import decode_header_message
        from .ecoflow.parsers.powerocean_proto import remap_bp_keys

        headers, _ = decode_header_message(payload)

        try:
            from .ecoflow.proto import ecocharge_pb2 as pb2
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

    def _parse_proto_device_data(self, payload: bytes) -> dict[str, Any] | None:
        """Parse SmartPlug/Delta protobuf heartbeat via generic wire-format decoder."""
        from .ecoflow.proto.decoder import decode_header_message

        headers, _ = decode_header_message(payload)
        for hdr in headers:
            pdata_hex = hdr.get("pdata")
            if not pdata_hex:
                continue
            try:
                pdata = bytes.fromhex(pdata_hex)
            except (ValueError, Exception):
                continue

            if self.device_type == DEVICE_TYPE_SMARTPLUG:
                from .ecoflow.parsers.smartplug import parse_smartplug_proto_heartbeat
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
    # SET commands (switches, numbers)
    # ------------------------------------------------------------------

    async def async_set_soc_limits(
        self, max_charge_soc: int, min_discharge_soc: int,
    ) -> bool:
        """Send SoC limits to PowerOcean via WSS Protobuf (Enhanced Mode only).

        Sends a SysBatChgDsgSet message (cmd_func=96, cmd_id=112) with
        2 fields: charge upper limit and discharge lower limit.
        """
        if not self._enhanced_mode:
            _LOGGER.warning("SoC limit SET requires Enhanced Mode (%s)", self.device_sn)
            return False
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning("Cannot send SoC limits - MQTT not connected (%s)", self.device_sn)
            return False

        from .ecoflow.energy_stream import build_soc_limit_set_payload

        payload = build_soc_limit_set_payload(max_charge_soc, min_discharge_soc)
        ok = await self.hass.async_add_executor_job(
            self._mqtt_client.send_proto_set, payload,
        )
        if ok:
            _LOGGER.debug(
                "SoC limits sent: max=%d, min=%d (%s)",
                max_charge_soc, min_discharge_soc, self.device_sn,
            )
            self._log_event("set_soc_limits", f"max={max_charge_soc}, min={min_discharge_soc}")
        else:
            _LOGGER.warning("SoC limits SET failed (%s)", self.device_sn)
            self._log_event("set_soc_limits_fail", f"max={max_charge_soc}, min={min_discharge_soc}")
        return ok

    async def async_set_powerocean_soc_debounced(
        self, backup_reserve_pct: int, solar_surplus_pct: int,
    ) -> bool:
        """Coalesce rapid-fire SoC SET requests (HA slider drag) into one frame.

        HA's Number-Entity emits one async_set_native_value call per 5%-step
        when the user drags the slider, producing 5-10 SETs in <1 s. The
        device cannot keep wire field 3 (sys_bat_backup_ratio, EMS) and
        field 4 (dev_soc, App-Layer) in sync at that cadence, so the two
        fields drift apart and the user sees stale values in HA or the
        EcoFlow app. This method stores the latest (backup, solar) and
        defers the actual MQTT SET by `POWEROCEAN_SOC_DEBOUNCE_S`. Each
        new call within the window resets the timer, so only the final
        value reaches the device.

        Returns True synchronously - the caller should treat this as an
        accepted user request and apply the optimistic UI value. The
        actual SET runs asynchronously and may still fail; failures are
        logged via the underlying async_set_powerocean_soc.
        """
        if not self._enhanced_mode:
            _LOGGER.warning(
                "PowerOcean SoC SET requires Enhanced Mode (%s)", self.device_sn,
            )
            return False
        if backup_reserve_pct > solar_surplus_pct:
            _LOGGER.warning(
                "PowerOcean SoC SET rejected locally: backup_reserve (%d) > "
                "solar_surplus (%d). Device requires backup <= solar.",
                backup_reserve_pct, solar_surplus_pct,
            )
            return False

        self._powerocean_soc_pending = (backup_reserve_pct, solar_surplus_pct)
        if self._powerocean_soc_debounce_unsub is not None:
            self._powerocean_soc_debounce_unsub.cancel()
        self._powerocean_soc_debounce_unsub = self.hass.loop.call_later(
            POWEROCEAN_SOC_DEBOUNCE_S,
            lambda: self.hass.async_create_task(self._flush_powerocean_soc()),
        )
        return True

    async def _flush_powerocean_soc(self) -> None:
        """Send the most recent debounced SoC SET to the device."""
        if self._powerocean_soc_debounce_unsub is not None:
            self._powerocean_soc_debounce_unsub.cancel()
            self._powerocean_soc_debounce_unsub = None
        pending = self._powerocean_soc_pending
        if pending is None:
            return
        self._powerocean_soc_pending = None
        backup, solar = pending
        await self.async_set_powerocean_soc(backup, solar)

    async def async_set_powerocean_soc(
        self, backup_reserve_pct: int, solar_surplus_pct: int,
    ) -> bool:
        """Send a 3-field SoC SET to PowerOcean (app-replay format).

        Wire: cmd_id=112 SysBatChgDsgSet with field 1=100 (sys_bat_chg_up_limit),
        field 2=backup (sys_bat_dsg_down_limit), field 3=solar_surplus
        (sys_bat_backup_ratio), plus extended envelope (check_type, from=ios,
        device_sn). The legacy `async_set_soc_limits` only sends fields 1+2
        and is silently ignored by the device for backup-reserve changes.
        """
        if not self._enhanced_mode:
            _LOGGER.warning("PowerOcean SoC SET requires Enhanced Mode (%s)", self.device_sn)
            return False
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning("Cannot send PowerOcean SoC - MQTT not connected (%s)", self.device_sn)
            return False
        if backup_reserve_pct > solar_surplus_pct:
            _LOGGER.warning(
                "PowerOcean SoC SET rejected locally: backup_reserve (%d) > "
                "solar_surplus (%d). Device requires backup <= solar.",
                backup_reserve_pct, solar_surplus_pct,
            )
            return False

        from .ecoflow.energy_stream import build_powerocean_soc_set_payload

        payload = build_powerocean_soc_set_payload(
            backup_reserve_pct,
            solar_surplus_pct,
            device_sn=self.device_sn,
        )
        ok = await self.hass.async_add_executor_job(
            self._mqtt_client.send_proto_set, payload,
        )
        label = f"backup={backup_reserve_pct} solar={solar_surplus_pct}"
        if ok:
            _LOGGER.debug("PowerOcean SoC sent: %s (%s)", label, self.device_sn)
            self._log_event("set_powerocean_soc", label)
        else:
            _LOGGER.warning("PowerOcean SoC SET failed: %s (%s)", label, self.device_sn)
            self._log_event("set_powerocean_soc_fail", label)
        return ok

    async def async_set_powerocean_work_mode(self, work_mode: int) -> bool:
        """Send SysWorkModeSet (cmd_id=98) for PowerOcean.

        Phase 1 supports only modes that work without sub-params:
        SELFUSE (0) and AI_SCHEDULE (12). TOU (1) and BACKUP (2) require
        TouParam/BackupParam and return result=1 if sent without them.
        """
        if not self._enhanced_mode:
            _LOGGER.warning(
                "Work-mode SET requires Enhanced Mode (%s)", self.device_sn,
            )
            return False
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning(
                "Cannot send work-mode - MQTT not connected (%s)", self.device_sn,
            )
            return False

        from .ecoflow.energy_stream import build_work_mode_set_payload

        payload = build_work_mode_set_payload(work_mode)
        ok = await self.hass.async_add_executor_job(
            self._mqtt_client.send_proto_set, payload,
        )
        if ok:
            _LOGGER.debug("Work-mode sent: %d (%s)", work_mode, self.device_sn)
            self._log_event("set_work_mode", str(work_mode))
        else:
            _LOGGER.warning("Work-mode SET failed: %d (%s)", work_mode, self.device_sn)
            self._log_event("set_work_mode_fail", str(work_mode))
        return ok

    async def async_send_proto_set_command(
        self, payload: bytes, label: str,
    ) -> bool:
        """Send a protobuf SET command via WSS MQTT."""
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning("Cannot send proto SET (%s) - MQTT not connected (%s)", label, self.device_sn)
            return False

        ok = await self.hass.async_add_executor_job(
            self._mqtt_client.send_proto_set, payload,
        )
        if ok:
            _LOGGER.debug("Proto SET sent: %s (%s)", label, self.device_sn)
            self._log_event(f"proto_set_{label}", "ok")
        else:
            _LOGGER.warning("Proto SET failed: %s (%s)", label, self.device_sn)
            self._log_event(f"proto_set_{label}_fail", "")
        return ok

    async def async_send_set_command(self, command: dict[str, Any]) -> bool:
        """Send a SET command to the device via MQTT.

        The IoT API SET format:
        Topic: /open/{certAccount}/{SN}/set
        Payload: {"id": <ts>, "version": "1.0", ...command}
        """
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning("Cannot send SET command - MQTT not connected (%s)", self.device_sn)
            return False

        msg_id = int(time.time() * 1000) % 1_000_000
        payload = json.dumps(
            {
                "from": "Android",
                "id": str(msg_id),
                "version": "1.0",
                **command,
            }
        )
        if self._mqtt_client.wss_mode:
            topic = f"/app/{self._mqtt_client.user_id}/{self.device_sn}/thing/property/set"
        else:
            topic = f"/open/{self._mqtt_client.cert_account}/{self.device_sn}/set"

        ok = await self.hass.async_add_executor_job(
            self._mqtt_client.publish, topic, payload, 1,
        )
        if ok:
            _LOGGER.debug("SET command sent: %s -> %s", topic, payload[:120])
            self._log_event("set_cmd", f"keys={list(command.keys())[:3]}")
        else:
            _LOGGER.warning("SET command failed: %s", topic)
            self._log_event("set_cmd_fail", f"keys={list(command.keys())[:3]}")
        return ok

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
