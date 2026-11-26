"""Button platform for EcoFlow Energy.

The PowerPulse 2 wallbox's start/stop controls (ADR-009). Availability is
structural only - the wallbox coordinator's own availability plus its
charge-action route's MQTT connection (the sibling PowerOcean's, or the
wallbox's own when there is no sibling, per PLAN-140), resolved fresh on
every read - never the wallbox's charge state, per decision 6.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import Literal

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_POWERPULSE2,
    DOMAIN,
    POWERPULSE2_BUTTONS,
    EcoFlowButtonDef,
    filter_defs_for_serial,
    supports_powerpulse_controls,
)
from .coordinator import EcoFlowDeviceCoordinator
from .entity import reading_reported, wallbox_control_withheld

_LOGGER = logging.getLogger(__name__)


def _gate_key(definition: EcoFlowButtonDef, route: Literal["sibling", "own"]) -> str:
    """Return the reading that must be reported before this button is created.

    On the "sibling" route the descriptor field (``definition.state_key``,
    i.e. ``ev_charger_sn``) is the gate, exactly as before PLAN-140. On the
    "own" route there is no sibling to carry a descriptor: a wallbox on an
    account without a PowerOcean never reports ``ev_charger_sn`` /
    ``ev_charger_dev_addr`` - those come from ``241/44``, which such an
    account never sends. Its heartbeat (``2/33``) does arrive and fills
    ``ev_charge_status``, so that reading is the gate instead.
    """
    if route == "own":
        return "ev_charge_status"
    return definition.state_key


def _wallbox_gate_reported(
    coordinator: EcoFlowDeviceCoordinator,
    route: Literal["sibling", "own"],
    definition: EcoFlowButtonDef,
) -> bool:
    """Return whether a PowerPulse 2 button's gate reading has been reported."""
    return reading_reported(coordinator, _gate_key(definition, route))


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
        if coordinator.device_type == DEVICE_TYPE_POWEROCEAN:
            _add_powerocean_buttons(entry, coordinator, entities, async_add_entities)
            continue
        if coordinator.device_type != DEVICE_TYPE_POWERPULSE2:
            continue
        if not supports_powerpulse_controls(coordinator.device_sn):
            continue
        if not coordinator.enhanced_mode:
            _LOGGER.debug(
                "Skipping wallbox buttons - Standard Mode has no write path (%s)",
                coordinator.device_tag,
            )
            continue
        route = coordinator.charge_action_route()
        if route is None:
            _LOGGER.debug(
                "Skipping wallbox buttons - two or more PowerOceans in entry, "
                "or no PowerOcean and no own route for this model (%s)",
                coordinator.device_tag,
            )
            continue

        defs = filter_defs_for_serial(POWERPULSE2_BUTTONS, coordinator.device_sn)
        pending: list[EcoFlowButtonDef] = []
        for defn in defs:
            if defn.enhanced_only and not coordinator.enhanced_mode:
                continue
            if defn.accessory and not reading_reported(
                coordinator, _gate_key(defn, route)
            ):
                pending.append(defn)
                continue
            entities.append(EcoFlowButton(coordinator, defn))

        if pending:
            _watch_for_gate(
                entry,
                coordinator,
                partial(_wallbox_gate_reported, coordinator, route),
                pending,
                async_add_entities,
            )

    async_add_entities(entities)


def _add_powerocean_buttons(
    entry: ConfigEntry,
    coordinator: EcoFlowDeviceCoordinator,
    entities: list[EcoFlowButton],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add Start/Stop Charging for a PowerPulse 1 this PowerOcean reports (#731).

    The wallbox has no channel of its own: the PowerOcean carries its status
    on 209/8 and takes the writes. Created once the status is reported and
    the reporting wallbox is not a PowerPulse 2, which has its own pair.
    """
    if not coordinator.enhanced_mode:
        return

    def ready(definition: EcoFlowButtonDef) -> bool:
        return reading_reported(
            coordinator, "ev_charge_status"
        ) and not wallbox_control_withheld(coordinator, definition.key)

    pending: list[EcoFlowButtonDef] = []
    for defn in filter_defs_for_serial(POWERPULSE2_BUTTONS, coordinator.device_sn):
        if ready(defn):
            entities.append(EcoFlowButton(coordinator, defn))
        else:
            pending.append(defn)
    if pending:
        _watch_for_gate(entry, coordinator, ready, pending, async_add_entities)


@callback
def _watch_for_gate(
    config_entry: ConfigEntry,
    coordinator: EcoFlowDeviceCoordinator,
    ready_for: Callable[[EcoFlowButtonDef], bool],
    pending: list[EcoFlowButtonDef],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the wallbox buttons as soon as their gate reading is first reported.

    Same pattern as switch.py's accessory wait. `ready_for` is fixed at setup
    like every other platform's entity set: the PowerPulse 2 passes its route's
    gate key, a PowerOcean its wallbox status and serial check.
    """

    @callback
    def _check_for_gate() -> None:
        ready = [definition for definition in pending if ready_for(definition)]
        for definition in ready:
            pending.remove(definition)
        if ready:
            async_add_entities(
                [EcoFlowButton(coordinator, definition) for definition in ready]
            )

    config_entry.async_on_unload(coordinator.async_add_listener(_check_for_gate))


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
        """Return True when the wallbox and its charge-action route are up.

        Structural only (ADR-009 decision 6): no charge-state precondition
        here, so the entity never flaps with every heartbeat. A press in the
        wrong state raises instead - see async_press(). The route is
        re-evaluated fresh on every read, same as before PLAN-140: on the
        "sibling" route the sibling PowerOcean's MQTT connection decides, on
        the "own" route the wallbox's own connection does.
        """
        if not (self.coordinator.device_available and super().available):
            return False
        if self.coordinator.device_type == DEVICE_TYPE_POWEROCEAN:
            # The PowerOcean carries the wallbox's writes itself: no route.
            return (
                self.coordinator.mqtt_client is not None
                and self.coordinator.mqtt_client.is_connected()
                and not wallbox_control_withheld(self.coordinator, self._definition.key)
            )
        route = self.coordinator.charge_action_route()
        if route == "sibling":
            sibling = self.coordinator.powerocean_sibling()
            return (
                sibling is not None
                and sibling.mqtt_client is not None
                and sibling.mqtt_client.is_connected()
            )
        if route == "own":
            return (
                self.coordinator.mqtt_client is not None
                and self.coordinator.mqtt_client.is_connected()
            )
        return False

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    async def async_press(self) -> None:
        """Send the start or stop command; let a refusal raise to the user."""
        if self.coordinator.device_type == DEVICE_TYPE_POWEROCEAN:
            await self.coordinator.async_set_wallbox_charge_action(
                self._definition.action
            )
            return
        await self.coordinator.async_set_powerpulse_charge_action(
            self._definition.action
        )
