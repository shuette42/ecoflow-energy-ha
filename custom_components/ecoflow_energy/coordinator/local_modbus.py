"""Coordinator for a three-phase PowerOcean over local Modbus/TCP.

Credential-free: one poll every ``LOCAL_MODBUS_POLL_INTERVAL_S``, no MQTT, no
HTTP and no energy integration. The cloud coordinator's mixins are deliberately
not used, because they carry an MQTT liveness head, cloud-only resolvers and an
energy integrator, none of which applies to a device that reports its own
lifetime counters.

Control is a separate, explicit step. ``async_set_control`` starts or stops the
control heartbeat (register 0x025F, a beat every
``LOCAL_MODBUS_HEARTBEAT_INTERVAL_S``); the unit hands control back to the app
once ``LOCAL_MODBUS_HEARTBEAT_LAPSE_S`` pass without an acknowledged beat, and
the coordinator then stops beating, reports control off and warns once.
``control_enabled`` starts False on every entry start and is never restored.
``async_write_register`` writes one of the two writable settings and confirms it
only by reading the register back. The connection (shared or own) comes from
``create_link``; only allowlisted registers are ever written.

Availability follows the poll: failures 1 to 4 keep the last data, the fifth
(``LOCAL_MODBUS_FAILURES_UNAVAILABLE``) in a row marks the device unavailable,
and one success restores it. An entry without credentials never starts a reauth.

The device's serial number is read on the first poll and again on the first
poll after the device was unavailable. A different serial (another device at
the same address, or a changed unit id) is a failed poll, never data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..const import (
    CONF_UNIT_ID,
    DEVICE_TYPE_DISPLAY_NAMES,
    DEVICE_TYPE_POWEROCEAN,
    DOMAIN,
    LOCAL_MODBUS_FAILURES_UNAVAILABLE,
    LOCAL_MODBUS_HEARTBEAT_INTERVAL_S,
    LOCAL_MODBUS_HEARTBEAT_LAPSE_S,
    LOCAL_MODBUS_POLL_INTERVAL_S,
)
from ..ecoflow.const import device_log_tag, get_device_name
from ..ecoflow.modbus_local import (
    BACKUP_RATIO_OFFSET,
    BRIGHTNESS_OFFSET,
    HEARTBEAT_OFFSET,
    MODBUS_DEFAULT_PORT,
    ModbusExceptionResponse,
    ModbusLocalError,
    ModbusTransport,
    decode_u16,
)
from ..ecoflow.parsers.powerocean_modbus import (
    LIFETIME_COUNTER_KEYS,
    POLL_BLOCKS,
    SETUP_BLOCKS,
    parse_device_info,
    parse_registers,
)
from .modbus_link import create_link

_LOGGER = logging.getLogger(__name__)

# The two settings the integration may write, by the data key that shows them.
_WRITABLE_KEYS: dict[str, int] = {
    "ems_backup_ratio_pct": BACKUP_RATIO_OFFSET,
    "local_indicator_brightness_pct": BRIGHTNESS_OFFSET,
}

# Seam for the heartbeat's clock. A module attribute rather than a call to
# ``time.monotonic`` so a test can move it without touching the event loop.
_monotonic = time.monotonic


class _SerialMismatchError(ModbusLocalError):
    """The device at the configured address reports another serial number."""


class EcoFlowLocalModbusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll one PowerOcean over Modbus/TCP and publish the decoded readings."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: dict[str, Any],
        client: ModbusTransport | None = None,
    ) -> None:
        """Initialize the coordinator for the device named in ``device_info``.

        ``client`` is a test seam. Without it the link comes from
        ``create_link``; a ``HomeAssistantError`` from there (the device is
        already held on the shared connection with other link settings)
        propagates and fails the setup with that reason.
        """
        self.device_sn: str = device_info["sn"]
        self.device_type: str = DEVICE_TYPE_POWEROCEAN
        product_name = device_info.get("product_name") or ""
        display_name = DEVICE_TYPE_DISPLAY_NAMES[DEVICE_TYPE_POWEROCEAN]
        self.device_name: str = (
            device_info.get("name")
            or get_device_name(product_name, self.device_sn)
            or display_name
        )
        self.product_name: str = (
            product_name
            or get_device_name(product_name, self.device_sn)
            or display_name
        )
        self._sw_version: str = device_info.get("sw_version", "")

        self.host: str = entry.data[CONF_HOST]
        self.port: int = entry.data.get(CONF_PORT, MODBUS_DEFAULT_PORT)
        self.unit_id: int = entry.data.get(CONF_UNIT_ID, 1)
        self._link: ModbusTransport = (
            client
            if client is not None
            else create_link(hass, entry, self.host, self.port, self.unit_id)
        )

        # Accumulated readings; a poll only overwrites the keys it delivered.
        self._device_data: dict[str, Any] = {}
        # Lower bound per lifetime counter, taken from the restored sensor state.
        self._counter_floor: dict[str, float] = {}
        # False until the serial was read and matched; reset when the device
        # becomes unavailable, so the next answer is checked again.
        self._identity_verified: bool = False
        self._mismatch_warned: bool = False
        self._device_available: bool = True
        self._consecutive_failures: int = 0
        self.last_poll_ok: bool | None = None
        self.last_poll_time: datetime | None = None
        self.last_error: str | None = None
        # Control heartbeat. A plain attribute, never restored: after a restart,
        # a reload or a reconfigure control is off until somebody turns it on.
        self.control_enabled: bool = False
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._last_beat_ack: float = 0.0

        super().__init__(
            hass,
            _LOGGER,
            name=f"EcoFlow local {device_log_tag(self.device_sn)}",
            config_entry=entry,
            update_interval=timedelta(seconds=LOCAL_MODBUS_POLL_INTERVAL_S),
        )
        # A beat that outlives the entry would keep a stopped entry in control.
        entry.async_on_unload(self._cancel_heartbeat)

    # ------------------------------------------------------------------
    # Surface the sensor platform reads
    # ------------------------------------------------------------------

    @property
    def device_tag(self) -> str:
        """Return the one-way log tag for this device."""
        return device_log_tag(self.device_sn)

    @property
    def device_data(self) -> dict[str, Any]:
        """Return the accumulated device readings."""
        return self._device_data

    @property
    def device_available(self) -> bool:
        """Return False after repeated failed polls."""
        return self._device_available

    @property
    def consecutive_failures(self) -> int:
        """Return the number of failed polls in a row."""
        return self._consecutive_failures

    @property
    def enhanced_mode(self) -> bool:
        """Local mode is neither Standard nor Enhanced."""
        return False

    @property
    def connection_mode(self) -> str:
        """Return the value of the connection mode diagnostic sensor."""
        return "local"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info; identifiers match the cloud device exactly."""
        info = DeviceInfo(
            identifiers={(DOMAIN, self.device_sn)},
            manufacturer="EcoFlow",
            model=self.product_name,
            name=f"EcoFlow {self.device_name}",
        )
        if self._sw_version:
            info["sw_version"] = self._sw_version
        return info

    def seed_energy_total(self, key: str, total_kwh: float) -> None:
        """Take a restored counter value as the lowest the device may publish.

        Nothing is integrated here, but a restored value is the best memory of
        where the device's lifetime counter stood. A reading below it (a bad
        read after a restart, when no earlier reading exists to compare with)
        is not published: the entity falls back to the restored value until the
        device passes it. The first poll runs before the entities are added,
        so a value it already published below the floor is withdrawn too.
        """
        if key not in LIFETIME_COUNTER_KEYS:
            return
        floor = max(total_kwh, self._counter_floor.get(key, 0.0))
        self._counter_floor[key] = floor
        published = self._device_data.get(key)
        if published is not None and published < floor:
            del self._device_data[key]
            if self.data is not None:
                self.data.pop(key, None)

    # ------------------------------------------------------------------
    # Control: heartbeat and writes
    # ------------------------------------------------------------------

    async def async_set_control(self, on: bool) -> None:
        """Start or stop sending the control heartbeat.

        Turning on writes the first beat and reports on only when the device
        acknowledged it; a failure raises ``HomeAssistantError`` and leaves
        control off. Turning off stops the beats and writes nothing: the unit
        returns control to the app by itself within the lapse time.
        """
        if not on:
            self._cancel_heartbeat()
            self.async_update_listeners()
            return
        if self.control_enabled:
            return
        try:
            await self._link.write_register(HEARTBEAT_OFFSET, 1)
        except ModbusLocalError as err:
            raise HomeAssistantError(
                f"The device did not acknowledge the control heartbeat: {err}"
            ) from err
        self._last_beat_ack = _monotonic()
        self.control_enabled = True
        self._heartbeat_task = self.config_entry.async_create_background_task(
            self.hass,
            self._heartbeat_loop(),
            name=f"ecoflow_energy local heartbeat {self.device_tag}",
        )
        self.async_update_listeners()

    def _cancel_heartbeat(self) -> None:
        """Stop the beats and show control off. Writes nothing."""
        task, self._heartbeat_task = self._heartbeat_task, None
        self.control_enabled = False
        if task is not None and not task.done():
            task.cancel()

    async def _heartbeat_loop(self) -> None:
        """Write a beat every interval until one lapses or the task is cancelled."""
        while True:
            await asyncio.sleep(LOCAL_MODBUS_HEARTBEAT_INTERVAL_S)
            if not await self._beat():
                return

    async def _beat(self) -> bool:
        """Write one beat. Return False once the heartbeat has stopped."""
        try:
            await self._link.write_register(HEARTBEAT_OFFSET, 1)
        except ModbusExceptionResponse as err:
            self._heartbeat_lapsed(f"the device refused a beat ({err})")
            return False
        except ModbusLocalError as err:
            # Retried on the next tick; only a full lapse window without an
            # acknowledged beat ends it.
            _LOGGER.debug(
                "Local Modbus heartbeat beat failed for %s: %s", self.device_tag, err
            )
            if _monotonic() - self._last_beat_ack >= LOCAL_MODBUS_HEARTBEAT_LAPSE_S:
                self._heartbeat_lapsed(
                    f"no beat acknowledged for {LOCAL_MODBUS_HEARTBEAT_LAPSE_S} s"
                )
                return False
            return True
        self._last_beat_ack = _monotonic()
        return True

    def _heartbeat_lapsed(self, reason: str) -> None:
        """Show control off after the heartbeat stopped by itself, with one WARNING.

        Runs inside the heartbeat task, which returns right after, so the task
        is not cancelled from here. The device has handed control back to the
        app; it is not taken again until somebody turns control on.
        """
        self._heartbeat_task = None
        self.control_enabled = False
        _LOGGER.warning(
            "Local Modbus %s: control stopped, %s. The device returns control "
            "to the app; turn Modbus control on again to take it back",
            self.device_tag,
            reason,
        )
        self.async_update_listeners()

    async def async_write_register(self, key: str, value: int) -> None:
        """Write a setting and apply it only once the device confirms it.

        The write is followed by a read of the same register. If the device
        holds another value, the action fails and the entity keeps showing what
        the device holds. There is no optimistic state.
        """
        offset = _WRITABLE_KEYS.get(key)
        if offset is None:
            raise ValueError(f"{key} is not a writable local setting")
        if not 0 <= value <= 100:
            raise ValueError(f"{key} must be between 0 and 100, got {value}")
        try:
            await self._link.write_register(offset, value)
            raw = await self._link.read_blocks([(offset, 1)])
        except ModbusLocalError as err:
            raise HomeAssistantError(
                f"The device did not take {key} = {value}: {err}"
            ) from err
        held = decode_u16(raw[offset])
        if held != value:
            raise HomeAssistantError(
                f"The device holds {key} = {held} after writing {value}"
            )
        self._device_data[key] = held
        self.async_set_updated_data(dict(self._device_data))

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Read the register blocks and fold them into the accumulated data."""
        # The identity blocks ride along until the serial was read and matched.
        blocks = POLL_BLOCKS if self._identity_verified else SETUP_BLOCKS + POLL_BLOCKS
        try:
            raw = await self._link.read_blocks(blocks)
        except ModbusLocalError as err:
            return self._handle_failure(err)

        if not self._identity_verified:
            info = parse_device_info(raw)
            serial = info.get("serial", "")
            if serial != self.device_sn:
                return self._handle_failure(
                    _SerialMismatchError(
                        "serial mismatch: the device at this address reports a "
                        f"serial starting with {serial[:4] or '(none)'}"
                    )
                )
            self._identity_verified = True
            self._sw_version = info.get("firmware") or self._sw_version

        self._handle_success()
        parsed = parse_registers(raw)
        self._hold_counters(parsed)
        self._device_data.update(parsed)
        return dict(self._device_data)

    def _handle_failure(self, err: ModbusLocalError) -> dict[str, Any]:
        """Count a failed poll; mark the device unavailable on the fifth in a row."""
        self._consecutive_failures += 1
        self.last_poll_ok = False
        self.last_poll_time = dt_util.utcnow()
        self.last_error = f"{type(err).__name__}: {err}"

        if not self._device_data:
            # Never delivered anything: HA turns this into ConfigEntryNotReady
            # on the first refresh and retries with its own backoff.
            raise UpdateFailed(
                f"Local Modbus device {self.device_tag} not reachable: {err}"
            ) from err

        if isinstance(err, _SerialMismatchError) and not self._mismatch_warned:
            # A configuration problem the user must act on, so it is worth a
            # WARNING, once. The first poll of an entry takes the UpdateFailed
            # path above, which Home Assistant reports itself.
            self._mismatch_warned = True
            _LOGGER.warning(
                "Local Modbus %s: %s. Check host, port and unit id, or remove "
                "the entry and add it again for the right device",
                self.device_tag,
                err,
            )

        if self._consecutive_failures < LOCAL_MODBUS_FAILURES_UNAVAILABLE:
            _LOGGER.debug(
                "Local Modbus poll failed for %s (%d in a row): %s",
                self.device_tag,
                self._consecutive_failures,
                err,
            )
        elif self._device_available:
            self._device_available = False
            # Whatever answers next is checked against the entry's serial again.
            self._identity_verified = False
            if isinstance(err, ModbusExceptionResponse):
                _LOGGER.warning(
                    "Local Modbus device %s rejected a read (%s). The register "
                    "map may have changed with a firmware update. If this "
                    "continues, report it with the diagnostics download",
                    self.device_tag,
                    err,
                )
            elif not isinstance(err, _SerialMismatchError):
                _LOGGER.warning(
                    "Local Modbus device %s is not answering (%s). Check that "
                    "Modbus is enabled on the device (EcoFlow support enables "
                    "it) and that no other Modbus client holds its single "
                    "connection",
                    self.device_tag,
                    err,
                )
        else:
            _LOGGER.debug(
                "Local Modbus device %s still not answering (%d in a row): %s",
                self.device_tag,
                self._consecutive_failures,
                err,
            )
        # Keep serving the last data: before the threshold so entities do not
        # flicker, after it because availability alone carries the state.
        return dict(self._device_data)

    def _handle_success(self) -> None:
        """Reset the failure count and restore availability."""
        recovered = not self._device_available
        self._consecutive_failures = 0
        self._device_available = True
        self._mismatch_warned = False
        self.last_poll_ok = True
        self.last_poll_time = dt_util.utcnow()
        self.last_error = None
        if recovered:
            _LOGGER.info("Local Modbus device %s is answering again", self.device_tag)

    def _hold_counters(self, parsed: dict[str, Any]) -> None:
        """Drop a counter reading that is lower than the lowest it may be.

        A lifetime counter only grows. A lower value is a bad read, and
        publishing it would register as a meter reset in the statistics. The
        bound is the last published value or, if that is higher, the restored
        value from ``seed_energy_total``: right after a restart there is no
        last published value, and the restored one is all there is to compare.
        """
        for key in LIFETIME_COUNTER_KEYS:
            new = parsed.get(key)
            if new is None:
                continue
            last = self._device_data.get(key)
            lowest = max(
                last if last is not None else 0.0,
                self._counter_floor.get(key, 0.0),
            )
            if new >= lowest:
                continue
            _LOGGER.debug(
                "Local Modbus %s: ignoring %s reading %s below %s",
                self.device_tag,
                key,
                new,
                lowest,
            )
            del parsed[key]
