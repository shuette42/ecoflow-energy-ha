"""Switch platform for EcoFlow Energy.

Implements optimistic lock: after a SET command the local state is
updated immediately and MQTT updates for that key are ignored for 5 s.
This prevents switch flicker while the device confirms the change.

The PowerPulse 2 settings switches (Continuous Charging, Block Battery
Discharge, Plug-and-Play) are the exception: they apply nothing on their own.
The coordinator write returns once the wallbox has reported the new switch
bits, and that report is what moves the displayed state (PLAN-172, the same
rule as the wallbox numbers and selects).
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

from .const import (
    DELTA2MAX_SWITCHES,
    DELTA3_SWITCHES,
    DELTA_PROFILE_R331,
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_POWERPULSE2,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_STREAM,
    DEVICE_TYPE_STREAM_AC5000,
    DEVICE_TYPE_WAVE3,
    DOMAIN,
    POWEROCEAN_SCHEDULE_PREFIXES,
    POWEROCEAN_SWITCHES,
    POWEROCEANLOCALONLY_SWITCHES,
    POWERPULSE2_SWITCHES,
    SMARTPLUG_SWITCH_COMMANDS,
    SMARTPLUG_SWITCHES,
    STREAM_SWITCHES,
    STREAMAC5000_SWITCHES,
    SWITCH_COMMANDS_R331,
    SWITCH_COMMANDS_R351,
    SWITCH_DECLARATIVE_R331,
    SWITCH_DECLARATIVE_R351,
    WAVE3_SWITCHES,
    EcoFlowSwitchDef,
    filter_defs_for_serial,
    supports_powerpulse_controls,
    supports_stream_ac5000_controls,
    supports_stream_controls,
)
from .coordinator import DeviceValueNotReported, EcoFlowDeviceCoordinator
from .coordinator.local_modbus import EcoFlowLocalModbusCoordinator
from .ecoflow.delta3_commands import (
    build_port_priority_command,
)
from .ecoflow.delta3_commands import (
    build_switch_command as build_delta3_switch_command,
)
from .ecoflow.parsers.delta3_proto import port_priority_keys
from .ecoflow.parsers.smartplug import build_plug_switch_payload
from .ecoflow.wave3_commands import Wave3WriteRefused
from .entity import (
    EcoFlowWriteGateMixin,
    as_known_int,
    raise_set_failed,
    raise_set_gone,
    raise_set_not_ready,
    raise_set_rejected,
    raise_set_unsupported,
    reading_reported,
)

_LOGGER = logging.getLogger(__name__)

OPTIMISTIC_LOCK_S = 5.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow switches from a config entry."""
    coordinators: dict[
        str, EcoFlowDeviceCoordinator | EcoFlowLocalModbusCoordinator
    ] = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = []

    for source in coordinators.values():
        if isinstance(source, EcoFlowLocalModbusCoordinator):
            # A local entry gets its control switch and nothing from the cloud
            # definition lists.
            entities.extend(
                EcoFlowLocalControlSwitch(source, defn)
                for defn in POWEROCEANLOCALONLY_SWITCHES
            )
            continue
        coordinator = source
        if (
            coordinator.device_type == DEVICE_TYPE_POWERPULSE2
            and not supports_powerpulse_controls(coordinator.device_sn)
        ):
            continue
        defs = filter_defs_for_serial(
            _get_switch_defs(coordinator.device_type, coordinator.device_sn),
            coordinator.device_sn,
        )
        if (
            coordinator.device_type == DEVICE_TYPE_POWERPULSE2
            and coordinator.charge_action_route() != "sibling"
        ):
            # The write goes through a PowerOcean on the wallbox's own
            # channel and has no evidenced route without exactly one (the
            # same rule as the wallbox numbers and selects): zero or
            # two-or-more PowerOceans in the entry gets no switch.
            continue
        pending: list[EcoFlowSwitchDef] = []
        for defn in defs:
            if defn.enhanced_only and not coordinator.enhanced_mode:
                continue
            if defn.accessory and not reading_reported(coordinator, defn.state_key):
                pending.append(defn)
                continue
            entities.append(EcoFlowSwitch(coordinator, defn))

        if pending:
            _watch_for_accessory(entry, coordinator, pending, async_add_entities)

    async_add_entities(entities)


@callback
def _watch_for_accessory(
    config_entry: ConfigEntry,
    coordinator: EcoFlowDeviceCoordinator,
    pending: list[EcoFlowSwitchDef],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add accessory switches as soon as the device first reports their state.

    The gate is the definition's read-back key, because that is the reading
    the control steers and the only one the device sends. Every definition
    that reaches this path names the same string for both, so the two are
    interchangeable today; the read-back key is the one named because it is
    the one a control has to wait for. A PowerOcean schedule can be created
    in the app while Home Assistant runs, so the wait has no deadline.
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
                [EcoFlowSwitch(coordinator, definition) for definition in ready]
            )

    config_entry.async_on_unload(coordinator.async_add_listener(_check_for_accessory))


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
        """Return True if entity is available.

        The PowerPulse 2 settings switches only ever exist on the sibling
        route (see `async_setup_entry`) and their write goes through the
        sibling PowerOcean's own MQTT connection, so availability also follows
        that connection, the same rule the wallbox's numbers and charging-mode
        select apply. The sibling is resolved fresh on every read: it is None
        with no PowerOcean in the entry, with two or more (a second one can
        join after setup, when the route is no longer unambiguous) and while
        the entry's coordinator table is gone during teardown, which is
        exactly `charge_action_route() == "sibling"`. Every other switch keeps
        the plain device availability.
        """
        if not (self.coordinator.device_available and super().available):
            return False
        if self.coordinator.device_type != DEVICE_TYPE_POWERPULSE2:
            return True
        sibling = self.coordinator.powerocean_sibling()
        return (
            sibling is not None
            and sibling.mqtt_client is not None
            and sibling.mqtt_client.is_connected()
        )

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

        The PowerPulse 2 settings switches show only what the wallbox
        reported: each one bit of the settings switch bits, unknown (never
        off) while the bits are absent, and no lock window or restored state.
        """
        if self.coordinator.device_type == DEVICE_TYPE_POWERPULSE2:
            bits = as_known_int(
                (self.coordinator.data or {}).get(self._definition.state_key)
            )
            mask = self._definition.bit_mask
            if bits is None or mask is None:
                return None
            return bool(bits & mask)

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
        if self.coordinator.device_type == DEVICE_TYPE_POWERPULSE2:
            # No optimistic state: the coordinator returns once the wallbox
            # has reported the new bits, and that report moves the switch.
            mask = self._definition.bit_mask
            if mask is None:
                raise_set_failed(self.entity_id)
            await self.coordinator.async_set_powerpulse_switch_bit(
                mask, turn_on, self._definition.key
            )
            return

        schedule_slot = self._schedule_slot()
        if schedule_slot is not None:
            prefix, slot = schedule_slot
            try:
                ok = await self.coordinator.async_set_powerocean_schedule_armed(
                    prefix, slot, turn_on
                )
            except DeviceValueNotReported:
                # The slot is not in the device's task list. The switch is
                # still here because a retracted reading keeps its entity, but
                # there is no task behind it to arm.
                raise_set_gone(self.entity_id)
            if not ok:
                raise_set_failed(self.entity_id)
            self._apply_optimistic(turn_on)
            return

        # SmartPlug app-auth: use protobuf SET (JSON cmdCode only works on /open/ topic)
        if (
            self.coordinator.device_type == DEVICE_TYPE_SMARTPLUG
            and self.coordinator.enhanced_mode
            and self._definition.key == "plug_switch"
        ):
            payload = build_plug_switch_payload(
                turn_on, device_sn=self.coordinator.device_sn
            )
            ok = await self.coordinator.async_send_proto_set_command(
                payload, "plug_switch"
            )
            if not ok:
                raise_set_failed(self.entity_id)
            self._apply_optimistic(turn_on)
            return

        if self.coordinator.device_type == DEVICE_TYPE_WAVE3:
            try:
                ok = await self.coordinator.async_send_wave3_set(
                    self._definition.key, turn_on
                )
            except Wave3WriteRefused as err:
                raise_set_rejected(self.entity_id, str(err))
            if not ok:
                raise_set_failed(self.entity_id)
            self._apply_optimistic(turn_on)
            return

        if self.coordinator.device_type == DEVICE_TYPE_STREAM_AC5000:
            ok = await self._async_set_stream_ac5000(turn_on)
            if not ok:
                raise_set_failed(self.entity_id)
            self._apply_optimistic(turn_on)
            return

        if (
            self.coordinator.device_type == DEVICE_TYPE_STREAM
            and self.coordinator.enhanced_mode
            and self._definition.key in ("ac_outlet_1_switch", "ac_outlet_2_switch")
        ):
            outlet = 1 if self._definition.key == "ac_outlet_1_switch" else 2
            ok = await self.coordinator.async_set_stream_ac_outlet(outlet, turn_on)
            if not ok:
                raise_set_failed(self.entity_id)
            self._apply_optimistic(turn_on)
            return

        command = self._build_command(turn_on)
        if command is None:
            raise_set_unsupported(self.entity_id)

        if self.coordinator.device_type == DEVICE_TYPE_DELTA3:
            ok = await self.coordinator.async_send_delta3_set(command)
        else:
            ok = await self.coordinator.async_send_set_command(command)
        if not ok:
            raise_set_failed(self.entity_id)
        if self._port_priority_stem() is not None:
            # The cutoff number reads this flag from the shared store when it
            # builds its own write, because the wire item carries both halves.
            # Seed the store with the flag just sent - until the device echoes
            # it back, a slider move would otherwise read the stale flag and
            # silently revert this switch. The number path seeds its cutoff
            # the same way (see `_apply_optimistic_number`); the next 968
            # read-back overwrites the seed with device truth either way.
            state_key = self._definition.state_key
            self.coordinator.set_device_value(state_key, turn_on)
            if self.coordinator.data is not None:
                self.coordinator.data[state_key] = turn_on
        self._apply_optimistic(turn_on)

    # Same NoReturn gap as number.py's _async_set_stream_value:
    # raise_set_unsupported always raises, ruff's RET503 does not see it
    # across the import from entity.py.
    async def _async_set_stream_ac5000(self, turn_on: bool) -> bool:  # noqa: RET503
        """Send one of the two STREAM AC 5000 switches as a config write.

        Both halves go out through the coordinator so they queue behind any
        other config write to this device rather than beside it.
        """
        key = self._definition.key

        if key == "backup_socket_switch":
            return await self.coordinator.async_set_stream_ac5000_backup_socket(turn_on)

        if key == "backup_reserve_switch":
            # Config field 30 holds the on/off and the reserve level, so the
            # level the user set has to travel with the switch. The number
            # entity owns that level, so this is the one config write whose
            # two halves sit on different platforms and the coordinator is
            # the only place that can serialise them.
            try:
                return await self.coordinator.async_set_stream_ac5000_backup_reserve(
                    enabled=turn_on
                )
            except DeviceValueNotReported:
                raise_set_not_ready(self.entity_id)
            except ValueError as err:
                raise_set_rejected(self.entity_id, str(err))

        raise_set_unsupported(self.entity_id)

    def _apply_optimistic(self, turn_on: bool) -> None:
        """Apply optimistic lock: immediately reflect the new state."""
        self._optimistic_value = turn_on
        self._optimistic_lock_until = time.monotonic() + OPTIMISTIC_LOCK_S
        self._write_state_always(turn_on)

    def _schedule_slot(self) -> tuple[str, int] | None:
        """Return the (family prefix, slot) for a PowerOcean schedule switch.

        Two families share this shape - the charge schedule (`schedule_`) and
        the feed-to-grid schedule (`feed_schedule_`) - and the prefix travels
        with the slot so the caller can route the write to the right task
        list without a second lookup.
        """
        if self.coordinator.device_type != DEVICE_TYPE_POWEROCEAN:
            return None
        key = self._definition.key
        for prefix in POWEROCEAN_SCHEDULE_PREFIXES:
            head = f"{prefix}_"
            if key.startswith(head) and key.endswith("_enabled"):
                return prefix, int(key[len(head) : -len("_enabled")])
        return None

    def _port_priority_stem(self) -> str | None:
        """Return the port stem for a port priority switch, else None."""
        key = self._definition.key
        if key.startswith("port_priority_") and key.endswith("_switch"):
            return key[len("port_priority_") : -len("_switch")]
        return None

    def _build_port_priority_command(
        self, stem: str, turn_on: bool
    ) -> dict[str, Any] | None:
        """Build a port priority write, carrying the port's current cutoff.

        The wire item holds the flag and the cutoff together, so the cutoff has
        to travel even when only the flag changed. It comes from the last
        read-back rather than from a default: inventing one here would silently
        move a threshold the user set in the app. Until the device has reported
        the cutoff the write is refused as not-ready rather than as
        unsupported: it is a window of at most one status frame, not a device
        limitation.
        """
        _, cutoff_key = port_priority_keys(stem)
        cutoff = (self.coordinator.data or {}).get(cutoff_key)
        if not isinstance(cutoff, (int, float)) or isinstance(cutoff, bool):
            _LOGGER.debug(
                "Port priority write for %s skipped - no cutoff reported yet",
                self.entity_id,
            )
            raise_set_not_ready(self.entity_id)
        return build_port_priority_command(stem, turn_on, int(cutoff))

    def _build_command(self, turn_on: bool) -> dict[str, Any] | None:
        """Build a SET command from legacy or declarative templates."""
        if self.coordinator.device_type == DEVICE_TYPE_DELTA3:
            stem = self._port_priority_stem()
            if stem is not None:
                return self._build_port_priority_command(stem, turn_on)
            return build_delta3_switch_command(self._definition.key, turn_on)

        if self.coordinator.device_type == DEVICE_TYPE_DELTA:
            commands = _get_delta_switch_commands(self.coordinator.delta_profile)
            declarative_templates = _get_delta_switch_declarative(
                self.coordinator.delta_profile
            )
        else:
            commands = _get_switch_commands(self.coordinator.device_type)
            declarative_templates = {}

        # Check declarative templates first (new switches)
        decl = declarative_templates.get(self._definition.key)
        if decl is not None:
            invert = decl.get("invert", False)
            value = (0 if turn_on else 1) if invert else (1 if turn_on else 0)

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


class EcoFlowLocalControlSwitch(
    EcoFlowWriteGateMixin,
    CoordinatorEntity[EcoFlowLocalModbusCoordinator],
    SwitchEntity,
):
    """Modbus control of a local entry: the heartbeat that takes control from the app.

    Deliberately not a RestoreEntity and not optimistic. The state is the
    coordinator's `control_enabled` flag, which is off after every restart,
    reload or reconfigure and turns off by itself when the heartbeat lapses;
    the coordinator announces that through its listeners, so the entity never
    holds a state of its own.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EcoFlowLocalModbusCoordinator,
        definition: EcoFlowSwitchDef,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._definition = definition
        self._attr_unique_id = f"{coordinator.device_sn}_{definition.key}"
        self._attr_translation_key = definition.key
        self._attr_icon = definition.icon

    @property
    def available(self) -> bool:
        """Return True while the device answers its polls, or while control is on.

        An unavailable entity is skipped by Home Assistant's service calls, so
        a switch that is on stays available: "off" must always be reachable.
        """
        if self.coordinator.control_enabled:
            return True
        return self.coordinator.device_available and super().available

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info from coordinator."""
        return self.coordinator.device_info

    @property
    def is_on(self) -> bool:
        """Return True while this integration sends the control heartbeat."""
        return self.coordinator.control_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Take control: write the first beat and start the heartbeat."""
        await self.coordinator.async_set_control(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the heartbeat; the unit hands control back to the app by itself."""
        await self.coordinator.async_set_control(False)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle a poll, a confirmed write or a lapsed heartbeat."""
        self._write_state_if_changed(self.is_on)


def _get_switch_defs(device_type: str, device_sn: str = "") -> list[EcoFlowSwitchDef]:
    """Return switch definitions based on device type.

    ``device_sn`` gates the STREAM AC 5000 controls, which are confirmed on
    the variants that have produced a write frame of their own. See
    STREAM_AC5000_CONTROL_PREFIXES.
    """
    if device_type == DEVICE_TYPE_DELTA:
        return DELTA2MAX_SWITCHES
    if device_type == DEVICE_TYPE_POWEROCEAN:
        return POWEROCEAN_SWITCHES
    if device_type == DEVICE_TYPE_SMARTPLUG:
        return SMARTPLUG_SWITCHES
    if device_type == DEVICE_TYPE_STREAM:
        if not supports_stream_controls(device_sn):
            return []
        return STREAM_SWITCHES
    if device_type == DEVICE_TYPE_DELTA3:
        return DELTA3_SWITCHES
    if device_type == DEVICE_TYPE_STREAM_AC5000:
        if not supports_stream_ac5000_controls(device_sn):
            return []
        return STREAMAC5000_SWITCHES
    if device_type == DEVICE_TYPE_WAVE3:
        return WAVE3_SWITCHES
    if device_type == DEVICE_TYPE_POWERPULSE2:
        return POWERPULSE2_SWITCHES
    return []


def _get_switch_commands(device_type: str) -> dict[str, dict[str, dict[str, Any]]]:
    """Return command templates based on device type."""
    if device_type == DEVICE_TYPE_SMARTPLUG:
        return SMARTPLUG_SWITCH_COMMANDS
    return SWITCH_COMMANDS_R351


def _get_delta_switch_commands(
    delta_profile: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return Delta switch command templates for the selected profile."""
    if delta_profile == DELTA_PROFILE_R331:
        return SWITCH_COMMANDS_R331
    return SWITCH_COMMANDS_R351


def _get_delta_switch_declarative(delta_profile: str) -> dict[str, dict[str, Any]]:
    """Return Delta declarative switch templates for the selected profile."""
    if delta_profile == DELTA_PROFILE_R331:
        return SWITCH_DECLARATIVE_R331
    return SWITCH_DECLARATIVE_R351
