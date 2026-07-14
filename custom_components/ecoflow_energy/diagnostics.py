"""Diagnostics support for EcoFlow Energy.

Exposes device status, MQTT connectivity, and data freshness.
NEVER exposes credentials (access_key, secret_key, email, password, certificates).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICES, CONF_MODE, DATA_SKIPPED_DEVICES, DEVICE_TYPE_DELTA3, DOMAIN
from .coordinator import EcoFlowDeviceCoordinator

REDACTED = "**REDACTED**"

# EcoFlow serial numbers are 16-char alphanumeric strings (e.g. D3M1TEST...).
# Any quota value containing a run matching this shape is redacted so a
# diagnostics dump never leaks a full device serial. The pattern is applied
# unanchored so serials embedded in longer strings are caught too.
_SERIAL_RE = re.compile(r"[A-Z0-9]{15,}")


def _redact_serials(value: Any) -> Any:
    """Redact values that look like EcoFlow serial numbers.

    Recurses into dict and list values so nested quota structures (e.g.
    ``powGetAcOutList``) cannot smuggle a serial past redaction. Over-redaction
    of long alphanumeric tokens is accepted by design: a diagnostics dump must
    never leak a device serial.
    """
    if isinstance(value, str):
        return _SERIAL_RE.sub(REDACTED, value)
    if isinstance(value, dict):
        return {key: _redact_serials(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_serials(item) for item in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinators: dict[str, EcoFlowDeviceCoordinator] = hass.data.get(DOMAIN, {}).get(
        entry.entry_id, {}
    )

    devices_diag: list[dict[str, Any]] = []
    for coordinator in coordinators.values():
        devices_diag.append(_device_diagnostics(coordinator))

    skipped_devices = hass.data.get(DATA_SKIPPED_DEVICES, {}).get(entry.entry_id, [])

    return {
        "config_entry": {
            "auth_method": entry.data.get("auth_method", "developer"),
            "mode": entry.data.get(CONF_MODE, "standard"),
            "device_count": len(entry.data.get(CONF_DEVICES, [])),
            "access_key": REDACTED,
            "secret_key": REDACTED,
            "email": REDACTED,
            "password": REDACTED,
        },
        "devices": devices_diag,
        "skipped_devices": [dict(item) for item in skipped_devices],
    }


def _device_diagnostics(coordinator: EcoFlowDeviceCoordinator) -> dict[str, Any]:
    """Build diagnostics dict for one device — no credentials."""
    now = time.monotonic()

    mqtt_client = coordinator.mqtt_client
    mqtt_connected = False
    mqtt_uptime_s: float | None = None
    mqtt_reconnect_attempts = 0
    if mqtt_client is not None:
        mqtt_connected = mqtt_client.is_connected()
        if mqtt_connected and mqtt_client.last_connect_time > 0:
            mqtt_uptime_s = round(now - mqtt_client.last_connect_time, 1)
        mqtt_reconnect_attempts = mqtt_client.reconnect_attempts

    last_mqtt_age_s: float | None = None
    if coordinator.last_mqtt_ts > 0:
        last_mqtt_age_s = round(now - coordinator.last_mqtt_ts, 1)

    data_keys = sorted(coordinator.device_data.keys()) if coordinator.device_data else []

    snapshot = coordinator.snapshot
    snapshot_age_s: float | None = None
    if snapshot.captured_at > 0:
        snapshot_age_s = round(now - snapshot.captured_at, 1)

    diag: dict[str, Any] = {
        "device_sn": coordinator.device_sn,
        "device_name": coordinator.device_name,
        "product_name": coordinator.product_name,
        "enhanced_mode": coordinator.enhanced_mode,
        "availability_stage": coordinator.availability_stage,
        "mqtt_status": {
            "status": coordinator.mqtt_status,
            "connected": mqtt_connected,
            "data_receiving": coordinator.data_receiving,
            "uptime_s": mqtt_uptime_s,
            "reconnect_attempts": mqtt_reconnect_attempts,
            "wss_mode": mqtt_client.wss_mode if mqtt_client else False,
        },
        "data_freshness": {
            "last_mqtt_age_s": last_mqtt_age_s,
            "update_interval": str(coordinator.update_interval) if coordinator.update_interval else None,
            "http_fallback_active": bool(
                coordinator.enhanced_mode and coordinator.update_interval is not None
            ),
        },
        "snapshot": {
            "source": snapshot.source or "none",
            "age_s": snapshot_age_s,
            "key_count": snapshot.key_count,
            "captured": snapshot.captured_at > 0,
        },
        "data_keys": data_keys,
        "data_key_count": len(data_keys),
        "event_log": _format_event_log(coordinator.event_log),
    }

    # Delta 3: the quota field map is community-researched but not yet
    # hardware-verified for every key. Expose the raw HTTP quota key/value
    # snapshot so a diagnostics dump can confirm existing mappings and reveal
    # keys still to be added. Serial-looking values are redacted.
    if coordinator.device_type == DEVICE_TYPE_DELTA3:
        raw_quota = coordinator.raw_quota
        raw_age_s: float | None = None
        if coordinator.raw_quota_captured_at > 0:
            raw_age_s = round(now - coordinator.raw_quota_captured_at, 1)
        diag["raw_quota"] = {
            "captured": bool(raw_quota),
            "age_s": raw_age_s,
            "key_count": len(raw_quota),
            "values": {
                key: _redact_serials(value)
                for key, value in sorted(raw_quota.items())
            },
        }

    return diag


def _format_event_log(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Format event log with ISO timestamps for human readability."""
    formatted = []
    for event in events:
        entry = dict(event)
        ts = entry.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            entry["ts_iso"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        formatted.append(entry)
    return formatted
