"""EcoFlow Energy integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICES, DOMAIN, PLATFORMS
from .coordinator import EcoFlowDeviceCoordinator

logger = logging.getLogger(__name__)

type EcoFlowConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: EcoFlowConfigEntry) -> bool:
    """Set up EcoFlow Energy from a config entry."""
    devices = entry.data.get(CONF_DEVICES, [])
    coordinators: dict[str, EcoFlowDeviceCoordinator] = {}

    for device_info in devices:
        sn = device_info["sn"]
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
    logger.info("Config entry updated — reloading EcoFlow Energy")
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
