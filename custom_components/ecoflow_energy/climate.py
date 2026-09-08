"""Climate platform for EcoFlow Energy (WAVE 3 portable AC unit).

One climate entity per WAVE 3 device, standing beside the switch, number and
select entities that already control the same settings. It is a second UI
surface over the same underlying state, not a second implementation: the two
can only disagree if one of them caches, so this entity holds no state of
its own (ADR-021 D-C1). Every property reads `self.coordinator.data` on
access, there is no optimistic write and no cached setpoint, and the write
path is the same one the switch platform already uses:
`coordinator.async_send_wave3_set` (plus `async_send_wave3_band` for the
constant-temperature band). The refusal rules live entirely in
`wave3_commands.py`; this entity only renders the reason a write was
refused.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEVICE_TYPE_WAVE3, DOMAIN
from .coordinator import EcoFlowDeviceCoordinator
from .ecoflow.wave3_commands import Wave3WriteRefused
from .entity import (
    EcoFlowWriteGateMixin,
    raise_set_failed,
    raise_set_not_ready,
    raise_set_rejected,
)

_LOGGER = logging.getLogger(__name__)

# How long a bundled gesture waits for the device to report a mode switch
# before it sends the value that switch was bundled with, and how often it
# looks. Measured on hardware 2026-09-07: the device reports a new mode 1.0
# to 2.6 s after the write, and a setpoint that arrives before that report
# is acknowledged and dropped.
MODE_SETTLE_TIMEOUT_S = 6.0
MODE_SETTLE_POLL_S = 0.25

_MODE_TO_HVAC = {
    "cooling": HVACMode.COOL,
    "heating": HVACMode.HEAT,
    "fan": HVACMode.FAN_ONLY,
    "dehumidify": HVACMode.DRY,
    "constant_temp": HVACMode.HEAT_COOL,
}
_HVAC_TO_MODE = {hvac: mode for mode, hvac in _MODE_TO_HVAC.items()}
_MODE_TO_ACTION = {
    "cooling": HVACAction.COOLING,
    "heating": HVACAction.HEATING,
    "fan": HVACAction.FAN,
    "dehumidify": HVACAction.DRYING,
}
FAN_MODES = ["20", "40", "60", "80", "100"]
PRESET_MODES = ["none", "normal", "max", "sleep", "eco"]

# The generic state keys the entity reads, compared as one tuple by the
# write gate - a single-value gate would swallow changes on a multi-attribute
# entity.
_STATE_KEYS = (
    "running",
    "operating_mode",
    "operating_submode",
    "target_temp_c",
    "airflow_speed_pct",
    "target_humidity_pct",
    "temp_ambient_c",
    "humi_ambient_pct",
    "constant_temp_lower_limit_c",
    "constant_temp_upper_limit_c",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the EcoFlow WAVE 3 climate entity from a config entry.

    The platform is forwarded for every config entry; a PowerOcean or Delta
    entry comes out of this with an empty list.
    """
    coordinators: dict[str, EcoFlowDeviceCoordinator] = hass.data[DOMAIN][
        entry.entry_id
    ]
    entities = [
        EcoFlowWave3Climate(coordinator)
        for coordinator in coordinators.values()
        if coordinator.device_type == DEVICE_TYPE_WAVE3
    ]
    async_add_entities(entities)


class EcoFlowWave3Climate(
    EcoFlowWriteGateMixin, CoordinatorEntity[EcoFlowDeviceCoordinator], ClimateEntity
):
    """The WAVE 3 portable AC unit as one climate entity.

    Takes the device name (`_attr_name = None`) - it is the one entity per
    device that does, per ADR-021 D-C2.
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 15.5
    _attr_max_temp = 30.0
    _attr_min_humidity = 40
    _attr_max_humidity = 80
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.FAN_ONLY,
        HVACMode.DRY,
        HVACMode.HEAT_COOL,
    ]
    _attr_fan_modes = FAN_MODES
    _attr_preset_modes = PRESET_MODES
    # Declared once and never changed at runtime (ADR-021): the mode decides
    # which pair of attributes is non-None, not which flags are advertised.
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.TARGET_HUMIDITY
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: EcoFlowDeviceCoordinator) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_sn}_climate"

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.device_available and super().available

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info from coordinator."""
        return self.coordinator.device_info

    def _read(self, key: str) -> Any:
        """Return one coordinator-reported value, or None if not reported yet.

        A plain passthrough, not a cache: the entity holds no state of its
        own (ADR-021 D-C1), so every property calls this on each access
        instead of remembering anything between calls.
        """
        data = self.coordinator.data
        return data.get(key) if data is not None else None

    @property
    def current_temperature(self) -> float | None:
        """Return the ambient temperature."""
        return self._read("temp_ambient_c")

    @property
    def current_humidity(self) -> int | None:
        """Return the ambient humidity, or None if not yet reported."""
        value = self._read("humi_ambient_pct")
        return round(value) if value is not None else None

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return the current mode, OFF when the unit is powered down."""
        if not self._read("running"):
            return HVACMode.OFF
        mode = self._read("operating_mode")
        return _MODE_TO_HVAC.get(mode)

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return what the unit is doing right now.

        OFF when not running, None in constant_temp (nothing on the wire
        says which side of the band it is working - ADR-021 D-C4), else the
        action for the reported mode.

        ADR-021 D-C4 also wants an IDLE action for "on but not running"
        (power on, unit not yet running). That branch is unreachable with
        the state keys this integration currently reads: the only power-side
        reading is `running` itself, so "on" and "running" are the same
        boolean here and there is nothing to distinguish standby from idle.
        Implementing IDLE would mean inventing a key the device has not been
        observed to report, which PC-B's verdict rules out - this stays OFF
        for not-running until a capture shows a distinct reading.
        """
        if not self._read("running"):
            return HVACAction.OFF
        mode = self._read("operating_mode")
        if mode == "constant_temp":
            return None
        return _MODE_TO_ACTION.get(mode)

    @property
    def target_temperature(self) -> float | None:
        """Return the single setpoint, only meaningful in COOL/HEAT."""
        if self.hvac_mode in (HVACMode.COOL, HVACMode.HEAT):
            return self._read("target_temp_c")
        return None

    @property
    def target_temperature_low(self) -> float | None:
        """Return the band's lower limit, only meaningful in HEAT_COOL."""
        if self.hvac_mode == HVACMode.HEAT_COOL:
            return self._read("constant_temp_lower_limit_c")
        return None

    @property
    def target_temperature_high(self) -> float | None:
        """Return the band's upper limit, only meaningful in HEAT_COOL."""
        if self.hvac_mode == HVACMode.HEAT_COOL:
            return self._read("constant_temp_upper_limit_c")
        return None

    @property
    def target_humidity(self) -> int | None:
        """Return the humidity setpoint, only meaningful in DRY."""
        if self.hvac_mode != HVACMode.DRY:
            return None
        value = self._read("target_humidity_pct")
        return round(value) if value is not None else None

    @property
    def fan_mode(self) -> str | None:
        """Return the current fan speed label, or None off the known set."""
        value = self._read("airflow_speed_pct")
        if value is None:
            return None
        label = str(int(value))
        return label if label in FAN_MODES else None

    @property
    def preset_mode(self) -> str | None:
        """Return the current submode label, or None off the known set."""
        value = self._read("operating_submode")
        return value if value in PRESET_MODES else None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        compared = tuple(self._read(key) for key in _STATE_KEYS)
        self._write_state_if_changed(compared)

    async def _send(self, key: str, value: Any, mode: str | None = None) -> None:
        """Send one WAVE 3 setting through the coordinator's single write path.

        `mode` is only set by a gesture that switches mode and writes a
        mode-gated value in the same call (`async_set_temperature` with both
        `hvac_mode` and `temperature`): it overrides the mode `write_refusal`
        checks against, because the mode frame's own ack has not landed in
        accumulated state yet when the setpoint frame is evaluated (E2,
        PLAN-047 Phase C review). No optimistic write follows a success
        either way: the climate entity holds no state of its own (ADR-021
        D-C1). The device confirms the change on its own report stream,
        which reaches this entity through the normal coordinator update
        like any other tick.
        """
        try:
            ok = await self.coordinator.async_send_wave3_set(key, value, mode=mode)
        except Wave3WriteRefused as err:
            raise_set_rejected(self.entity_id, str(err))
        if not ok:
            raise_set_failed(self.entity_id)

    async def _send_band(self, lower: float, upper: float) -> None:
        """Write the constant-temperature band as one ConfigWrite frame.

        Same no-cache discipline as `_send`. The read-back gate is met on
        the push path rather than the write path (ADR-021): the ack only
        echoes the upper limit, but the coordinator's own per-mode upload
        carries both limits, confirming the lower one within 120 s at the
        latest.
        """
        try:
            ok = await self.coordinator.async_send_wave3_band(lower, upper)
        except Wave3WriteRefused as err:
            raise_set_rejected(self.entity_id, str(err))
        if not ok:
            raise_set_failed(self.entity_id)

    async def async_turn_on(self) -> None:
        """Turn the unit on."""
        await self._send("power", True)

    async def async_turn_off(self) -> None:
        """Turn the unit off."""
        await self._send("power", False)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Switch operating mode, powering on first if the unit is off.

        Two frames, sequential, in this order - the app does the same and
        it is the exact case a manual power-off leaves behind: field 4
        (power on) has to land before field 153 (operating_mode) is
        accepted.
        """
        if hvac_mode == HVACMode.OFF:
            if self._read("running"):
                await self._send("power", False)
            return
        if not self._read("running"):
            await self._send("power", True)
        await self._send("operating_mode", _HVAC_TO_MODE[hvac_mode])

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set a single setpoint or the constant-temperature band.

        A mode switch bundled into the same call happens first. When it is
        bundled with a mode-gated setpoint, the target mode is passed
        straight to the write gate (E2, PLAN-047 Phase C review) instead of
        being read back from accumulated state, which would still hold the
        pre-switch mode until the device's next report; the setpoint write
        is still refused if the target mode itself has no such value (a fan
        speed switch has no setpoint, for instance).

        For the band, HA's climate card can call this with only one of the
        two limits; the other side is then read out of accumulated
        coordinator state and sent along, never guessed. If that side has
        not been reported yet the write waits for the next status frame
        rather than guessing (PLAN-047 capture analysis, PC-A).
        """
        target_mode: str | None = None
        if ATTR_HVAC_MODE in kwargs:
            new_mode = kwargs[ATTR_HVAC_MODE]
            if new_mode != self.hvac_mode:
                await self.async_set_hvac_mode(new_mode)
                target_mode = _HVAC_TO_MODE.get(new_mode)
                if target_mode is not None:
                    await self._await_mode(target_mode)

        if ATTR_TARGET_TEMP_LOW in kwargs or ATTR_TARGET_TEMP_HIGH in kwargs:
            lower = kwargs.get(ATTR_TARGET_TEMP_LOW)
            upper = kwargs.get(ATTR_TARGET_TEMP_HIGH)
            if lower is None:
                lower = self._read("constant_temp_lower_limit_c")
            if upper is None:
                upper = self._read("constant_temp_upper_limit_c")
            if lower is None or upper is None:
                raise_set_not_ready(self.entity_id)
            await self._send_band(float(lower), float(upper))
        elif ATTR_TEMPERATURE in kwargs:
            await self._send(
                "target_temp_c", float(kwargs[ATTR_TEMPERATURE]), mode=target_mode
            )

    async def _await_mode(self, mode: str) -> None:
        """Wait for the device to report `mode` before the next write goes out.

        Measured on hardware 2026-09-07: a mode write and a setpoint write
        sent 13 ms apart both reach the device and are both acknowledged,
        and the setpoint is silently dropped. The device applies the mode
        switch first and reports its own stored values for the new mode
        about a second later; a setpoint arriving inside that window is
        lost. The same setpoint sent on its own, with the mode already
        settled, is applied, which is the control that separates a device
        race from a defect in the frame.

        So a bundled call waits for the device's own report of the new
        mode. Observed latency is 1.0 to 2.6 s; the timeout is generous
        against a slow answer and the write is attempted anyway when it
        expires, since a refusal would be worse than a write the device
        may still take.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MODE_SETTLE_TIMEOUT_S
        while loop.time() < deadline:
            if self._read("operating_mode") == mode:
                return
            await asyncio.sleep(MODE_SETTLE_POLL_S)
        _LOGGER.debug(
            "%s did not report mode %s within %.0fs, sending the value anyway",
            self.entity_id,
            mode,
            MODE_SETTLE_TIMEOUT_S,
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the fan speed."""
        await self._send("airflow_speed_pct", int(fan_mode))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the operating submode."""
        await self._send("operating_submode", preset_mode)

    async def async_set_humidity(self, humidity: int) -> None:
        """Set the humidity setpoint."""
        await self._send("target_humidity_pct", float(humidity))
