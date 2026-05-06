"""Select platform for EcoFlow Energy.

Currently used for PowerOcean Work Mode selection (Self-use, AI Schedule).
Implements the same optimistic-lock pattern as switch.py and number.py:
after a SET, the local state is updated immediately and MQTT updates for
the same key are ignored for 5 seconds.
"""

from __future__ import annotations

import logging
import time

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_TYPE_POWEROCEAN,
    DOMAIN,
    EcoFlowSelectDef,
    POWEROCEAN_SELECTS,
)
from .coordinator import EcoFlowDeviceCoordinator

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
        defs = _get_select_defs(coordinator.device_type)
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
            wire_value = WORK_MODE_TO_INT.get(option)
            if wire_value is None:
                _LOGGER.warning("No wire mapping for work-mode option %s", option)
                return
            if self.coordinator.device_type != DEVICE_TYPE_POWEROCEAN:
                _LOGGER.warning("work_mode select only supports PowerOcean")
                return
            ok = await self.coordinator.async_set_powerocean_work_mode(wire_value)
            if ok:
                self._apply_optimistic_select(option)
            return

        _LOGGER.warning("No SET handler for select %s", self._definition.key)

    def _apply_optimistic_select(self, option: str) -> None:
        """Apply the optimistic lock so the UI reflects the change immediately."""
        state_key = self._definition.state_key
        self.coordinator.set_device_value(state_key, option)
        if self.coordinator.data is not None:
            self.coordinator.data[state_key] = option
        self._optimistic_value = option
        self._optimistic_lock_until = time.monotonic() + OPTIMISTIC_LOCK_S
        self.async_write_ha_state()


def _get_select_defs(device_type: str) -> list[EcoFlowSelectDef]:
    """Return select definitions based on device type."""
    if device_type == DEVICE_TYPE_POWEROCEAN:
        return POWEROCEAN_SELECTS
    return []
