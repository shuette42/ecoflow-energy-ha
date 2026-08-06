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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_STREAM,
    DEVICE_TYPE_STREAM_AC5000,
    DOMAIN,
    DELTA2MAX_SENSORS,
    DELTA3_SENSORS,
    EcoFlowSensorDef,
    filter_defs_for_serial,
    POWEROCEAN_SENSORS,
    SMARTPLUG_SENSORS,
    STREAM_SENSORS,
    STREAMAC5000_SENSORS,
)
from .coordinator import EcoFlowDeviceCoordinator
from .entity import EcoFlowWriteGateMixin

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
    registry = er.async_get(hass)
    entities: list[SensorEntity] = []

    for coordinator in coordinators.values():
        sensor_defs = filter_defs_for_serial(
            _get_sensor_defs(coordinator.device_type), coordinator.device_sn
        )
        pending: list[EcoFlowSensorDef] = []
        for sensor_def in sensor_defs:
            if sensor_def.enhanced_only and not coordinator.enhanced_mode:
                continue
            if sensor_def.accessory and not _reported(coordinator, sensor_def.key):
                pending.append(sensor_def)
                continue
            entities.append(EcoFlowSensor(coordinator, sensor_def))

        # Diagnostic sensors (coordinator properties, not data-driven)
        entities.append(EcoFlowDiagnosticSensor(coordinator, "mqtt_status"))
        entities.append(EcoFlowDiagnosticSensor(coordinator, "connection_mode"))

        if pending:
            _watch_for_accessory(
                entry, registry, coordinator, pending, async_add_entities
            )

    async_add_entities(entities)


@callback
def _reported(coordinator: EcoFlowDeviceCoordinator, key: str) -> bool:
    """Return whether the device has this reading in its current state.

    Both stores are checked because they are filled at different points:
    the persistent device data by the parsers, the coordinator payload by
    the update that follows.
    """
    return key in coordinator.device_data or key in (coordinator.data or {})


@callback
def _drop_stale_accessory_entity(
    registry: er.EntityRegistry,
    coordinator: EcoFlowDeviceCoordinator,
    definition: EcoFlowSensorDef,
) -> None:
    """Remove a leftover registry entry of an accessory the device lacks.

    Earlier releases created these entities on every device, so skipping
    creation alone would leave the entry behind and it would still show up
    in the device's entity list. Removal is limited to entries the
    integration itself disabled and nobody ever touched: such an entity
    never wrote a state, so nothing recorded is lost. An owner who enabled
    the reading keeps entity and history, and so does one who enabled it and
    later switched it off again, because that history exists either way.
    """
    unique_id = f"{coordinator.device_sn}_{definition.key}"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
    if entity_id is None:
        return
    entry = registry.async_get(entity_id)
    if entry is None or entry.disabled_by is not er.RegistryEntryDisabler.INTEGRATION:
        return
    registry.async_remove(entity_id)


@callback
def _watch_for_accessory(
    config_entry: ConfigEntry,
    registry: er.EntityRegistry,
    coordinator: EcoFlowDeviceCoordinator,
    pending: list[EcoFlowSensorDef],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add accessory sensors as soon as the device first reports them.

    An accessory can be attached while Home Assistant is running, and the
    first report may also arrive after setup. Both cases add the entities
    without a reload. The first update that carries device data without the
    accessory is also the point where a leftover entry from an earlier
    release can be cleaned up: before that update, "not reported" and "not
    connected yet" look the same.
    """
    cleaned = False

    @callback
    def _check_for_accessory() -> None:
        nonlocal cleaned
        ready = [
            definition
            for definition in pending
            if _reported(coordinator, definition.key)
        ]
        for definition in ready:
            pending.remove(definition)
        if ready:
            async_add_entities(
                [EcoFlowSensor(coordinator, definition) for definition in ready]
            )
        if not cleaned and coordinator.device_data:
            cleaned = True
            for definition in pending:
                _drop_stale_accessory_entity(registry, coordinator, definition)

    config_entry.async_on_unload(coordinator.async_add_listener(_check_for_accessory))


class EcoFlowSensor(
    EcoFlowWriteGateMixin, CoordinatorEntity[EcoFlowDeviceCoordinator], RestoreSensor
):
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
        self._last_written_value: float | int | str | None = None

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
        if definition.options:
            self._attr_options = definition.options

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.device_available and super().available

    async def async_added_to_hass(self) -> None:
        """Restore last known value when entity is added."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) and last.native_value is not None:
            # Enum sensors: discard restored values not in options list.
            # After migrating from numeric to enum, old values like "0"
            # or "WORKMODE_SELFUSE" are invalid and would block entity setup.
            if self._definition.options and str(last.native_value) not in self._definition.options:
                return
            self._restored_value = last.native_value
            self._last_written_value = last.native_value
            # Seed the energy integrator so a lost or corrupt state file
            # does not reset totals to zero. set_total is monotonic-guarded,
            # so a stale restored value can never lower a live total.
            if self._definition.state_class == "total_increasing" and isinstance(
                last.native_value, (int, float)
            ):
                self.coordinator.seed_energy_total(
                    self._definition.key, float(last.native_value)
                )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info from coordinator."""
        return self.coordinator.device_info

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._write_state_if_changed(self.native_value)

    @property
    def native_value(self) -> float | int | str | None:
        """Return the sensor value, falling back to restored state.

        A key PRESENT with value None is an explicit clear from the parser
        (e.g. the inactive Delta 3 remain-time direction) and shows as
        unknown. Only a MISSING key falls back to the restored value.
        """
        data = self.coordinator.data
        if data is not None and self._definition.key in data:
            val = data[self._definition.key]
            if val is None:
                return None
            # Enum sensors: a live value outside the options list would make
            # HA raise ValueError on every state write. Fall back to the
            # restored value, mirroring the restore-path guard in
            # async_added_to_hass.
            if self._definition.options and str(val) not in self._definition.options:
                return self._restored_value
            return self._round_value(val)
        return self._restored_value

    def _round_value(self, val: float | int | str) -> float | int | str:
        """Round numeric values based on suggested_display_precision."""
        precision = self._definition.suggested_display_precision
        if precision is None or not isinstance(val, (int, float)):
            return val
        return round(val, precision) if precision > 0 else int(round(val, 0))


class EcoFlowDiagnosticSensor(
    EcoFlowWriteGateMixin, CoordinatorEntity[EcoFlowDeviceCoordinator], SensorEntity
):
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
        self._last_written_value: str | None = None

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info from coordinator."""
        return self.coordinator.device_info

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._write_state_if_changed(self.native_value)

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
    if device_type == DEVICE_TYPE_DELTA3:
        return DELTA3_SENSORS
    if device_type == DEVICE_TYPE_POWEROCEAN:
        return POWEROCEAN_SENSORS
    if device_type == DEVICE_TYPE_SMARTPLUG:
        return SMARTPLUG_SENSORS
    if device_type == DEVICE_TYPE_STREAM:
        return STREAM_SENSORS
    if device_type == DEVICE_TYPE_STREAM_AC5000:
        return STREAMAC5000_SENSORS
    return []
