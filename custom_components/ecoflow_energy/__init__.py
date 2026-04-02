"""EcoFlow Energy integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    AUTH_METHOD_APP,
    AUTH_METHOD_DEVELOPER,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    DEVICE_TYPE_UNKNOWN,
    DOMAIN,
    MODE_ENHANCED,
    PLATFORMS,
)
from .coordinator import EcoFlowDeviceCoordinator

_LOGGER = logging.getLogger(__name__)

type EcoFlowConfigEntry = ConfigEntry


CONFIG_VERSION = 3


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entries to current schema."""
    if config_entry.version > CONFIG_VERSION:
        return False

    if config_entry.version < 3:
        _LOGGER.debug(
            "Migrating config entry %s from version %d to 3",
            config_entry.entry_id, config_entry.version,
        )
        new_data = {**config_entry.data}
        new_data.setdefault(CONF_AUTH_METHOD, AUTH_METHOD_DEVELOPER)
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, version=3,
        )
        _LOGGER.info(
            "Migration of config entry %s to version 3 successful",
            config_entry.entry_id,
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: EcoFlowConfigEntry) -> bool:
    """Set up EcoFlow Energy from a config entry."""
    # Auto-upgrade: Enhanced Mode entries with email+password -> app-auth.
    # This lets existing Enhanced users benefit from the app-auth path
    # (no Developer Keys needed for MQTT) without manual reconfiguration.
    if (
        entry.data.get(CONF_MODE) == MODE_ENHANCED
        and entry.data.get(CONF_AUTH_METHOD) != AUTH_METHOD_APP
        and entry.data.get(CONF_EMAIL)
        and entry.data.get(CONF_PASSWORD)
    ):
        new_data = {**entry.data, CONF_AUTH_METHOD: AUTH_METHOD_APP}
        hass.config_entries.async_update_entry(entry, data=new_data)
        _LOGGER.info(
            "Auto-upgraded Enhanced Mode entry %s to app-auth",
            entry.entry_id,
        )

    devices = entry.data.get(CONF_DEVICES, [])
    coordinators: dict[str, EcoFlowDeviceCoordinator] = {}

    is_enhanced = entry.data.get(CONF_MODE) == MODE_ENHANCED
    enhanced_count = len(devices) if is_enhanced else 0
    standard_count = len(devices) - enhanced_count
    _LOGGER.debug(
        "EcoFlow Energy: %d device(s) configured (Enhanced: %d, Standard: %d)",
        len(devices), enhanced_count, standard_count,
    )

    for device_info in devices:
        sn = device_info["sn"]
        device_type = device_info.get("device_type", "")
        if not device_type or device_type == DEVICE_TYPE_UNKNOWN:
            _LOGGER.debug(
                "Skipping device %s... - no parser available",
                sn[:4],
            )
            continue
        coordinator = EcoFlowDeviceCoordinator(hass, entry, device_info)
        await coordinator.async_setup()
        # First refresh — raises ConfigEntryNotReady on failure so HA retries
        await coordinator.async_config_entry_first_refresh()
        coordinators[sn] = coordinator

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinators

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload integration when config entry data changes (e.g. mode switch via Options Flow)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when the config entry is updated."""
    _LOGGER.debug("Config entry updated — reloading EcoFlow Energy")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EcoFlowConfigEntry) -> bool:
    """Unload an EcoFlow Energy config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    # Shut down coordinators
    coordinators: dict[str, EcoFlowDeviceCoordinator] = hass.data[DOMAIN].pop(
        entry.entry_id, {}
    )
    for coordinator in coordinators.values():
        await coordinator.async_shutdown()

    return True
