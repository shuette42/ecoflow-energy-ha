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
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .ecoflow.delta3_commands import (
    build_switch_command as build_delta3_switch_command,
)
from .const import (
    DELTA2MAX_SWITCHES,
    DELTA3_SWITCHES,
    DELTA_PROFILE_R331,
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_STREAM,
    DOMAIN,
    EcoFlowSwitchDef,
    SMARTPLUG_SWITCH_COMMANDS,
    SMARTPLUG_SWITCHES,
    STREAM_SWITCHES,
    SWITCH_COMMANDS_R331,
    SWITCH_COMMANDS_R351,
    SWITCH_DECLARATIVE_R331,
    SWITCH_DECLARATIVE_R351,
)
from .coordinator import EcoFlowDeviceCoordinator
from .ecoflow.parsers.smartplug import build_plug_switch_payload
from .entity import EcoFlowWriteGateMixin

_LOGGER = logging.getLogger(__name__)

OPTIMISTIC_LOCK_S = 5.0


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


class EcoFlowSwitch(
    EcoFlowWriteGateMixin,
    CoordinatorEntity[EcoFlowDeviceCoordinator],
    SwitchEntity,
    RestoreEntity,
):
    """An EcoFlow switch entity with optimistic lock and state restore."""

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

        self._restored_is_on: bool | None = None
        self._last_written_value: bool | None = None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.device_available and super().available

    async def async_added_to_hass(self) -> None:
        """Restore the last known on/off state when the entity is added.

        Enhanced Mode devices can take up to two minutes before the first
        full status frame arrives. Without a restored state HA renders the
        switch as unknown for that whole window. The restored state is only
        a placeholder: as soon as live data delivers the key, the live
        value always wins (see ``is_on``).
        """
        await super().async_added_to_hass()
        data = self.coordinator.data
        if data is not None and self._definition.state_key in data:
            return  # live value already present, nothing to restore
        last = await self.async_get_last_state()
        if last is None or last.state not in ("on", "off"):
            return  # discard unavailable/unknown restored states
        self._restored_is_on = last.state == "on"
        # Seed the write gate so an identical first live frame does not
        # trigger a redundant recorder write (mirrors the sensor restore).
        self._last_written_value = self._restored_is_on

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info from coordinator."""
        return self.coordinator.device_info

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on.

        During the optimistic lock window, returns the locally set value
        instead of the coordinator data to prevent flicker. When the key
        is missing from the coordinator data (e.g. right after a restart,
        before the first full status frame), falls back to the restored
        state. A live value always beats the restored one.
        """
        if time.monotonic() < self._optimistic_lock_until:
            return self._optimistic_value

        data = self.coordinator.data
        if data is not None and self._definition.state_key in data:
            value = data[self._definition.state_key]
            if value is None:
                return None
            if isinstance(value, (int, float)):
                return value != 0
            return bool(value)
        return self._restored_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._send_command(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._send_command(False)

    async def _send_command(self, turn_on: bool) -> None:
        """Send a SET command; apply the optimistic lock only on success.

        A failed send must not flip the UI to a state the device never
        received - the switch would show the wrong state for the whole
        lock window and then snap back.
        """
        # SmartPlug app-auth: use protobuf SET (JSON cmdCode only works on /open/ topic)
        if (
            self.coordinator.device_type == DEVICE_TYPE_SMARTPLUG
            and self.coordinator.enhanced_mode
            and self._definition.key == "plug_switch"
        ):
            payload = build_plug_switch_payload(turn_on, device_sn=self.coordinator.device_sn)
            ok = await self.coordinator.async_send_proto_set_command(payload, "plug_switch")
            if ok:
                self._apply_optimistic(turn_on)
            return

        command = self._build_command(turn_on)
        if command is None:
            _LOGGER.warning("No command template for %s", self._definition.key)
            return

        if self.coordinator.device_type == DEVICE_TYPE_DELTA3:
            ok = await self.coordinator.async_send_delta3_set(command)
        else:
            ok = await self.coordinator.async_send_set_command(command)
        if ok:
            self._apply_optimistic(turn_on)

    def _apply_optimistic(self, turn_on: bool) -> None:
        """Apply optimistic lock: immediately reflect the new state."""
        self._optimistic_value = turn_on
        self._optimistic_lock_until = time.monotonic() + OPTIMISTIC_LOCK_S
        self._write_state_always(turn_on)

    def _build_command(self, turn_on: bool) -> dict[str, Any] | None:
        """Build a SET command from legacy or declarative templates."""
        if self.coordinator.device_type == DEVICE_TYPE_DELTA3:
            return build_delta3_switch_command(self._definition.key, turn_on)

        if self.coordinator.device_type == DEVICE_TYPE_DELTA:
            commands = _get_delta_switch_commands(self.coordinator.delta_profile)
            declarative_templates = _get_delta_switch_declarative(self.coordinator.delta_profile)
        else:
            commands = _get_switch_commands(self.coordinator.device_type)
            declarative_templates = {}

        # Check declarative templates first (new switches)
        decl = declarative_templates.get(self._definition.key)
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
        return commands.get(self._definition.key, {}).get(cmd_key)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._write_state_if_changed(self.is_on)


def _get_switch_defs(device_type: str) -> list[EcoFlowSwitchDef]:
    """Return switch definitions based on device type."""
    if device_type == DEVICE_TYPE_DELTA:
        return DELTA2MAX_SWITCHES
    if device_type == DEVICE_TYPE_SMARTPLUG:
        return SMARTPLUG_SWITCHES
    if device_type == DEVICE_TYPE_STREAM:
        return STREAM_SWITCHES
    if device_type == DEVICE_TYPE_DELTA3:
        return DELTA3_SWITCHES
    return []


def _get_switch_commands(device_type: str) -> dict[str, dict[str, dict[str, Any]]]:
    """Return command templates based on device type."""
    if device_type == DEVICE_TYPE_SMARTPLUG:
        return SMARTPLUG_SWITCH_COMMANDS
    return SWITCH_COMMANDS_R351


def _get_delta_switch_commands(delta_profile: str) -> dict[str, dict[str, dict[str, Any]]]:
    """Return Delta switch command templates for the selected profile."""
    if delta_profile == DELTA_PROFILE_R331:
        return SWITCH_COMMANDS_R331
    return SWITCH_COMMANDS_R351


def _get_delta_switch_declarative(delta_profile: str) -> dict[str, dict[str, Any]]:
    """Return Delta declarative switch templates for the selected profile."""
    if delta_profile == DELTA_PROFILE_R331:
        return SWITCH_DECLARATIVE_R331
    return SWITCH_DECLARATIVE_R351
