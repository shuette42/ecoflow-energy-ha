"""Opt-in per-vehicle completed charging energy, independent of live MQTT."""

from __future__ import annotations

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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import CONF_DEVICES, DOMAIN
from .ecoflow.app_api import AppApiClient, HistoryDeferred, HistoryLoginError
from .ecoflow.charging_history import (
    identity,
    is_identity,
    merge_orders,
    valid_saved_ledger,
    vehicle_totals,
)
from .ecoflow.const import device_log_tag

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
            # HA prints this name in "Error fetching ... data" lines: tag, not serial.
            name=f"EcoFlow charging history ({device_log_tag(serial)})",
            config_entry=entry,
            update_interval=timedelta(minutes=5),
        )
        self.serial = serial
        self.api = api
        self.store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}_charging_history_{entry.entry_id}_{identity(serial)}"
        )
        self.orders: dict[str, dict[str, Any]] = {}
        self.data = {}
        self.history_available = True
        self._restore_failed = False

    async def async_restore(self) -> None:
        """Restore the ledger, including vehicles no longer returned by cloud."""
        try:
            saved = await self.store.async_load()
        except (OSError, ValueError, HomeAssistantError):
            self._restore_failed = True
            self.history_available = False
            _LOGGER.warning(
                "Could not restore charging ledger for %s; preserving store",
                device_log_tag(self.serial),
            )
            return
        self._restore_failed = False
        self._mark_available()
        if saved is None:
            return
        if not valid_saved_ledger(saved):
            _LOGGER.warning(
                "Invalid stored charging ledger; starting empty (%s)",
                device_log_tag(self.serial),
            )
            return
        self.orders = saved["orders"]
        self.data = saved["vehicles"]

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """An incomplete fetch never publishes or persists partial totals."""
        if self._restore_failed:
            await self.async_restore()
            if self._restore_failed:
                raise UpdateFailed(
                    "Charging ledger could not be loaded; preserving store"
                )
        try:
            # The per-fetch timeout lives in the client, around the network
            # read: the entry-wide lock wait must not spend this charger's budget.
            rows = await self.api.get_powerpulse_orders(self.serial)
            self.update_interval = timedelta(minutes=5)
            orders = merge_orders(self.orders, rows, self.serial)
            totals = {
                key: {**value, "energy_wh": 0, "sessions": 0}
                for key, value in self.data.items()
            }
            totals.update(vehicle_totals(orders))
            if orders != self.orders or totals != self.data:
                saved = {"orders": orders, "vehicles": totals}
                await self.store.async_save(saved)
                if await self.store.async_load() != saved:
                    raise OSError("Charging ledger write could not be verified")
            self.orders = orders
            self._mark_available()
            return totals
        except HistoryDeferred as err:
            self.update_interval = timedelta(seconds=min(3600, max(1, err.delay)))
            if err.authentication:
                self._mark_unavailable("Charging history sign-in is backed off")
            return self.data
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            if isinstance(err, HistoryLoginError):
                self.update_interval = timedelta(hours=1)
            self._mark_unavailable("Could not read completed charging history")
            return self.data
        except (OSError, HomeAssistantError) as err:
            raise UpdateFailed("Could not persist completed charging history") from err

    def _mark_available(self) -> None:
        """Say once that a failed read recovered, so the earlier warning has an end."""
        if not self.history_available:
            _LOGGER.info(
                "Completed charging history readable again (%s)",
                device_log_tag(self.serial),
            )
        self.history_available = True

    def _mark_unavailable(self, message: str) -> None:
        """Warn once per failure transition without exposing cloud response data."""
        if self.history_available:
            _LOGGER.warning("%s (%s)", message, device_log_tag(self.serial))
        self.history_available = False


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
    def available(self) -> bool:
        """A deferred or failed cloud read must not present stale totals as live."""
        return super().available and self.coordinator.history_available

    @property
    def native_value(self) -> float | None:
        """Return cumulative imported completed-session energy."""
        value = self.coordinator.data.get(self.vehicle)
        return value["energy_wh"] / 1000 if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose coverage without leaking order, user or vehicle IDs."""
        value = self.coordinator.data.get(self.vehicle)
        if value is None:
            return {}
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


def _known_hashes(saved: Any) -> set[str]:
    """Charger identities from the stored index; anything malformed is dropped."""
    if not isinstance(saved, list):
        return set()
    return {item for item in saved if is_identity(item)}


def _history_index(hass: HomeAssistant, entry: ConfigEntry) -> Store[list[str]]:
    return Store(hass, 1, f"{DOMAIN}_charging_history_index_{entry.entry_id}")


async def async_register_history_stores(
    hass: HomeAssistant, entry: ConfigEntry, serials: list[str]
) -> None:
    """Remember ledgers even if a charger is later deselected from the entry."""
    index = _history_index(hass, entry)
    try:
        saved = await index.async_load()
    except (OSError, ValueError, HomeAssistantError):
        _LOGGER.warning("Could not load charging history index; preserving store")
        return
    hashes = _known_hashes(saved)
    hashes.update(identity(serial) for serial in serials)
    expected = sorted(hashes)
    await index.async_save(expected)
    if await index.async_load() != expected:
        raise OSError("Charging history index write could not be verified")


async def async_remove_history_stores(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Deleting the entry deletes its retained charging history, not disabling it."""
    index = _history_index(hass, entry)
    await history_limits_store(hass, entry).async_remove()
    try:
        saved = await index.async_load()
    except (OSError, ValueError, HomeAssistantError):
        _LOGGER.warning(
            "Could not load charging history index; "
            "only selected ledgers can be removed"
        )
        saved = None
    hashes = _known_hashes(saved)
    hashes.update(
        identity(d["sn"])
        for d in entry.data.get(CONF_DEVICES, [])
        if isinstance(d.get("sn"), str)
    )
    for hashed in hashes:
        await Store(
            hass, 1, f"{DOMAIN}_charging_history_{entry.entry_id}_{hashed}"
        ).async_remove()
    await index.async_remove()


def history_limits_store(
    hass: HomeAssistant, entry: ConfigEntry
) -> Store[dict[str, Any]]:
    """One entry-wide auth deadline and per-charger read times, across restarts."""
    return Store(hass, 1, f"{DOMAIN}_charging_history_limits_{entry.entry_id}")
