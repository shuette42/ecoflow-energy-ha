"""Keep-alive timers for the EcoFlow device coordinator (Enhanced Mode)."""

from __future__ import annotations

import logging
import time

from ..const import (
    DEVICE_TYPE_SMARTPLUG,
    ENERGY_STREAM_KEEPALIVE_S,
    PING_KEEPALIVE_S,
    QUOTAS_KEEPALIVE_S,
    SMARTPLUG_GET_ALL_KEEPALIVE_S,
)

_LOGGER = logging.getLogger(__name__)


class KeepaliveMixin:
    """Mixin providing EnergyStreamSwitch, latestQuotas, and ping keep-alives."""

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

