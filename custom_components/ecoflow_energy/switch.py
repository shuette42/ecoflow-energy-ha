"""Switch platform for EcoFlow Energy.

Implements optimistic lock: after a SET command the local state is
updated immediately and MQTT updates for that key are ignored for 5 s.
This prevents switch flicker while the device confirms the change.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DELTA2MAX_SWITCHES, DEVICE_TYPE_DELTA, DEVICE_TYPE_SMARTPLUG, DOMAIN, EcoFlowSwitchDef, SMARTPLUG_SWITCHES
from .coordinator import EcoFlowDeviceCoordinator

_LOGGER = logging.getLogger(__name__)

OPTIMISTIC_LOCK_S = 5.0

# Smart Plug SET-command templates (uses cmdCode format)
SMARTPLUG_COMMANDS: dict[str, dict[str, dict[str, Any]]] = {
    "plug_switch": {
        "on": {
            "cmdCode": "WN511_SOCKET_SET_PLUG_SWITCH_MESSAGE",
            "params": {"plugSwitch": 1},
        },
        "off": {
            "cmdCode": "WN511_SOCKET_SET_PLUG_SWITCH_MESSAGE",
            "params": {"plugSwitch": 0},
        },
    },
}

# IoT API SET-command templates (Delta 2 Max)
# moduleType: 1=PD, 2=BMS, 3=INV, 5=MPPT
#
# Legacy switches use on/off dict with full params.
# New switches use declarative format with param_key, invert, extra_params.
SWITCH_COMMANDS: dict[str, dict[str, dict[str, Any]]] = {
    "ac_switch": {
        "on": {
            "moduleType": 3,
            "operateType": "acOutCfg",
            "params": {"enabled": 1, "out_voltage": 4294967295, "out_freq": 1, "xboost": 1},
        },
        "off": {
            "moduleType": 3,
            "operateType": "acOutCfg",
            "params": {"enabled": 0, "out_voltage": 4294967295, "out_freq": 1, "xboost": 0},
        },
    },
    "dc_switch": {
        "on": {"moduleType": 1, "operateType": "dcOutCfg", "params": {"enabled": 1}},
        "off": {"moduleType": 1, "operateType": "dcOutCfg", "params": {"enabled": 0}},
    },
    "car_12v_switch": {
        "on": {"moduleType": 5, "operateType": "mpptCar", "params": {"enabled": 1}},
        "off": {"moduleType": 5, "operateType": "mpptCar", "params": {"enabled": 0}},
    },
}

# Declarative switch command templates (Delta 2 Max)
# param_key: the parameter name sent in the command (default: "enabled")
# invert: when True, ON sends 0 and OFF sends 1 (e.g. beeper vs quiet mode)
# extra_params: additional fixed parameters merged into the command params
SWITCH_DECLARATIVE: dict[str, dict[str, Any]] = {
    "beeper_switch": {
        "moduleType": 1,
        "operateType": "quietCfg",
        "param_key": "enabled",
        "invert": True,
    },
    "xboost_switch": {
        "moduleType": 3,
        "operateType": "acOutCfg",
        "param_key": "xboost",
    },
    "ac_auto_on_switch": {
        "moduleType": 1,
        "operateType": "newAcAutoOnCfg",
        "param_key": "enabled",
        "extra_params": {"minAcSoc": 5},
    },
    "backup_reserve_switch": {
        "moduleType": 1,
        "operateType": "watthConfig",
        "param_key": "isConfig",
        "extra_params": {"bpPowerSoc": 50, "minChgSoc": 0, "minDsgSoc": 0},
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow switches from a config entry."""
    coordinators: dict[str, EcoFlowDeviceCoordinator] = hass.data[DOMAIN][entry.entry_id]
    entities: list[EcoFlowSwitch] = []

    for coordinator in coordinators.values():
        defs = _get_switch_defs(coordinator.device_type)
        for defn in defs:
            entities.append(EcoFlowSwitch(coordinator, defn))

    async_add_entities(entities)


class EcoFlowSwitch(CoordinatorEntity[EcoFlowDeviceCoordinator], SwitchEntity):
    """An EcoFlow switch entity with optimistic lock."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EcoFlowDeviceCoordinator,
        definition: EcoFlowSwitchDef,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._definition = definition
        self._attr_unique_id = f"{coordinator.device_sn}_{definition.key}"
        self._attr_translation_key = definition.key
        self._attr_icon = definition.icon

        # Optimistic lock state
        self._optimistic_value: bool | None = None
        self._optimistic_lock_until: float = 0.0

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.device_available and super().available

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info from coordinator."""
        return self.coordinator.device_info

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on.

        During the optimistic lock window, returns the locally set value
        instead of the coordinator data to prevent flicker.
        """
        if time.monotonic() < self._optimistic_lock_until:
            return self._optimistic_value

        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self._definition.state_key)
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return value != 0
        return bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._send_command(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._send_command(False)

    async def _send_command(self, turn_on: bool) -> None:
        """Send a SET command and apply optimistic lock."""
        command = self._build_command(turn_on)
        if command is None:
            _LOGGER.warning("No command template for %s", self._definition.key)
            return

        # Optimistic update: immediately reflect the new state
        self._optimistic_value = turn_on
        self._optimistic_lock_until = time.monotonic() + OPTIMISTIC_LOCK_S
        self.async_write_ha_state()

        # Send the command via the coordinator
        await self.coordinator.async_send_set_command(command)

    def _build_command(self, turn_on: bool) -> dict[str, Any] | None:
        """Build a SET command from legacy or declarative templates."""
        # Check declarative templates first (new switches)
        decl = SWITCH_DECLARATIVE.get(self._definition.key)
        if decl is not None:
            invert = decl.get("invert", False)
            if invert:
                value = 0 if turn_on else 1
            else:
                value = 1 if turn_on else 0

            params = {decl["param_key"]: value}
            if "extra_params" in decl:
                params.update(decl["extra_params"])

            return {
                "moduleType": decl["moduleType"],
                "operateType": decl["operateType"],
                "params": params,
            }

        # Legacy on/off templates
        cmd_key = "on" if turn_on else "off"
        commands = _get_switch_commands(self.coordinator.device_type)
        return commands.get(self._definition.key, {}).get(cmd_key)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


def _get_switch_defs(device_type: str) -> list[EcoFlowSwitchDef]:
    """Return switch definitions based on device type."""
    if device_type == DEVICE_TYPE_DELTA:
        return DELTA2MAX_SWITCHES
    if device_type == DEVICE_TYPE_SMARTPLUG:
        return SMARTPLUG_SWITCHES
    return []


def _get_switch_commands(device_type: str) -> dict[str, dict[str, dict[str, Any]]]:
    """Return command templates based on device type."""
    if device_type == DEVICE_TYPE_SMARTPLUG:
        return SMARTPLUG_COMMANDS
    return SWITCH_COMMANDS
