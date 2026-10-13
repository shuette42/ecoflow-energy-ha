"""SET command dispatch for the EcoFlow device coordinator."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast

from homeassistant.exceptions import HomeAssistantError

from ..const import (
    AUTH_METHOD_APP,
    DEVICE_TYPE_OCEAN2,
    DOMAIN,
    POWERGLOW_WRITE_WINDOW_S,
    POWEROCEAN_SCHEDULE_PREFIXES,
    POWEROCEAN_SOC_DEBOUNCE_S,
    POWEROCEAN_SOC_STATE_KEYS,
    POWERPULSE2_CHARGE_ACTION_PRECONDITION,
    POWERPULSE2_CHARGE_ACTION_WINDOW_S,
    POWERPULSE2_CHARGE_CURRENT_WINDOW_S,
    POWERPULSE2_CHARGE_MODE_OPTIONS,
    POWERPULSE2_CHARGE_MODE_WIRE,
    POWERPULSE2_MAX_CURRENT_RANGE_A,
    POWERPULSE2_MAX_CURRENT_WINDOW_S,
    POWERPULSE2_PHASE_SETTING_OPTIONS,
    POWERPULSE2_PHASE_SETTING_WIRE,
    POWERPULSE2_SETTINGS_FRESH_WAIT_S,
    POWERPULSE2_SETTINGS_MAX_AGE_S,
    POWERPULSE2_SETTINGS_WINDOW_S,
    supports_ocean2_controls,
)
from ..ecoflow.const import (
    POWEROCEAN_FEED_SCHEDULE_POWER_MIN_W,
    POWEROCEAN_SCHEDULE_POWER_STEP_W,
    POWERPULSE2_DISPLAY_BRIGHTNESS_STEPS,
    POWERPULSE2_DISPLAY_KEYS,
    POWERPULSE2_DISPLAY_SWITCH_KEYS,
    powerpulse2_display_value_valid,
    schedule_power_max_w,
    schedule_power_min_w,
)
from ..ecoflow.frame_capture import sanitize_frame
from ..entity import as_known_int

_LOGGER = logging.getLogger(__name__)

# Marks a value that was absent from the store rather than present and None.
# The accessory platforms read a present key as a reading the device has sent,
# so a rollback has to be able to put absence back.
_UNSET = object()

# Ocean 2 read-back (Backup Reserve, PLAN-151; Storm Guard, PLAN-192). The read
# before a reserve write waits at most 5 s for a get-all answer; the follow-up
# requests go out 8 s and 30 s after either write and the pre-write value comes
# back at 35 s if no snapshot carried the written key since the 8 s request.
# The apply latency measured on the device is up to about 5 s, which the 8 s
# request clears.
_OCEAN2_SNAPSHOT_TIMEOUT_S = 5.0
_OCEAN2_READBACK_REQUEST_S = 8.0
_OCEAN2_READBACK_RETRY_S = 30.0
_OCEAN2_READBACK_EXPIRE_S = 35.0
# An acknowledgement within this long after our own write is attributed to it.
# The set_reply topic is shared with the vendor app, same rule as the WAVE 3.
_OCEAN2_OWN_WRITE_WINDOW_S = 30.0


@dataclass
class _Ocean2Write:
    """One Ocean 2 write (Backup Reserve, Storm Guard) while its read-back is pending.

    Created on the event loop right before the publish and read on the Paho
    thread by the acknowledgement check (`pre`, `sent_at`, `parse_ack`): a
    plain reference read, safe under CPython's GIL. `state_key` is the store
    key the write moves and the read-back must report, `pre` the device's own
    value of it from before the write (`_UNSET` when the store had none), the
    value a missing read-back or a refusal puts back. `parse_ack` reads this
    write's own acknowledgement out of a `set_reply` payload (`None` for a
    reply that answers something else), `label` and `event_detail` name the
    write in the log and the event log. `snapshot_seen` means a snapshot
    carrying `state_key` arrived since the 8 s request. `reported` means a
    snapshot carrying `state_key` arrived after this write's value was seeded,
    so the store holds a value the unit reported and no longer this write's
    seed; it is cleared once at seeding and never reset afterwards.
    `rejected` means the device refused this write, which can be known before
    the write's own coroutine has resumed from the publish (event loop only).
    """

    state_key: str
    pre: Any
    written: Any
    sent_at: float
    parse_ack: Callable[[bytes], bool | None]
    label: str
    event_detail: str
    handles: list[asyncio.TimerHandle] = field(default_factory=list)
    snapshot_seen: bool = False
    reported: bool = False
    rejected: bool = False

    def is_recent(self, now: float) -> bool:
        """Whether an acknowledgement arriving at `now` can be ours."""
        return now - self.sent_at <= _OCEAN2_OWN_WRITE_WINDOW_S

    def cancel_handles(self) -> None:
        for handle in self.handles:
            handle.cancel()
        self.handles.clear()


@dataclass(frozen=True)
class _SettingsWrite:
    """What one PowerPulse 2 settings write sends and waits for (PLAN-172).

    `fields` is the `EDevPileParamSet` write field number -> value map the
    payload builder takes, `expected_value` what the wallbox must report back
    on the write's state key, and `bit_mask` (switch-bits writes only) which
    bits of that reading count as the confirmation. `display` (PLAN-188) is
    the four known bytes of the display block, `(LED on, Screen on, LED
    brightness %, Screen brightness %)`: a display write sends that block
    alone through its own builder and leaves `fields` empty.
    """

    fields: dict[int, int]
    expected_value: float
    bit_mask: int | None = None
    display: tuple[int, int, int, int] | None = None

    def __post_init__(self) -> None:
        if self.display is not None and self.fields:
            raise ValueError("a display write sends the display block alone")
        if self.display is None and not self.fields:
            raise ValueError("a settings write needs fields or a display block")


class _SettingsReportNotFresh(HomeAssistantError):
    """A settings write found no usable settings report yet (#495).

    Raised by a write's `plan` when the latest settings report is missing or
    older than `POWERPULSE2_SETTINGS_MAX_AGE_S`, the two cases where the next
    report fixes it. `_async_write_powerpulse_settings` waits a bounded time
    for that report and raises this same error, with its own translation
    key, only when none arrives.
    """


def _describe_amps(value: Any) -> str:
    """A reported current for an exception text, or that none arrived yet."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g} A"
    return "no value yet"


def _describe_switch_bit(bits: Any, mask: int) -> str:
    """One reported settings switch bit as on/off, or no value yet."""
    if type(bits) is not int:
        return "no value yet"
    return "on" if bits & mask else "off"


# PLAN-188: the name the refusal text gives each display value, in the byte
# order of the display block (`POWERPULSE2_DISPLAY_KEYS`: `ev_led_enabled` is
# byte 0 ... `ev_screen_brightness_pct` byte 3).
_POWERPULSE2_DISPLAY_BYTES: tuple[tuple[str, str], ...] = tuple(
    zip(
        POWERPULSE2_DISPLAY_KEYS,
        ("LED indicator", "screen", "LED brightness", "screen brightness"),
        strict=True,
    )
)


def _describe_display(key: str, reported: Any) -> str:
    """One reported display value as the not-confirmed text shows it."""
    if not powerpulse2_display_value_valid(key, reported):
        return "no value yet"
    if key in POWERPULSE2_DISPLAY_SWITCH_KEYS:
        return "on" if reported else "off"
    return f"{reported} %"


class DeviceValueNotReported(Exception):
    """A write needs a value the device has not reported yet.

    Raised where the value is read rather than where the user-facing error is
    built, because the read has to happen inside the write lock. Platforms
    translate it into `raise_set_not_ready`.
    """


@dataclass(frozen=True)
class _PowerOceanScheduleFamily:
    """The write-side plumbing that differs between the two schedule families.

    Everything else that touches a schedule write - the lock, the seed and
    rollback, the armed latch, and the `DeviceValueNotReported` checks - is
    one code path shared by both families in the two methods below. `Callable
    [..., bytes]` rather than a precise signature because the two builders
    disagree on one parameter's shape (`time_table` is a scalar int for the
    charge schedule, a short list for the feed-to-grid schedule).
    """

    build_payload: Callable[..., bytes]
    log_label: str
    power_label: str
    range_source: str
    power_bounds: Callable[[dict[str, Any], str], tuple[int, int]]
    read_echoed: Callable[[dict[str, Any], str, str], dict[str, Any]]


def _read_timer_task_echoed(
    data: dict[str, Any], prefix: str, slot: str
) -> dict[str, Any]:
    """Charge schedule: four scalar fields, every one of them required.

    `time_table` is the one decoded varint the read path publishes, and a
    `time_param` is on every captured task of this family.
    """
    echoed: dict[str, Any] = {}
    for name, suffix in (
        ("task_type", "type"),
        ("time_mode", "time_mode"),
        ("time_param", "time_param"),
        ("time_table", "time_table"),
    ):
        value = as_known_int(data.get(f"{prefix}{suffix}"))
        if value is None:
            raise DeviceValueNotReported(f"{slot} {suffix}")
        echoed[name] = value
    return echoed


def _read_tou_task_echoed(
    data: dict[str, Any], prefix: str, slot: str
) -> dict[str, Any]:
    """Feed-to-grid schedule: three required fields and one optional.

    `time_table` is a list of one or two windows, as the packed field on the
    wire carries it. `time_param` may be absent: the daily task (time_mode
    65) never carries it in any of the nine captured writes, so None means
    "omit field 2", not "the device has not reported it yet".
    """
    task_type = as_known_int(data.get(f"{prefix}type"))
    if task_type is None:
        raise DeviceValueNotReported(f"{slot} type")
    time_mode = as_known_int(data.get(f"{prefix}time_mode"))
    if time_mode is None:
        raise DeviceValueNotReported(f"{slot} time_mode")
    time_table = data.get(f"{prefix}time_table")
    if not (
        isinstance(time_table, list)
        and 1 <= len(time_table) <= 2
        and all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in time_table
        )
    ):
        raise DeviceValueNotReported(f"{slot} time_table")
    return {
        "task_type": task_type,
        "time_mode": time_mode,
        "time_param": as_known_int(data.get(f"{prefix}time_param")),
        "time_table": time_table,
    }


def _schedule_power_bounds(data: dict[str, Any], device_sn: str) -> tuple[int, int]:
    """Charge schedule (`schedule`): pack-derived floor, prefix ceiling."""
    floor = schedule_power_min_w(as_known_int(data.get("bp_online_sum")))
    ceiling = schedule_power_max_w(device_sn)
    return floor, ceiling


def _feed_schedule_power_bounds(
    data: dict[str, Any], device_sn: str
) -> tuple[int, int]:
    """Feed-to-grid schedule (`feed_schedule`): fixed floor, device ceiling.

    Raises `DeviceValueNotReported` when the device has not reported its own
    feed limit yet - unlike the charge schedule, there is no model-derived
    fallback to check the write against.
    """
    del device_sn  # unused; kept so both bounds callables share one signature
    ceiling = as_known_int(data.get("ems_feed_power_limit_w"))
    if ceiling is None or ceiling <= POWEROCEAN_FEED_SCHEDULE_POWER_MIN_W:
        raise DeviceValueNotReported("feed power limit")
    return POWEROCEAN_FEED_SCHEDULE_POWER_MIN_W, ceiling


def _powerocean_schedule_families() -> dict[str, _PowerOceanScheduleFamily]:
    """Return the per-family table, one entry per `POWEROCEAN_SCHEDULE_PREFIXES`.

    The proto builders are imported lazily here, matching every other
    proto-builder import in this module (see `async_set_stream_soc_limits`
    below) - deferred to keep this module free of an import-time dependency
    on `..ecoflow.energy_stream`.
    """
    from ..ecoflow.energy_stream import (
        build_timer_task_set_payload,
        build_tou_task_set_payload,
    )

    return {
        "schedule": _PowerOceanScheduleFamily(
            build_payload=build_timer_task_set_payload,
            log_label="powerocean_schedule",
            power_label="charge power",
            range_source="this model accepts",
            power_bounds=_schedule_power_bounds,
            read_echoed=_read_timer_task_echoed,
        ),
        "feed_schedule": _PowerOceanScheduleFamily(
            build_payload=build_tou_task_set_payload,
            log_label="powerocean_feed_schedule",
            power_label="export power",
            range_source="the feed power limit allows",
            power_bounds=_feed_schedule_power_bounds,
            read_echoed=_read_tou_task_echoed,
        ),
    }


if TYPE_CHECKING:
    from ._typing import CoordinatorState as _Base
    from .core import EcoFlowDeviceCoordinator, PendingDeviceWrite
else:
    _Base = object


class SetCommandsMixin(_Base):
    """Mixin providing SET command dispatch and SoC debounce."""

    # ------------------------------------------------------------------
    # SET commands (switches, numbers)
    # ------------------------------------------------------------------

    async def async_set_soc_limits(
        self,
        max_charge_soc: int,
        min_discharge_soc: int,
    ) -> bool:
        """Send SoC limits to PowerOcean via WSS Protobuf (Enhanced Mode only).

        Sends a SysBatChgDsgSet message (cmd_func=96, cmd_id=112) with
        2 fields: charge upper limit and discharge lower limit.
        """
        if not self._enhanced_mode:
            _LOGGER.warning(
                "SoC limit SET requires Enhanced Mode (%s)", self.device_tag
            )
            return False
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning(
                "Cannot send SoC limits - MQTT not connected (%s)", self.device_tag
            )
            return False

        from ..ecoflow.energy_stream import build_soc_limit_set_payload

        payload = build_soc_limit_set_payload(max_charge_soc, min_discharge_soc)
        ok = await self.hass.async_add_executor_job(
            partial(self._mqtt_client.send_proto_set, payload, wait=True),
        )
        if ok:
            _LOGGER.debug(
                "SoC limits sent: max=%d, min=%d (%s)",
                max_charge_soc,
                min_discharge_soc,
                self.device_tag,
            )
            self._log_event(
                "set_soc_limits", f"max={max_charge_soc}, min={min_discharge_soc}"
            )
        else:
            _LOGGER.warning("SoC limits SET failed (%s)", self.device_tag)
            self._log_event(
                "set_soc_limits_fail", f"max={max_charge_soc}, min={min_discharge_soc}"
            )
        return ok

    async def async_set_powerocean_soc_debounced(
        self,
        backup_reserve_pct: int,
        solar_surplus_pct: int,
    ) -> bool:
        """Coalesce rapid-fire SoC SET requests (HA slider drag) into one frame.

        HA's Number-Entity emits one async_set_native_value call per 5%-step
        when the user drags the slider, producing 5-10 SETs in <1 s. The
        device cannot keep wire field 3 (sys_bat_backup_ratio, EMS) and
        field 4 (dev_soc, App-Layer) in sync at that cadence, so the two
        fields drift apart and the user sees stale values in HA or the
        EcoFlow app. This method stores the latest (backup, solar) and
        defers the actual MQTT SET by `POWEROCEAN_SOC_DEBOUNCE_S`. Each
        new call within the window resets the timer, so only the final
        value reaches the device.

        Returns True synchronously - the caller should treat this as an
        accepted user request and apply the optimistic UI value. The
        actual SET runs asynchronously and may still fail; failures are
        logged via the underlying async_set_powerocean_soc.
        """
        if self._shutdown:
            return False
        if not self._enhanced_mode:
            _LOGGER.warning(
                "PowerOcean SoC SET requires Enhanced Mode (%s)",
                self.device_tag,
            )
            return False
        if backup_reserve_pct > solar_surplus_pct:
            _LOGGER.warning(
                "PowerOcean SoC SET rejected locally: backup_reserve (%d) > "
                "solar_surplus (%d). Device requires backup <= solar.",
                backup_reserve_pct,
                solar_surplus_pct,
            )
            return False

        self._powerocean_soc_request_revision += 1
        revision = self._powerocean_soc_request_revision
        self._powerocean_soc_latest_outcome = None
        if not self._powerocean_soc_cycle_open:
            # A cycle is opening. This is the last moment the device values
            # are still in _device_data: the caller applies its optimistic
            # value the instant this returns True, so a snapshot taken later
            # - in the flush, as the first version of this did - captures the
            # requested value and "rolling back" to it changes nothing.
            #
            # The gate is the open cycle, not the pending value. The flush
            # clears the pending value when it starts, so a drag arriving
            # while its write is still in flight would look like a fresh
            # window and snapshot the failing write's optimistic values.
            self._powerocean_soc_generation += 1
            self._powerocean_soc_cycle_open = True
            self._powerocean_soc_before = {
                key: self._device_data.get(key)
                for key in POWEROCEAN_SOC_STATE_KEYS
                if key in self._device_data
            }

        self._powerocean_soc_pending = (backup_reserve_pct, solar_surplus_pct)
        self._powerocean_soc_pending_revision = revision
        if self._powerocean_soc_debounce_unsub is not None:
            self._powerocean_soc_debounce_unsub.cancel()
        handle: asyncio.TimerHandle | None = None

        def _timer_fired() -> None:
            # call_later never runs inline, so assignment has completed before
            # this callback can execute.
            assert handle is not None
            self._powerocean_soc_debounce_fired(handle)

        handle = self.hass.loop.call_later(POWEROCEAN_SOC_DEBOUNCE_S, _timer_fired)
        self._powerocean_soc_debounce_unsub = handle
        return True

    def _claim_powerocean_soc_pending(
        self,
    ) -> tuple[tuple[int, int], int, int] | None:
        """Claim the current pending values before another window can replace them."""
        pending = self._powerocean_soc_pending
        if pending is None:
            return None
        self._powerocean_soc_pending = None
        revision = self._powerocean_soc_pending_revision
        self._powerocean_soc_pending_revision = 0
        self._powerocean_soc_active_revisions.add(revision)
        return (
            pending,
            self._powerocean_soc_generation,
            revision,
        )

    def _powerocean_soc_debounce_fired(
        self,
        handle: asyncio.TimerHandle,
    ) -> None:
        """Start the flush owned by *handle*, ignoring stale callbacks."""
        if self._shutdown or self._powerocean_soc_debounce_unsub is not handle:
            return
        self._powerocean_soc_debounce_unsub = None
        claim = self._claim_powerocean_soc_pending()
        if claim is None:
            return

        # ConfigEntry tasks are eager on supported HA versions. Gate the
        # coroutine until its identity is in our set, otherwise a no-op or
        # shutdown-time flush could finish before it can be tracked.
        tracked = asyncio.Event()

        async def _run_tracked_flush() -> None:
            await tracked.wait()
            await self._flush_powerocean_soc(claim)

        task = self._entry.async_create_task(
            self.hass,
            _run_tracked_flush(),
            name=f"PowerOcean SoC flush {self.device_tag}",
        )
        self._powerocean_soc_flush_tasks.add(task)
        task.add_done_callback(self._powerocean_soc_flush_done)
        tracked.set()

    def _powerocean_soc_flush_done(self, task: asyncio.Task[None]) -> None:
        """Forget exactly the flush task that completed."""
        self._powerocean_soc_flush_tasks.discard(task)

    def _powerocean_soc_write_done(self, task: asyncio.Future[bool]) -> None:
        """Forget exactly the coordinator-owned direct write that completed."""
        self._powerocean_soc_write_tasks.discard(task)
        # ConfigEntry reports task failures too, but retrieving the result here
        # avoids an unobserved-exception warning if it finishes before unload.
        if task.cancelled():
            return
        task.exception()

    def _schedule_powerocean_soc_write(
        self,
        backup_reserve_pct: int,
        solar_surplus_pct: int,
        *,
        name: str,
    ) -> asyncio.Task[object] | None:
        """Create and identity-track a direct SoC write without a start race."""
        if self._shutdown:
            return None
        tracked = asyncio.Event()

        async def _run_tracked_write() -> bool:
            await tracked.wait()
            return await self.async_set_powerocean_soc(
                backup_reserve_pct, solar_surplus_pct
            )

        task = self._entry.async_create_task(
            self.hass,
            _run_tracked_write(),
            name=name,
        )
        self._powerocean_soc_write_tasks.add(task)
        task.add_done_callback(self._powerocean_soc_write_done)
        tracked.set()
        return task

    def _resolve_powerocean_soc_cycle(self, generation: int) -> None:
        """Resolve a cycle only after its pending and claimed work settles."""
        if (
            self._shutdown
            or generation != self._powerocean_soc_generation
            or not self._powerocean_soc_cycle_open
            or self._powerocean_soc_debounce_unsub is not None
            or self._powerocean_soc_pending is not None
            or self._powerocean_soc_active_revisions
            or self._powerocean_soc_latest_outcome is None
        ):
            return

        latest_succeeded = self._powerocean_soc_latest_outcome
        self._powerocean_soc_cycle_open = False
        self._powerocean_soc_latest_outcome = None
        if latest_succeeded:
            return

        # The latest request failed. Reconcile the optimistic sliders to the
        # newest request that actually reached the device, or to the snapshot
        # captured before this cycle if no request succeeded.
        before = dict(self._powerocean_soc_before)
        for key in POWEROCEAN_SOC_STATE_KEYS:
            if key in before:
                self.set_device_value(key, before[key])
                if self.data is not None:
                    self.data[key] = before[key]
            else:
                self._device_data.pop(key, None)
                if self.data is not None:
                    self.data.pop(key, None)
        # The entities hold a 5 s optimistic lock and would ignore the update
        # below without advancing the rollback generation.
        self._write_rollback_generation += 1
        self.async_update_listeners()

    async def _flush_powerocean_soc(
        self,
        claim: tuple[tuple[int, int], int, int] | None = None,
    ) -> None:
        """Send the most recent debounced SoC SET to the device.

        The service call returned before this runs, so a failure here can
        no longer be raised at the caller. What it must not do is leave the
        sliders showing a value the device never took: both entities applied
        an optimistic value on the way in, and on failure that value is the
        only thing left saying the write worked (issue #185).
        """
        if self._shutdown:
            return
        if claim is None and self._powerocean_soc_debounce_unsub is not None:
            # Manual flushes (used by tests and immediate internal callers)
            # own the currently visible handle. Timer callbacks claim their
            # handle and pending pair synchronously before creating a task.
            self._powerocean_soc_debounce_unsub.cancel()
            self._powerocean_soc_debounce_unsub = None
        if claim is None:
            claim = self._claim_powerocean_soc_pending()
        if claim is None:
            return
        pending, generation, revision = claim
        backup, solar = pending

        cancelled: asyncio.CancelledError | None = None
        try:
            delivered = await self.async_set_powerocean_soc(backup, solar)
        except asyncio.CancelledError as err:
            # A cancelled flush is a settled failed revision. Reconcile it if
            # the coordinator is still live, then preserve cancellation for
            # the owner/shutdown collector.
            cancelled = err
            delivered = False
        except Exception:
            # Anything unexpected here would otherwise skip the rollback and
            # leave the optimistic value standing, which is the failure this
            # whole path exists to prevent.
            _LOGGER.debug("PowerOcean SoC flush raised", exc_info=True)
            delivered = False

        if self._shutdown:
            # Shutdown owns and clears the cycle before it waits for an
            # executor-backed publish. Its eventual result belongs to the old
            # coordinator and must not touch state or listeners after unload.
            if cancelled is not None:
                raise cancelled
            return

        if generation != self._powerocean_soc_generation:
            self._powerocean_soc_active_revisions.discard(revision)
            if cancelled is not None:
                raise cancelled
            return
        if delivered and revision > self._powerocean_soc_confirmed_revision:
            self._powerocean_soc_confirmed_revision = revision
            self._powerocean_soc_before = {
                POWEROCEAN_SOC_STATE_KEYS[0]: backup,
                POWEROCEAN_SOC_STATE_KEYS[1]: solar,
            }
        if revision == self._powerocean_soc_request_revision:
            self._powerocean_soc_latest_outcome = delivered
        self._powerocean_soc_active_revisions.discard(revision)
        self._resolve_powerocean_soc_cycle(generation)
        if cancelled is not None:
            raise cancelled

    async def async_set_powerocean_soc(
        self,
        backup_reserve_pct: int,
        solar_surplus_pct: int,
    ) -> bool:
        """Send a 3-field SoC SET to PowerOcean (app-replay format).

        Wire: cmd_id=112 SysBatChgDsgSet with field 1=100 (sys_bat_chg_up_limit),
        field 2=backup (sys_bat_dsg_down_limit), field 3=solar_surplus
        (sys_bat_backup_ratio), plus extended envelope (check_type, from=ios,
        device_sn). The legacy `async_set_soc_limits` only sends fields 1+2
        and is silently ignored by the device for backup-reserve changes.
        """
        if self._shutdown:
            return False
        if not self._enhanced_mode:
            _LOGGER.warning(
                "PowerOcean SoC SET requires Enhanced Mode (%s)", self.device_tag
            )
            return False
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning(
                "Cannot send PowerOcean SoC - MQTT not connected (%s)", self.device_tag
            )
            return False
        if backup_reserve_pct > solar_surplus_pct:
            _LOGGER.warning(
                "PowerOcean SoC SET rejected locally: backup_reserve (%d) > "
                "solar_surplus (%d). Device requires backup <= solar.",
                backup_reserve_pct,
                solar_surplus_pct,
            )
            return False

        from ..ecoflow.energy_stream import build_powerocean_soc_set_payload

        payload = build_powerocean_soc_set_payload(
            backup_reserve_pct,
            solar_surplus_pct,
            device_sn=self.device_sn,
        )
        executor_job = self.hass.async_add_executor_job(
            partial(self._mqtt_client.send_proto_set, payload, wait=True),
        )
        self._powerocean_soc_write_tasks.add(executor_job)
        executor_job.add_done_callback(self._powerocean_soc_write_done)
        # Cancelling the coroutine cannot stop a worker thread already inside
        # send_proto_set. Shielding leaves the actual broker job tracked so
        # unload still waits for it before disconnecting MQTT.
        ok = await asyncio.shield(executor_job)
        if self._shutdown:
            # The executor job cannot be stopped once it has entered the MQTT
            # client. Its result belongs to the coordinator being unloaded,
            # so do not append a success/failure action after shutdown.
            return ok
        label = f"backup={backup_reserve_pct} solar={solar_surplus_pct}"
        if ok:
            _LOGGER.debug("PowerOcean SoC sent: %s (%s)", label, self.device_tag)
            self._log_event("set_powerocean_soc", label)
        else:
            _LOGGER.warning(
                "PowerOcean SoC SET failed: %s (%s)", label, self.device_tag
            )
            self._log_event("set_powerocean_soc_fail", label)
        return ok

    async def async_set_powerocean_work_mode(self, work_mode: int) -> bool:
        """Send SysWorkModeSet (cmd_id=98) for PowerOcean.

        Phase 1 supports only modes that work without sub-params:
        SELFUSE (0) and AI_SCHEDULE (12). TOU (1) and BACKUP (2) require
        TouParam/BackupParam and return result=1 if sent without them.
        """
        if not self._enhanced_mode:
            _LOGGER.warning(
                "Work-mode SET requires Enhanced Mode (%s)",
                self.device_tag,
            )
            return False
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning(
                "Cannot send work-mode - MQTT not connected (%s)",
                self.device_tag,
            )
            return False

        from ..ecoflow.energy_stream import build_work_mode_set_payload

        payload = build_work_mode_set_payload(work_mode)
        ok = await self.hass.async_add_executor_job(
            partial(self._mqtt_client.send_proto_set, payload, wait=True),
        )
        if ok:
            _LOGGER.debug("Work-mode sent: %d (%s)", work_mode, self.device_tag)
            self._log_event("set_work_mode", str(work_mode))
        else:
            _LOGGER.warning("Work-mode SET failed: %d (%s)", work_mode, self.device_tag)
            self._log_event("set_work_mode_fail", str(work_mode))
        return ok

    # ------------------------------------------------------------------
    # PowerOcean scheduled tasks: charge schedule (96/125) and
    # feed-to-grid schedule (96/143, ADR-027 addendum, #381)
    # ------------------------------------------------------------------
    #
    # Both families are a slot the owner already created in the app, share
    # the arm/disarm/power operations, and go through the identical lock,
    # seed/rollback and DeviceValueNotReported checks below. What differs -
    # the wire builder, the log label, the power range and its wording, and
    # the shape of the echoed fields (one scalar window on the charge
    # schedule, a list of up to two on the feed-to-grid schedule, whose
    # `time_param` may be absent) - lives in the per-family table
    # (`_powerocean_schedule_families`); the two methods that use it never
    # branch on `family` themselves.

    async def async_set_powerocean_schedule_armed(
        self,
        family: str,
        task_index: int,
        armed: bool,
    ) -> bool:
        """Arm or disarm one scheduled task, on either schedule family.

        A short frame that carries nothing but the slot, so it needs no value
        from the last read. The slot itself still has to be there. A task the
        owner deletes in the app simply drops out of the device's task list,
        and the parser then retracts every key of that slot to None - which
        leaves the switch standing and toggleable, since a key that is present
        and None still counts as reported. Sending against a slot the device
        no longer holds would name a task that does not exist and then show it
        as armed, so an arming flag that is not a bool refuses the write.

        The flag is seeded before the send rather than after it, because a
        power change on the same slot has to carry the arming along and the
        two writes share this lock: a seed that lands after the next read
        would let that write send the flag it just replaced. The same seed is
        held against a device frame that was already in flight, for the window
        named by `POWEROCEAN_SCHEDULE_ARMED_LATCH_S`. A failed send puts the
        previous value back and drops the hold with it.

        A write that starts during unload is refused outright. Every state it
        touches - the seed, the hold, the rollback - belongs to a coordinator
        that is being torn down, and the same guard sits on the other
        PowerOcean writes for the same reason.

        Raises:
            ValueError: `family` is not a known PowerOcean schedule prefix.
        """
        if family not in POWEROCEAN_SCHEDULE_PREFIXES:
            raise ValueError(f"unknown PowerOcean schedule family {family!r}")
        config = _powerocean_schedule_families()[family]

        if self._shutdown:
            return False
        state_key = f"{family}_{task_index}_enabled"
        async with self._device_config_lock:
            previous = self.device_data.get(state_key)
            if not isinstance(previous, bool):
                raise DeviceValueNotReported(f"{family} {task_index} enabled")
            payload = config.build_payload(
                "arm" if armed else "disarm",
                task_index,
                device_sn=self.device_sn,
            )
            self._seed_device_values(**{state_key: armed})
            self.latch_schedule_armed(state_key, armed)
            if not await self.async_send_proto_set_command(
                payload, label=f"{config.log_label}_{task_index}_armed"
            ):
                self._seed_device_values(**{state_key: previous})
                self.clear_schedule_armed_latch(state_key)
                return False
            return True

    async def async_set_powerocean_schedule_power(
        self,
        family: str,
        task_index: int,
        power_w: int,
    ) -> bool:
        """Change the power of one scheduled task, on either schedule family.

        The full body carries the whole task, so the fields the device owns
        travel with it: the task type and the recurrence fields. They go out
        exactly as the last read reported them - composing a default for a
        missing one would rewrite the days and times the owner set in the
        app, which is the one thing this write refuses to risk, so an
        unreported field raises `DeviceValueNotReported` instead. The charge
        schedule requires all four echoed fields (type, time_mode, time_param,
        time_table); the feed-to-grid schedule requires three of them and
        treats a missing `time_param` as "omit it from the wire" rather than
        "not reported yet", because the daily task never carries that field on
        any captured frame (#381).

        The arming flag travels too. A full body clears it as a side effect,
        and the device's own task list confirms that a frame carrying field 4
        leaves the schedule armed, so the current flag is read and sent back.

        The power itself is checked against the range this family and model
        offer - see `_powerocean_schedule_families` - on the 100 W grid, and a
        value outside it raises `ValueError` rather than being rounded onto
        the grid.

        Refused during unload for the same reason as the arming write above.

        Raises:
            ValueError: `family` is not a known PowerOcean schedule prefix, or
                the requested power is outside the model's range or off the
                100 W grid.
        """
        if family not in POWEROCEAN_SCHEDULE_PREFIXES:
            raise ValueError(f"unknown PowerOcean schedule family {family!r}")
        config = _powerocean_schedule_families()[family]

        if self._shutdown:
            return False
        prefix = f"{family}_{task_index}_"
        async with self._device_config_lock:
            data = self.device_data

            echoed = config.read_echoed(data, prefix, f"{family} {task_index}")

            armed = data.get(f"{prefix}enabled")
            if not isinstance(armed, bool):
                raise DeviceValueNotReported(f"{family} {task_index} enabled")

            # The app's own range, checked here as well as on the entity: a
            # service call carries any number the caller likes, and the
            # entity's step and bounds only constrain the slider. Refused
            # rather than rounded - a value off the 100 W grid is one no app
            # has ever sent, so silently moving it would put a setpoint on the
            # wire that nobody asked for.
            floor, ceiling = config.power_bounds(data, self.device_sn)
            if power_w < floor or power_w > ceiling:
                raise ValueError(
                    f"{config.power_label} {power_w} W is outside the range "
                    f"{config.range_source} ({floor}-{ceiling} W)"
                )
            if power_w % POWEROCEAN_SCHEDULE_POWER_STEP_W:
                raise ValueError(
                    f"{config.power_label} {power_w} W is not a multiple of "
                    f"{POWEROCEAN_SCHEDULE_POWER_STEP_W} W"
                )

            state_key = f"{prefix}power_w"
            previous = data.get(state_key, _UNSET)
            payload = config.build_payload(
                "power",
                task_index,
                device_sn=self.device_sn,
                power_w=power_w,
                armed=armed,
                **echoed,
            )
            self._seed_device_values(**{state_key: power_w})
            if not await self.async_send_proto_set_command(
                payload, label=f"{config.log_label}_{task_index}_power"
            ):
                self._restore_device_values(**{state_key: previous})
                return False
            return True

    # ------------------------------------------------------------------
    # Stream BK-series SoC config writes (254/17)
    # ------------------------------------------------------------------

    async def async_set_stream_soc_limits(
        self,
        *,
        charge: int | None = None,
        discharge: int | None = None,
    ) -> bool:
        """Write the grouped Stream charge/discharge limit configuration.

        The app sends charge limit, discharge limit and backup reserve in one
        frame, so all three travel on every write. The two the caller did not
        touch are read from live telemetry and sent back unchanged; a default
        substituted for a missing one would rewrite a setting nobody asked to
        change, which is why an unreported value refuses the write instead.
        """
        from ..ecoflow.energy_stream import build_stream_soc_limits_payload

        async with self._device_config_lock:
            data = self.data or {}
            current_charge = as_known_int(data.get("max_charge_soc_pct"))
            current_discharge = as_known_int(data.get("min_discharge_soc_pct"))
            backup = as_known_int(data.get("backup_reserve_pct"))
            if current_charge is None or current_discharge is None or backup is None:
                raise DeviceValueNotReported("Stream SoC limits")

            requested_charge = current_charge if charge is None else charge
            requested_discharge = current_discharge if discharge is None else discharge

            payload = build_stream_soc_limits_payload(
                requested_charge,
                requested_discharge,
                backup,
                self.device_sn,
            )
            if not await self.async_send_proto_set_command(
                payload, label="stream_soc_limits"
            ):
                return False
            self._seed_device_values(
                max_charge_soc_pct=requested_charge,
                min_discharge_soc_pct=requested_discharge,
                backup_reserve_pct=backup,
            )
            return True

    async def async_set_stream_backup_reserve(self, reserve_pct: int) -> bool:
        """Write Stream backup reserve without racing a grouped limit write."""
        from ..ecoflow.energy_stream import build_stream_backup_reserve_payload

        async with self._device_config_lock:
            payload = build_stream_backup_reserve_payload(reserve_pct, self.device_sn)
            if not await self.async_send_proto_set_command(
                payload, label="stream_backup_reserve"
            ):
                return False
            self._seed_device_values(backup_reserve_pct=reserve_pct)
            return True

    async def async_set_stream_led_brightness(self, brightness_pct: int) -> bool:
        """Write Stream LED brightness without racing another config write.

        This one reads nothing and seeds nothing: no other write consumes
        `led_brightness`, and the device reports the live value back on its
        own. It still takes the lock, so that every Stream config write goes
        out through one place. An exception here would be the only writer
        that a future grouped write could not see queued behind it.
        """
        from ..ecoflow.energy_stream import build_stream_led_brightness_payload

        async with self._device_config_lock:
            payload = build_stream_led_brightness_payload(
                brightness_pct, self.device_sn
            )
            return await self.async_send_proto_set_command(
                payload, label="stream_led_brightness"
            )

    async def async_set_stream_ac_outlet(self, outlet: int, turn_on: bool) -> bool:
        """Write a Stream AC outlet without racing another config write.

        Same reasoning as the LED brightness write above: this one reads
        nothing, so no update can be lost today. It goes through the lock so
        that every Stream config write leaves from one place, and a future
        grouped write sees it queued rather than in flight beside it.
        """
        from ..ecoflow.energy_stream import build_stream_ac_outlet_payload

        async with self._device_config_lock:
            payload = build_stream_ac_outlet_payload(
                outlet, turn_on, device_sn=self.device_sn
            )
            return await self.async_send_proto_set_command(
                payload, f"stream_ac_outlet_{outlet}"
            )

    async def async_set_stream_ac5000_work_mode(self, option: str) -> bool:
        """Write the STREAM AC 5000 work mode under the config lock.

        The mode decides whether the two power setpoints act at all, so a
        setpoint write landing between this frame and the device's echo would
        act against the mode it is replacing.
        """
        from ..ecoflow.stream_ac5000_commands import (
            build_work_mode_payload as build_stream_ac5000_work_mode_payload,
        )

        async with self._device_config_lock:
            payload = build_stream_ac5000_work_mode_payload(option, self.device_sn)
            return await self.async_send_proto_set_command(
                payload, label="stream_ac5000_work_mode"
            )

    async def async_set_stream_ac5000_backup_socket(self, turn_on: bool) -> bool:
        """Write the STREAM AC 5000 backup socket under the config lock."""
        from ..ecoflow.stream_ac5000_commands import (
            build_backup_socket_payload as build_stream_ac5000_backup_socket_payload,
        )

        async with self._device_config_lock:
            payload = build_stream_ac5000_backup_socket_payload(turn_on, self.device_sn)
            return await self.async_send_proto_set_command(
                payload, label="stream_ac5000_backup_socket"
            )

    # ------------------------------------------------------------------
    # STREAM AC 5000 config writes (254/38)
    #
    # Each one reads what the device currently reports before it can send,
    # so the read and the send are one operation under
    # `_device_config_lock`. A caller that read first and sent after
    # would interleave with the other entity writing the same config field.
    # The values sent are seeded into the store before the lock is released,
    # for the same reason: a waiter deciding what to send must not see the
    # state from before the write it is queued behind. The Stream writes
    # above share both the lock and `_seed_device_values` for exactly these
    # two reasons.
    # ------------------------------------------------------------------

    async def async_set_stream_ac5000_soc_limits(
        self,
        *,
        charge: int | None = None,
        discharge: int | None = None,
    ) -> bool:
        """Write config field 29, which holds both SoC limits.

        The limit not being changed travels at its current value. Pass the one
        being set; the other is read here.
        """
        from ..ecoflow.stream_ac5000_commands import build_soc_limits_payload

        async with self._device_config_lock:
            data = self.data or {}
            if charge is None:
                charge = as_known_int(data.get("max_charge_soc_pct"))
            if discharge is None:
                discharge = as_known_int(data.get("min_discharge_soc_pct"))
            if charge is None or discharge is None:
                raise DeviceValueNotReported("SoC limits")
            # Raises ValueError on a pair the device would refuse.
            payload = build_soc_limits_payload(charge, discharge, self.device_sn)
            if not await self.async_send_proto_set_command(
                payload, label="stream_ac5000_soc_limits"
            ):
                return False
            self._seed_device_values(
                max_charge_soc_pct=charge, min_discharge_soc_pct=discharge
            )
            return True

    async def async_set_stream_ac5000_grid_output_power(self, power_w: int) -> bool:
        """Write config field 10, the grid-tied output setpoint.

        The two companion values the app sends with the setpoint are read
        here rather than passed in: they belong to the device, not to the
        caller, and a write that invented them would send one unit's numbers
        to another.
        """
        from ..ecoflow.stream_ac5000_commands import (
            build_grid_output_power_payload,
        )

        async with self._device_config_lock:
            data = self.data or {}
            field_4 = as_known_int(data.get("_grid_output_field_4"))
            field_5 = as_known_int(data.get("_grid_output_field_5"))
            if field_4 is None or field_5 is None:
                raise DeviceValueNotReported("grid-tied output power")
            payload = build_grid_output_power_payload(
                power_w, field_4, field_5, self.device_sn
            )
            if not await self.async_send_proto_set_command(
                payload, label="stream_ac5000_grid_output_power"
            ):
                return False
            self._seed_device_values(max_grid_output_power_w=power_w)
            return True

    async def async_set_stream_ac5000_grid_input_power(self, power_w: int) -> bool:
        """Write config field 10 subfield 2, the grid input setpoint.

        Nothing is read from the device first: the app writes this value on
        its own, without the companions the output setpoint travels with. The
        config lock is still held, because the frame names config field 10 and
        the output write names the same field.
        """
        from ..ecoflow.stream_ac5000_commands import (
            build_grid_input_power_payload,
        )

        async with self._device_config_lock:
            payload = build_grid_input_power_payload(power_w, self.device_sn)
            if not await self.async_send_proto_set_command(
                payload, label="stream_ac5000_grid_input_power"
            ):
                return False
            self._seed_device_values(max_grid_input_power_w=power_w)
            return True

    async def async_set_stream_ac5000_backup_reserve(
        self,
        *,
        enabled: bool | None = None,
        reserve_pct: int | None = None,
    ) -> bool:
        """Write config field 30, which holds the on/off flag and the level.

        The switch owns the flag and a number owns the level, so this is the
        one config write whose two halves live on different platforms.
        """
        from ..ecoflow.stream_ac5000_commands import build_backup_reserve_payload

        async with self._device_config_lock:
            data = self.data or {}
            if enabled is None:
                enabled = data.get("backup_reserve_enabled")
            if reserve_pct is None:
                reserve_pct = as_known_int(data.get("backup_reserve_pct"))
            if not isinstance(enabled, bool) or reserve_pct is None:
                raise DeviceValueNotReported("backup reserve")
            payload = build_backup_reserve_payload(enabled, reserve_pct, self.device_sn)
            if not await self.async_send_proto_set_command(
                payload, label="stream_ac5000_backup_reserve"
            ):
                return False
            self._seed_device_values(
                backup_reserve_enabled=enabled, backup_reserve_pct=reserve_pct
            )
            return True

    async def async_set_ocean2_backup_reserve(self, reserve_pct: int) -> bool:
        """Write the Ocean 2 Backup Reserve (fields 10 and 92) with a read-back.

        Raises `DeviceValueNotReported` (no fresh snapshot within 5 s, or
        field 92 missing from it) and `ValueError` (outside 1..100, above the
        fresh field 92, or refused by the device before this call returned).
        False means the publish failed. Nothing is published for the first
        two raises other than the get-all that asks for the snapshot.

        The app always sends both fields, and field 92 is re-asserted at the
        value the device reports now, so the snapshot is requested inside the
        lock and the write waits for it. After the publish the written value
        is seeded and a request sequence confirms it (see
        `_arm_ocean2_readback`).
        """
        from ..ecoflow.ocean2_commands import (
            build_ocean2_backup_reserve_payload,
            parse_ocean2_property_ack,
        )

        if not 1 <= reserve_pct <= 100:
            raise ValueError(
                f"Ocean 2 backup reserve must be 1..100, got {reserve_pct}"
            )

        async with self._device_config_lock:
            client = self._mqtt_client
            if client is None or not client.is_connected():
                _LOGGER.debug(
                    "Cannot write Ocean 2 backup reserve - MQTT not connected (%s)",
                    self.device_tag,
                )
                return False

            # Cleared before the request goes out: `_apply_data` (event loop)
            # sets it when a frame carrying field 92 is in the store.
            self._ocean2_snapshot_event.clear()
            _LOGGER.debug("Ocean 2 get-all before write sent for %s", self.device_tag)
            if not await self.hass.async_add_executor_job(client.send_get_all):
                return False
            try:
                await asyncio.wait_for(
                    self._ocean2_snapshot_event.wait(), _OCEAN2_SNAPSHOT_TIMEOUT_S
                )
            except TimeoutError:
                _LOGGER.debug(
                    "No Ocean 2 snapshot within %.0f s for %s",
                    _OCEAN2_SNAPSHOT_TIMEOUT_S,
                    self.device_tag,
                )
                raise DeviceValueNotReported("Ocean 2 backup reserve") from None

            data = self.data or {}
            ratio = as_known_int(data.get("edev_soc_ratio_pct"))
            if ratio is None:
                raise DeviceValueNotReported("Ocean 2 backup reserve")
            if reserve_pct > ratio:
                raise ValueError(
                    f"Ocean 2 backup reserve {reserve_pct} is above the "
                    f"device's own reading of {ratio}"
                )

            payload = build_ocean2_backup_reserve_payload(
                reserve_pct, ratio, self.device_sn
            )
            return await self._ocean2_publish(
                payload,
                _Ocean2Write(
                    state_key="backup_reserve_pct",
                    pre=self._ocean2_last_reported("backup_reserve_pct"),
                    written=reserve_pct,
                    sent_at=time.monotonic(),
                    parse_ack=parse_ocean2_property_ack,
                    label="backup reserve",
                    event_detail="field=10",
                ),
                send_label="ocean2_backup_reserve",
                refusal=(
                    f"Ocean 2 backup reserve {reserve_pct} was refused by the device"
                ),
            )

    async def async_set_ocean2_storm_guard(self, enable: bool) -> bool:
        """Switch the Ocean 2 Storm Guard on or off with a read-back (PLAN-192).

        Raises `ValueError` (not an Enhanced Mode Ocean 2 of a model whose
        write is confirmed, or refused by the device before this call
        returned) and `DeviceValueNotReported` (the unit has not reported the
        switch yet, so there is no value to put back if the write is not
        confirmed). False means MQTT is down or the publish failed.

        Unlike the reserve there is no companion field and so no snapshot to
        read first. The device answers an action write (`254/37`) with an
        acknowledgement, but the acknowledgement is not the state: the switch
        is confirmed only by the next full snapshot, which is the one frame
        type that carries `storm_guard_enabled` (see `_arm_ocean2_readback`).
        """
        from ..ecoflow.ocean2_commands import (
            ACTION_STORM,
            build_ocean2_storm_guard_payload,
            parse_ocean2_action_ack,
        )

        if (
            not self._enhanced_mode
            or self.device_type != DEVICE_TYPE_OCEAN2
            or not supports_ocean2_controls(self.device_sn)
        ):
            raise ValueError("Ocean 2 storm guard is not available on this unit")

        async with self._device_config_lock:
            client = self._mqtt_client
            if client is None or not client.is_connected():
                _LOGGER.debug(
                    "Cannot write Ocean 2 storm guard - MQTT not connected (%s)",
                    self.device_tag,
                )
                return False

            pre = self._ocean2_last_reported("storm_guard_enabled")
            if not isinstance(pre, bool):
                raise DeviceValueNotReported("Ocean 2 storm guard")

            payload = build_ocean2_storm_guard_payload(enable, self.device_sn)
            return await self._ocean2_publish(
                payload,
                _Ocean2Write(
                    state_key="storm_guard_enabled",
                    pre=pre,
                    written=enable,
                    sent_at=time.monotonic(),
                    parse_ack=partial(parse_ocean2_action_ack, action_id=ACTION_STORM),
                    label="storm guard",
                    event_detail=f"action={ACTION_STORM}",
                ),
                send_label="ocean2_storm_guard",
                refusal=(
                    f"Ocean 2 storm guard {'on' if enable else 'off'} "
                    "was refused by the device"
                ),
            )

    async def _ocean2_publish(
        self,
        payload: bytes,
        write: _Ocean2Write,
        *,
        send_label: str,
        refusal: str,
    ) -> bool:
        """Publish an Ocean 2 write, seed its value and arm its read-back.

        Runs inside `_device_config_lock`, which is what serialises the
        Backup Reserve and the Storm Guard against each other. The read-back
        that follows does not hold the lock, so each write owns one record
        per state key and the two confirmations run side by side.
        """
        previous = self._ocean2_writes.get(write.state_key)
        # Visible to the acknowledgement check before the publish returns.
        self._ocean2_writes[write.state_key] = write
        if not await self.async_send_proto_set_command(payload, label=send_label):
            if previous is None:
                self._ocean2_writes.pop(write.state_key, None)
            else:
                self._ocean2_writes[write.state_key] = previous
            return False
        if previous is not None:
            previous.cancel_handles()
        if write.rejected:
            # The refusal beat this continuation and has already put `pre`
            # back. Seeding now would overwrite that with the refused value.
            # The record goes too, so later frames are not matched to it.
            self._ocean2_writes.pop(write.state_key, None)
            raise ValueError(refusal)
        self._seed_device_values(**{write.state_key: write.written})
        # The seed replaces whatever a frame during the publish put in the
        # store, so the store holds this write's value again, not the unit's.
        write.reported = False
        self._arm_ocean2_readback(write)
        return True

    def _ocean2_last_reported(self, state_key: str) -> Any:
        """The value the unit last reported for `state_key` (`_UNSET` if none).

        What a missing read-back or a refusal puts back must be a value the
        device reported, never a seed. While an earlier write of this key is
        still unconfirmed the store holds that write's seed, so a second write
        takes the first one's `pre` instead; once a snapshot carrying the key
        has arrived the store holds the unit's own value again.
        """
        previous = self._ocean2_writes.get(state_key)
        if previous is not None and not previous.reported:
            return previous.pre
        return (self.data or {}).get(state_key, _UNSET)

    def _ocean2_is_current(self, write: _Ocean2Write) -> bool:
        """Whether `write` is still the record for its state key (event loop)."""
        return self._ocean2_writes.get(write.state_key) is write

    def _arm_ocean2_readback(self, write: _Ocean2Write) -> None:
        """Schedule the requests that confirm a written value (event loop).

        Property pushes never carry the reserve (field 10) and action pushes
        never carry the storm guard, so without a later snapshot a refused or
        clamped write would show the requested value for good.
        +8 s asks and resets the marker, +30 s asks again only when nothing
        carrying the key came back, +35 s puts the pre-write value back if
        that is still so. Each timer carries its own write: a newer write
        swaps the record before its publish returns, and a timer of the older
        one must not act on the newer one.
        """
        if self._shutdown:
            # Shutdown landed during the publish, after the cancel had already
            # run: nothing may be left armed on a stopped coordinator.
            return
        loop = self.hass.loop
        write.handles = [
            loop.call_later(
                _OCEAN2_READBACK_REQUEST_S, self._ocean2_readback_request, write
            ),
            loop.call_later(
                _OCEAN2_READBACK_RETRY_S, self._ocean2_readback_retry, write
            ),
            loop.call_later(
                _OCEAN2_READBACK_EXPIRE_S, self._ocean2_readback_expire, write
            ),
        ]

    def _cancel_ocean2_readback(self) -> None:
        """Drop the pending read-back sequences (shutdown, event loop)."""
        for write in list(self._ocean2_writes.values()):
            write.cancel_handles()
        self._ocean2_writes.clear()

    def _ocean2_send_get_all(self, reason: str) -> None:
        client = self._mqtt_client
        if client is None or not client.is_connected():
            _LOGGER.debug(
                "Ocean 2 get-all (%s) skipped for %s (not connected)",
                reason,
                self.device_tag,
            )
            return
        self.hass.async_add_executor_job(client.send_get_all)
        _LOGGER.debug("Ocean 2 get-all (%s) sent for %s", reason, self.device_tag)

    def _ocean2_readback_request(self, write: _Ocean2Write) -> None:
        if not self._ocean2_is_current(write):
            return
        # Frames that arrived before this request (the app's own polls during
        # the apply window) do not count as the answer to it.
        write.snapshot_seen = False
        self._ocean2_send_get_all("read-back")

    def _ocean2_readback_retry(self, write: _Ocean2Write) -> None:
        if not self._ocean2_is_current(write) or write.snapshot_seen:
            return
        self._ocean2_send_get_all("read-back retry")

    def _ocean2_readback_expire(self, write: _Ocean2Write) -> None:
        if not self._ocean2_is_current(write):
            return
        del self._ocean2_writes[write.state_key]
        if write.snapshot_seen:
            return
        _LOGGER.debug(
            "No Ocean 2 snapshot confirmed the %s for %s, "
            "restoring the value read before the write",
            write.label,
            self.device_tag,
        )
        self._ocean2_restore_write(write)
        self._log_event("readback_missing", write.event_detail)

    def _ocean2_restore_write(self, write: _Ocean2Write) -> None:
        """Put the pre-write value back and tell the entities (event loop)."""
        self._restore_device_values(**{write.state_key: write.pre})
        # The entities hold a 5 s optimistic lock after a write and would ignore
        # the update below; a new generation lifts it.
        self._write_rollback_generation += 1
        self.async_update_listeners()

    def _ocean2_reject_write(self, write: _Ocean2Write) -> None:
        """Take back `write` after the device refused it (event loop).

        Ignored when `write` is no longer the current record: a newer write
        replaced it, or its read-back ran out, and the store then holds
        another value than the one this refusal is about.
        """
        if not self._ocean2_is_current(write):
            return
        write.rejected = True
        self._ocean2_restore_write(write)

    def _note_ocean2_snapshot(self, parsed: dict[str, Any]) -> None:
        """Observe a parsed Ocean 2 frame for the writes in flight (event loop).

        Called from `_apply_data` after the frame is in the store, so a waiter
        woken by the event finds the fresh values already there.
        """
        if "edev_soc_ratio_pct" in parsed:
            self._ocean2_snapshot_event.set()
        for write in list(self._ocean2_writes.values()):
            if write.state_key not in parsed:
                continue
            write.snapshot_seen = True
            write.reported = True
            if parsed[write.state_key] != write.written:
                # May be the user's own change in the app on the shared topic.
                _LOGGER.debug(
                    "Ocean 2 %s reports %s %s, this integration wrote %s",
                    self.device_tag,
                    write.label,
                    parsed[write.state_key],
                    write.written,
                )

    async def async_set_stream_ac5000_task_power(
        self,
        kind: str,
        power_w: int,
    ) -> bool:
        """Replace the scheduled task with one of `kind` at `power_w`.

        This device has no direct power setpoint: a scheduled task is the
        setpoint. Charge and discharge are separate whole-day tasks, so the
        other kind is removed before the new one is written, or the device
        sees overlapping time periods and acts on neither.
        """
        from ..ecoflow.stream_ac5000_commands import (
            MINUTES_PER_DAY,
            TASK_REMOVE,
            build_task_payload,
        )

        other = "discharge" if kind == "charge" else "charge"
        async with self._device_config_lock:
            data = self.data or {}
            if as_known_int(data.get(f"scheduled_{other}_power_w")) is not None:
                # `39.1.2` goes out as the device last reported it for this task
                # rather than as the number derived from the kind. The two agree
                # in every frame this integration wrote, and they did not agree
                # once the app got involved: on 2026-08-08 the app removed the
                # charge task numbered 1 and added its discharge task at 1 in
                # the same frame. If that number is the task's slot, a removal
                # naming the derived one names no task, and what is left
                # standing is the overlapping pair the removal exists to
                # prevent. Where nothing has been observed, the derived number
                # is sent, which is what every removal before this one carried.
                if not await self.async_send_proto_set_command(
                    build_task_payload(
                        other,
                        0,
                        MINUTES_PER_DAY - 1,
                        0,
                        self.device_sn,
                        operation=TASK_REMOVE,
                        task_slot=as_known_int(
                            data.get(f"scheduled_{other}_task_slot")
                        ),
                    ),
                    label=f"stream_ac5000_{other}_task_remove",
                ):
                    # The task may well still be there, so writing the new one
                    # would leave exactly the overlapping pair.
                    return False
                self._clear_stream_ac5000_task(other)
            payload = self._build_stream_ac5000_task(kind, power_w, data)
            if not await self.async_send_proto_set_command(
                payload, label=f"stream_ac5000_{kind}_power"
            ):
                return False
            self._seed_device_values(**{f"scheduled_{kind}_power_w": power_w})
            return True

    def _seed_device_values(self, **values: Any) -> None:
        """Record values just sent, in both the store and the snapshot.

        A STREAM AC 5000 task's own number is deliberately never seeded. The
        device decides where a new task lands, so a guess here would put a
        fabricated number into the next removal. The real one arrives in the
        readback in seconds.
        """
        for key, value in values.items():
            self.set_device_value(key, value)
            if self.data is not None:
                self.data[key] = value

    def _restore_device_values(self, **values: Any) -> None:
        """Put values back after a failed write, forgetting absent ones.

        A key that was not in the store before the write has to go back to not
        being there, rather than back as a present None. The accessory
        platforms create a control the moment its reading exists, and a
        present None counts as one - so seeding it would build the entity for
        a slot the device has never described, and nothing would take it away
        again. Pass `_UNSET` for a value that was absent.
        """
        for key, value in values.items():
            if value is _UNSET:
                self._device_data.pop(key, None)
                if self.data is not None:
                    self.data.pop(key, None)
                continue
            self.set_device_value(key, value)
            if self.data is not None:
                self.data[key] = value

    def _clear_stream_ac5000_task(self, kind: str) -> None:
        """Forget a task this integration has just removed.

        The device stops mentioning a deleted task rather than reporting it
        empty, and the parser's clear-everything branch only fires on an empty
        task list, which the replacement task keeps it from being. So nothing
        would ever retract these values and the setpoint entity would go on
        showing a task that no longer exists.

        `scheduled_charge_soc_target` deliberately survives: it is the app's
        charge limit, the next charge write reads it back, and clearing it
        would reset a task set to stop at 80% into charging to 100%.
        """
        for suffix in ("power_w", "enabled", "start_min", "end_min", "task_slot"):
            state_key = f"scheduled_{kind}_{suffix}"
            self.set_device_value(state_key, None)
            if self.data is not None:
                self.data[state_key] = None

    def _build_stream_ac5000_task(
        self, kind: str, power_w: int, data: dict[str, Any]
    ) -> bytes:
        """Build the task frame that carries a power setpoint.

        It is always a whole-day, enabled task, so the value asked for is the
        value that acts. The only thing carried over from a reported task is
        the charge target SoC, which decides what charging does rather than
        whether it happens.

        Zero is a real setpoint, not an absence: a 0 W discharge task parks the
        battery, while removing every task leaves it on its own 200 W base
        output.

        An update names the number the device reported for this kind, for the
        same reason a removal does. It is only ever known when this kind's power
        was reported, which is exactly when the operation is an update, so an
        add is never affected.
        """
        from ..ecoflow.stream_ac5000_commands import (
            MINUTES_PER_DAY,
            TASK_ADD,
            TASK_UPDATE,
            build_task_payload,
        )

        mode = data.get("work_mode")
        if mode is not None and mode != "custom":
            _LOGGER.warning(
                "Setting the %s power on %s while it is in %s mode: a "
                "scheduled task is only acted on in custom mode, so the "
                "device will accept this and may do nothing with it",
                kind,
                self.device_tag,
                mode,
            )

        reported = as_known_int(data.get(f"scheduled_{kind}_power_w")) is not None
        soc_target = data.get("scheduled_charge_soc_target")
        return build_task_payload(
            kind,
            0,
            MINUTES_PER_DAY - 1,
            power_w,
            self.device_sn,
            enabled=True,
            operation=TASK_UPDATE if reported else TASK_ADD,
            charge_soc_target=soc_target if isinstance(soc_target, int) else 100,
            task_slot=as_known_int(data.get(f"scheduled_{kind}_task_slot")),
        )

    async def async_send_proto_set_command(
        self,
        payload: bytes,
        label: str,
    ) -> bool:
        """Send a protobuf SET command via WSS MQTT."""
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.debug(
                "Cannot send proto SET (%s) - MQTT not connected (%s)",
                label,
                self.device_tag,
            )
            return False

        ok = await self.hass.async_add_executor_job(
            partial(self._mqtt_client.send_proto_set, payload, wait=True),
        )
        if ok:
            _LOGGER.debug("Proto SET sent: %s (%s)", label, self.device_tag)
            self._log_event(f"proto_set_{label}", "ok")
        else:
            _LOGGER.debug("Proto SET not delivered: %s (%s)", label, self.device_tag)
            self._log_event(f"proto_set_{label}_fail", "")
        return ok

    async def async_set_powerpulse_charge_action(self, action: str) -> None:
        """Start or stop this PowerPulse 2's charging session (ADR-009).

        Unlike every other SET method in this file, this one raises rather
        than returning a bool: there is no button entity to translate a
        failure into a `HomeAssistantError` on this coordinator's behalf
        (Phase 3b), and every failure here already has an exact reason.

        `charge_action_route()` decides which of two commands is sent. With
        exactly one PowerOcean in the entry, the command leaves on that
        sibling's set topic (decision 2) - the route used above. With none,
        it leaves on this coordinator's own set topic instead, addressed to
        the wallbox itself (PLAN-140, ADR-009 addendum of 2026-09-12, from
        one recording of the app doing exactly that on an account with no
        PowerOcean). Either way the command is confirmed on this
        coordinator's own wallbox heartbeat (decision 4). The precondition
        and in-progress checks, the publish, and the pending record all
        happen under `self._pending_write_lock` so a second press cannot
        slip past the in-progress check before the first press's record
        exists. The confirmation wait happens outside the lock, since it may
        run for up to `POWERPULSE2_CHARGE_ACTION_WINDOW_S[action]` seconds
        and must not block a second, later press from being refused
        promptly.
        """
        from ..ecoflow.energy_stream import (
            build_powerpulse_charge_action_payload,
            build_powerpulse_standalone_charge_ctrl_payload,
        )
        from .core import PendingDeviceWrite

        async with self._pending_write_lock:
            if self._shutdown:
                # A press during teardown must not report success for a
                # command that was never sent (issue #185 is the rule).
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_action_not_delivered",
                )
            if action not in POWERPULSE2_CHARGE_ACTION_WINDOW_S:
                raise ValueError(f"action must be 'start' or 'stop', got {action!r}")
            typed_action = cast("Literal['start', 'stop']", action)
            if self._pending_write is not None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_action_in_progress",
                )
            route = self.charge_action_route()
            if route is None:
                # Two or more PowerOceans, or the entry's coordinator table
                # is already gone (teardown, ADR-009 decision 2): the first
                # has a message of its own, the second is a press that
                # cannot be delivered any more.
                if self._powerocean_coordinators() is None:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerpulse_action_not_delivered",
                    )
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_sibling_missing",
                )
            sender: EcoFlowDeviceCoordinator
            if route == "sibling":
                sibling = self.powerocean_sibling()
                if sibling is None:
                    # Unreachable within one event-loop tick: the route was
                    # resolved from the same table a moment ago. Kept as a
                    # guard against the two calls ever straddling an await.
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerpulse_action_not_delivered",
                    )
                sibling_mqtt = sibling._mqtt_client
                if sibling_mqtt is None or not sibling_mqtt.is_connected():
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerpulse_sibling_offline",
                    )
                dev_addr = self._device_data.get("ev_charger_dev_addr")
                dev_sn = self._device_data.get("ev_charger_sn")
                if (
                    not isinstance(dev_addr, int)
                    or not isinstance(dev_sn, str)
                    or len(dev_sn) != 16
                ):
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerpulse_descriptor_missing",
                    )
                self._require_wallbox_action_state(typed_action)
                sender = sibling
                payload = build_powerpulse_charge_action_payload(
                    typed_action, dev_addr, dev_sn
                )
            else:
                if self._mqtt_client is None or not self._mqtt_client.is_connected():
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerpulse_own_channel_offline",
                    )
                self._require_wallbox_action_state(typed_action)
                sender = cast("EcoFlowDeviceCoordinator", self)
                payload = build_powerpulse_standalone_charge_ctrl_payload(
                    typed_action, self.device_sn
                )
            future: asyncio.Future[str | float] = self.hass.loop.create_future()
            record = PendingDeviceWrite(
                action=typed_action, issued_at=time.monotonic(), future=future
            )
            # The record lives exactly as long as this call: every exit,
            # the failed publish, the timeout, the confirmation and a
            # cancelled press alike, clears it in the `finally` below. A
            # record that outlived a cancelled press would refuse every
            # later press as in progress until the entry reloaded (review
            # finding of 2026-09-11).
            self._pending_write = record
            try:
                delivered = await sender.async_send_proto_set_command(
                    payload, f"powerpulse_{typed_action}"
                )
                if not delivered:
                    self._log_event(f"powerpulse_{typed_action}_not_delivered", "")
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerpulse_action_not_delivered",
                    )
            except BaseException:
                self._clear_pending_write(record)
                raise

        try:
            confirmed_status = await asyncio.wait_for(
                future, POWERPULSE2_CHARGE_ACTION_WINDOW_S[typed_action]
            )
        except TimeoutError:
            last_status = self._device_data.get("ev_charge_status")
            self._log_event(f"powerpulse_{typed_action}_unconfirmed", str(last_status))
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="powerpulse_action_not_confirmed",
            ) from None
        else:
            self._log_event(f"powerpulse_{typed_action}", str(confirmed_status))
        finally:
            self._clear_pending_write(record)

    async def async_set_powerpulse_max_current(self, max_current_a: int) -> None:
        """Set this PowerPulse 2's maximum charge current (PLAN-146).

        Unlike start/stop, this write has only the sibling route: the app's
        own write path stays on a PowerOcean's set topic on every recording
        on file, and the wallbox's own-channel write (the route
        `charge_action_route()` names `"own"`, used by start/stop on an
        account with no PowerOcean) has no such recording to build from -
        so an entry without a PowerOcean is refused rather than guessed at
        (PLAN-146 decision 2).

        No charging-state precondition applies: the app changes the maximum
        current both while idle and while charging. Confirmation runs on
        `ev_max_current_a` reaching the requested value on the frame being
        applied (`PendingDeviceWrite.expected_value`,
        `_resolve_pending_write`), never on the accumulated store, for the
        same reason ADR-009 decision 4 gives for start/stop: a stale reading
        already in `self._device_data` must not confirm a write that has not
        actually happened yet. The lock, the pending-record lifecycle and the
        confirmation wait happening outside the lock all follow
        `async_set_powerpulse_charge_action` above.
        """
        from ..ecoflow.energy_stream import build_powerpulse_param_set_current_payload
        from .core import PendingDeviceWrite

        low, high = POWERPULSE2_MAX_CURRENT_RANGE_A

        async with self._pending_write_lock:
            if self._shutdown:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_action_not_delivered",
                )
            if type(max_current_a) is not int or not (low <= max_current_a <= high):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_max_current_range",
                    translation_placeholders={"min": str(low), "max": str(high)},
                )
            if self._pending_write is not None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_action_in_progress",
                )
            route = self.charge_action_route()
            if route is None:
                if self._powerocean_coordinators() is None:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerpulse_action_not_delivered",
                    )
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_sibling_missing",
                )
            if route == "own":
                # The write path on the wallbox's own channel is unevidenced
                # (PLAN-146 decision 2) - unlike start/stop, no recording
                # shows the app doing this without a PowerOcean in the loop.
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_max_current_needs_powerocean",
                )
            sibling = self.powerocean_sibling()
            if sibling is None:
                # Unreachable within one event-loop tick: same guard as
                # `async_set_powerpulse_charge_action` above.
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_action_not_delivered",
                )
            sibling_mqtt = sibling._mqtt_client
            if sibling_mqtt is None or not sibling_mqtt.is_connected():
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_sibling_offline",
                )
            dev_addr = self._device_data.get("ev_charger_dev_addr")
            dev_sn = self._device_data.get("ev_charger_sn")
            if (
                not isinstance(dev_addr, int)
                or not isinstance(dev_sn, str)
                or len(dev_sn) != 16
            ):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_descriptor_missing",
                )
            try:
                payload = build_powerpulse_param_set_current_payload(
                    max_current_a, dev_addr, dev_sn, sibling.device_sn
                )
            except ValueError:
                # The range is already checked above; a ValueError here can
                # only be a malformed serial (dev_sn/sibling.device_sn) - the
                # same failure mode as the descriptor check just above.
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_descriptor_missing",
                ) from None
            future: asyncio.Future[str | float] = self.hass.loop.create_future()
            record = PendingDeviceWrite(
                action="max_current",
                issued_at=time.monotonic(),
                future=future,
                state_key="ev_max_current_a",
                expected_value=float(max_current_a),
            )
            # Same lifecycle as `async_set_powerpulse_charge_action`: the
            # record lives exactly as long as this call and is cleared on
            # every exit in the `finally` below.
            self._pending_write = record
            try:
                delivered = await sibling.async_send_proto_set_command(
                    payload, "powerpulse_max_current"
                )
                if not delivered:
                    self._log_event("powerpulse_max_current_not_delivered", "")
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerpulse_action_not_delivered",
                    )
            except BaseException:
                self._clear_pending_write(record)
                raise

        try:
            confirmed_value = await asyncio.wait_for(
                future, POWERPULSE2_MAX_CURRENT_WINDOW_S
            )
        except TimeoutError:
            last_reported = self._device_data.get("ev_max_current_a")
            self._log_event("powerpulse_max_current_unconfirmed", str(last_reported))
            reported = (
                f"{last_reported:g} A"
                if isinstance(last_reported, (int, float))
                else "no value yet"
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="powerpulse_max_current_not_confirmed",
                translation_placeholders={"reported": reported},
            ) from None
        else:
            self._log_event("powerpulse_max_current", str(confirmed_value))
        finally:
            self._clear_pending_write(record)

    async def async_set_powerpulse_charge_current(self, current_a: int) -> None:
        """Set this standalone PowerPulse 2's charge current (issue #7, 2026-09-14).

        The mirror image of `async_set_powerpulse_max_current` above: this
        write has only the own-channel route. The app's write for this field
        has only ever been seen on a wallbox's own set topic, on an account
        without a PowerOcean - never relayed through one, unlike the maximum
        current (issue #7, 2026-09-14 recording). So `route == "sibling"` is
        refused here, the opposite of the guard in
        `async_set_powerpulse_max_current`.

        No charging-state precondition: unlike start/stop there is no
        recorded evaluation that assumes a particular session state before
        this write, so none is enforced here either. Confirmation runs on
        `ev_charge_current_a` reaching the requested value on the frame being
        applied (`PendingDeviceWrite.expected_value`,
        `_resolve_pending_write`), the same rule every other setting write in
        this file follows.
        """
        from ..ecoflow.energy_stream import (
            build_powerpulse_standalone_current_ctrl_payload,
        )
        from .core import PendingDeviceWrite

        low, high = POWERPULSE2_MAX_CURRENT_RANGE_A

        async with self._pending_write_lock:
            if self._shutdown:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_action_not_delivered",
                )
            if type(current_a) is not int or not (low <= current_a <= high):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_charge_current_range",
                    translation_placeholders={"min": str(low), "max": str(high)},
                )
            if self._pending_write is not None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_action_in_progress",
                )
            route = self.charge_action_route()
            if route is None:
                if self._powerocean_coordinators() is None:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerpulse_action_not_delivered",
                    )
                # Two or more PowerOceans (`charge_action_route()`): the
                # own-topic write has only been observed on an account with
                # none, so it is refused here too - not "sibling_missing",
                # which frames this as picking a relay parent, the sibling
                # route's failure mode, not this own-only one's.
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_charge_current_route_ambiguous",
                )
            if route == "sibling":
                # The write path on a PowerOcean-coupled wallbox is
                # unevidenced - the opposite of the guard in
                # `async_set_powerpulse_max_current` above.
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_charge_current_needs_own_channel",
                )
            if self._mqtt_client is None or not self._mqtt_client.is_connected():
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_own_channel_offline",
                )
            payload = build_powerpulse_standalone_current_ctrl_payload(
                current_a, self.device_sn
            )
            future: asyncio.Future[str | float] = self.hass.loop.create_future()
            record = PendingDeviceWrite(
                action="charge_current",
                issued_at=time.monotonic(),
                future=future,
                state_key="ev_charge_current_a",
                expected_value=float(current_a),
            )
            self._pending_write = record
            try:
                delivered = await self.async_send_proto_set_command(
                    payload, "powerpulse_charge_current"
                )
                if not delivered:
                    self._log_event("powerpulse_charge_current_not_delivered", "")
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerpulse_action_not_delivered",
                    )
            except BaseException:
                self._clear_pending_write(record)
                raise

        try:
            confirmed_value = await asyncio.wait_for(
                future, POWERPULSE2_CHARGE_CURRENT_WINDOW_S
            )
        except TimeoutError:
            last_reported = self._device_data.get("ev_charge_current_a")
            self._log_event("powerpulse_charge_current_unconfirmed", str(last_reported))
            reported = (
                f"{last_reported:g} A"
                if isinstance(last_reported, (int, float))
                else "no value yet"
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="powerpulse_charge_current_not_confirmed",
                translation_placeholders={"reported": reported},
            ) from None
        else:
            self._log_event("powerpulse_charge_current", str(confirmed_value))
        finally:
            self._clear_pending_write(record)

    async def async_set_powerpulse_charge_mode(self, option: str) -> None:
        """Set this PowerPulse 2's charging mode (PLAN-147).

        Same sibling-only routing as `async_set_powerpulse_max_current`: the
        app's write path stays on a PowerOcean's set topic on every recording
        on file, so an entry without a PowerOcean is refused rather than
        guessed at (PLAN-147 decision 2).

        `option` must be one of `POWERPULSE2_CHARGE_MODE_OPTIONS`. Both the
        option check and the "smart" refusal run before the lock takes any
        other action - before the in-progress check, before anything is
        published - because neither one is a device failure that should
        consume the pending-write slot. "smart" is refused because the app
        never sends that mode without a `smart_mode` block (departure time,
        charging target) this integration does not build, and a bare write
        to it is unobserved (PLAN-147 decision 3); the option stays in the
        select's list so the entity can still display the wallbox's own
        smart-mode state.

        Confirmation runs on `ev_charge_mode` reaching the requested option
        on the frame being applied (`PendingDeviceWrite.expected_value`,
        `_resolve_pending_write`), the same "never on the accumulated
        store" reasoning `async_set_powerpulse_max_current` gives, within
        `POWERPULSE2_SETTINGS_WINDOW_S` (20 s). The confirming frame is the
        wallbox's settings report, about a second after the write, which
        `_resolve_wallbox_charge_mode` turns into `ev_charge_mode`; the
        heartbeat can confirm it only while no settings report from the last
        ten seconds (`POWERPULSE2_SETTINGS_MAX_AGE_S`) owns the key.
        """
        from ..ecoflow.energy_stream import build_powerpulse_param_set_mode_payload
        from .core import PendingDeviceWrite

        async with self._pending_write_lock:
            if self._shutdown:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_action_not_delivered",
                )
            if option not in POWERPULSE2_CHARGE_MODE_OPTIONS:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_charge_mode_unknown",
                    translation_placeholders={"option": str(option)},
                )
            if option == "smart":
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_charge_mode_smart_in_app",
                )
            if self._pending_write is not None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_action_in_progress",
                )
            route = self.charge_action_route()
            if route is None:
                if self._powerocean_coordinators() is None:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerpulse_action_not_delivered",
                    )
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_sibling_missing",
                )
            if route == "own":
                # The write path on the wallbox's own channel is unevidenced
                # (PLAN-147 decision 2, the same reasoning
                # async_set_powerpulse_max_current gives) - no recording
                # shows the app doing this without a PowerOcean in the loop.
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_charge_mode_needs_powerocean",
                )
            sibling = self.powerocean_sibling()
            if sibling is None:
                # Unreachable within one event-loop tick: same guard as
                # `async_set_powerpulse_max_current` above.
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_action_not_delivered",
                )
            sibling_mqtt = sibling._mqtt_client
            if sibling_mqtt is None or not sibling_mqtt.is_connected():
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_sibling_offline",
                )
            dev_addr = self._device_data.get("ev_charger_dev_addr")
            dev_sn = self._device_data.get("ev_charger_sn")
            if (
                not isinstance(dev_addr, int)
                or not isinstance(dev_sn, str)
                or len(dev_sn) != 16
            ):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_descriptor_missing",
                )
            work_mode = POWERPULSE2_CHARGE_MODE_WIRE[option]
            try:
                payload = build_powerpulse_param_set_mode_payload(
                    work_mode, dev_addr, dev_sn, sibling.device_sn
                )
            except ValueError:
                # The option and its wire value are already validated above;
                # a ValueError here can only be a malformed serial
                # (dev_sn/sibling.device_sn) - the same failure mode as the
                # descriptor check just above.
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_descriptor_missing",
                ) from None
            future: asyncio.Future[str | float] = self.hass.loop.create_future()
            record = PendingDeviceWrite(
                action="charge_mode",
                issued_at=time.monotonic(),
                future=future,
                state_key="ev_charge_mode",
                expected_value=option,
            )
            # Same lifecycle as `async_set_powerpulse_max_current`: the
            # record lives exactly as long as this call and is cleared on
            # every exit in the `finally` below.
            self._pending_write = record
            try:
                delivered = await sibling.async_send_proto_set_command(
                    payload, "powerpulse_charge_mode"
                )
                if not delivered:
                    self._log_event("powerpulse_charge_mode_not_delivered", "")
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerpulse_action_not_delivered",
                    )
            except BaseException:
                self._clear_pending_write(record)
                raise

        try:
            confirmed_value = await asyncio.wait_for(
                future, POWERPULSE2_SETTINGS_WINDOW_S
            )
        except TimeoutError:
            last_reported = self._device_data.get("ev_charge_mode")
            self._log_event("powerpulse_charge_mode_unconfirmed", str(last_reported))
            reported = (
                last_reported if isinstance(last_reported, str) else "no value yet"
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="powerpulse_charge_mode_not_confirmed",
                translation_placeholders={"reported": reported},
            ) from None
        else:
            self._log_event("powerpulse_charge_mode", str(confirmed_value))
        finally:
            self._clear_pending_write(record)

    def _fresh_settings_report(self) -> dict[str, Any]:
        """The latest settings report, if a write may be built on it (#495).

        Raises `_SettingsReportNotFresh` (`report_missing`, or `report_stale`)
        when there is none, when it is older than
        `POWERPULSE2_SETTINGS_MAX_AGE_S`, or when no report was recorded since
        the last settings write left: a write that was sent and then cancelled
        by its caller has changed the wallbox in a way the older report does
        not show, and rebuilding the byte from that report would send the old
        bits back. Waiting for the next report fixes all three.
        """
        report = self._settings_report
        if report is None:
            raise _SettingsReportNotFresh(
                translation_domain=DOMAIN,
                translation_key="powerpulse_continuous_report_missing",
            )
        received_at, values = report
        if (
            time.monotonic() - received_at > POWERPULSE2_SETTINGS_MAX_AGE_S
            or self._settings_report_seq <= self._settings_published_seq
        ):
            raise _SettingsReportNotFresh(
                translation_domain=DOMAIN,
                translation_key="powerpulse_continuous_report_stale",
            )
        return values

    async def _async_write_powerpulse_settings(
        self,
        *,
        action: str,
        state_key: str,
        needs_powerocean_key: str,
        not_confirmed_key: str,
        plan: Callable[[], _SettingsWrite],
        describe_reported: Callable[[Any], str],
    ) -> None:
        """Publish one PowerPulse 2 settings write and wait for its read-back.

        The shared body of the PowerPulse 2 settings writes (PLAN-172, PLAN-188):
        the sibling-only routing, the pending-record lifecycle and the
        confirmation wait outside the lock all follow
        `async_set_powerpulse_max_current`. The caller has
        already range-checked its own value. `plan` runs inside the lock, after
        every route check and immediately before the payload is built, so a
        write that depends on the latest settings report (Continuous charging)
        reads it at dispatch time and not before a lock wait. It may raise
        `HomeAssistantError` itself.

        Settings writes queue instead of refusing each other (#495). While
        another settings write waits for its read-back, this call waits for it
        to finish (confirmed, failed or timed out) and then publishes, up to
        the confirmation window plus `POWERPULSE2_SETTINGS_FRESH_WAIT_S`; past
        that it raises `powerpulse_action_in_progress`. A write of another
        kind (start, stop, maximum current, charging mode) still refuses at
        once, as it refuses a settings write. The newest call per `action` (one
        target: a display value, one switch bit, a current) wins: a call still
        waiting for its turn when a newer call for the same target arrives
        ends without sending and without an error, because the newer call
        carries the final choice. The older call is dropped when the newer one
        is made, before the newer one passes any check: if the newer call is
        then refused, the older caller has still returned without an error and
        nothing was sent. No PowerPulse 2 setter shows an optimistic state, so
        the entity stays on the value the wallbox reported. Calls for different
        targets run one after
        the other, each built on the report that confirmed the one before
        (waiters wake first come, first served, and the order of two different
        targets does not change what the wallbox ends up with). The `action`,
        not the `state_key`, names the target, because the three switch-bit
        switches share one state key and one must not cancel another.

        When `plan` finds the settings report missing or older than
        `POWERPULSE2_SETTINGS_MAX_AGE_S`, or no report was recorded since the
        last settings write left (`_fresh_settings_report`), the call waits up
        to `POWERPULSE2_SETTINGS_FRESH_WAIT_S`, never past its queue bound, for
        the next report, then runs `plan` again. Nothing is sent while it
        waits, and a report that still does not come is refused with the
        report's own text. Only the display and switch-bit writes read the
        report.
        """
        from ..ecoflow.energy_stream import (
            build_powerpulse_param_set_display_payload,
            build_powerpulse_param_set_settings_payload,
        )
        from .core import PendingDeviceWrite

        loop = asyncio.get_running_loop()
        self._settings_write_seq += 1
        ticket = self._settings_write_seq
        replaced = [t for t, a in self._settings_calls.items() if a == action]
        for older in replaced:
            del self._settings_calls[older]
        self._settings_calls[ticket] = action
        if replaced:
            # An older call for this target that still waits ends now.
            self._notify_settings_waiters()
        queue_deadline = (
            loop.time()
            + POWERPULSE2_SETTINGS_WINDOW_S
            + POWERPULSE2_SETTINGS_FRESH_WAIT_S
        )
        fresh_deadline: float | None = None
        try:
            record: PendingDeviceWrite | None = None
            while record is None:
                not_fresh: _SettingsReportNotFresh | None = None
                async with self._pending_write_lock:
                    # Taken with the checks below and before any await, so a
                    # change after them wakes this very event.
                    wake = self._settings_wake
                    if self._shutdown:
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="powerpulse_action_not_delivered",
                        )
                    if ticket not in self._settings_calls:
                        return
                    pending = self._pending_write
                    if pending is not None and not pending.settings_write:
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="powerpulse_action_in_progress",
                        )
                    if pending is None:
                        route = self.charge_action_route()
                        if route is None:
                            if self._powerocean_coordinators() is None:
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="powerpulse_action_not_delivered",
                                )
                            raise HomeAssistantError(
                                translation_domain=DOMAIN,
                                translation_key="powerpulse_sibling_missing",
                            )
                        if route == "own":
                            # No recording shows the app writing these settings
                            # on the wallbox's own channel without a PowerOcean
                            # in the loop, the same reasoning
                            # `async_set_powerpulse_max_current` gives.
                            raise HomeAssistantError(
                                translation_domain=DOMAIN,
                                translation_key=needs_powerocean_key,
                            )
                        sibling = self.powerocean_sibling()
                        if sibling is None:
                            # Unreachable within one event-loop tick: same
                            # guard as `async_set_powerpulse_max_current`.
                            raise HomeAssistantError(
                                translation_domain=DOMAIN,
                                translation_key="powerpulse_action_not_delivered",
                            )
                        sibling_mqtt = sibling._mqtt_client
                        if sibling_mqtt is None or not sibling_mqtt.is_connected():
                            raise HomeAssistantError(
                                translation_domain=DOMAIN,
                                translation_key="powerpulse_sibling_offline",
                            )
                        dev_addr = self._device_data.get("ev_charger_dev_addr")
                        dev_sn = self._device_data.get("ev_charger_sn")
                        if (
                            not isinstance(dev_addr, int)
                            or not isinstance(dev_sn, str)
                            or len(dev_sn) != 16
                        ):
                            raise HomeAssistantError(
                                translation_domain=DOMAIN,
                                translation_key="powerpulse_descriptor_missing",
                            )
                        try:
                            write = plan()
                        except _SettingsReportNotFresh as err:
                            not_fresh = err
                        else:
                            try:
                                if write.display is not None:
                                    payload = (
                                        build_powerpulse_param_set_display_payload(
                                            *write.display,
                                            dev_addr,
                                            dev_sn,
                                            sibling.device_sn,
                                        )
                                    )
                                else:
                                    payload = (
                                        build_powerpulse_param_set_settings_payload(
                                            write.fields,
                                            dev_addr,
                                            dev_sn,
                                            sibling.device_sn,
                                        )
                                    )
                            except ValueError:
                                # The caller's value is already validated; a
                                # ValueError here can only be a malformed
                                # serial, the same failure mode as the
                                # descriptor check just above.
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="powerpulse_descriptor_missing",
                                ) from None
                            future: asyncio.Future[str | float] = loop.create_future()
                            record = PendingDeviceWrite(
                                action=action,
                                issued_at=time.monotonic(),
                                future=future,
                                state_key=state_key,
                                expected_value=write.expected_value,
                                bit_mask=write.bit_mask,
                                settings_write=True,
                            )
                            # Same lifecycle as `async_set_powerpulse_max_current`:
                            # the record lives exactly as long as this call and
                            # is cleared on every exit.
                            self._pending_write = record
                            # Marked before the send, not after it: a call
                            # cancelled during the send hop may still have
                            # published, so the next write waits for a report
                            # newer than this one either way (#495 review).
                            self._settings_published_seq = self._settings_report_seq
                            try:
                                delivered = await sibling.async_send_proto_set_command(
                                    payload, f"powerpulse_{action}"
                                )
                                if not delivered:
                                    self._log_event(
                                        f"powerpulse_{action}_not_delivered", ""
                                    )
                                    raise HomeAssistantError(
                                        translation_domain=DOMAIN,
                                        translation_key="powerpulse_action_not_delivered",
                                    )
                            except BaseException:
                                self._clear_pending_write(record)
                                raise
                if record is not None:
                    break
                # Nothing was sent. Wait outside the lock, so a start or stop
                # is not held up, for the next change this call cares about.
                if not_fresh is None:
                    # Behind another settings write.
                    remaining = queue_deadline - loop.time()
                    if remaining <= 0:
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="powerpulse_action_in_progress",
                        )
                else:
                    if fresh_deadline is None:
                        fresh_deadline = min(
                            loop.time() + POWERPULSE2_SETTINGS_FRESH_WAIT_S,
                            queue_deadline,
                        )
                    remaining = fresh_deadline - loop.time()
                    if remaining <= 0:
                        raise not_fresh
                # A timeout only ends this wait; the bound is checked above on
                # the next pass, after one more look at the state.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), remaining)

            try:
                confirmed_value = await asyncio.wait_for(
                    record.future, POWERPULSE2_SETTINGS_WINDOW_S
                )
            except TimeoutError:
                last_reported = self._device_data.get(state_key)
                self._log_event(f"powerpulse_{action}_unconfirmed", str(last_reported))
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key=not_confirmed_key,
                    translation_placeholders={
                        "reported": describe_reported(last_reported)
                    },
                ) from None
            else:
                self._log_event(f"powerpulse_{action}", str(confirmed_value))
            finally:
                self._clear_pending_write(record)
        finally:
            self._settings_calls.pop(ticket, None)
            # The next call in line may be waiting for this one to end.
            self._notify_settings_waiters()

    async def async_set_powerpulse_solar_min_current(self, current_a: int) -> None:
        """Set the Solar mode's minimum charge current (PLAN-172, issue #480).

        Writes field 4 of `EDevPileParamSet` alone, no mode and no switch
        bits: the 2026-10-04 field-4-only runs show the wallbox taking exactly
        that. Same range as the maximum current. Confirmed on
        `ev_solar_min_current_a` reaching the requested value on the frame
        being applied. Routing, lock and wait: `_async_write_powerpulse_settings`.
        """
        low, high = POWERPULSE2_MAX_CURRENT_RANGE_A
        if type(current_a) is not int or not (low <= current_a <= high):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="powerpulse_solar_min_current_range",
                translation_placeholders={"min": str(low), "max": str(high)},
            )
        await self._async_write_powerpulse_settings(
            action="solar_min_current",
            state_key="ev_solar_min_current_a",
            needs_powerocean_key="powerpulse_solar_min_current_needs_powerocean",
            not_confirmed_key="powerpulse_solar_min_current_not_confirmed",
            plan=lambda: _SettingsWrite({4: current_a * 10}, float(current_a)),
            describe_reported=_describe_amps,
        )

    async def async_set_powerpulse_custom_current(self, current_a: int) -> None:
        """Set the Custom mode's charge current (PLAN-172, issue #480).

        Writes field 6 of `EDevPileParamSet` alone and confirms on
        `ev_custom_current_a`, the same shape as
        `async_set_powerpulse_solar_min_current`, with one difference: a value
        above the reported Maximum Current is refused before anything is
        published. The Solar minimum current has no such check, the wallbox
        keeps it above the maximum (issue #480, 2026-10-05).
        """
        low, high = POWERPULSE2_MAX_CURRENT_RANGE_A
        if type(current_a) is not int or not (low <= current_a <= high):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="powerpulse_custom_current_range",
                translation_placeholders={"min": str(low), "max": str(high)},
            )

        def plan() -> _SettingsWrite:
            # The wallbox does not store a Custom current above its configured
            # Maximum Current (a write of 15 A over 14 A was never reported
            # back on a C376, issue #481, 2026-10-05), so it is refused here
            # with the maximum named instead of failing after the window. The
            # maximum is read when the write is built. An unreported maximum
            # lets the write through: the value is never guessed.
            maximum = self._device_data.get("ev_max_current_a")
            if (
                isinstance(maximum, (int, float))
                and not isinstance(maximum, bool)
                and current_a > maximum
            ):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_custom_current_above_maximum",
                    translation_placeholders={
                        "requested": str(current_a),
                        "maximum": f"{maximum:g}",
                    },
                )
            return _SettingsWrite({6: current_a * 10}, float(current_a))

        await self._async_write_powerpulse_settings(
            action="custom_current",
            state_key="ev_custom_current_a",
            needs_powerocean_key="powerpulse_custom_current_needs_powerocean",
            not_confirmed_key="powerpulse_custom_current_not_confirmed",
            plan=plan,
            describe_reported=_describe_amps,
        )

    async def async_set_powerpulse_phase_setting(self, option: str) -> None:
        """Set the phase the wallbox charges on (PLAN-172, issue #482).

        `option` is one of `POWERPULSE2_PHASE_SETTING_OPTIONS`. Writes field 5
        of `EDevPileParamSet` alone; Auto is wire value 0 and is written to the
        wire like any other (the builder tracks presence). Confirmed on
        `ev_phase_setting` reaching the wire value.
        """
        if option not in POWERPULSE2_PHASE_SETTING_OPTIONS:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="powerpulse_phase_setting_unknown",
                translation_placeholders={"option": str(option)},
            )
        wire = POWERPULSE2_PHASE_SETTING_WIRE[option]
        wire_to_option = {v: k for k, v in POWERPULSE2_PHASE_SETTING_WIRE.items()}
        await self._async_write_powerpulse_settings(
            action="phase_setting",
            state_key="ev_phase_setting",
            needs_powerocean_key="powerpulse_phase_setting_needs_powerocean",
            not_confirmed_key="powerpulse_phase_setting_not_confirmed",
            plan=lambda: _SettingsWrite({5: wire}, float(wire)),
            describe_reported=lambda value: (
                wire_to_option.get(value, str(value))
                if type(value) is int
                else "no value yet"
            ),
        )

    async def async_set_powerpulse_switch_bit(
        self, bit: int, enabled: bool, key: str
    ) -> None:
        """Set or clear one bit of the wallbox's settings switch byte.

        The one write behind the three settings switches (Continuous charging,
        Block Battery Discharge, Plug and Play; PLAN-172, PLAN-187). `bit` is
        the switch definition's `bit_mask`, `enabled` the requested state (for
        the discharge block, True blocks the discharge, the app's polarity) and
        `key` the switch's entity key; the write's action name, which labels
        the send and the event log, is the key without its `ev_` prefix. The
        wallbox takes the whole byte, so the write is `{1: new_bits}` with the
        byte rebuilt from the latest settings report. Field 1 alone, no mode
        and no Solar minimum:
        proven on the reporter's C376 in both directions for Continuous
        (2026-10-04), where the settings report that followed each write showed
        the new bits with the mode and the Solar minimum unchanged.

        The report is read at dispatch time inside the lock (`plan`), as the
        frame snapshot `_record_settings_report` took, and refused when there
        is none, when it is older than `POWERPULSE2_SETTINGS_MAX_AGE_S` or
        when it lacks the bits (`report_missing` or `report_stale`, where
        waiting helps), or when the bits are not a byte the builder can send
        (`report_unusable`, where waiting does not help). Confirmed on the
        requested bit of `ev_settings_switch_bits` alone (`bit_mask`); the
        other bits in the byte are not this write's business. The refusal and
        confirmation texts are keyed `powerpulse_continuous_*` for all three.
        """
        action = key.removeprefix("ev_")

        def plan() -> _SettingsWrite:
            values = self._fresh_settings_report()
            bits = values.get("ev_settings_switch_bits")
            if type(bits) is not int:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_continuous_report_missing",
                )
            # A report that exists but carries a byte the builder cannot send:
            # waiting for the next report cannot help, so this is its own
            # message and names the field and the value.
            if not 0 <= bits <= 255:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerpulse_continuous_report_unusable",
                    translation_placeholders={
                        "field": "switch bits",
                        "value": str(bits),
                    },
                )
            new_bits = bits | bit if enabled else bits & ~bit
            return _SettingsWrite({1: new_bits}, float(new_bits), bit)

        await self._async_write_powerpulse_settings(
            action=action,
            state_key="ev_settings_switch_bits",
            needs_powerocean_key="powerpulse_continuous_needs_powerocean",
            not_confirmed_key="powerpulse_continuous_not_confirmed",
            plan=plan,
            describe_reported=lambda bits: _describe_switch_bit(bits, bit),
        )

    async def async_set_powerpulse_display(self, key: str, value: float) -> None:
        """Set one value of the wallbox's display block (PLAN-188, issue #495).

        `key` is one of `ev_led_enabled`, `ev_screen_enabled` (`value` 0 or 1,
        a bool is accepted), `ev_led_brightness_pct` and
        `ev_screen_brightness_pct` (`value` one of
        `POWERPULSE2_DISPLAY_BRIGHTNESS_STEPS`, an `int`; a non-integer
        value is refused with `powerpulse_display_brightness_invalid`, like any
        value that is not one of the steps). The wallbox takes
        the display as one six-byte value (field 21 of `EDevPileParamSet`),
        so the write rebuilds `[LED on, Screen on, LED brightness, Screen
        brightness, 0, 0]` from the latest settings report, changes the one
        byte that is asked for and sends that block alone. The two trailing
        bytes are the wallbox's own and are always sent as 0, never copied from
        the report. A brightness is writable while the screen or the LED is
        off: the write does not look at the on/off state.

        The report is read at dispatch time inside the lock (`plan`), like the
        switch-bit write: refused when there is none (`report_missing`, waiting
        helps), when it is older than `POWERPULSE2_SETTINGS_MAX_AGE_S`
        (`report_stale`), or when a value in it is not one the builder can send
        or is absent from a settings report that was received
        (`report_unusable`: the parser leaves out a byte that is not a value
        the wallbox takes, and waiting does not help). Confirmed when
        a frame arriving after the write reports `key` at the requested value;
        nothing is published optimistically. The refusal and confirmation texts
        reuse the `powerpulse_continuous_*` keys, as the three settings
        switches do.
        """
        if key not in POWERPULSE2_DISPLAY_KEYS:
            raise ValueError(f"{key!r} is not a PowerPulse 2 display value")
        index = POWERPULSE2_DISPLAY_KEYS.index(key)
        if key in POWERPULSE2_DISPLAY_SWITCH_KEYS:
            if not powerpulse2_display_value_valid(key, value):
                raise ValueError(f"{key} takes 0 or 1, got {value!r}")
        elif not powerpulse2_display_value_valid(key, value):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="powerpulse_display_brightness_invalid",
                translation_placeholders={
                    "allowed": ", ".join(
                        str(step) for step in POWERPULSE2_DISPLAY_BRIGHTNESS_STEPS
                    ),
                    "value": str(value),
                },
            )
        target = int(value)

        def plan() -> _SettingsWrite:
            values = self._fresh_settings_report()
            block: list[int] = []
            for name, label in _POWERPULSE2_DISPLAY_BYTES:
                reported = values.get(name)
                if reported is None:
                    if "ev_settings_switch_bits" in values:
                        # A settings report was received and the parser left
                        # this byte out as not a value the wallbox takes:
                        # waiting for the next report does not help.
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="powerpulse_continuous_report_unusable",
                            translation_placeholders={
                                "field": label,
                                "value": "no valid value",
                            },
                        )
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerpulse_continuous_report_missing",
                    )
                if not powerpulse2_display_value_valid(name, reported):
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerpulse_continuous_report_unusable",
                        translation_placeholders={
                            "field": label,
                            "value": str(reported),
                        },
                    )
                block.append(int(reported))
            block[index] = target
            led_on, screen_on, led_pct, screen_pct = block
            return _SettingsWrite(
                {},
                float(target),
                display=(led_on, screen_on, led_pct, screen_pct),
            )

        await self._async_write_powerpulse_settings(
            action=key.removeprefix("ev_"),
            state_key=key,
            needs_powerocean_key="powerpulse_continuous_needs_powerocean",
            not_confirmed_key="powerpulse_continuous_not_confirmed",
            plan=plan,
            describe_reported=lambda reported: _describe_display(key, reported),
        )

    def _require_wallbox_action_state(self, action: Literal["start", "stop"]) -> None:
        """Raise unless the wallbox's last reported state allows `action`.

        Start only from `finishing`, stop from `charging` or
        `suspended_charger` (ADR-009 decision 5, amended for #515), on both
        routes, checked after the route's own preconditions so the message
        names the first thing that is wrong.
        """
        status = self._device_data.get("ev_charge_status")
        if status not in POWERPULSE2_CHARGE_ACTION_PRECONDITION[action]:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="powerpulse_action_state",
                translation_placeholders={"status": str(status)},
            )

    def _clear_pending_write(self, record: PendingDeviceWrite) -> None:
        """Drop the pending record if it is still the one this call set."""
        if self._pending_write is record:
            self._pending_write = None

    async def _async_powerglow_write(
        self,
        action: str,
        state_key: str,
        expected_value: float | str,
        setting: dict[str, Any],
        validate: Callable[[str], None] | None = None,
    ) -> None:
        """Send one PowerGlow setting and wait for the rod to report it (PLAN-190).

        The shared body of every PowerGlow write: one `HeatingRodParamSet`
        carrying exactly one setting (`setting` is the keyword for
        `build_powerglow_param_set_payload`), published on this PowerOcean's
        own set topic and confirmed by `state_key` reaching `expected_value` on
        the next `212/8` report applied (`PendingDeviceWrite`,
        `_resolve_pending_write`) - never on the accumulated store, so a stale
        reading cannot confirm a write that has not happened. The record is
        tied to the rod it was addressed to (`require`): a report carrying
        another rod's serial does not confirm it.

        Preconditions run in this order, each with its own translated error:
        shutdown, Enhanced Mode with a live MQTT connection, the rod's serial
        as reported, `validate(serial)` (the caller's range check, which needs
        the serial for the power ceiling), no other write in flight. The check,
        the publish and the pending record happen under
        `self._pending_write_lock`; the confirmation wait happens outside it so
        a second write is refused promptly instead of queueing behind it.
        """
        from ..ecoflow.energy_stream import build_powerglow_param_set_payload
        from .core import PendingDeviceWrite

        async with self._pending_write_lock:
            if self._shutdown:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerglow_not_delivered",
                )
            mqtt_client = self._mqtt_client
            if (
                not self._enhanced_mode
                or mqtt_client is None
                or not mqtt_client.is_connected()
            ):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerglow_offline",
                )
            rod_sn = self._device_data.get("heating_rod_sn")
            if not isinstance(rod_sn, str) or len(rod_sn) != 16:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerglow_not_reported",
                )
            if validate is not None:
                validate(rod_sn)
            if self._pending_write is not None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerglow_write_in_progress",
                )
            try:
                payload = build_powerglow_param_set_payload(
                    rod_sn, self.device_sn, **setting
                )
            except ValueError:
                # The values are already checked; a ValueError here can only
                # be a malformed serial, the same failure mode as a rod serial
                # that has not been reported.
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="powerglow_not_reported",
                ) from None
            future: asyncio.Future[str | float] = self.hass.loop.create_future()
            record = PendingDeviceWrite(
                action=action,
                issued_at=time.monotonic(),
                future=future,
                state_key=state_key,
                expected_value=expected_value,
                require=("heating_rod_sn", rod_sn),
            )
            # The record lives exactly as long as this call and is cleared on
            # every exit in the `finally` below.
            self._pending_write = record
            try:
                delivered = await self.async_send_proto_set_command(
                    payload, f"powerglow_{action}"
                )
                if not delivered:
                    self._log_event(f"powerglow_{action}_not_delivered", "")
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="powerglow_not_delivered",
                    )
            except BaseException:
                self._clear_pending_write(record)
                raise

        try:
            confirmed_value = await asyncio.wait_for(future, POWERGLOW_WRITE_WINDOW_S)
        except TimeoutError:
            last_reported = self._device_data.get(state_key)
            self._log_event(f"powerglow_{action}_unconfirmed", str(last_reported))
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="powerglow_not_confirmed",
            ) from None
        else:
            self._log_event(f"powerglow_{action}", str(confirmed_value))
        finally:
            self._clear_pending_write(record)

    async def async_set_powerglow_value(
        self, setting: Literal["target_power", "target_temp"], value: int
    ) -> None:
        """Set the PowerGlow heating rod's target power (W) or target temperature (C).

        Enhanced Mode only: the write leaves on this PowerOcean's set topic and
        is confirmed on the rod's own `212/8` report
        (`heating_rod_target_power_w` / `heating_rod_target_temp_c`). Range:
        10 to 80 degrees; 0 up to the ceiling of the rod's serial prefix
        (`powerglow_max_power_w`). A value out of range is refused before
        anything is sent. Lock, record and wait: `_async_powerglow_write`.
        """
        from ..ecoflow.energy_stream import (
            POWERGLOW_TARGET_TEMP_C_MAX,
            POWERGLOW_TARGET_TEMP_C_MIN,
            powerglow_max_power_w,
        )

        def validate(rod_sn: str) -> None:
            if setting == "target_temp":
                low, high = POWERGLOW_TARGET_TEMP_C_MIN, POWERGLOW_TARGET_TEMP_C_MAX
                key = "powerglow_target_temp_range"
            else:
                low, high = 0, powerglow_max_power_w(rod_sn)
                key = "powerglow_target_power_range"
            if type(value) is not int or not low <= value <= high:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key=key,
                    translation_placeholders={"min": str(low), "max": str(high)},
                )

        if setting == "target_temp":
            state_key = "heating_rod_target_temp_c"
            builder_arg = "target_temp_c"
        else:
            state_key = "heating_rod_target_power_w"
            builder_arg = "target_power_w"
        await self._async_powerglow_write(
            setting,
            state_key,
            float(value),
            {builder_arg: value},
            validate,
        )

    async def async_set_powerglow_operating_mode(
        self, option: Literal["off", "solar", "manual"]
    ) -> None:
        """Set the PowerGlow heating rod to off, Solar or Manual (PLAN-190).

        The device has two settings where the app shows one control: a run
        switch (`heating_rod_run_state`, 0 off / non-zero on) and a mode
        (`heating_rod_mode`, 0 Solar / 1 Manual). Off is always sent, as one
        write of the run switch, even when the rod already reports off.
        Solar and Manual configure the mode first and switch the rod on
        afterwards - the order of the hardware-tested community client - and
        each step is its own confirmed write. A step that is already true
        (mode already the target, rod already running) is not written again,
        and when both are already true nothing is sent at all.

        Both final steps are confirmed on the select's own derived key
        (`heating_rod_operating_mode`, the string the entity shows), so the
        write only succeeds once the rod reports the option that was asked
        for; the mode step, which leaves the rod off, is confirmed on
        `heating_rod_mode`. A mode change the rod refuses (the app blocks it
        in one device state) never reaches its `212/8` report, so that step
        ends as `powerglow_not_confirmed` and the run switch is not touched.
        An option outside off/solar/manual raises `ValueError` before
        anything is sent.
        """
        if option not in ("off", "solar", "manual"):
            raise ValueError(f"{option!r} is not a PowerGlow operating mode")
        if option == "off":
            await self._async_powerglow_write(
                "run_state", "heating_rod_operating_mode", "off", {"run_on": False}
            )
            return
        target_mode = 0 if option == "solar" else 1
        if self._device_data.get("heating_rod_mode") != target_mode:
            await self._async_powerglow_write(
                "mode", "heating_rod_mode", float(target_mode), {"mode": target_mode}
            )
        # Same reading of "on" as the parser's derived key: any non-zero run
        # state, not only 1.
        if self._device_data.get("heating_rod_run_state") in (None, 0):
            await self._async_powerglow_write(
                "run_state", "heating_rod_operating_mode", option, {"run_on": True}
            )

    async def async_send_delta3_set(self, command: dict[str, Any]) -> bool:
        """Apply a Delta 3 setting on whichever channel this entry is using.

        Developer keys write over the official HTTP endpoint
        `PUT /iot-open/sign/device/quota`. App logins have no HTTP endpoint and
        write the same setting as a binary ConfigWrite frame on the app channel
        instead - verified against hardware (ack plus readback).
        """
        if self._auth_method == AUTH_METHOD_APP:
            return await self._async_send_delta3_set_proto(command)

        if self._http_client is None:
            _LOGGER.warning(
                "Cannot apply setting for %s - no write channel available "
                "(no HTTP client for this entry)",
                self.device_tag,
            )
            return False

        result = await self._http_client.set_quota(command)
        params = command.get("params", {})
        if result is None:
            _LOGGER.warning("SET failed for %s: no response", self.device_tag)
            self._log_event("set_cmd_fail", f"params={list(params)[:3]}")
            return False

        _LOGGER.debug("SET applied for %s: %s", self.device_tag, params)
        self._log_event("set_cmd", f"params={list(params)[:3]}")
        # The device needs a moment before the change shows up in the quota.
        # The entity holds an optimistic value until then, so a plain refresh
        # on the next scheduled poll is enough.
        await self.async_request_refresh()
        return True

    async def _async_send_delta3_set_proto(self, command: dict[str, Any]) -> bool:
        """Write a Delta 3 setting as a ConfigWrite frame on the app channel."""
        from ..ecoflow.delta3_commands import build_proto_command

        params = command.get("params", {})
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning(
                "Cannot apply setting for %s - device connection is down",
                self.device_tag,
            )
            self._log_event("set_cmd_fail", f"params={list(params)[:3]}")
            return False

        payload = build_proto_command(command, self.device_sn)
        if payload is None:
            _LOGGER.warning(
                "Cannot apply setting for %s - unsupported control %s",
                self.device_tag,
                list(params)[:3],
            )
            self._log_event("set_cmd_fail", f"params={list(params)[:3]}")
            return False

        ok = await self.hass.async_add_executor_job(
            partial(self._mqtt_client.send_proto_set, payload, wait=True),
        )
        if not ok:
            _LOGGER.warning("SET failed for %s: not sent", self.device_tag)
            self._log_event("set_cmd_fail", f"params={list(params)[:3]}")
            return False

        _LOGGER.debug("SET sent for %s: %s", self.device_tag, params)
        self._log_event("set_cmd", f"params={list(params)[:3]}")
        # The device echoes the new value on its own report stream, so the
        # entity only has to hold its optimistic value until then.
        return True

    async def async_send_wave3_set(
        self, key: str, value: Any, mode: str | None = None
    ) -> bool:
        """Write one WAVE 3 (AC71) setting as a ConfigWrite frame.

        Two gates run before anything is built. `write_refusal` checks
        `value` against the device's own accumulated state (the operating
        mode a control is gated to, or a label the app's client never
        writes) - it needs accumulated state because `operating_mode`
        regularly arrives on a different frame than the write under
        consideration. `build_write` then checks `value` against the
        control's own table (range, step, allowed set) while encoding it.
        Either can raise `Wave3WriteRefused`, which the platform layer turns
        into a user-facing error via `raise_set_rejected`.

        `mode` is forwarded to `write_refusal` unchanged: it overrides the
        accumulated `operating_mode` for a gesture that switches mode and
        writes a mode-gated value in the same call, where the mode frame's
        own ack has not landed in accumulated state yet (E2, PLAN-047 Phase
        C review).

        The power switch is not in `WAVE3_CONTROLS` - both gates would
        report it as an unknown control - so it skips `write_refusal`
        entirely and is encoded by `build_power_write` instead. No optimistic
        write into `_device_data`: the entity holds its own requested value,
        and the device pushes the confirmed change on its own report stream
        within about 2 s (verified on the maintainer's unit, PLAN-047).
        """
        from ..ecoflow.wave3_commands import (
            WAVE3_CONTROLS,
            WAVE3_POWER_ON_FIELD,
            WAVE3_STANDBY_FIELD,
            Wave3WriteRefused,
            build_power_write,
            build_write,
            write_refusal,
        )

        if key != "power":
            reason = write_refusal(key, value, self._device_data, mode=mode)
            if reason is not None:
                _LOGGER.debug(
                    "WAVE 3 write refused for %s: %s", self.device_tag, reason
                )
                self._log_event("set_refused", f"key={key}")
                raise Wave3WriteRefused(reason)

        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning(
                "Cannot apply setting for %s - device connection is down",
                self.device_tag,
            )
            self._log_event("set_cmd_fail", f"key={key}")
            return False

        if key == "power":
            payload = build_power_write(bool(value), self.device_sn)
            field = WAVE3_POWER_ON_FIELD if value else WAVE3_STANDBY_FIELD
        else:
            try:
                payload = build_write(key, value, self.device_sn)
            except Wave3WriteRefused:
                self._log_event("set_refused", f"key={key}")
                raise
            field = WAVE3_CONTROLS[key].field

        # Recorded so `_check_config_write_ack` (mqtt_ingest.py, Paho thread)
        # can tell a rejection of our own write from a rejection of the
        # vendor app's write on the same shared set_reply topic. Written on
        # the event loop here, read on the Paho thread there: one dict
        # assignment and one `.get`, safe under CPython's GIL.
        self._config_writes_sent[field] = time.monotonic()

        ok = await self.hass.async_add_executor_job(
            partial(self._mqtt_client.send_proto_set, payload, wait=True),
        )
        if not ok:
            _LOGGER.warning("SET failed for %s: not sent", self.device_tag)
            self._log_event("set_cmd_fail", f"key={key}")
            return False

        _LOGGER.debug("SET sent for %s: key=%s", self.device_tag, key)
        self._log_event("set_cmd", f"key={key}")
        return True

    async def async_send_wave3_band(self, lower: float, upper: float) -> bool:
        """Write the WAVE 3 constant-temperature band as one ConfigWrite frame.

        Mirrors `async_send_wave3_set`: `band_write_refusal` checks the mode
        gate against accumulated state, `build_band_write` checks the values
        against their own table while encoding them. Either can raise
        `Wave3WriteRefused`, which the platform layer turns into a
        user-facing error via `raise_set_rejected`. The ack only echoes the
        upper limit (observed in the app's own traffic, PLAN-047); the field
        recorded in `_config_writes_sent` is the upper one for that reason.
        """
        from ..ecoflow.wave3_commands import (
            WAVE3_BAND_UPPER_FIELD,
            Wave3WriteRefused,
            band_write_refusal,
            build_band_write,
        )

        reason = band_write_refusal(self._device_data)
        if reason is not None:
            _LOGGER.debug(
                "WAVE 3 band write refused for %s: %s", self.device_tag, reason
            )
            self._log_event("set_refused", "key=constant_temp_band")
            raise Wave3WriteRefused(reason)

        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning(
                "Cannot apply setting for %s - device connection is down",
                self.device_tag,
            )
            self._log_event("set_cmd_fail", "key=constant_temp_band")
            return False

        try:
            payload = build_band_write(lower, upper, self.device_sn)
        except Wave3WriteRefused:
            self._log_event("set_refused", "key=constant_temp_band")
            raise

        self._config_writes_sent[WAVE3_BAND_UPPER_FIELD] = time.monotonic()

        ok = await self.hass.async_add_executor_job(
            partial(self._mqtt_client.send_proto_set, payload, wait=True),
        )
        if not ok:
            _LOGGER.warning("SET failed for %s: not sent", self.device_tag)
            self._log_event("set_cmd_fail", "key=constant_temp_band")
            return False

        _LOGGER.debug("SET sent for %s: key=constant_temp_band", self.device_tag)
        self._log_event("set_cmd", "key=constant_temp_band")
        return True

    async def async_send_set_command(self, command: dict[str, Any]) -> bool:
        """Send a SET command to the device via MQTT.

        The IoT API SET format:
        Topic: /open/{certAccount}/{SN}/set
        Payload: {"id": <ts>, "version": "1.0", ...command}
        """
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.debug(
                "Cannot send SET command - MQTT not connected (%s)", self.device_tag
            )
            return False

        msg_id = int(time.time() * 1000) % 1_000_000
        payload = json.dumps(
            {
                "from": "Android",
                "id": str(msg_id),
                "version": "1.0",
                **command,
            }
        )
        if self._mqtt_client.wss_mode:
            topic = (
                f"/app/{self._mqtt_client.user_id}/{self.device_sn}/thing/property/set"
            )
        else:
            topic = f"/open/{self._mqtt_client.cert_account}/{self.device_sn}/set"

        # wait=True: the caller reports the outcome to the user, so a
        # locally queued message is not good enough (issue #185).
        ok = await self.hass.async_add_executor_job(
            partial(self._mqtt_client.publish, topic, payload, 1, wait=True),
        )
        if ok:
            # The command body is what makes a failed write debuggable, but it
            # carries the full serial (the Smart Plug JSON has an "sn" field),
            # and reporters attach debug logs to public issues. Masked first
            # and truncated second, so the cut can never leave a serial
            # fragment too short for the mask to recognize.
            _LOGGER.debug(
                "SET command sent: %s -> %s",
                self._mqtt_client.mask_topic(topic),
                sanitize_frame(payload.encode(), [self.device_sn]).decode()[:120],
            )
            self._log_event("set_cmd", f"keys={list(command.keys())[:3]}")
        else:
            # The entity raises for the user; a warning here would only
            # duplicate the error Home Assistant already logs.
            _LOGGER.debug(
                "SET command not delivered: %s",
                self._mqtt_client.mask_topic(topic),
            )
            self._log_event("set_cmd_fail", f"keys={list(command.keys())[:3]}")
        return ok
