"""Sensor platform for EcoFlow Energy."""

from __future__ import annotations

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_SMARTPLUG,
    DOMAIN,
    DELTA2MAX_SENSORS,
    EcoFlowSensorDef,
    POWEROCEAN_SENSORS,
    SMARTPLUG_SENSORS,
)
from .coordinator import EcoFlowDeviceCoordinator

# Map string → HA enum
_STATE_CLASS_MAP = {
    "measurement": SensorStateClass.MEASUREMENT,
    "total_increasing": SensorStateClass.TOTAL_INCREASING,
    "total": SensorStateClass.TOTAL,
}

_ENTITY_CATEGORY_MAP = {
    "diagnostic": EntityCategory.DIAGNOSTIC,
    "config": EntityCategory.CONFIG,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow sensors from a config entry."""
    coordinators: dict[str, EcoFlowDeviceCoordinator] = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []

    for coordinator in coordinators.values():
        sensor_defs = _get_sensor_defs(coordinator.device_type)
        for sensor_def in sensor_defs:
            if sensor_def.enhanced_only and not coordinator.enhanced_mode:
                continue
            entities.append(EcoFlowSensor(coordinator, sensor_def))

        # Diagnostic sensors (coordinator properties, not data-driven)
        entities.append(EcoFlowDiagnosticSensor(coordinator, "mqtt_status"))
        entities.append(EcoFlowDiagnosticSensor(coordinator, "connection_mode"))

    async_add_entities(entities)


class EcoFlowSensor(CoordinatorEntity[EcoFlowDeviceCoordinator], RestoreSensor):
    """An EcoFlow sensor entity with state restore across reloads."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EcoFlowDeviceCoordinator,
        definition: EcoFlowSensorDef,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._definition = definition
        self._attr_unique_id = f"{coordinator.device_sn}_{definition.key}"
        self._attr_translation_key = definition.key
        self._attr_native_unit_of_measurement = definition.unit
        self._attr_icon = definition.icon
        self._restored_value: float | int | str | None = None

        if definition.device_class:
            self._attr_device_class = SensorDeviceClass(definition.device_class)
        if definition.state_class:
            self._attr_state_class = _STATE_CLASS_MAP.get(definition.state_class)
        if definition.entity_category:
            self._attr_entity_category = _ENTITY_CATEGORY_MAP.get(definition.entity_category)
        if definition.suggested_display_precision is not None:
            self._attr_suggested_display_precision = definition.suggested_display_precision
        if definition.disabled_by_default:
            self._attr_entity_registry_enabled_default = False

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.device_available and super().available

    async def async_added_to_hass(self) -> None:
        """Restore last known value when entity is added."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) and last.native_value is not None:
            self._restored_value = last.native_value

    @property
    def device_info(self) -> dict:
        """Return device info from coordinator."""
        return self.coordinator.device_info

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | int | str | None:
        """Return the sensor value, falling back to restored state."""
        if self.coordinator.data is not None:
            val = self.coordinator.data.get(self._definition.key)
            if val is not None:
                return val
        return self._restored_value


class EcoFlowDiagnosticSensor(CoordinatorEntity[EcoFlowDeviceCoordinator], SensorEntity):
    """Diagnostic sensor that reads coordinator properties directly."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: EcoFlowDeviceCoordinator,
        key: str,
    ) -> None:
        """Initialize the diagnostic sensor."""
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.device_sn}_{key}"
        self._attr_translation_key = key

    @property
    def device_info(self) -> dict:
        """Return device info from coordinator."""
        return self.coordinator.device_info

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        """Return the diagnostic sensor value from coordinator property."""
        if self._key == "mqtt_status":
            return self.coordinator.mqtt_status
        if self._key == "connection_mode":
            return self.coordinator.connection_mode
        return None


def _get_sensor_defs(device_type: str) -> list[EcoFlowSensorDef]:
    """Return sensor definitions based on device type."""
    if device_type == DEVICE_TYPE_DELTA:
        return DELTA2MAX_SENSORS
    if device_type == DEVICE_TYPE_POWEROCEAN:
        return POWEROCEAN_SENSORS
    if device_type == DEVICE_TYPE_SMARTPLUG:
        return SMARTPLUG_SENSORS
    return []
