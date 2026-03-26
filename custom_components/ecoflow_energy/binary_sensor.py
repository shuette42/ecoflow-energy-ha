"""Binary sensor platform for EcoFlow Energy."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DELTA2MAX_BINARY_SENSORS,
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_SMARTPLUG,
    DOMAIN,
    EcoFlowBinarySensorDef,
    POWEROCEAN_BINARY_SENSORS,
    SMARTPLUG_BINARY_SENSORS,
)
from .coordinator import EcoFlowDeviceCoordinator

_ENTITY_CATEGORY_MAP = {
    "diagnostic": EntityCategory.DIAGNOSTIC,
    "config": EntityCategory.CONFIG,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow binary sensors from a config entry."""
    coordinators: dict[str, EcoFlowDeviceCoordinator] = hass.data[DOMAIN][entry.entry_id]
    entities: list[EcoFlowBinarySensor] = []

    for coordinator in coordinators.values():
        defs = _get_binary_sensor_defs(coordinator.device_type)
        for defn in defs:
            entities.append(EcoFlowBinarySensor(coordinator, defn))

    async_add_entities(entities)


class EcoFlowBinarySensor(
    CoordinatorEntity[EcoFlowDeviceCoordinator], BinarySensorEntity
):
    """An EcoFlow binary sensor entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EcoFlowDeviceCoordinator,
        definition: EcoFlowBinarySensorDef,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._definition = definition
        self._attr_unique_id = f"{coordinator.device_sn}_{definition.key}"
        self._attr_name = definition.name
        self._attr_icon = definition.icon

        if definition.device_class:
            self._attr_device_class = BinarySensorDeviceClass(definition.device_class)
        if definition.entity_category:
            self._attr_entity_category = _ENTITY_CATEGORY_MAP.get(definition.entity_category)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.device_sn)},
            manufacturer="EcoFlow",
            model=self.coordinator.product_name,
            name=f"EcoFlow {self.coordinator.device_name}",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self._definition.key)
        if value is None:
            return None
        # Numeric 1/1.0 → True, 0/0.0 → False; string "ON"/"OFF"
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.upper() == "ON"
        return bool(value)


def _get_binary_sensor_defs(
    device_type: str,
) -> list[EcoFlowBinarySensorDef]:
    """Return binary sensor definitions based on device type."""
    if device_type == DEVICE_TYPE_DELTA:
        return DELTA2MAX_BINARY_SENSORS
    if device_type == DEVICE_TYPE_POWEROCEAN:
        return POWEROCEAN_BINARY_SENSORS
    if device_type == DEVICE_TYPE_SMARTPLUG:
        return SMARTPLUG_BINARY_SENSORS
    return []
