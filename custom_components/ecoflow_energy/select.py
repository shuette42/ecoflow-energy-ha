"""Select platform for EcoFlow Energy.

Three settings so far: the PowerOcean work mode (self-use, AI schedule), the
STREAM AC 5000 work mode, and the Delta 3 LCD screen timeout. All use the same
optimistic-lock pattern as switch.py and number.py - after a SET the local
state is updated immediately and device updates for the same key are ignored
for five seconds.

The two work modes share the entity key and nothing else: their modes and
their wire values are unrelated, so each has its own branch.

They also differ in what the device reports back. A work mode arrives as a
label the parser has already resolved; the screen timeout arrives as a number
of seconds, because the parser keeps the device's own vocabulary so that a
diagnostics download shows what was actually said. A definition carrying a
`value_map` is of the second kind, and everything that reads or writes state
for it works in wire values rather than labels.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DELTA3_SELECTS,
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_STREAM_AC5000,
    DOMAIN,
    EcoFlowSelectDef,
    POWEROCEAN_SELECTS,
    STREAMAC5000_SELECTS,
    filter_defs_for_serial,
)
from .coordinator import EcoFlowDeviceCoordinator
from .ecoflow.delta3_commands import build_select_command as build_delta3_select_command
from .ecoflow.stream_ac5000_commands import (
    build_work_mode_payload as build_stream_ac5000_work_mode_payload,
)
from .entity import raise_set_failed, raise_set_unsupported

_LOGGER = logging.getLogger(__name__)

OPTIMISTIC_LOCK_S = 5.0

# Maps the user-facing select option (state_key value) to the wire-level
# work-mode integer that goes into SysWorkModeSet (cmd_id=98) field 1.
# Verified 2026-05-06 against live device probe.
WORK_MODE_TO_INT: dict[str, int] = {
    "self_use": 0,
    "ai_schedule": 12,
    # Modes that need TouParam/BackupParam are intentionally out of scope:
    # "time_of_use": 1, "backup": 2 - device returns SetAck result=1
    # without the nested sub-params.
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow select entities from a config entry."""
    coordinators: dict[str, EcoFlowDeviceCoordinator] = hass.data[DOMAIN][entry.entry_id]
    entities: list[EcoFlowSelect] = []

    for coordinator in coordinators.values():
        defs = filter_defs_for_serial(
            _get_select_defs(coordinator.device_type), coordinator.device_sn
        )
        for defn in defs:
            if defn.enhanced_only and not coordinator.enhanced_mode:
                continue
            entities.append(EcoFlowSelect(coordinator, defn))

    async_add_entities(entities)


class EcoFlowSelect(CoordinatorEntity[EcoFlowDeviceCoordinator], SelectEntity):
    """An EcoFlow select entity with optimistic lock."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EcoFlowDeviceCoordinator,
        definition: EcoFlowSelectDef,
    ) -> None:
        """Initialize the select."""
        super().__init__(coordinator)
        self._definition = definition
        self._attr_unique_id = f"{coordinator.device_sn}_{definition.key}"
        self._attr_translation_key = definition.key
        self._attr_icon = definition.icon
        self._attr_options = list(definition.options)
        self._optimistic_value: str | None = None
        self._optimistic_lock_until: float = 0.0

    @property
    def available(self) -> bool:
        """Return True if the coordinator is available."""
        return self.coordinator.device_available and super().available

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data, honoring the optimistic lock window."""
        if time.monotonic() < self._optimistic_lock_until:
            # Still within lock - ignore device-reported value
            return
        self._optimistic_value = None
        super()._handle_coordinator_update()

    @property
    def current_option(self) -> str | None:
        """Return the currently selected option."""
        if (
            self._optimistic_value is not None
            and time.monotonic() < self._optimistic_lock_until
        ):
            return self._optimistic_value
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self._definition.state_key)
        if value is None:
            return None
        # Some settings are reported as a number rather than as a label. The
        # parser keeps the raw value, so translate it here. A value the device
        # reports outside the known set leaves the entity unknown rather than
        # showing a neighbouring option that is not what is actually set.
        if self._definition.value_map is not None:
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            return self._definition.value_map.get(value)
        # Coordinator stores the human-readable enum label (e.g. "self_use").
        # If it's not in our exposed options, return None so the UI shows
        # an empty selection rather than an invalid one.
        if value in self._definition.options:
            return value
        return None

    async def async_select_option(self, option: str) -> None:
        """Send a SET command to change the selected option."""
        if option not in self._definition.options:
            _LOGGER.warning(
                "Select option %s not in allowed options %s for %s",
                option, self._definition.options, self._definition.key,
            )
            return

        if self._definition.key == "work_mode":
            if self.coordinator.device_type == DEVICE_TYPE_STREAM_AC5000:
                # A different device family with its own modes, so it does not
                # share the PowerOcean wire values.
                payload = build_stream_ac5000_work_mode_payload(
                    option, self.coordinator.device_sn
                )
                ok = await self.coordinator.async_send_proto_set_command(
                    payload, label="stream_ac5000_work_mode"
                )
                if not ok:
                    raise_set_failed(self.entity_id)
                self._apply_optimistic_select(option)
                return

            wire_value = WORK_MODE_TO_INT.get(option)
            if wire_value is None:
                raise_set_unsupported(self.entity_id)
            if self.coordinator.device_type != DEVICE_TYPE_POWEROCEAN:
                raise_set_unsupported(self.entity_id)
            ok = await self.coordinator.async_set_powerocean_work_mode(wire_value)
            if not ok:
                raise_set_failed(self.entity_id)
            self._apply_optimistic_select(option)
            return

        if self.coordinator.device_type == DEVICE_TYPE_DELTA3:
            wire_value = self._wire_value(option)
            if wire_value is None:
                raise_set_unsupported(self.entity_id)
            command = build_delta3_select_command(self._definition.key, wire_value)
            if command is None:
                raise_set_unsupported(self.entity_id)
            ok = await self.coordinator.async_send_delta3_set(command)
            if not ok:
                raise_set_failed(self.entity_id)
            self._apply_optimistic_select(option)
            return

        raise_set_unsupported(self.entity_id)

    def _wire_value(self, option: str) -> int | None:
        """Return the device value behind an option label."""
        value_map = self._definition.value_map
        if value_map is None:
            return None
        for wire, label in value_map.items():
            if label == option:
                return wire
        return None

    def _apply_optimistic_select(self, option: str) -> None:
        """Apply the optimistic lock so the UI reflects the change immediately.

        The coordinator keeps the value in the shape the device reports it, not
        the label, so that the optimistic write and the push that follows it are
        the same kind of thing. Otherwise the label would sit in the data until
        the next push and `current_option` would try to map a label as if it
        were a wire value.

        The stored value is derived from the option here rather than passed in,
        so a caller cannot store None by forgetting an argument.
        """
        state_key = self._definition.state_key
        stored: Any = option
        if self._definition.value_map is not None:
            wire_value = self._wire_value(option)
            if wire_value is None:
                # Cannot happen after the option check above; storing the label
                # would poison a key the rest of the entity reads as an int.
                return
            stored = wire_value
        self.coordinator.set_device_value(state_key, stored)
        if self.coordinator.data is not None:
            self.coordinator.data[state_key] = stored
        self._optimistic_value = option
        self._optimistic_lock_until = time.monotonic() + OPTIMISTIC_LOCK_S
        self.async_write_ha_state()


def _get_select_defs(device_type: str) -> list[EcoFlowSelectDef]:
    """Return select definitions based on device type."""
    if device_type == DEVICE_TYPE_POWEROCEAN:
        return POWEROCEAN_SELECTS
    if device_type == DEVICE_TYPE_DELTA3:
        return DELTA3_SELECTS
    if device_type == DEVICE_TYPE_STREAM_AC5000:
        return STREAMAC5000_SELECTS
    return []
