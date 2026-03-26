"""DataUpdateCoordinator for EcoFlow devices.

Standard Mode: HTTP polling via IoT Developer API (POST /iot-open/sign/device/quota).
  - Primary data source is HTTP polling (update_interval=30s).
  - MQTT is used for SET commands (switches, numbers) only.

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
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_ACCESS_KEY,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SECRET_KEY,
    CONF_USER_ID,
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_UNKNOWN,
    DOMAIN,
    ENERGY_STREAM_KEEPALIVE_S,
    HTTP_FALLBACK_INTERVAL_S,
    MODE_ENHANCED,
    PING_KEEPALIVE_S,
    QUOTAS_KEEPALIVE_S,
    STALE_THRESHOLD_S,
)
from .ecoflow.cloud_http import EcoFlowHTTPQuota
from .ecoflow.cloud_mqtt import EcoFlowMQTTClient
from .ecoflow.energy_integrator import EnergyIntegrator
from .ecoflow.iot_api import IoTApiClient
from .ecoflow.parsers.delta import parse_delta_report
from .ecoflow.parsers.delta_http import parse_delta_http_quota
from .ecoflow.parsers.powerocean import parse_powerocean_http_quota
from .ecoflow.parsers.smartplug import parse_smartplug_http_quota
from .ecoflow.proto.runtime import decode_proto_runtime_frame

logger = logging.getLogger(__name__)


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
        self.device_name: str = device_info.get("name", "EcoFlow Device")
        self.product_name: str = device_info.get("product_name", "Unknown")
        # Re-classify device type from product_name if stored as "unknown"
        stored_type = device_info.get("device_type", "")
        if stored_type == DEVICE_TYPE_UNKNOWN:
            from .const import get_device_type
            stored_type = get_device_type(device_info.get("product_name", ""))
        self.device_type: str = stored_type

        # Enhanced Mode only applies to PowerOcean (WSS Protobuf stream).
        # Delta and Smart Plug have no WSS data source — always Standard.
        enhanced_mode = (
            entry.data.get(CONF_MODE) == MODE_ENHANCED
            and self.device_type == DEVICE_TYPE_POWEROCEAN
        )

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
            logger,
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

        # Energy integrator for power → kWh Riemann sum (PowerOcean)
        self._energy_integrator: EnergyIntegrator | None = None
        if self.device_type == DEVICE_TYPE_POWEROCEAN:
            state_path = hass.config.path(f".storage/ecoflow_energy_{self.device_sn}.json")
            self._energy_integrator = EnergyIntegrator(state_path)

    @property
    def device_data(self) -> dict[str, Any]:
        """Return the current device data dict."""
        return self._device_data

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

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Set up the data source for this device."""
        access_key = self._entry.data[CONF_ACCESS_KEY]
        secret_key = self._entry.data[CONF_SECRET_KEY]
        session = async_get_clientsession(self.hass)

        self._iot_api = IoTApiClient(session, access_key, secret_key)

        # Load energy integrator state from disk (non-blocking)
        if self._energy_integrator is not None:
            await self.hass.async_add_executor_job(self._energy_integrator.load_state)

        # HTTP client (used in Standard Mode as primary, Enhanced as fallback)
        self._http_client = EcoFlowHTTPQuota(
            session, access_key, secret_key, self.device_sn,
        )

        if self._enhanced_mode:
            await self._setup_enhanced(session)
        else:
            # Standard Mode: HTTP polling is the primary data source.
            # MQTT is set up for SET commands (switches/numbers) only.
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
                    subscribe_data=False,
                )
                await self.hass.async_add_executor_job(self._start_mqtt)
            logger.info(
                "Standard Mode: HTTP polling every %ds for %s",
                HTTP_FALLBACK_INTERVAL_S, self.device_sn,
            )

    async def _setup_enhanced(self, session: Any) -> None:
        """Set up Enhanced Mode (WSS MQTT push)."""
        creds, user_id = await self._fetch_enhanced_credentials(session)

        if creds is None:
            logger.error("Failed to fetch MQTT credentials for %s", self.device_sn)
            return

        # Portal returns userName/password, IoT API returns certificateAccount/certificatePassword
        cert_account = creds.get("certificateAccount") or creds.get("userName", "")
        cert_password = creds.get("certificatePassword") or creds.get("password", "")

        self._mqtt_client = EcoFlowMQTTClient(
            certificate_account=cert_account,
            certificate_password=cert_password,
            device_sn=self.device_sn,
            message_handler=self._on_mqtt_message,
            user_id=user_id,
            wss_mode=True,
        )

        await self.hass.async_add_executor_job(self._start_mqtt)
        self._schedule_keepalive()
        self._schedule_quotas_poll()
        self._schedule_ping()
        self._schedule_stale_check()

    async def _fetch_enhanced_credentials(
        self, session: Any
    ) -> tuple[dict[str, Any] | None, str]:
        """Fetch Enhanced Mode MQTT credentials + userId.

        Enhanced Mode requires Portal/App credentials (app-* prefix), NOT the
        IoT Developer API credentials (open-* prefix).  Only app-* credentials
        have access to energy_stream_report data on the WSS broker.

        Strategy:
        1. Login with email + password → JWT token + userId
        2. Use JWT token to fetch Portal certification → AES-decrypt → app-* creds
        3. Fallback: IoT Developer API creds (open-*) — works for heartbeats but
           NOT for energy_stream_report

        Returns (credentials_dict, user_id) or (None, "").
        """
        email = self._entry.data.get(CONF_EMAIL, "")
        password = self._entry.data.get(CONF_PASSWORD, "")
        user_id = self._entry.data.get(CONF_USER_ID, "")

        # --- Step 1: Login → JWT token + userId ---
        token = ""
        if email and password:
            from .ecoflow.enhanced_auth import enhanced_login

            login_result = await enhanced_login(session, email, password)
            if login_result is not None:
                token = login_result["token"]
                user_id = login_result["user_id"]
                logger.info("Enhanced Mode: login OK, userId obtained for %s", self.device_sn)
            else:
                logger.warning("Enhanced Mode: login failed for %s", self.device_sn)

        if not user_id:
            logger.error("Enhanced Mode: no userId available for %s", self.device_sn)
            return None, ""

        # --- Step 2: Portal certification → app-* credentials ---
        creds = None
        if token:
            from .ecoflow.enhanced_auth import get_enhanced_credentials

            creds = await get_enhanced_credentials(session, token)
            if creds is not None:
                logger.info(
                    "Enhanced Mode: Portal credentials obtained (account=%s...) for %s",
                    str(creds.get("certificateAccount", ""))[:12], self.device_sn,
                )

        # --- Step 3: Fallback to IoT Developer API (open-* credentials) ---
        if creds is None:
            logger.warning(
                "Enhanced Mode: Portal credentials unavailable for %s — "
                "falling back to IoT API (energy_stream may not work)",
                self.device_sn,
            )
            creds = await self._iot_api.get_mqtt_credentials()
            if creds is None:
                logger.error("Enhanced Mode: all credential sources failed for %s", self.device_sn)
                return None, ""

        return creds, user_id

    def _start_mqtt(self) -> None:
        """Start the MQTT client (runs in executor thread)."""
        if self._mqtt_client is None:
            return
        if self._mqtt_client.create_client():
            if self._mqtt_client.connect():
                self._mqtt_client.start_loop()
                mode_label = "WSS Enhanced" if self._enhanced_mode else "TCP Standard"
                logger.info("MQTT started for %s (%s)", self.device_sn, mode_label)
            else:
                logger.error("MQTT connect failed for %s", self.device_sn)
        else:
            logger.error("MQTT client creation failed for %s", self.device_sn)

    async def async_shutdown(self) -> None:
        """Stop the MQTT client and cancel timers."""
        self._shutdown = True
        for handle in (self._keepalive_unsub, self._quotas_unsub, self._ping_unsub, self._stale_check_unsub):
            if handle is not None:
                handle.cancel()
        self._keepalive_unsub = None
        self._quotas_unsub = None
        self._ping_unsub = None
        self._stale_check_unsub = None
        if self._mqtt_client is not None:
            await self.hass.async_add_executor_job(self._mqtt_client.disconnect)
            self._mqtt_client = None
        if self._energy_integrator is not None:
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
            logger.debug("EnergyStreamSwitch keepalive sent for %s", self.device_sn)
        else:
            logger.debug("EnergyStreamSwitch skipped for %s (not connected)", self.device_sn)
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
    _PROTO_TO_SENSOR: dict[str, str] = {
        "solar": "solar_w",
        "home_direct": "home_w",
        "batt_pb": "batt_w",
        "grid_raw_f2": "grid_w",
        "soc": "soc_pct",
    }

    def _on_mqtt_message(self, topic: str, payload: bytes) -> None:
        """Handle an incoming MQTT message (Paho thread).

        In Standard Mode, MQTT is only used for SET commands — data updates
        come from HTTP polling. In Enhanced Mode, MQTT is the primary source.
        """
        if not self._enhanced_mode:
            return  # Standard Mode: ignore MQTT data, HTTP is primary
        parsed = self._parse_message(topic, payload)
        if parsed:
            self.hass.loop.call_soon_threadsafe(self._apply_data, parsed)

    def _parse_message(self, topic: str, payload: bytes) -> dict[str, Any] | None:
        """Parse an MQTT message payload."""
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
                # PowerOcean sends flat {"params": {...}} or flat dicts
                if data.get("params"):
                    return data["params"]
                return data
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            return None

        # Protobuf topic: /app/device/property/{sn}
        if b"\x0a" in payload[:4]:
            try:
                result = decode_proto_runtime_frame(payload)
                if result.mapped.get("_is_energy_stream"):
                    raw = {
                        k: v
                        for k, v in result.mapped.items()
                        if not k.startswith("_")
                    }
                    return self._remap_proto_keys(raw)
                # Enhanced Mode: heartbeat with nested extraction
                if result.mapped.get("_is_ems_heartbeat"):
                    raw = {
                        k: v
                        for k, v in result.mapped.items()
                        if not k.startswith("_")
                    }
                    return self._flatten_heartbeat(raw)
                # Enhanced Mode: change reports and battery heartbeat
                if result.mapped.get("_is_ems_change") or result.mapped.get("_is_bp_heartbeat"):
                    raw = {
                        k: v
                        for k, v in result.mapped.items()
                        if not k.startswith("_")
                    }
                    return self._remap_bp_keys(raw)
            except Exception:
                logger.warning("Protobuf decode error for %s", self.device_sn, exc_info=True)
            return None

        return None

    def _remap_proto_keys(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Remap protobuf decoder keys to sensor keys (F-001 fix).

        Protobuf outputs: solar, home_direct, batt_pb, grid_raw_f2, soc
        Sensors expect:   solar_w, home_w, batt_w, grid_w, soc_pct

        Also computes derived power splits (grid import/export, batt charge/discharge)
        to match the HTTP parser output format.
        """
        result: dict[str, Any] = {}
        for proto_key, value in raw.items():
            sensor_key = self._PROTO_TO_SENSOR.get(proto_key, proto_key)
            result[sensor_key] = value

        # Derived power splits (same logic as HTTP parser)
        grid_w = result.get("grid_w")
        if grid_w is not None:
            result["grid_import_power_w"] = grid_w if grid_w > 0.0 else 0.0
            result["grid_export_power_w"] = abs(grid_w) if grid_w < 0.0 else 0.0

        batt_w = result.get("batt_w")
        if batt_w is not None:
            result["batt_charge_power_w"] = batt_w if batt_w > 0.0 else 0.0
            result["batt_discharge_power_w"] = abs(batt_w) if batt_w < 0.0 else 0.0

        return result

    # Heartbeat (cmd_id=1) key → sensor key mapping
    _HEARTBEAT_TO_SENSOR: dict[str, str] = {
        "pcs_ac_freq": "pcs_ac_freq_hz",
        "ems_bp_alive_num": "ems_bp_alive_num",
        "ems_pv_inv_pwr": "pv_inverter_power_w",
        "ems_work_mode": "ems_work_mode",
    }

    # Battery heartbeat (cmd_id=7) key → sensor key mapping
    _BP_TO_SENSOR: dict[str, str] = {
        "bp_soh": "bp_soh_pct",
        "bp_cycles": "bp_cycles",
        "bp_remain_watth": "bp_remain_watth",
        "bp_vol": "bp_voltage_v",
        "bp_amp": "bp_current_a",
        "bp_max_cell_temp": "bp_max_cell_temp_c",
        "bp_min_cell_temp": "bp_min_cell_temp_c",
        "bp_env_temp": "bp_env_temp_c",
        "bp_max_mos_temp": "bp_max_mos_temp_c",
        "bp_cell_max_vol": "bp_cell_max_vol_mv",
        "bp_cell_min_vol": "bp_cell_min_vol_mv",
        "bp_real_soc": "bp_real_soc_pct",
        "bp_real_soh": "bp_real_soh_pct",
        "bp_down_limit_soc": "bp_down_limit_soc_pct",
        "bp_up_limit_soc": "bp_up_limit_soc_pct",
    }

    # EMS change report (cmd_id=8) key → sensor key mapping
    _EMS_CHANGE_TO_SENSOR: dict[str, str] = {
        "bp_online_sum": "bp_online_sum",
        "ems_feed_mode": "ems_feed_mode",
        "ems_feed_ratio": "ems_feed_ratio_pct",
        "ems_feed_pwr": "ems_feed_power_limit_w",
        "sys_grid_sta": "grid_status",
        "bp_chg_dsg_sta": "batt_charge_discharge_state",
        "pcs_run_sta": "pcs_run_state",
        "ems_work_mode": "ems_work_mode",
        "pcs_pf_value": "pcs_power_factor",
        "bp_total_chg_energy": "batt_charge_energy_kwh",
        "bp_total_dsg_energy": "batt_discharge_energy_kwh",
    }

    def _flatten_heartbeat(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Extract nested messages from EMS heartbeat (cmd_id=1).

        Extracts: MPPT per-string, grid phase data, and scalar diagnostics.
        Mirrors the main EcoFlow service's mqtt_primary_pipeline extraction.
        """
        result: dict[str, Any] = {}

        # Scalar fields → sensor keys
        for proto_key, sensor_key in self._HEARTBEAT_TO_SENSOR.items():
            val = raw.get(proto_key)
            if val is not None:
                result[sensor_key] = float(val) if isinstance(val, (int, float)) else val

        # MPPT per-string (nested in mppt_heart_beat[0].mppt_pv[])
        mppt_hb = raw.get("mppt_heart_beat")
        if isinstance(mppt_hb, list) and mppt_hb:
            mppt_data = mppt_hb[0] if isinstance(mppt_hb[0], dict) else {}
            pv_arr = mppt_data.get("mppt_pv", [])
            for idx, pv in enumerate(pv_arr[:2]):
                if isinstance(pv, dict):
                    prefix = f"mppt_pv{idx + 1}"
                    for field, suffix in (("pwr", "power_w"), ("vol", "voltage_v"), ("amp", "current_a")):
                        val = pv.get(field)
                        if val is not None:
                            result[f"{prefix}_{suffix}"] = float(val)

        # Grid phase data (nested in pcs_load_info[] or pcs_a/b/c_phase)
        load_info = raw.get("pcs_load_info")
        if isinstance(load_info, list):
            phase_names = ("a", "b", "c")
            for idx, phase in enumerate(load_info[:3]):
                if isinstance(phase, dict):
                    label = phase_names[idx]
                    for field, suffix in (("vol", "voltage_v"), ("amp", "current_a"), ("pwr", "active_power_w")):
                        val = phase.get(field)
                        if val is not None:
                            result[f"grid_phase_{label}_{suffix}"] = float(val)

        # Fallback: pcs_a/b/c_phase (JTS1PhaseInfo nested messages)
        for phase_key, label in (("pcs_a_phase", "a"), ("pcs_b_phase", "b"), ("pcs_c_phase", "c")):
            phase = raw.get(phase_key)
            if isinstance(phase, dict):
                for field, suffix in (("vol", "voltage_v"), ("amp", "current_a"), ("act_pwr", "active_power_w")):
                    if f"grid_phase_{label}_{suffix}" not in result:
                        val = phase.get(field)
                        if val is not None:
                            result[f"grid_phase_{label}_{suffix}"] = float(val)

        return result

    def _remap_bp_keys(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Remap battery heartbeat (cmd_id=7) and EMS change (cmd_id=8) keys to sensor keys."""
        result: dict[str, Any] = {}

        # Try battery key mapping first, then EMS change mapping
        for proto_key, value in raw.items():
            sensor_key = (
                self._BP_TO_SENSOR.get(proto_key)
                or self._EMS_CHANGE_TO_SENSOR.get(proto_key)
            )
            if sensor_key:
                # Energy totals from EMS change report: Wh → kWh
                if sensor_key in ("batt_charge_energy_kwh", "batt_discharge_energy_kwh"):
                    if isinstance(value, (int, float)):
                        result[sensor_key] = float(value) / 1000.0
                else:
                    result[sensor_key] = float(value) if isinstance(value, (int, float)) else value

        return result

    def _apply_data(self, parsed: dict[str, Any]) -> None:
        """Apply parsed data and notify listeners (HA event loop)."""
        now = time.time()
        self._last_mqtt_ts = now
        self._device_data.update(parsed)

        # F-005 fix: integrate power → energy in MQTT path too
        if self._energy_integrator is not None:
            self._integrate_energy(parsed)
            # Throttle flush scheduling: at most once per 60s (matches integrator's SAVE_INTERVAL_S)
            if now - self._last_flush_ts > 60:
                self._last_flush_ts = now
                self.hass.async_create_task(self._async_flush_energy_state())

        self.async_set_updated_data(dict(self._device_data))

    async def _async_flush_energy_state(self) -> None:
        """Flush energy integrator state to disk (non-blocking)."""
        if self._energy_integrator is not None:
            await self.hass.async_add_executor_job(self._energy_integrator.flush)

    # ------------------------------------------------------------------
    # SET commands (switches, numbers)
    # ------------------------------------------------------------------

    async def async_send_set_command(self, command: dict[str, Any]) -> bool:
        """Send a SET command to the device via MQTT.

        The IoT API SET format:
        Topic: /open/{certAccount}/{SN}/set
        Payload: {"id": <ts>, "version": "1.0", ...command}
        """
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            logger.warning("Cannot send SET command — MQTT not connected (%s)", self.device_sn)
            return False

        msg_id = int(time.time() * 1000) % 1_000_000
        payload = json.dumps({"id": msg_id, "version": "1.0", **command})
        topic = f"/open/{self._mqtt_client.cert_account}/{self.device_sn}/set"

        ok = await self.hass.async_add_executor_job(
            self._mqtt_client.publish, topic, payload, 1,
        )
        if ok:
            logger.info("SET command sent: %s → %s", topic, payload[:120])
        else:
            logger.warning("SET command failed: %s", topic)
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
            return dict(self._device_data)

        if self.device_type == DEVICE_TYPE_POWEROCEAN:
            parsed = parse_powerocean_http_quota(raw)
        elif self.device_type == DEVICE_TYPE_DELTA:
            parsed = parse_delta_http_quota(raw)
        elif self.device_type == DEVICE_TYPE_SMARTPLUG:
            parsed = parse_smartplug_http_quota(raw)
        else:
            parsed = raw
        self._device_data.update(parsed)

        # Riemann sum: integrate power → energy for PowerOcean
        if self._energy_integrator is not None:
            self._integrate_energy(parsed)
            # Flush state to disk periodically (non-blocking)
            await self.hass.async_add_executor_job(self._energy_integrator.flush)

        return dict(self._device_data)

    # ------------------------------------------------------------------
    # Energy integration (Riemann sum)
    # ------------------------------------------------------------------

    # Power key → energy key mapping for Riemann sum integration
    _POWER_TO_ENERGY = {
        "solar_w": "solar_energy_kwh",
        "home_w": "home_energy_kwh",
        "grid_import_power_w": "grid_import_energy_kwh",
        "grid_export_power_w": "grid_export_energy_kwh",
    }

    def _integrate_energy(self, parsed: dict[str, Any]) -> None:
        """Integrate power readings into energy totals via Riemann sum.

        For battery charge/discharge energy, prefer API totals if available.
        For solar/home/grid energy, always use Riemann sum (API doesn't provide totals).
        """
        assert self._energy_integrator is not None

        for power_key, energy_key in self._POWER_TO_ENERGY.items():
            power_w = parsed.get(power_key)
            if power_w is not None:
                total = self._energy_integrator.integrate(energy_key, abs(power_w))
                if total is not None:
                    self._device_data[energy_key] = round(total, 3)

        # Battery: prefer API totals (more accurate), use Riemann as fallback
        for power_key, energy_key in [
            ("batt_charge_power_w", "batt_charge_energy_kwh"),
            ("batt_discharge_power_w", "batt_discharge_energy_kwh"),
        ]:
            if energy_key in parsed:
                # API provided a total — use it (already set by parser)
                self._energy_integrator.set_total(energy_key, parsed[energy_key])
            else:
                # No API total — integrate from power
                power_w = parsed.get(power_key)
                if power_w is not None:
                    total = self._energy_integrator.integrate(energy_key, abs(power_w))
                    if total is not None:
                        self._device_data[energy_key] = round(total, 3)

    # ------------------------------------------------------------------
    # Stale detection + fallback switching (4-tier reconnect tier 4)
    # ------------------------------------------------------------------

    def _schedule_stale_check(self) -> None:
        """Schedule a periodic check for stale MQTT data."""
        self._stale_check_unsub = self.hass.loop.call_later(
            STALE_THRESHOLD_S, self._check_stale,
        )

    def _check_stale(self) -> None:
        """Check if MQTT data is stale and switch to HTTP fallback if needed.

        4-tier reconnect strategy:
        1. Paho auto-reconnect (immediate, same ClientID) — handled by Paho
        2. Force-reconnect with new ClientID — handled by cloud_mqtt.try_reconnect()
        3. Counter-reset every 30 min (never give up) — handled by cloud_mqtt
        4. HTTP fallback — handled here when MQTT is stale
        """
        if self._shutdown:
            return

        age = time.time() - self._last_mqtt_ts if self._last_mqtt_ts > 0 else float("inf")

        if age > STALE_THRESHOLD_S and self.update_interval is None:
            logger.warning(
                "MQTT stale for %s (%.0fs) — switching to HTTP fallback (tier 4)",
                self.device_sn, age,
            )
            self.update_interval = timedelta(seconds=HTTP_FALLBACK_INTERVAL_S)
        elif age <= STALE_THRESHOLD_S and self.update_interval is not None:
            logger.info("MQTT recovered for %s — disabling HTTP fallback", self.device_sn)
            self.update_interval = None

        # Tier 2+3: try MQTT reconnect if disconnected
        if self._mqtt_client is not None and not self._mqtt_client.is_connected():
            self.hass.async_add_executor_job(self._mqtt_client.try_reconnect)

        # Re-schedule unless shutting down
        self._stale_check_unsub = self.hass.loop.call_later(
            STALE_THRESHOLD_S, self._check_stale,
        )
