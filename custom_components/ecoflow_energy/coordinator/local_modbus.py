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
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..const import (
    CONF_UNIT_ID,
    DEVICE_TYPE_DISPLAY_NAMES,
    DEVICE_TYPE_POWEROCEAN,
    DOMAIN,
    LOCAL_COUNTER_HELD_NOTICE_S,
    LOCAL_COUNTER_LABELS,
    LOCAL_MODBUS_FAILURES_UNAVAILABLE,
    LOCAL_MODBUS_HEARTBEAT_INTERVAL_S,
    LOCAL_MODBUS_HEARTBEAT_LAPSE_S,
    LOCAL_MODBUS_OUTAGE_NOTICE_S,
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
)
from ..ecoflow.parsers.powerocean_modbus import (
    FAULT_BLOCKS,
    LIFETIME_COUNTER_KEYS,
    POLL_BLOCKS,
    SETUP_BLOCKS,
    parse_device_info,
    parse_registers,
)
from ..fault_codes import FAULT_CODES_KEY
from .modbus_link import create_link

_LOGGER = logging.getLogger(__name__)

# Modbus exception codes that mean "this device does not offer that read":
# illegal function, illegal data address, illegal data value. Any other code
# (slave busy, device failure, ...) says nothing about the register map.
_REFUSAL_CODES = frozenset({1, 2, 3})

# The two settings the integration may write, by the data key that shows them.
_WRITABLE_KEYS: dict[str, int] = {
    "ems_backup_ratio_pct": BACKUP_RATIO_OFFSET,
    "local_indicator_brightness_pct": BRIGHTNESS_OFFSET,
}

# Seam for the heartbeat's clock. A module attribute rather than a call to
# ``time.monotonic`` so a test can move it without touching the event loop.
_monotonic = time.monotonic
# Same seam for the wall clock that dates a held counter.
_utcnow = dt_util.utcnow

# Window arithmetic of the heartbeat. The unit hands control back after
# LOCAL_MODBUS_HEARTBEAT_LAPSE_S (60 s) without an acknowledged beat and a beat
# is due every LOCAL_MODBUS_HEARTBEAT_INTERVAL_S (15 s), so the last attempt of
# a window starts 45 s after the last ack with 15 s left. A beat may wait for
# the link (a poll on the own client holds it up to 18 s, a number write with
# its read-back up to 27 s) and then needs its own write (up to 9 s own, 12 s
# shared). Its whole wait is therefore capped at what is left of the window
# minus this margin: the last attempt gets 13 s, which fits the shared link's
# 12 s budget. A beat that cannot be acknowledged inside the window fails
# instead of landing after the unit let go, and a window with no more than the
# margin left is treated as lapsed.
_BEAT_WINDOW_MARGIN_S = 2.0


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
        # Counters whose readings are being dropped, by key: when the first
        # drop of the current run happened, the latest dropped reading and the
        # bound it was compared with. In memory only.
        self._held_counters: dict[str, dict[str, Any]] = {}
        # The raw register blocks (offset -> bytes) of the last successful
        # poll, for the diagnostics download. Replaced on every success.
        self._last_raw: dict[int, bytes] = {}
        # The fault events are read apart (see ``FAULT_BLOCKS``). A device that
        # refuses them is not asked again; any other failure is retried.
        self._fault_events_supported = True
        self._fault_read_failing = False
        # The placeholders each open repairs notice was created with, by key, so
        # a poll writes the registry only when the wording would change.
        self._notice_placeholders: dict[str, dict[str, str]] = {}
        # False until the serial was read and matched; reset when the device
        # becomes unavailable, so the next answer is checked again.
        self._identity_verified: bool = False
        self._mismatch_warned: bool = False
        self._device_available: bool = True
        # When the device turned unavailable (``_monotonic`` seconds) and
        # whether this run raised the outage notice for that outage. In memory
        # only, like the held counters: a restart starts a new outage clock.
        self._unavailable_since: float | None = None
        self._outage_notice_raised: bool = False
        self._consecutive_failures: int = 0
        self.last_poll_ok: bool | None = None
        self.last_poll_time: datetime | None = None
        self.last_error: str | None = None
        # Control heartbeat. A plain attribute, never restored: after a restart,
        # a reload or a reconfigure control is off until somebody turns it on.
        self.control_enabled: bool = False
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._last_beat_ack: float = 0.0
        # What the last ``async_set_control`` call asked for. Recorded before
        # the call waits for the lock, so a turn-off that arrives while a
        # turn-on is still writing its first beat wins over it.
        self._control_wanted: bool = False
        # Serialises turn-on calls: one first beat, one heartbeat task.
        self._control_lock = asyncio.Lock()
        # Set when the entry unloads; a turn-on still in flight starts nothing.
        self._closed: bool = False

        super().__init__(
            hass,
            _LOGGER,
            name=f"EcoFlow local {device_log_tag(self.device_sn)}",
            config_entry=entry,
            update_interval=timedelta(seconds=LOCAL_MODBUS_POLL_INTERVAL_S),
        )
        # A beat that outlives the entry would keep a stopped entry in control.
        entry.async_on_unload(self._close)

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

    @property
    def last_raw(self) -> dict[int, bytes]:
        """Return the raw register blocks of the last successful poll."""
        return dict(self._last_raw)

    def held_counters(self, older_than_s: float = 0.0) -> dict[str, dict[str, Any]]:
        """Return the counters held back for at least ``older_than_s`` seconds.

        Each entry carries ``since`` (first drop of the current run),
        ``device_value`` (the latest dropped reading) and ``published`` (the
        bound it was compared with). The result is a copy. The timer lives in
        memory only, so a restart starts every held counter's clock again.
        """
        now = _utcnow()
        return {
            key: dict(entry)
            for key, entry in self._held_counters.items()
            if (now - entry["since"]).total_seconds() >= older_than_s
        }

    def accept_device_counter(self, key: str) -> bool:
        """Take the device's held reading as the new floor for ``key``.

        For a counter the inverter really reset: the floor drops to the held
        device value, the entry is cleared and the device value is published
        at once, so a state write before the next poll shows it rather than
        the value restored at startup. Returns False and changes nothing when
        ``key`` is not held.
        """
        entry = self._held_counters.pop(key, None)
        if entry is None:
            return False
        self._counter_floor[key] = entry["device_value"]
        self._device_data[key] = entry["device_value"]
        self.async_set_updated_data(dict(self._device_data))
        return True

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
        # The intent is recorded before anything is awaited, so the last call
        # to arrive is the one that holds, whatever order the writes finish in.
        self._control_wanted = on
        if not on:
            self._cancel_heartbeat()
            self.async_update_listeners()
            return
        async with self._control_lock:
            if self._closed or not self._control_wanted or self.control_enabled:
                return
            try:
                await self._link.write_register(HEARTBEAT_OFFSET, 1)
            except ModbusLocalError as err:
                raise HomeAssistantError(
                    f"The device did not acknowledge the control heartbeat: {err}"
                ) from err
            # The first beat was awaited: a turn-off or an unload may have
            # arrived meanwhile. The unit then returns control by itself
            # within the lapse time, and no heartbeat starts.
            if self._closed or not self._control_wanted:
                return
            if self._heartbeat_task is not None and not self._heartbeat_task.done():
                return
            self._last_beat_ack = _monotonic()
            self.control_enabled = True
            self._heartbeat_task = self.config_entry.async_create_background_task(
                self.hass,
                self._heartbeat_loop(),
                name=f"ecoflow_energy local heartbeat {self.device_tag}",
            )
            self.async_update_listeners()

    def _close(self) -> None:
        """Unload callback: stop the beats for good and refuse a late turn-on.

        The entry's repairs notices go with it: the held timers live in memory
        only, so a notice left behind would outlive the state that justifies it.
        A notice the user chose Ignore on is kept. The issue is not persistent,
        so after a restart Home Assistant restores it only as an inactive stub
        that still carries the Ignore; raising the notice again later reuses
        that record and the notice comes back ignored. Deleting it here would
        throw the choice away.
        """
        self._closed = True
        self._cancel_heartbeat()
        self._notice_placeholders.clear()
        registry = ir.async_get(self.hass)
        outage = registry.async_get_issue(DOMAIN, self._outage_notice_id())
        if (
            self._outage_notice_raised
            and outage is not None
            and outage.dismissed_version is None
        ):
            ir.async_delete_issue(self.hass, DOMAIN, self._outage_notice_id())
        self._outage_notice_raised = False
        for key in LIFETIME_COUNTER_KEYS:
            issue = registry.async_get_issue(DOMAIN, self._notice_id(key))
            if issue is not None and issue.dismissed_version is None:
                ir.async_delete_issue(self.hass, DOMAIN, self._notice_id(key))

    def _cancel_heartbeat(self) -> None:
        """Stop the beats and show control off. Writes nothing.

        Cancels exactly the task that is registered and drops that reference
        in the same step, so a later task is never mistaken for this one.
        """
        task, self._heartbeat_task = self._heartbeat_task, None
        self._control_wanted = False
        self.control_enabled = False
        if task is not None and not task.done():
            task.cancel()

    async def _heartbeat_loop(self) -> None:
        """Write a beat every interval until one lapses or the task is cancelled."""
        try:
            while True:
                await asyncio.sleep(LOCAL_MODBUS_HEARTBEAT_INTERVAL_S)
                if not await self._beat():
                    return
        except Exception as err:  # noqa: BLE001
            # Anything outside the mapped error family must not end the task
            # silently with the switch still showing on. Only the type is
            # named: the text of a socket error may carry the device address.
            _LOGGER.debug(
                "Local Modbus heartbeat for %s ended on an unexpected error",
                self.device_tag,
                exc_info=True,
            )
            self._heartbeat_lapsed(
                f"an unexpected {type(err).__name__} ended the heartbeat"
            )

    def _window_left(self) -> float:
        """Return the seconds left until the unit lets go, counted from the last ack."""
        return LOCAL_MODBUS_HEARTBEAT_LAPSE_S - (_monotonic() - self._last_beat_ack)

    async def _beat(self) -> bool:
        """Write one beat. Return False once the heartbeat has stopped."""
        # Nothing is written to a device that stopped answering its polls or
        # that has not passed the serial number check; the switch could not
        # even be turned off through the service while it is unavailable.
        if not self._device_available or not self._identity_verified:
            self._heartbeat_lapsed(
                "the device is not answering its polls or failed the serial "
                "number check"
            )
            return False
        # Checked before the write: a beat acknowledged after the unit let go
        # would take control again without anybody asking for it.
        if self._window_left() <= _BEAT_WINDOW_MARGIN_S:
            self._heartbeat_lapsed(
                f"no beat acknowledged for {LOCAL_MODBUS_HEARTBEAT_LAPSE_S} s"
            )
            return False
        try:
            # The wait for the link counts: see _BEAT_WINDOW_MARGIN_S.
            async with asyncio.timeout(self._window_left() - _BEAT_WINDOW_MARGIN_S):
                await self._link.write_register(HEARTBEAT_OFFSET, 1)
        except ModbusExceptionResponse as err:
            self._heartbeat_lapsed(f"the device refused a beat ({err})")
            return False
        except (ModbusLocalError, TimeoutError) as err:
            # Retried on the next tick; only a full window without an
            # acknowledged beat ends it.
            _LOGGER.debug(
                "Local Modbus heartbeat beat failed for %s: %s",
                self.device_tag,
                type(err).__name__,
            )
            if self._window_left() <= _BEAT_WINDOW_MARGIN_S:
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
        app; it is not taken again until somebody turns control on. A loop
        that is no longer the registered task (replaced or cancelled) changes
        nothing: it must not null the reference to, or switch off, the live one.
        """
        if self._heartbeat_task is not asyncio.current_task():
            return
        self._heartbeat_task = None
        self._control_wanted = False
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
        # The register is decoded by the poll's own parser, so a read-back can
        # never publish what a poll would not.
        held = parse_registers({offset: raw[offset]}).get(key)
        if held is None:
            raise HomeAssistantError(f"The device returned no usable value for {key}")
        # What the device holds is shown whether or not it is what was asked for.
        self._device_data[key] = held
        self.async_set_updated_data(dict(self._device_data))
        if held != value:
            raise HomeAssistantError(
                f"The device holds {key} = {held} after writing {value}"
            )

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
        fault_count = parsed.get("fault_count")
        if fault_count:
            # Only a device that reports a fault is asked for the event list:
            # a healthy one costs no extra connection and no unprobed read.
            raw = {**raw, **await self._read_fault_events()}
            parsed = parse_registers(raw)
        elif fault_count == 0:
            parsed[FAULT_CODES_KEY] = []
        self._last_raw = raw
        if FAULT_CODES_KEY not in parsed:
            # Unknown beats a list from an earlier poll that may be out of date.
            self._device_data.pop(FAULT_CODES_KEY, None)
        self._hold_counters(parsed)
        self._device_data.update(parsed)
        self._sync_held_notices()
        return dict(self._device_data)

    async def _read_fault_events(self) -> dict[int, bytes]:
        """Read the fault event registers; a failure costs the list, never the poll."""
        if not self._fault_events_supported:
            return {}
        try:
            events = await self._link.read_blocks(FAULT_BLOCKS)
        except ModbusLocalError as err:
            if isinstance(err, ModbusExceptionResponse) and err.code in _REFUSAL_CODES:
                self._fault_events_supported = False
                _LOGGER.debug(
                    "Local Modbus %s refused the fault event registers, the "
                    "fault list stays empty: %s",
                    self.device_tag,
                    err,
                )
                return {}
            if not self._fault_read_failing:
                self._fault_read_failing = True
                _LOGGER.debug(
                    "Local Modbus %s: fault events not read, trying again: %s",
                    self.device_tag,
                    err,
                )
            return {}
        self._fault_read_failing = False
        return events

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
            self._unavailable_since = _monotonic()
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
        self._sync_outage_notice()
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
        self._unavailable_since = None
        if self._outage_notice_raised:
            self._outage_notice_raised = False
            ir.async_delete_issue(self.hass, DOMAIN, self._outage_notice_id())
        if recovered:
            _LOGGER.info("Local Modbus device %s is answering again", self.device_tag)

    def _notice_id(self, key: str) -> str:
        """Return the repairs issue id of ``key``; the entry id, never the serial."""
        return f"local_counter_held_{self.config_entry.entry_id}_{key}"

    def _outage_notice_id(self) -> str:
        """Return the repairs issue id of the outage notice, never the serial."""
        return (
            f"local_device_not_answering_{self.config_entry.entry_id}_{self.device_tag}"
        )

    def _sync_outage_notice(self) -> None:
        """Raise one repairs notice once the device has been silent long enough.

        Called from every failed poll, so the notice appears within one poll of
        the threshold and is written once per outage. It is deleted only by the
        poll that finds the device answering again, and only when this run
        raised it: the same stub rule as ``_sync_held_notices``, no sweep of an
        id this run never raised (after a restart Home Assistant keeps such an
        issue as an inactive stub that carries the user's Ignore).
        """
        since = self._unavailable_since
        if (
            self._device_available
            or since is None
            or self._outage_notice_raised
            or _monotonic() - since < LOCAL_MODBUS_OUTAGE_NOTICE_S
        ):
            return
        self._outage_notice_raised = True
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._outage_notice_id(),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="local_device_not_answering",
            translation_placeholders={"device": self.device_tag},
        )

    def _sync_held_notices(self) -> None:
        """Raise a repairs notice per counter held 24 h, drop it once it is not.

        The notice offers to take the inverter's value (``repairs.py``);
        Home Assistant's own Ignore is the choice to keep the current total.
        A notice is deleted only when this run raised it and the counter has
        since recovered or been accepted. There is no sweep of ids this run
        never raised: after a restart Home Assistant holds such an issue as an
        inactive stub that carries the user's Ignore, and deleting the stub
        would bring the notice back as new 24 h later.
        """
        held = self.held_counters(LOCAL_COUNTER_HELD_NOTICE_S)
        for key in LIFETIME_COUNTER_KEYS:
            entry = held.get(key)
            if entry is None:
                if self._notice_placeholders.pop(key, None) is not None:
                    ir.async_delete_issue(self.hass, DOMAIN, self._notice_id(key))
                continue
            # The device value shows one decimal: after a real reset it moves
            # with every poll, and three decimals would rewrite the notice
            # each time. The held value (ha_value) is steady, so it keeps three.
            placeholders = {
                "counter": LOCAL_COUNTER_LABELS.get(key, key),
                "ha_value": f"{entry['published']:.3f}",
                "device_value": f"{entry['device_value']:.1f}",
            }
            if self._notice_placeholders.get(key) == placeholders:
                continue
            self._notice_placeholders[key] = placeholders
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._notice_id(key),
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="local_counter_held",
                translation_placeholders=placeholders,
                data={"entry_id": self.config_entry.entry_id, "key": key},
            )

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
                self._held_counters.pop(key, None)
                continue
            held = self._held_counters.get(key)
            self._held_counters[key] = {
                "since": held["since"] if held is not None else _utcnow(),
                "device_value": new,
                "published": lowest,
            }
            _LOGGER.debug(
                "Local Modbus %s: ignoring %s reading %s below %s",
                self.device_tag,
                key,
                new,
                lowest,
            )
            del parsed[key]
