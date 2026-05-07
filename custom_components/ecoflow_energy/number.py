"""Number platform for EcoFlow Energy."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DELTA2MAX_NUMBERS,
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_SMARTPLUG,
    DOMAIN,
    EcoFlowNumberDef,
    NUMBER_COMMANDS,
    POWEROCEAN_NUMBERS,
    SMARTPLUG_NUMBER_COMMANDS,
    SMARTPLUG_NUMBERS,
)
from .coordinator import EcoFlowDeviceCoordinator
from .ecoflow.parsers.smartplug import (
    build_plug_brightness_payload,
    build_plug_max_watts_payload,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow numbers from a config entry."""
    coordinators: dict[str, EcoFlowDeviceCoordinator] = hass.data[DOMAIN][entry.entry_id]
    entities: list[EcoFlowNumber] = []

    for coordinator in coordinators.values():
        defs = _get_number_defs(coordinator.device_type)
        for defn in defs:
            if defn.enhanced_only and not coordinator.enhanced_mode:
                continue
            entities.append(EcoFlowNumber(coordinator, defn))

    async_add_entities(entities)


class EcoFlowNumber(CoordinatorEntity[EcoFlowDeviceCoordinator], NumberEntity):
    """An EcoFlow number entity."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: EcoFlowDeviceCoordinator,
        definition: EcoFlowNumberDef,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._definition = definition
        self._attr_unique_id = f"{coordinator.device_sn}_{definition.key}"
        self._attr_translation_key = definition.key
        self._attr_icon = definition.icon
        self._attr_native_unit_of_measurement = definition.unit
        self._attr_native_min_value = definition.min_value
        self._attr_native_max_value = definition.max_value
        self._attr_native_step = definition.step

        self._last_written_value: float | None = None
        self._optimistic_lock_until: float = 0.0

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.device_available and super().available

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info from coordinator."""
        return self.coordinator.device_info

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if time.monotonic() < self._optimistic_lock_until:
            return  # ignore incoming data during optimistic lock
        new_value = self.native_value
        if new_value != self._last_written_value:
            self._last_written_value = new_value
            self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Return the current value."""
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self._definition.state_key)
        if value is None:
            return None
        try:
            fval = float(value)
            # Show clean integers when step is >= 1 (no fractional steps)
            if self._attr_native_step and self._attr_native_step >= 1:
                return round(fval)
            return fval
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Set a new value via the EcoFlow IoT API."""
        # PowerOcean uses protobuf SET via Enhanced Mode (WSS)
        if self.coordinator.device_type == DEVICE_TYPE_POWEROCEAN:
            await self._async_set_powerocean_value(value)
            return

        # Smart Plug number commands
        sp_template = SMARTPLUG_NUMBER_COMMANDS.get(self._definition.key)
        if sp_template is not None:
            # App-auth: use protobuf SET (JSON cmdCode only works on /open/ topic)
            if self.coordinator.enhanced_mode:
                ok = await self._async_set_smartplug_proto(self._definition.key, value)
                if ok:
                    self._apply_optimistic_number(value)
                return

            # Standard Mode: JSON cmdCode format
            scale = sp_template.get("scale", 1)
            command = {
                "sn": self.coordinator.device_sn,
                "cmdCode": sp_template["cmdCode"],
                "params": {sp_template["param_key"]: int(value * scale)},
            }
            await self.coordinator.async_send_set_command(command)
            self._apply_optimistic_number(value)
            return

        # Delta uses moduleType/operateType format
        cmd_template = NUMBER_COMMANDS.get(self._definition.key)
        if cmd_template is None:
            _LOGGER.warning("No command template for number %s", self._definition.key)
            return

        params = {cmd_template["param_key"]: int(value)}
        if "extra_params" in cmd_template:
            params.update(cmd_template["extra_params"])

        command = {
            "moduleType": cmd_template["moduleType"],
            "operateType": cmd_template["operateType"],
            "params": params,
        }

        await self.coordinator.async_send_set_command(command)
        self._apply_optimistic_number(value)


    def _apply_optimistic_number(self, value: float) -> None:
        """Apply optimistic lock: immediately reflect the new value."""
        state_key = self._definition.state_key
        self.coordinator.set_device_value(state_key, value)
        if self.coordinator.data is not None:
            self.coordinator.data[state_key] = value
        self._last_written_value = float(value)
        self._optimistic_lock_until = time.monotonic() + 5.0
        self.async_write_ha_state()

    async def _async_set_smartplug_proto(self, key: str, value: float) -> bool:
        """Set a SmartPlug number value via WSS Protobuf (app-auth mode)."""
        int_value = int(value)
        sn = self.coordinator.device_sn
        if key == "led_brightness":
            # User sets 0-100%, device expects 0-1023
            raw_brightness = int(round(value * 1023.0 / 100.0))
            payload = build_plug_brightness_payload(raw_brightness, device_sn=sn)
            label = "brightness"
        elif key == "max_watts":
            payload = build_plug_max_watts_payload(int_value, device_sn=sn)
            label = "max_watts"
        else:
            _LOGGER.warning("No SmartPlug proto SET handler for %s", key)
            return False
        return await self.coordinator.async_send_proto_set_command(payload, label)

    async def _async_set_powerocean_value(self, value: float) -> None:
        """Set a PowerOcean number value via WSS Protobuf.

        SysBatChgDsgSet (cmd_id=112) is sent as a 4-field app-replay
        payload: field 1=100 constant, field 2=backup_reserve_pct,
        field 3+4=solar_surplus_pct (EMS state + app-UI state). The
        unchanged value is read from coordinator data so both slider
        positions stay consistent with the device and the EcoFlow app.

        SET delivery goes through the coordinator's debouncer so that
        rapid-fire slider drags coalesce into a single frame.
        """
        key = self._definition.key
        int_value = int(value)

        if self.coordinator.data is None:
            _LOGGER.warning("No data available yet for %s", self.coordinator.device_sn)
            return

        if key == "backup_reserve":
            current_solar = int(self.coordinator.data.get("ems_backup_ratio_pct", 100))
            backup = int_value
            solar = max(current_solar, backup)  # enforce backup <= solar
            self.coordinator._last_user_surplus_set_ts = time.monotonic()
            ok = await self.coordinator.async_set_powerocean_soc_debounced(backup, solar)
            if ok:
                self._apply_optimistic_number(value)
            return

        if key == "solar_surplus_threshold":
            current_backup = int(self.coordinator.data.get("ems_discharge_lower_limit_pct", 0))
            solar = int_value
            backup = min(current_backup, solar)  # enforce backup <= solar
            self.coordinator._last_user_surplus_set_ts = time.monotonic()
            ok = await self.coordinator.async_set_powerocean_soc_debounced(backup, solar)
            if ok:
                self._apply_optimistic_number(value)
            return

        _LOGGER.warning("No PowerOcean SET handler for %s", key)


def _get_number_defs(device_type: str) -> list[EcoFlowNumberDef]:
    """Return number definitions based on device type."""
    if device_type == DEVICE_TYPE_DELTA:
        return DELTA2MAX_NUMBERS
    if device_type == DEVICE_TYPE_POWEROCEAN:
        return POWEROCEAN_NUMBERS
    if device_type == DEVICE_TYPE_SMARTPLUG:
        return SMARTPLUG_NUMBERS
    return []
