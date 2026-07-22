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
from .credentials import CredentialsMixin
from .http_poll import HttpPollMixin
from .keepalive import KeepaliveMixin
from .mqtt_ingest import MqttIngestMixin
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
    MqttIngestMixin,
    SetCommandsMixin,
    HttpPollMixin,
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
