"""Number platform for EcoFlow Energy."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DELTA2MAX_NUMBERS,
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_SMARTPLUG,
    DOMAIN,
    EcoFlowNumberDef,
    SMARTPLUG_NUMBERS,
)
from .coordinator import EcoFlowDeviceCoordinator

logger = logging.getLogger(__name__)

# IoT API SET-command templates for number entities (Delta 2 Max)
NUMBER_COMMANDS: dict[str, dict[str, Any]] = {
    "ac_charge_speed": {
        "moduleType": 3,
        "operateType": "acChgCfg",
        "param_key": "fastChgWatts",
        "extra_params": {"slowChgWatts": 400, "chgPauseFlag": 0},
    },
    "max_charge_soc": {
        "moduleType": 2,
        "operateType": "upsConfig",
        "param_key": "maxChgSoc",
    },
    "min_discharge_soc": {
        "moduleType": 2,
        "operateType": "dsgCfg",
        "param_key": "minDsgSoc",
    },
    "standby_timeout": {
        "moduleType": 1,
        "operateType": "standbyTime",
        "param_key": "standbyMin",
    },
    "car_standby_timeout": {
        "moduleType": 5,
        "operateType": "standbyTime",
        "param_key": "standbyMins",
    },
    "screen_brightness": {
        "moduleType": 1,
        "operateType": "lcdCfg",
        "param_key": "brighLevel",
        # delayOff=0: device sentinel for "do not change timeout"
        "extra_params": {"delayOff": 0},
    },
    "screen_timeout": {
        "moduleType": 1,
        "operateType": "lcdCfg",
        "param_key": "delayOff",
        # brighLevel=255: device sentinel for "do not change brightness"
        "extra_params": {"brighLevel": 255},
    },
    "backup_reserve_soc": {
        "moduleType": 1,
        "operateType": "watthConfig",
        "param_key": "bpPowerSoc",
        "extra_params": {"isConfig": 1, "minChgSoc": 0, "minDsgSoc": 0},
    },
}

# Smart Plug SET-command templates (cmdCode format, different from Delta's moduleType/operateType)
SMARTPLUG_NUMBER_COMMANDS: dict[str, dict[str, Any]] = {
    "led_brightness": {
        "cmdCode": "WN511_SOCKET_SET_BRIGHTNESS_PACK",
        "param_key": "brightness",
        "scale": 1,
    },
    "max_watts": {
        "cmdCode": "WN511_SOCKET_SET_MAX_WATTS",
        "param_key": "maxWatts",
        # scale=1: maxWatts is in watts (not deci-watts). Confirmed by HTTP parser
        # which reads maxWatts without /10 scaling (unlike watts which is /10).
        "scale": 1,
    },
}


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

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.device_available and super().available

    @property
    def device_info(self) -> dict:
        """Return device info from coordinator."""
        return self.coordinator.device_info

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
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
            return float(value)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Set a new value via the EcoFlow IoT API."""
        # Smart Plug uses cmdCode format (different from Delta's moduleType/operateType)
        sp_template = SMARTPLUG_NUMBER_COMMANDS.get(self._definition.key)
        if sp_template is not None:
            scale = sp_template.get("scale", 1)
            command = {
                "sn": self.coordinator.device_sn,
                "cmdCode": sp_template["cmdCode"],
                "params": {sp_template["param_key"]: int(value * scale)},
            }
            await self.coordinator.async_send_set_command(command)

            if self.coordinator.data is not None:
                self.coordinator.data[self._definition.state_key] = value
                self.async_write_ha_state()
            return

        # Delta uses moduleType/operateType format
        cmd_template = NUMBER_COMMANDS.get(self._definition.key)
        if cmd_template is None:
            logger.warning("No command template for number %s", self._definition.key)
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

        # Optimistic update — show new value immediately in UI.
        # Mutates coordinator.data (dispatched snapshot), not _device_data.
        # Next poll overwrites this with the actual device value.
        if self.coordinator.data is not None:
            self.coordinator.data[self._definition.state_key] = value
            self.async_write_ha_state()


def _get_number_defs(device_type: str) -> list[EcoFlowNumberDef]:
    """Return number definitions based on device type."""
    if device_type == DEVICE_TYPE_DELTA:
        return DELTA2MAX_NUMBERS
    if device_type == DEVICE_TYPE_SMARTPLUG:
        return SMARTPLUG_NUMBERS
    return []
