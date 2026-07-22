"""Availability staging and stale detection for the EcoFlow device coordinator."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from ..const import (
    DEVICE_TYPE_SMARTPLUG,
    HARD_UNAVAILABLE_S,
    HTTP_FALLBACK_INTERVAL_S,
    MQTT_HEALTH_CHECK_INTERVAL_S,
    SMARTPLUG_HARD_UNAVAILABLE_S,
    SMARTPLUG_SOFT_UNAVAILABLE_S,
    SMARTPLUG_STALE_THRESHOLD_S,
    SOFT_UNAVAILABLE_S,
    STALE_THRESHOLD_S,
)

_LOGGER = logging.getLogger(__name__)


class AvailabilityMixin:
    """Mixin providing graduated availability and stale checks."""

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
        from .core import DeviceSnapshot

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
                    # No data flows while the stream is down, so entities must
                    # be told about the availability flip explicitly.
                    self.async_update_listeners()
            else:
                if not self._device_available:
                    _LOGGER.info(
                        "MQTT recovered for %s [%s] - device available again",
                        self.device_name,
                        self.device_sn,
                    )
                    self._log_event("stale_recovered", "device_available")
                    self._device_available = True
                    # Push the recovery to entities even before the next data
                    # frame arrives, so they return from unavailable promptly.
                    self.async_update_listeners()
                self._last_stale_reconnect_ts = 0.0

        # Tier 2+3: try MQTT reconnect if disconnected
        if self._mqtt_client is not None and not mqtt_connected:
            self.hass.async_add_executor_job(self._mqtt_client.try_reconnect)

        # Re-schedule unless shutting down
        self._stale_check_unsub = self.hass.loop.call_later(
            min(stale_threshold_s, MQTT_HEALTH_CHECK_INTERVAL_S), self._check_stale,
        )

