"""Diagnostics support for EcoFlow Energy.

Exposes device status, MQTT connectivity, and data freshness.
NEVER exposes credentials (access_key, secret_key, email, password, certificates).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICES, CONF_MODE, DOMAIN
from .coordinator import EcoFlowDeviceCoordinator

REDACTED = "**REDACTED**"


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

    return {
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
        "data_keys": data_keys,
        "data_key_count": len(data_keys),
        "event_log": _format_event_log(coordinator.event_log),
    }


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
