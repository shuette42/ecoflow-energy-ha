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
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_UNKNOWN,
    DOMAIN,
    ENERGY_STREAM_KEEPALIVE_S,
    APP_AUTH_UNAVAILABLE_GRACE_S,
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
    SMARTPLUG_POWER_TO_ENERGY,
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
        self._keepalive_unsub: asyncio.TimerHandle | None = None
        self._stale_check_unsub: asyncio.TimerHandle | None = None
        self._quotas_unsub: asyncio.TimerHandle | None = None
        self._ping_unsub: asyncio.TimerHandle | None = None
        self._enhanced_mode: bool = enhanced_mode
        self._shutdown: bool = False
        self._last_flush_ts: float = 0.0
        self._consecutive_http_failures: int = 0
        self._device_available: bool = True
        self._last_stale_reconnect_ts: float = 0.0
        self._last_smartplug_get_all_ts: float = 0.0
        self._credential_obtained_ts: float = 0.0
        self._credential_refresh_unsub: asyncio.TimerHandle | None = None
        self._event_log: deque[dict[str, Any]] = deque(maxlen=50)
        # Stable SN → pack index mapping for proto heartbeats (cmd_id=7).
        # Each heartbeat contains only one pack; this map ensures the same
        # physical pack always maps to the same pack{n}_* sensor keys.
        self._bp_sn_to_index: dict[str, int] = {}

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
    def mqtt_status(self) -> str:
        """Return MQTT connection status for diagnostic sensor.

        Three-state model:
        - "receiving": TCP connected AND data flowing within stale threshold
        - "connected_stale": TCP connected but no recent data
        - "disconnected": TCP not connected
        - "not_configured": no MQTT client
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
        """Record an event for diagnostics (bounded FIFO, max 20 entries)."""
        self._event_log.append({
            "ts": time.time(),
            "type": event_type,
            "detail": detail,
        })

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for all entities of this device."""
        auth_method = getattr(self, "_auth_method", AUTH_METHOD_DEVELOPER)
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

        auth_method = getattr(self, "_auth_method", AUTH_METHOD_DEVELOPER)

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

        auth_method = getattr(self, "_auth_method", AUTH_METHOD_DEVELOPER)
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
                    return quota_map
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            # Proto get_reply: binary protobuf
            if b"\x0a" in payload[:4]:
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
                # Enhanced Mode: change reports and battery heartbeat
                if result.mapped.get("_is_ems_change") or result.mapped.get("_is_bp_heartbeat"):
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
        if now - getattr(self, "_last_mqtt_event_ts", 0) > 60:
            self._last_mqtt_event_ts = now
            self._log_event("mqtt_data", f"keys={len(parsed)}")
        self._enforce_monotonic(parsed)
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

        # Integrate power → energy via Riemann sum
        self._integrate_energy(parsed)
        # Throttle flush scheduling: at most once per 60s (matches integrator's SAVE_INTERVAL_S)
        if now - self._last_flush_ts > 60:
            self._last_flush_ts = now
            self.hass.async_create_task(self._async_flush_energy_state())

        self.async_set_updated_data(dict(self._device_data))

    async def _async_flush_energy_state(self) -> None:
        """Flush energy integrator state to disk (non-blocking)."""
        await self.hass.async_add_executor_job(self._energy_integrator.flush)

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
        self._device_data.update(parsed)

        # Riemann sum: integrate power → energy
        self._integrate_energy(parsed)
        # Flush state to disk periodically (non-blocking)
        await self.hass.async_add_executor_job(self._energy_integrator.flush)

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
        """Check if MQTT data is stale and switch to HTTP fallback if needed.

        4-tier reconnect strategy:
        1. Paho auto-reconnect (immediate, same ClientID) - handled by Paho
        2. Force-reconnect with new ClientID - handled by cloud_mqtt.try_reconnect()
        3. Counter-reset every 30 min (never give up) - handled by cloud_mqtt
        4. HTTP fallback - handled here when MQTT is stale (developer-auth only)
        """
        if self._shutdown:
            return

        stale_threshold_s = self._stale_threshold_s()
        age = time.monotonic() - self._last_mqtt_ts if self._last_mqtt_ts > 0 else float("inf")
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
            # App-auth: MQTT-only path, mark unavailable after sustained stale
            if age > stale_threshold_s:
                unavailable_after_s = stale_threshold_s + APP_AUTH_UNAVAILABLE_GRACE_S

                # Connected-but-silent sessions are common after broker-side rotation.
                # Force a reconnect at most once per stale threshold window.
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

                if self._device_available and age >= unavailable_after_s:
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
