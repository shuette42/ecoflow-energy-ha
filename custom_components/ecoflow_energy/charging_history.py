"""Opt-in per-vehicle completed charging energy, independent of live MQTT."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import CONF_DEVICES, DOMAIN
from .ecoflow.app_api import AppApiClient
from .ecoflow.charging_history import (
    identity,
    merge_orders,
    valid_saved_ledger,
    vehicle_totals,
)

_LOGGER = logging.getLogger(__name__)


class ChargingHistoryCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Persist completed orders before publishing derived totals."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, serial: str, api: AppApiClient
    ) -> None:
        """Use one cloud history poll per charger, not one per vehicle."""
        super().__init__(
            hass,
            _LOGGER,
            name="EcoFlow charging history",
            config_entry=entry,
            update_interval=timedelta(minutes=5),
        )
        self.serial = serial
        self.api = api
        self.store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}_charging_history_{identity(serial)}"
        )
        self.orders: dict[str, dict[str, Any]] = {}
        self.data = {}

    async def async_restore(self) -> None:
        """Restore the ledger, including vehicles no longer returned by cloud."""
        try:
            saved = await self.store.async_load()
        except (OSError, ValueError):
            _LOGGER.warning("Could not restore charging ledger; starting empty")
            return
        if saved is None:
            return
        if not valid_saved_ledger(saved):
            _LOGGER.warning("Invalid stored charging ledger; starting empty")
            return
        self.orders = saved["orders"]
        self.data = saved["vehicles"]

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """An incomplete fetch never publishes or persists partial totals."""
        try:
            async with asyncio.timeout(60):
                rows = await self.api.get_powerpulse_orders(self.serial)
            orders = merge_orders(self.orders, rows, self.serial)
            totals = {
                key: {**value, "energy_wh": 0, "sessions": 0}
                for key, value in self.data.items()
            }
            totals.update(vehicle_totals(orders))
            if orders != self.orders or totals != self.data:
                await self.store.async_save({"orders": orders, "vehicles": totals})
            self.orders = orders
            return totals
        except (aiohttp.ClientError, TimeoutError, ValueError, OSError) as err:
            # URLs and bodies can contain serials, account IDs and profile names.
            raise UpdateFailed("Could not update completed charging history") from err


class VehicleEnergySensor(CoordinatorEntity[ChargingHistoryCoordinator], SensorEntity):
    """Energy attributed by completed records, not the currently selected car."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    # Corrections can reduce a vehicle's total. Do not treat these as resets.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: ChargingHistoryCoordinator,
        vehicle: str,
        device_info: DeviceInfo,
    ) -> None:
        """Names may change; profile identity and statistics IDs must not."""
        super().__init__(coordinator)
        self.vehicle = vehicle
        self._attr_unique_id = f"{coordinator.serial}_vehicle_energy_{vehicle}"
        self._attr_device_info = device_info
        self._attr_translation_key = (
            "other_vehicle_energy"
            if coordinator.data[vehicle]["other"]
            else (
                "vehicle_energy"
                if coordinator.data[vehicle]["name"]
                else "unnamed_vehicle_energy"
            )
        )
        self._attr_translation_placeholders = {
            "vehicle": coordinator.data[vehicle]["name"]
        }

    @property
    def native_value(self) -> float | None:
        """Return cumulative imported completed-session energy."""
        value = self.coordinator.data.get(self.vehicle)
        return value["energy_wh"] / 1000 if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose coverage without leaking order, user or vehicle IDs."""
        value = self.coordinator.data[self.vehicle]
        return {"completed_sessions": value["sessions"], "profile_name": value["name"]}


async def async_setup_charging_history(
    hass: HomeAssistant,
    entry: ConfigEntry,
    serial: str,
    device_info: DeviceInfo,
    async_add_entities: AddEntitiesCallback,
    api: AppApiClient,
) -> ChargingHistoryCoordinator:
    """Discover each profile after its first completed record, without reload."""
    coordinator = ChargingHistoryCoordinator(hass, entry, serial, api)
    # Explicit setup allows restored entities to remain present on API failure.
    await coordinator.async_restore()
    known: set[str] = set()

    @callback
    def discover() -> None:
        new = set(coordinator.data) - known
        if new:
            async_add_entities(
                [
                    VehicleEnergySensor(coordinator, key, device_info)
                    for key in sorted(new)
                ]
            )
            known.update(new)

    entry.async_on_unload(coordinator.async_add_listener(discover))
    entry.async_on_unload(coordinator.async_shutdown)
    discover()
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), "EcoFlow initial charging history"
    )
    return coordinator


def _history_index(hass: HomeAssistant, entry: ConfigEntry) -> Store[list[str]]:
    return Store(hass, 1, f"{DOMAIN}_charging_history_index_{entry.entry_id}")


async def async_register_history_stores(
    hass: HomeAssistant, entry: ConfigEntry, serials: list[str]
) -> None:
    """Remember ledgers even if a charger is later deselected from the entry."""
    index = _history_index(hass, entry)
    saved = await index.async_load()
    hashes = (
        {
            s
            for s in (saved or [])
            if isinstance(s, str)
            and len(s) == 64
            and all(c in "0123456789abcdef" for c in s)
        }
        if isinstance(saved, list)
        else set()
    )
    hashes.update(identity(serial) for serial in serials)
    await index.async_save(sorted(hashes))


async def async_remove_history_stores(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Deleting the entry deletes its retained charging history, not disabling it."""
    index = _history_index(hass, entry)
    saved = await index.async_load()
    hashes = (
        {
            s
            for s in (saved or [])
            if isinstance(s, str)
            and len(s) == 64
            and all(c in "0123456789abcdef" for c in s)
        }
        if isinstance(saved, list)
        else set()
    )
    hashes.update(
        identity(d["sn"])
        for d in entry.data.get(CONF_DEVICES, [])
        if isinstance(d.get("sn"), str)
    )
    for hashed in hashes:
        await Store(hass, 1, f"{DOMAIN}_charging_history_{hashed}").async_remove()
    await index.async_remove()
