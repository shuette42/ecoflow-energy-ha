"""HTTP quota polling for the EcoFlow device coordinator."""

from __future__ import annotations

import logging
import time
from typing import Any

from ..const import (
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_POWERSTREAM,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_STREAM,
)
from ..ecoflow.firmware import extract_firmware_versions
from ..ecoflow.parsers.delta_http import parse_delta_http_quota
from ..ecoflow.parsers.delta3_http import parse_delta3_http_quota
from ..ecoflow.parsers.powerocean import parse_powerocean_http_quota
from ..ecoflow.parsers.powerstream_http import parse_powerstream_quota
from ..ecoflow.parsers.smartplug import parse_smartplug_http_quota
from ..ecoflow.parsers.stream_http import parse_stream_quota

_LOGGER = logging.getLogger(__name__)


class HttpPollMixin:
    """Mixin providing the HTTP polling update path."""

    # ------------------------------------------------------------------
    # HTTP fallback (called when MQTT is stale)
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """HTTP polling - primary source in Standard Mode, fallback in Enhanced.

        PowerOcean: POST /iot-open/sign/device/quota (with quotas array)
        Delta:      GET  /iot-open/sign/device/quota/all?sn=...
        """
        from .core import DeviceSnapshot

        if self._http_client is None:
            return self._device_data

        # All device types use GET /quota/all - returns the most complete data
        raw = await self._http_client.get_quota_all()
        if not raw:
            error_code = self._http_client.last_error_code

            # Error 1006 = device not linked to API key - config issue, not auth (#2)
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

            # Two things that are not an expired key, and both used to be
            # read as one (#289).
            #
            # A transport failure says nothing about credentials. The client
            # reports `network` when every attempt died on a timeout or a
            # refused connection, which is exactly what a device that has
            # gone offline looks like - a Stream microinverter at dusk, for
            # instance, whose owner was asked to re-enter his secret key
            # several times per evening while data kept arriving.
            #
            # And the "MQTT is carrying the data, so HTTP noise is expected"
            # exemption was gated on Enhanced Mode, while Delta, Smart Plug
            # and Stream subscribe to push in Standard Mode as well. For
            # those the flag was false by construction, so the exemption
            # could never apply to the devices that needed it most.
            #
            # What still triggers a prompt: the API answering with an error
            # of its own, five times running, with nothing arriving over
            # MQTT either. That is what an invalidated key looks like.
            transport_failure = error_code == "network"
            mqtt_active = self._last_mqtt_ts > 0.0
            if (
                self._consecutive_http_failures == 5
                and not mqtt_active
                and not transport_failure
            ):
                _LOGGER.warning(
                    "HTTP quota failed %d consecutive times for %s - triggering re-authentication",
                    self._consecutive_http_failures, self.device_sn[:4],
                )
                self._entry.async_start_reauth(self.hass)
            return dict(self._device_data)

        self._consecutive_http_failures = 0
        self._device_available = True
        self._log_event("http_ok", f"keys={len(raw)}")

        # Firmware, before any device-specific parsing: the quota is the only
        # endpoint that carries a revision at all, and which subsystems report
        # one differs per device family rather than per parser.
        self._firmware = extract_firmware_versions(raw)

        if self.device_type == DEVICE_TYPE_POWEROCEAN:
            # Keep the raw quota snapshot for diagnostics. Accessories such as
            # the PowerGlow heating rod report through the PowerOcean quota
            # rather than as devices of their own, and their key names are not
            # documented anywhere. A dump from an owner is the only way to learn
            # which keys an accessory actually contributes.
            self._raw_quota = dict(raw)
            self._raw_quota_captured_at = time.monotonic()
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
        elif self.device_type == DEVICE_TYPE_STREAM:
            # Standard mode delivers flat camelCase JSON. Keep the raw
            # snapshot for diagnostics: the field map covers the keys seen on
            # Stream Ultra / Ultra X so far, and beta dumps are what extends
            # it (#139).
            self._raw_quota = dict(raw)
            self._raw_quota_captured_at = time.monotonic()
            parsed = parse_stream_quota(raw)
        elif self.device_type == DEVICE_TYPE_POWERSTREAM:
            # Keep the raw snapshot for diagnostics. The field map rests on a
            # single reporter capture with an idle battery and empty history
            # counters, so the keys it leaves unmapped are exactly the ones a
            # second dump from a busier unit would settle (#230).
            self._raw_quota = dict(raw)
            self._raw_quota_captured_at = time.monotonic()
            parsed = parse_powerstream_quota(raw)
        else:
            parsed = raw
        # Resolve the Stream unit/system SoC sources before merging, just as
        # the MQTT path does. This also removes the parser-private fallback.
        self._resolve_soc(parsed)
        self._enforce_monotonic(parsed)
        # Same pop as in _apply_data: prevent EMS raw battery state from
        # overwriting the power-derived value (#50).
        parsed.pop("batt_charge_discharge_state", None)
        self._note_value_change(parsed)
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
