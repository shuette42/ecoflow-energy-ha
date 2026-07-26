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
    DATA_DEVICE_PROBES,
    DATA_SKIPPED_DEVICES,
    DEVICE_TYPE_UNKNOWN,
    DOMAIN,
    MODE_ENHANCED,
    PLATFORMS,
    get_device_type,
)
from .coordinator import EcoFlowDeviceCoordinator
from .device_probe import UnroutedDeviceProbe, async_start_probes

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

    skipped_devices: list[dict[str, str]] = []
    for device_info in devices:
        sn = device_info["sn"]
        # Both device producers may pass product_name through as null, so
        # coalesce to "" here (get(..., "") would still return None).
        product_name = device_info.get("product_name") or ""
        # Re-classify from product_name + SN on every setup: classification
        # rules improve over releases (e.g. Delta 3 split from Delta 2 Max),
        # and the type stored at config-flow time may be outdated.
        device_type = get_device_type(product_name, sn)
        if device_type == DEVICE_TYPE_UNKNOWN:
            device_type = device_info.get("device_type", "")
        if not device_type or device_type == DEVICE_TYPE_UNKNOWN:
            # One WARNING per unsupported device per setup: the user sees
            # the device in the EcoFlow account but gets no entities, so
            # this degradation must be visible and actionable.
            _LOGGER.warning(
                "Skipping unsupported EcoFlow device %s... (%s) - no parser "
                "available for this model yet. Please open an issue at "
                "https://github.com/shuette42/ecoflow-energy-ha/issues so "
                "support can be added",
                sn[:4],
                product_name or "unknown product",
            )
            skipped_devices.append({
                "sn_prefix": sn[:4],
                # Full SN is carried in-memory only so diagnostics can fetch
                # this device's raw quota to help add parser support. It is
                # never persisted and never included in diagnostics output
                # (only the prefix is exposed there).
                "sn": sn,
                "product_name": product_name,
                "reason": "no parser available for this device type",
            })
            continue
        device_info = {**device_info, "device_type": device_type}
        coordinator = EcoFlowDeviceCoordinator(hass, entry, device_info)
        await coordinator.async_setup()
        # First refresh — raises ConfigEntryNotReady on failure so HA retries.
        # Partial-setup cleanup is handled by HA core: the coordinator
        # registers async_shutdown as an entry on_unload callback, and HA
        # processes those callbacks when setup fails, so coordinators
        # created before a failing device do not leak MQTT clients
        # (guarded by a regression test).
        await coordinator.async_config_entry_first_refresh()
        coordinators[sn] = coordinator

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinators
    hass.data.setdefault(DATA_SKIPPED_DEVICES, {})[entry.entry_id] = skipped_devices

    # A skipped device produces no entities and no data path, so the bytes
    # needed to add support for it can never be collected from a normal
    # installation. In app-auth mode a listen-only probe captures them for
    # diagnostics. Standard mode has the raw quota capture instead, and for
    # the models where the Developer API refuses (error 1006) this is the
    # only route that exists.
    if skipped_devices and entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_APP:
        probes = await async_start_probes(
            hass,
            skipped_devices,
            entry.data.get(CONF_EMAIL, ""),
            entry.data.get(CONF_PASSWORD, ""),
        )
        hass.data.setdefault(DATA_DEVICE_PROBES, {})[entry.entry_id] = probes

        # Probes hold live WSS connections and their own paho threads. If
        # setup fails after this point, async_unload_entry never runs, so
        # every HA retry would leave another set of connections behind.
        # Popping here means the regular unload path wins when it ran, and
        # this callback only has work to do when it did not.
        async def _stop_orphaned_probes() -> None:
            for probe in hass.data.get(DATA_DEVICE_PROBES, {}).pop(entry.entry_id, []):
                await probe.async_stop()

        entry.async_on_unload(_stop_orphaned_probes)

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
    hass.data.get(DATA_SKIPPED_DEVICES, {}).pop(entry.entry_id, None)
    probes: list[UnroutedDeviceProbe] = hass.data.get(DATA_DEVICE_PROBES, {}).pop(
        entry.entry_id, []
    )
    for probe in probes:
        await probe.async_stop()
    for coordinator in coordinators.values():
        await coordinator.async_shutdown()

    return True
