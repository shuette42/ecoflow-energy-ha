"""Number platform for EcoFlow Energy."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DELTA2MAX_NUMBERS,
    DELTA3_NUMBERS,
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_STREAM,
    DEVICE_TYPE_STREAM_AC5000,
    DOMAIN,
    EcoFlowNumberDef,
    filter_defs_for_serial,
    NUMBER_COMMANDS,
    POWEROCEAN_NUMBERS,
    SMARTPLUG_NUMBER_COMMANDS,
    SMARTPLUG_NUMBERS,
    STREAM_NUMBERS,
    STREAMAC5000_NUMBERS,
    supports_stream_ac5000_controls,
    supports_stream_controls,
)
from .coordinator import DeviceValueNotReported, EcoFlowDeviceCoordinator
from .entity import (
    raise_set_gone,
    as_known_int,
    EcoFlowWriteGateMixin,
    reading_reported,
    raise_set_failed,
    raise_set_not_ready,
    raise_set_rejected,
    raise_set_unsupported,
)
from .ecoflow.delta3_commands import (
    build_number_command as build_delta3_number_command,
    build_port_priority_command,
    port_priority_soc_bounds,
)
from .ecoflow.const import (
    schedule_power_max_w,
    schedule_power_min_w,
)
from .ecoflow.energy_stream import stream_backup_reserve_floor
from .ecoflow.parsers.delta3_proto import port_priority_keys
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
        defs = filter_defs_for_serial(
            _get_number_defs(coordinator.device_type, coordinator.device_sn),
            coordinator.device_sn,
        )
        pending: list[EcoFlowNumberDef] = []
        for defn in defs:
            if defn.enhanced_only and not coordinator.enhanced_mode:
                continue
            if defn.accessory and not reading_reported(coordinator, defn.state_key):
                pending.append(defn)
                continue
            entities.append(EcoFlowNumber(coordinator, defn))

        if pending:
            _watch_for_accessory(entry, coordinator, pending, async_add_entities)

    async_add_entities(entities)


@callback
def _watch_for_accessory(
    config_entry: ConfigEntry,
    coordinator: EcoFlowDeviceCoordinator,
    pending: list[EcoFlowNumberDef],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add accessory numbers as soon as the device first reports their value.

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
                [EcoFlowNumber(coordinator, definition) for definition in ready]
            )

    config_entry.async_on_unload(coordinator.async_add_listener(_check_for_accessory))


class EcoFlowNumber(
    EcoFlowWriteGateMixin, CoordinatorEntity[EcoFlowDeviceCoordinator], RestoreNumber
):
    """An EcoFlow number entity with state restore across restarts."""

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

        self._restored_value: float | None = None
        self._last_written_value: float | None = None
        self._last_written_bounds: tuple[float, float] | None = None
        self._optimistic_lock_until: float = 0.0
        # Seeded from the coordinator rather than from zero: an entity added
        # after a rollback already happened must not read that rollback as
        # one of its own.
        self._seen_rollback_generation: int = getattr(
            coordinator, "_powerocean_soc_rollback_generation", 0
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.device_available and super().available

    async def async_added_to_hass(self) -> None:
        """Restore the last known value when the entity is added.

        Enhanced Mode devices can take up to two minutes before the first
        full status frame arrives. Without a restored value HA renders the
        number as an empty field for that whole window. The restored value
        is only a placeholder: as soon as live data delivers the key, the
        live value always wins (see ``native_value``).
        """
        await super().async_added_to_hass()
        data = self.coordinator.data
        if data is not None and self._definition.state_key in data:
            return  # live value already present, nothing to restore
        last = await self.async_get_last_number_data()
        if last is None or last.native_value is None:
            return  # nothing restorable (e.g. state was unknown/unavailable)
        value = float(last.native_value)
        if value < self.native_min_value or value > self.native_max_value:
            return  # out-of-range restored values are discarded
        self._restored_value = value
        # Seed the write gate so an identical first live frame does not
        # trigger a redundant recorder write (mirrors the sensor restore).
        self._last_written_value = value

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info from coordinator."""
        return self.coordinator.device_info

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        rollback = getattr(
            self.coordinator, "_powerocean_soc_rollback_generation", 0
        )
        if rollback != self._seen_rollback_generation:
            # A write of ours was rejected and the coordinator restored the
            # device value. Holding the optimistic lock now would keep showing
            # the value the device refused, and on a dead connection no later
            # update would ever correct it.
            self._seen_rollback_generation = rollback
            self._optimistic_lock_until = 0.0
        elif time.monotonic() < self._optimistic_lock_until:
            return  # ignore incoming data during optimistic lock
        if self._bounds_moved():
            # Home Assistant snapshots min/max into the state machine only on
            # a state write, and the gate skips writes when the value did not
            # move. A battery-limit change would otherwise leave stale bounds
            # published: the frontend then offers cutoffs the service call
            # rejects, or refuses cutoffs that became legal. Checked after the
            # lock on purpose - the next push (about a second later) catches
            # up, and writing mid-lock could resurface the bounced value the
            # lock exists to suppress.
            self._write_state_always(self.native_value)
            return
        self._write_state_if_changed(self.native_value)

    @property
    def native_value(self) -> float | None:
        """Return the current value, falling back to the restored one.

        Only a MISSING key falls back to the restored value. A key present
        with value None is an explicit clear and shows as unknown. A live
        value always beats the restored one.
        """
        data = self.coordinator.data
        if data is None or self._definition.state_key not in data:
            return self._restored_value
        value = data[self._definition.state_key]
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

    @property
    def native_min_value(self) -> float:
        """Return the lower bound, narrowed where the device derives it."""
        bounds = self._derived_bounds()
        if bounds is None:
            return self._attr_native_min_value
        return bounds[0]

    @property
    def native_max_value(self) -> float:
        """Return the upper bound, narrowed where the device derives it."""
        bounds = self._derived_bounds()
        if bounds is None:
            return self._attr_native_max_value
        return bounds[1]

    def _derived_bounds(self) -> tuple[float, float] | None:
        """Return the runtime bounds, or None where the declared range holds.

        Three controls have a range the device moves under them. Two follow a
        battery limit the user can change while HA runs, the Delta 3 port
        priority cutoffs and the Stream backup reserve. The third, the STREAM
        AC 5000 grid-tied output, follows a ceiling only EcoFlow can raise.
        The scheduled charge power is a fourth of a different kind: its floor
        follows the reported pack count, its ceiling the model.
        Every other number keeps the range its definition declares.
        """
        if self._schedule_slot() is not None:
            return self._schedule_power_bounds()
        if self._port_priority_stem() is not None:
            lower, upper = self._port_priority_bounds()
            return float(lower), float(upper)
        if self._is_stream_ac5000_grid_output():
            ceiling = as_known_int(
                (self.coordinator.data or {}).get("_grid_output_ceiling_w")
            )
            if ceiling is None:
                return None
            # Never widen past the declared rating, and never collapse the
            # range: a ceiling the device has not reported sensibly would
            # otherwise leave a control that cannot be moved.
            upper = min(float(ceiling), self._attr_native_max_value)
            if upper <= self._attr_native_min_value:
                return None
            return self._attr_native_min_value, upper
        if self._is_stream_backup_reserve():
            data = self.coordinator.data or {}
            # Read through `as_known_int`, not raw: HA hands `number.set_value`
            # a float, so for the seconds between a discharge-limit write and
            # the device echoing it back the key holds 40.0 rather than 40. A
            # strict check there would drop the floor to the declared 3 for
            # exactly as long as the value is in flight.
            floor = stream_backup_reserve_floor(
                as_known_int(data.get("min_discharge_soc_pct")),
                int(self._attr_native_min_value),
                int(self._attr_native_max_value),
            )
            return float(floor), self._attr_native_max_value
        return None

    def _is_stream_ac5000_grid_output(self) -> bool:
        """Return True for the grid-tied output setpoint on this family.

        Its upper bound is the device's own ceiling on `f10.6` rather than
        the model rating: EcoFlow sets it per account and it has been seen at
        600 W on a unit rated 2500, so offering the rating would present a
        range three quarters of which the device would not honour.
        """
        return (
            self._definition.key == "max_grid_output_power_w"
            and self.coordinator.device_type == DEVICE_TYPE_STREAM_AC5000
        )

    def _is_stream_backup_reserve(self) -> bool:
        """Return True for the Stream backup reserve on a write-gated model.

        The key alone does not identify it: the STREAM AC 5000 carries a
        backup reserve of its own, and the Stream list creates this one on
        every BK serial. The gate is `supports_stream_controls`, the same
        allowlist the other Stream writes ride, rather than a second set
        holding the one model the floor was measured on - see the note at
        STREAM_CONTROL_PREFIXES. Everything outside it keeps the declared
        range.
        """
        return (
            self._definition.key == "backup_reserve"
            and self.coordinator.device_type == DEVICE_TYPE_STREAM
            and supports_stream_controls(self.coordinator.device_sn)
        )

    def _schedule_power_bounds(self) -> tuple[float, float]:
        """Return the charge power range the app would offer for this unit.

        The floor is 100 W per online battery pack and the ceiling comes from
        the serial prefix, which is exactly how the app builds this prompt:
        neither is a device-reported rating, and no frame carries one. A pack
        count that has not arrived yet gives the one-pack floor rather than
        zero, because the app cannot send zero and what the device would do
        with it has never been seen.
        """
        packs = as_known_int((self.coordinator.data or {}).get("bp_online_sum"))
        upper = schedule_power_max_w(self.coordinator.device_sn)
        lower = schedule_power_min_w(packs)
        if lower > upper:
            # More packs than the model's ceiling allows for at 100 W each.
            # No installation looks like this, and a floor above the ceiling
            # would leave a control that cannot be moved at all.
            lower = schedule_power_min_w(None)
        return float(lower), float(upper)

    def _port_priority_stem(self) -> str | None:
        """Return the port stem for a port priority cutoff, else None."""
        key = self._definition.key
        if key.startswith("port_priority_") and key.endswith("_soc"):
            return key[len("port_priority_") : -len("_soc")]
        return None

    def _build_port_priority_command(
        self, stem: str, value: float
    ) -> dict[str, Any] | None:
        """Build a cutoff write, carrying the port's current essential flag.

        Both halves of the wire item travel together, so the flag has to come
        along. It is read back rather than assumed - writing a default here
        would move a port between essential and non-essential as a side effect
        of changing a threshold. Until the device has reported the flag the
        write is refused as not-ready rather than as unsupported: it is a
        window of at most one status frame, not a device limitation.
        """
        limited_key, _ = port_priority_keys(stem)
        limited = (self.coordinator.data or {}).get(limited_key)
        if not isinstance(limited, bool):
            _LOGGER.debug(
                "Port priority write for %s skipped - port state not reported yet",
                self.entity_id,
            )
            raise_set_not_ready(self.entity_id)
        lower, upper = self._port_priority_bounds()
        clamped = max(lower, min(upper, int(round(value))))
        return build_port_priority_command(stem, limited, clamped)

    def _port_priority_bounds(self) -> tuple[int, int]:
        """Return the cutoff bounds derived from the device's battery limits."""
        data = self.coordinator.data or {}
        return port_priority_soc_bounds(
            data.get("max_charge_soc_pct"), data.get("min_discharge_soc_pct")
        )

    def _bounds_moved(self) -> bool:
        """Return True after recording a change of the derived slider bounds.

        A number with a declared range keeps it for good and never reports a
        move; only the controls with runtime bounds can.
        """
        bounds = self._derived_bounds()
        if bounds is None:
            return False
        if bounds == self._last_written_bounds:
            return False
        self._last_written_bounds = bounds
        return True

    async def async_set_native_value(self, value: float) -> None:
        """Set a new value via the EcoFlow IoT API."""
        # PowerOcean uses protobuf SET via Enhanced Mode (WSS)
        if self.coordinator.device_type == DEVICE_TYPE_POWEROCEAN:
            await self._async_set_powerocean_value(value)
            return
        if self.coordinator.device_type == DEVICE_TYPE_STREAM:
            ok = await self._async_set_stream_value(self._definition.key, value)
            if not ok:
                raise_set_failed(self.entity_id)
            self._apply_optimistic_number(value)
            return
        if self.coordinator.device_type == DEVICE_TYPE_STREAM_AC5000:
            ok = await self._async_set_stream_ac5000_value(
                self._definition.key, value
            )
            if not ok:
                raise_set_failed(self.entity_id)
            self._apply_optimistic_number(value)
            return
        if self.coordinator.device_type == DEVICE_TYPE_DELTA3:
            stem = self._port_priority_stem()
            if stem is not None:
                command = self._build_port_priority_command(stem, value)
            else:
                command = build_delta3_number_command(self._definition.key, value)
            if command is None:
                raise_set_unsupported(self.entity_id)
            ok = await self.coordinator.async_send_delta3_set(command)
            if not ok:
                raise_set_failed(self.entity_id)
            self._apply_optimistic_number(value)
            return

        # Smart Plug number commands
        sp_template = SMARTPLUG_NUMBER_COMMANDS.get(self._definition.key)
        if sp_template is not None:
            # App-auth: use protobuf SET (JSON cmdCode only works on /open/ topic)
            if self.coordinator.enhanced_mode:
                ok = await self._async_set_smartplug_proto(self._definition.key, value)
                if not ok:
                    raise_set_failed(self.entity_id)
                self._apply_optimistic_number(value)
                return

            # Standard Mode: JSON cmdCode format
            scale = sp_template.get("scale", 1)
            command = {
                "sn": self.coordinator.device_sn,
                "cmdCode": sp_template["cmdCode"],
                "params": {sp_template["param_key"]: int(value * scale)},
            }
            ok = await self.coordinator.async_send_set_command(command)
            if not ok:
                raise_set_failed(self.entity_id)
            self._apply_optimistic_number(value)
            return

        # Delta uses moduleType/operateType format
        cmd_template = NUMBER_COMMANDS.get(self._definition.key)
        if cmd_template is None:
            raise_set_unsupported(self.entity_id)

        params = {cmd_template["param_key"]: int(value)}
        # Mirror the value into additional param keys (e.g. acChgCfg sends
        # the same watts as slowChgWatts and fastChgWatts, issue #95).
        for extra_key in cmd_template.get("value_params", []):
            params[extra_key] = int(value)
        if "extra_params" in cmd_template:
            params.update(cmd_template["extra_params"])

        command = {
            "moduleType": cmd_template["moduleType"],
            "operateType": cmd_template["operateType"],
            "params": params,
        }

        ok = await self.coordinator.async_send_set_command(command)
        if not ok:
            raise_set_failed(self.entity_id)
        self._apply_optimistic_number(value)

    def _apply_optimistic_number(self, value: float) -> None:
        """Apply optimistic lock: immediately reflect the new value."""
        state_key = self._definition.state_key
        self.coordinator.set_device_value(state_key, value)
        if self.coordinator.data is not None:
            self.coordinator.data[state_key] = value
        self._optimistic_lock_until = time.monotonic() + 5.0
        self._write_state_always(float(value))

    async def _async_set_smartplug_proto(self, key: str, value: float) -> bool:
        """Set a SmartPlug number value via WSS Protobuf (app-auth mode)."""
        sn = self.coordinator.device_sn
        if key == "led_brightness":
            # User sets 0-100%, device expects 0-1023
            raw_brightness = int(round(value * 1023.0 / 100.0))
            payload = build_plug_brightness_payload(raw_brightness, device_sn=sn)
            label = "brightness"
        elif key == "max_watts":
            payload = build_plug_max_watts_payload(int(value), device_sn=sn)
            label = "max_watts"
        else:
            # Nothing was ever sent, so "did not reach the device" would be
            # the wrong thing to tell the user.
            raise_set_unsupported(self.entity_id)
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

        slot = self._schedule_slot()
        if slot is not None:
            try:
                ok = await self.coordinator.async_set_powerocean_schedule_power(
                    slot, int_value
                )
            except DeviceValueNotReported:
                # The write needs four fields the device owns, and composing
                # one would rewrite the recurrence the owner set in the app.
                # Two different situations reach here and they need different
                # words: a schedule the device has stopped reporting is gone
                # and will not come back on its own, while a slot that simply
                # has not been reported yet resolves with the next frame. The
                # armed flag tells them apart, because a retracted slot
                # publishes it as None.
                if not isinstance(
                    self.coordinator.device_data.get(f"schedule_{slot}_enabled"),
                    bool,
                ):
                    raise_set_gone(self.entity_id)
                raise_set_not_ready(self.entity_id)
            except ValueError as err:
                raise_set_rejected(self.entity_id, str(err))
            if not ok:
                raise_set_failed(self.entity_id)
            # The int, not the float Home Assistant handed in: the coordinator
            # has already seeded this key with the int it put on the wire, and
            # re-seeding the same key as a float would leave the type of a
            # stored reading depending on which path last wrote it.
            self._apply_optimistic_number(int_value)
            return

        if self.coordinator.data is None:
            # Both PowerOcean sliders are sent as a pair, so the unchanged
            # one has to be read from coordinator data. Without data there
            # is nothing to send, and silently doing nothing would look
            # like a successful write.
            raise_set_failed(self.entity_id)

        if key == "backup_reserve":
            # Read the current solar-surplus from the user-side mirror
            # (ems_app_surplus_pct = dev_soc), not from the EMS-side
            # ems_backup_ratio_pct which can be clamped/derived. The
            # constraint backup <= solar must hold against the user's
            # actual surplus setting.
            current_solar = int(self.coordinator.data.get("ems_app_surplus_pct", 100))
            backup = int_value
            solar = max(current_solar, backup)  # enforce backup <= solar
            self.coordinator.mark_user_surplus_set()
            ok = await self.coordinator.async_set_powerocean_soc_debounced(backup, solar)
            if not ok:
                raise_set_failed(self.entity_id)
            self._apply_optimistic_number(value)
            return

        if key == "solar_surplus_threshold":
            current_backup = int(self.coordinator.data.get("ems_discharge_lower_limit_pct", 0))
            solar = int_value
            backup = min(current_backup, solar)  # enforce backup <= solar
            self.coordinator.mark_user_surplus_set()
            ok = await self.coordinator.async_set_powerocean_soc_debounced(backup, solar)
            if not ok:
                raise_set_failed(self.entity_id)
            self._apply_optimistic_number(value)
            return

        raise_set_unsupported(self.entity_id)

    def _schedule_slot(self) -> int | None:
        """Return the slot number for a PowerOcean schedule number, else None.

        The device type is checked before the key, as the switch does: the
        write this routes to is a PowerOcean frame, so a key of the same shape
        on another device must not reach it. No other device defines one
        today, and this is here so that adding one cannot quietly change where
        it is sent.
        """
        if self.coordinator.device_type != DEVICE_TYPE_POWEROCEAN:
            return None
        key = self._definition.key
        if not key.startswith("schedule_") or not key.endswith("_power_w"):
            return None
        return int(key[len("schedule_") : -len("_power_w")])

    async def _async_set_stream_value(self, key: str, value: float) -> bool:
        """Set a Stream AC Pro number value via WSS Protobuf SET.

        JSON SET does not work on the /app/ WSS topic (SmartPlug proves
        this). Stream numbers are sent as protobuf ConfigWrite frames.
        """
        if key == "backup_reserve":
            return await self.coordinator.async_set_stream_backup_reserve(
                int(value)
            )
        if key in ("stream_charge_limit", "stream_discharge_limit"):
            try:
                if key == "stream_charge_limit":
                    return await self.coordinator.async_set_stream_soc_limits(
                        charge=int(value)
                    )
                return await self.coordinator.async_set_stream_soc_limits(
                    discharge=int(value)
                )
            except DeviceValueNotReported:
                raise_set_not_ready(self.entity_id)
            except ValueError as err:
                raise_set_rejected(self.entity_id, str(err))
        if key == "led_brightness":
            try:
                return await self.coordinator.async_set_stream_led_brightness(
                    int(value)
                )
            except ValueError as err:
                raise_set_rejected(self.entity_id, str(err))

        # Nothing was ever sent, so "did not reach the device" would be the
        # wrong thing to tell the user.
        raise_set_unsupported(self.entity_id)

    async def _async_set_stream_ac5000_value(self, key: str, value: float) -> bool:
        """Set a STREAM AC 5000 number via a 254/38 config write.

        Most of these read a value the device reported before they can send:
        two of the config fields hold two settings each, and a power setpoint
        is a remove-then-write across two frames. The coordinator owns those
        sequences so they cannot interleave with each other. The grid input
        setpoint is the exception, because the app writes it on its own.
        """
        try:
            if key == "max_charge_soc_pct":
                return await self.coordinator.async_set_stream_ac5000_soc_limits(
                    charge=int(value)
                )
            if key == "min_discharge_soc_pct":
                return await self.coordinator.async_set_stream_ac5000_soc_limits(
                    discharge=int(value)
                )
            if key == "backup_reserve":
                return await self.coordinator.async_set_stream_ac5000_backup_reserve(
                    reserve_pct=int(value)
                )
            if key in ("scheduled_charge_power_w", "scheduled_discharge_power_w"):
                kind = "charge" if key == "scheduled_charge_power_w" else "discharge"
                return await self.coordinator.async_set_stream_ac5000_task_power(
                    kind, int(value)
                )
            if key == "max_grid_output_power_w":
                return (
                    await self.coordinator.async_set_stream_ac5000_grid_output_power(
                        int(value)
                    )
                )
            if key == "max_grid_input_power_w":
                return (
                    await self.coordinator.async_set_stream_ac5000_grid_input_power(
                        int(value)
                    )
                )
        except DeviceValueNotReported:
            # Sending a guessed counterpart would change a setting the user did
            # not touch. Temporary, so not "unsupported".
            raise_set_not_ready(self.entity_id)
        except ValueError as err:
            # The device holds both SoC limits in one field and refuses a pair
            # where they cross. Both entity ranges reach 50, so the user can
            # ask for exactly that.
            raise_set_rejected(self.entity_id, str(err))

        raise_set_unsupported(self.entity_id)


def _get_number_defs(device_type: str, device_sn: str = "") -> list[EcoFlowNumberDef]:
    """Return number definitions based on device type.

    ``device_sn`` gates the STREAM AC 5000 and Stream controls, each confirmed
    on the variants of its family that have produced a write frame of their
    own: both for the STREAM AC 5000, one for the Stream. See
    STREAM_AC5000_CONTROL_PREFIXES and STREAM_CONTROL_PREFIXES. Both gates are
    closed on an empty serial: a write entity that cannot be tied to a
    confirmed model is not created.
    """
    if device_type == DEVICE_TYPE_DELTA:
        return DELTA2MAX_NUMBERS
    if device_type == DEVICE_TYPE_POWEROCEAN:
        return POWEROCEAN_NUMBERS
    if device_type == DEVICE_TYPE_SMARTPLUG:
        return SMARTPLUG_NUMBERS
    if device_type == DEVICE_TYPE_STREAM:
        if not supports_stream_controls(device_sn):
            # An allowlist, not a denylist. `backup_reserve` predates the control
            # gate and exists on every Stream, so removing it would orphan
            # registrations owners already have. Everything else stays out until
            # a capture from that prefix confirms the write, which means a number
            # added to STREAM_NUMBERS later reaches only BK31 until someone
            # deliberately lists it here. Named the other way round, a new control
            # would land on unconfirmed prefixes by default.
            return [
                definition
                for definition in STREAM_NUMBERS
                if definition.key in {"backup_reserve"}
            ]
        return STREAM_NUMBERS
    if device_type == DEVICE_TYPE_DELTA3:
        return DELTA3_NUMBERS
    if device_type == DEVICE_TYPE_STREAM_AC5000:
        if not supports_stream_ac5000_controls(device_sn):
            return []
        return STREAMAC5000_NUMBERS
    return []
