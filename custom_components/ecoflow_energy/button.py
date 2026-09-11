"""Button platform for EcoFlow Energy.

The PowerPulse 2 wallbox's start/stop controls (ADR-009). Availability is
structural only - the wallbox coordinator's own availability plus the
sibling PowerOcean's MQTT connection, resolved fresh on every read - never
the wallbox's charge state, per decision 6.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_TYPE_POWERPULSE2,
    DOMAIN,
    POWERPULSE2_BUTTONS,
    EcoFlowButtonDef,
    filter_defs_for_serial,
)
from .coordinator import EcoFlowDeviceCoordinator
from .entity import reading_reported

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow buttons from a config entry."""
    coordinators: dict[str, EcoFlowDeviceCoordinator] = hass.data[DOMAIN][
        entry.entry_id
    ]
    entities: list[EcoFlowButton] = []

    for coordinator in coordinators.values():
        if coordinator.device_type != DEVICE_TYPE_POWERPULSE2:
            continue
        if not coordinator.enhanced_mode:
            _LOGGER.debug(
                "Skipping wallbox buttons - Standard Mode has no write path (%s)",
                coordinator.device_tag,
            )
            continue
        if coordinator.powerocean_sibling() is None:
            _LOGGER.debug(
                "Skipping wallbox buttons - no sibling PowerOcean in entry (%s)",
                coordinator.device_tag,
            )
            continue

        defs = filter_defs_for_serial(POWERPULSE2_BUTTONS, coordinator.device_sn)
        pending: list[EcoFlowButtonDef] = []
        for defn in defs:
            if defn.enhanced_only and not coordinator.enhanced_mode:
                continue
            if defn.accessory and not reading_reported(coordinator, defn.state_key):
                pending.append(defn)
                continue
            entities.append(EcoFlowButton(coordinator, defn))

        if pending:
            _watch_for_accessory(entry, coordinator, pending, async_add_entities)

    async_add_entities(entities)


@callback
def _watch_for_accessory(
    config_entry: ConfigEntry,
    coordinator: EcoFlowDeviceCoordinator,
    pending: list[EcoFlowButtonDef],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the wallbox buttons as soon as the descriptor is first reported.

    Same pattern as switch.py's accessory wait: the gate is the definition's
    state_key, which is the descriptor field (ev_charger_sn) rather than a
    value the buttons themselves read.
    """

    @callback
    def _check_for_accessory() -> None:
        ready = [
            definition
            for definition in pending
            if reading_reported(coordinator, definition.state_key)
        ]
        for definition in ready:
            pending.remove(definition)
        if ready:
            async_add_entities(
                [EcoFlowButton(coordinator, definition) for definition in ready]
            )

    config_entry.async_on_unload(coordinator.async_add_listener(_check_for_accessory))


class EcoFlowButton(CoordinatorEntity[EcoFlowDeviceCoordinator], ButtonEntity):
    """A wallbox start/stop button (ADR-009)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EcoFlowDeviceCoordinator,
        definition: EcoFlowButtonDef,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._definition = definition
        self._attr_unique_id = f"{coordinator.device_sn}_{definition.key}"
        self._attr_translation_key = definition.key
        self._attr_icon = definition.icon

    @property
    def available(self) -> bool:
        """Return True when the wallbox and its sibling PowerOcean are up.

        Structural only (ADR-009 decision 6): no charge-state precondition
        here, so the entity never flaps with every heartbeat. A press in the
        wrong state raises instead - see async_press().
        """
        if not (self.coordinator.device_available and super().available):
            return False
        sibling = self.coordinator.powerocean_sibling()
        return (
            sibling is not None
            and sibling.mqtt_client is not None
            and sibling.mqtt_client.is_connected()
        )

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    async def async_press(self) -> None:
        """Send the start or stop command; let a refusal raise to the user."""
        await self.coordinator.async_set_powerpulse_charge_action(
            self._definition.action
        )
