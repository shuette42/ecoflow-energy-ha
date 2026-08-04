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
    DOMAIN,
    EcoFlowNumberDef,
    filter_defs_for_serial,
    NUMBER_COMMANDS,
    POWEROCEAN_NUMBERS,
    SMARTPLUG_NUMBER_COMMANDS,
    SMARTPLUG_NUMBERS,
    STREAM_NUMBERS,
)
from .coordinator import EcoFlowDeviceCoordinator
from .entity import EcoFlowWriteGateMixin, raise_set_failed, raise_set_unsupported
from .ecoflow.delta3_commands import (
    build_number_command as build_delta3_number_command,
    build_port_priority_command,
    port_priority_soc_bounds,
)
from .ecoflow.energy_stream import build_stream_backup_reserve_payload
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
            _get_number_defs(coordinator.device_type), coordinator.device_sn
        )
        for defn in defs:
            if defn.enhanced_only and not coordinator.enhanced_mode:
                continue
            entities.append(EcoFlowNumber(coordinator, defn))

    async_add_entities(entities)


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
        """Return the lower bound, narrowed for port priority cutoffs."""
        if self._port_priority_stem() is None:
            return self._attr_native_min_value
        return float(self._port_priority_bounds()[0])

    @property
    def native_max_value(self) -> float:
        """Return the upper bound, narrowed for port priority cutoffs."""
        if self._port_priority_stem() is None:
            return self._attr_native_max_value
        return float(self._port_priority_bounds()[1])

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
        of changing a threshold.
        """
        limited_key, _ = port_priority_keys(stem)
        limited = self.coordinator.data.get(limited_key)
        if not isinstance(limited, bool):
            _LOGGER.debug(
                "Port priority write for %s skipped - port state not reported yet",
                self.entity_id,
            )
            return None
        lower, upper = self._port_priority_bounds()
        clamped = max(lower, min(upper, int(round(value))))
        return build_port_priority_command(stem, limited, clamped)

    def _port_priority_bounds(self) -> tuple[int, int]:
        """Return the cutoff bounds derived from the device's battery limits."""
        data = self.coordinator.data or {}
        return port_priority_soc_bounds(
            data.get("max_charge_soc_pct"), data.get("min_discharge_soc_pct")
        )

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

    async def _async_set_stream_value(self, key: str, value: float) -> bool:
        """Set a Stream AC Pro number value via WSS Protobuf SET.

        JSON SET does not work on the /app/ WSS topic (SmartPlug proves
        this). Stream numbers are sent as protobuf ConfigWrite frames.
        """
        if key == "backup_reserve":
            payload = build_stream_backup_reserve_payload(
                int(value), self.coordinator.device_sn
            )
            return await self.coordinator.async_send_proto_set_command(
                payload, label="stream_backup_reserve"
            )

        # Nothing was ever sent, so "did not reach the device" would be the
        # wrong thing to tell the user.
        raise_set_unsupported(self.entity_id)


def _get_number_defs(device_type: str) -> list[EcoFlowNumberDef]:
    """Return number definitions based on device type."""
    if device_type == DEVICE_TYPE_DELTA:
        return DELTA2MAX_NUMBERS
    if device_type == DEVICE_TYPE_POWEROCEAN:
        return POWEROCEAN_NUMBERS
    if device_type == DEVICE_TYPE_SMARTPLUG:
        return SMARTPLUG_NUMBERS
    if device_type == DEVICE_TYPE_STREAM:
        return STREAM_NUMBERS
    if device_type == DEVICE_TYPE_DELTA3:
        return DELTA3_NUMBERS
    return []
