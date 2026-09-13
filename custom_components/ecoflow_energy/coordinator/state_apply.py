"""State application, battery state derivation, and energy integration."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import device_registry as dr

from ..const import (
    APP_SURPLUS_SYNC_MIN_INTERVAL_S,
    APP_SURPLUS_SYNC_USER_GRACE_S,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_WAVE3,
    DOMAIN,
    POWEROCEAN_SCHEDULE_ARMED_LATCH_S,
    POWERPULSE2_CHARGE_ACTION_CONFIRMED,
    STREAM_OWN_UNIT_ENTRY_HOLD_S,
)
from ..ecoflow.parsers.stream_ac5000_proto import (
    UNIT_POWER_BY_SN_KEY,
    UNIT_PV_BY_SN_KEY,
    UNIT_PV_ENTRY_SOC_KEY,
)
from ..ecoflow.parsers.stream_proto import SOC_FALLBACK_KEY
from ..ecoflow.parsers.wave3_proto import (
    WAVE3_ACTIVE_MODE_INPUTS,
    resolve_active_mode,
)

_LOGGER = logging.getLogger(__name__)


def _registry_device(
    registry: dr.DeviceRegistry, device_sn: str, entry_id: str
) -> dr.DeviceEntry | None:
    """Look a device up by its identifier on whichever registry API is present.

    Home Assistant 2026.9 deprecates `async_get_device` for identifier lookups,
    because identifiers are no longer unique across config entries, and logs a
    warning per call site; the replacement takes the owning config entry. The
    oldest release this integration supports has only the old call, so the
    lookup follows the registry rather than the other way round.
    """
    lookup = getattr(registry, "async_get_device_by_identifier", None)
    if lookup is not None:
        return lookup((DOMAIN, device_sn), entry_id)
    return registry.async_get_device(identifiers={(DOMAIN, device_sn)})


# ADR-013: at most two writes per divergent (app, ems) pair per process
# lifetime. The first write can only be judged against a report that left
# the device after it, and the 30s throttle usually lands the next
# evaluation on such a report but not always (the EmsChangeReport echo is
# event-driven, not on a fixed cadence), so a second write is the cheapest
# way to rule out a lost or raced first one. After two acknowledged writes
# and two reports that still hold the old value, the device has answered,
# and a third write would just be the loop this bound exists to stop.
_SURPLUS_SYNC_MAX_WRITES = 2

if TYPE_CHECKING:
    from ._typing import CoordinatorState as _Base
else:
    _Base = object


class StateApplyMixin(_Base):
    """Mixin applying parsed data to coordinator state."""

    def _resolve_soc(self, parsed: dict[str, Any]) -> None:
        """Decide whether a unit's own state of charge stands in for `soc_pct`.

        A Stream reports two states of charge: the system figure (protobuf
        field 262, quota `cmsBattSoc`), which a pair of units on a parallel
        cable reports as one battery, and the unit's own BMS figure (field
        242, quota `soc`). The parsers put the system figure on `soc_pct` and
        offer the unit figure on a private key when a message has no system
        figure. Only this layer sees the messages accumulate, so only this
        layer can tell the two cases apart: a single unit that never reports
        a system figure keeps its battery sensor from the stand-in, while a
        pair latches onto the system figure the moment it arrives and a
        BMS-only frame can no longer pull the sensor back to one unit's
        reading (#323).

        Not every member of a linked system computes that figure. In a
        two-unit capture the Stream Ultra X reported it tracking its own BMS
        across a full charge, while the Stream AC Pro beside it sent the
        field present and zero on all twelve frames that carried it, its own
        BMS meanwhile moving between 24% and 92%. A system figure is the mean
        over the members, and a mean cannot read zero while one member is
        above zero, so a zero arriving next to a positive unit figure is not
        this system's charge - it is a unit that does not calculate one. Such
        a frame yields to the unit's own reading and does not latch, which
        also lets a later BMS-only frame still stand in (#336).

        A genuinely empty battery is untouched: then the unit figure reads
        zero as well and nothing contradicts the system figure.

        The contradicting reading is looked up in the accumulated state, not
        only in the message at hand. `254/21` is an incremental container, so
        a frame may carry the system figure while the unit figure it
        contradicts arrived in an earlier one. Two frames of the capture
        carried 242 without 262 and none the other way round, but the
        container permits it, and reading only the current message would drop
        the sensor to zero until the next frame carrying both.
        """
        fallback = parsed.pop(SOC_FALLBACK_KEY, None)
        if "soc_pct" in parsed:
            if parsed["soc_pct"]:
                self._soc_from_system = True
                return
            # A zero never latches. Where the unit's own reading contradicts
            # it, that reading wins; where it does not, the zero stands but
            # stays open to a later frame that carries a figure.
            unit = parsed.get("unit_soc_pct") or self._device_data.get("unit_soc_pct")
            if unit:
                parsed["soc_pct"] = unit
            return
        if fallback is not None and not self._soc_from_system:
            parsed["soc_pct"] = fallback

    def _claim_pv_entry(
        self,
        entry: dict[str, Any],
        parsed: dict[str, Any],
        stats: dict[str, Any],
        *,
        own_connection: bool,
    ) -> dict[str, Any]:
        """The five PV keys of an entry claimed as ours, with its identity noted.

        The entry's own state of charge (`f50.1.2`) is taken out before the
        keys are published. When the frame is this unit's own and carries its
        precise state of charge as well, the two are written to the stats as
        a pair. With serials masked in a diagnostics download, that pair is
        the one thing that says whether the entry stamped with this unit's
        serial is this unit's reading: on the pair frames on file the
        connection owner's `soc_precise_pct` rounds to the `.2` of exactly
        one entry, and on the reporter's third download that is the entry
        with his serial on one connection and, at the restart, the other
        entry on the other (#401, 2026-09-13). Only a frame that carries both
        is recorded, so the pair is never stitched from two moments.
        """
        strings = dict(entry)
        soc = strings.pop(UNIT_PV_ENTRY_SOC_KEY, None)
        own = parsed.get("soc_precise_pct")
        if own_connection and soc is not None and own is not None:
            stats["own_pv_entry_soc_pct"] = soc
            stats["own_soc_precise_pct"] = round(float(own), 2)
        return strings

    def _resolve_unit_power(
        self, parsed: dict[str, Any], *, own_connection: bool, now: float
    ) -> None:
        """Take this unit's own entry out of the STREAM per-unit blocks.

        Two or three STREAM units linked on one account are reported as one
        system: the state of charge is their mean and the battery power their
        sum. The device also sends the underlying per-unit readings, each
        stamped with the serial it belongs to, and this picks out the one that
        is ours. Two blocks carry such entries: `f54` with the battery power
        per unit, and `f50` with the PV strings per unit (#401 - decoded flat,
        the master's frame left the neighbour's strings on the master's page).

        A block travels on whichever unit's connection happened to carry it
        (PLAN-145). A foreign entry seen on an own-connection frame is handed
        to the coordinator whose serial it names, through the identical
        `_apply_data` path its own frames use, with `own_connection=False` so
        the hand-over never vouches for that coordinator's connection. This
        device's own entry wins over a handed one for
        `STREAM_OWN_UNIT_ENTRY_HOLD_S` after it last arrived on this device's
        own connection: the two copies of one block differ (rounded, and a
        push behind), and last-writer-wins would alternate a value at the
        push cadence for no information. `own_connection=False` also skips
        the hand-over loop entirely, which is what rules out ping-pong
        between two coordinators handing each other the same entry back.

        `*_listed` / `own_*_matched` describe only the last own-connection
        block, unchanged from before. The eight hand-over counters
        (`linked_unit_stats`) are running totals.
        """
        power = parsed.pop(UNIT_POWER_BY_SN_KEY, None)
        strings = parsed.pop(UNIT_PV_BY_SN_KEY, None)
        stats = dict(self._unit_power_stats or {})
        handoffs: dict[str, dict[str, Any]] = {}

        if isinstance(power, dict) and power:
            own = power.get(self.device_sn)
            if own_connection:
                stats["units_listed"] = len(power)
                stats["own_unit_matched"] = own is not None
                if own is not None:
                    parsed["unit_batt_w"] = own
                    self._own_unit_entry_ts[UNIT_POWER_BY_SN_KEY] = now
                for serial, value in power.items():
                    if serial == self.device_sn:
                        continue
                    stats["units_handed_over"] = stats.get("units_handed_over", 0) + 1
                    handoffs.setdefault(serial, {})[UNIT_POWER_BY_SN_KEY] = {
                        serial: value
                    }
            elif own is not None:
                since = self._own_unit_entry_ts.get(UNIT_POWER_BY_SN_KEY, float("-inf"))
                if now - since >= STREAM_OWN_UNIT_ENTRY_HOLD_S:
                    parsed["unit_batt_w"] = own
                    stats["units_received"] = stats.get("units_received", 0) + 1
                else:
                    stats["units_held"] = stats.get("units_held", 0) + 1

        if isinstance(strings, dict) and strings:
            own_strings = strings.get(self.device_sn)
            if own_connection:
                stats["pv_units_listed"] = len(strings)
                stats["own_pv_matched"] = own_strings is not None
                if isinstance(own_strings, dict):
                    parsed.update(
                        self._claim_pv_entry(
                            own_strings, parsed, stats, own_connection=True
                        )
                    )
                    self._own_unit_entry_ts[UNIT_PV_BY_SN_KEY] = now
                elif not self._unit_pv_unmatched_logged:
                    # The one failure this can have that looks like a unit
                    # without PV: the block lists units and none is this one.
                    # On a single unit that would leave five on-by-default
                    # entities stale with nothing in the log, so it is said
                    # once, with the serials as the device wrote them.
                    self._unit_pv_unmatched_logged = True
                    _LOGGER.warning(
                        "%s: the PV string block lists %d unit(s) (%s) and none "
                        "is this device's serial; its PV String sensors are not "
                        "updated from it",
                        self.device_tag,
                        len(strings),
                        ", ".join(f"{serial[:4]}..." for serial in strings),
                    )
                for serial, value in strings.items():
                    if serial == self.device_sn:
                        continue
                    stats["pv_units_handed_over"] = (
                        stats.get("pv_units_handed_over", 0) + 1
                    )
                    handoffs.setdefault(serial, {})[UNIT_PV_BY_SN_KEY] = {serial: value}
            elif isinstance(own_strings, dict):
                since = self._own_unit_entry_ts.get(UNIT_PV_BY_SN_KEY, float("-inf"))
                if now - since >= STREAM_OWN_UNIT_ENTRY_HOLD_S:
                    parsed.update(
                        self._claim_pv_entry(
                            own_strings, parsed, stats, own_connection=False
                        )
                    )
                    stats["pv_units_received"] = stats.get("pv_units_received", 0) + 1
                else:
                    stats["pv_units_held"] = stats.get("pv_units_held", 0) + 1

        if own_connection:
            for serial, handoff in handoffs.items():
                sibling = self._linked_unit_coordinator(serial)
                if sibling is None:
                    for block_key in handoff:
                        counter = (
                            "units_unrouted"
                            if block_key == UNIT_POWER_BY_SN_KEY
                            else "pv_units_unrouted"
                        )
                        stats[counter] = stats.get(counter, 0) + 1
                    continue
                # The sibling's apply path pops and rewrites the dict it is
                # given, so it gets a copy and the block keys are read first.
                block_keys = tuple(handoff)
                try:
                    sibling.apply_linked_unit_entry(dict(handoff))
                except Exception:  # noqa: BLE001 - the sibling's listeners are not ours
                    # The hand-over runs the sibling's whole apply path,
                    # listeners included, inside this frame. A listener of
                    # theirs that raises must not cost this device its own
                    # frame, so it is counted and logged, and the frame
                    # goes on.
                    for block_key in block_keys:
                        counter = (
                            "units_handoff_failed"
                            if block_key == UNIT_POWER_BY_SN_KEY
                            else "pv_units_handoff_failed"
                        )
                        stats[counter] = stats.get(counter, 0) + 1
                    _LOGGER.exception(
                        "%s: handing a per-unit entry to %s failed",
                        self.device_tag,
                        sibling.device_tag,
                    )

        if stats:
            self._unit_power_stats = stats

    def _resolve_schedule_armed(self, parsed: dict[str, Any]) -> None:
        """Hold a just-written arming flag against a frame that predates it.

        A scheduled-task write seeds `schedule_N_enabled` in the store before
        it sends, because the power write for the same slot reads the flag
        back out of the store and carries it along. A device frame that left
        before the write arrived would put the old flag back, and the next
        power change would then re-send it - re-arming a schedule the owner
        had just disarmed, which is the one side effect this write exists to
        avoid.

        So a flag under the hold wins over a frame that disagrees with it, and
        loses to one that agrees: an agreeing frame is the device confirming
        the write, which ends the hold there and then. Only the flag is held.
        Everything else the frame carries, this slot included, is applied.
        """
        if not self._schedule_armed_latch:
            return
        now = time.monotonic()
        for key, (written, until) in list(self._schedule_armed_latch.items()):
            expired = now >= until
            if key in parsed:
                if parsed[key] == written:
                    del self._schedule_armed_latch[key]
                    continue
                if not expired:
                    del parsed[key]
                    continue
            if expired:
                del self._schedule_armed_latch[key]

    def _resolve_wallbox_action(self, parsed: dict[str, Any]) -> None:
        """Resolve a pending PowerPulse 2 start/stop against the arriving frame.

        ADR-009 decision 4: the check runs on the frame being applied, never
        on the accumulated store, so a stale reading already in
        `self._device_data` cannot confirm an action that has not actually
        happened yet. `future.done()` is checked because a resolved-but-not-
        yet-cleared record could otherwise be resolved a second time by a
        later confirming frame (e.g. "finishing" then "available" both
        confirm a stop).
        """
        record = self._wallbox_action_pending
        if record is None or record.future.done():
            return
        if "ev_charge_status" not in parsed:
            return
        status = parsed["ev_charge_status"]
        if status in POWERPULSE2_CHARGE_ACTION_CONFIRMED.get(
            record.action, frozenset()
        ):
            record.future.set_result(status)

    def latch_schedule_armed(self, state_key: str, armed: bool) -> None:
        """Start the hold for one arming flag this integration just sent."""
        self._schedule_armed_latch[state_key] = (
            armed,
            time.monotonic() + POWEROCEAN_SCHEDULE_ARMED_LATCH_S,
        )

    def clear_schedule_armed_latch(self, state_key: str) -> None:
        """Drop the hold for one arming flag, used when the send failed."""
        self._schedule_armed_latch.pop(state_key, None)

    def _apply_data(
        self, parsed: dict[str, Any], *, own_connection: bool = True
    ) -> None:
        """Apply parsed data and notify listeners (HA event loop).

        `own_connection=False` is a hand-over from a linked STREAM sibling
        (`apply_linked_unit_entry`, PLAN-145, #401): the entry travelled on
        the sibling's connection, not this device's, so the liveness head
        below - which would otherwise mark this device available on a
        message it never received - is skipped. `now` is read unconditionally
        because `_resolve_unit_power` needs it either way. Everything from
        `_resolve_unit_power` onward runs the same for both, since a handed
        value goes through the identical apply path an own frame uses.
        """
        from .core import DeviceSnapshot

        now = time.monotonic()
        if own_connection:
            self._last_mqtt_ts = now
            self._device_available = True
            # MQTT data proves credentials are valid - prevent false reauth (#2)
            self._consecutive_http_failures = 0
            # Rate-limited event log: at most once per 60s to avoid flooding the deque
            if now - self._last_mqtt_event_ts > 60:
                self._last_mqtt_event_ts = now
                self._log_event("mqtt_data", f"keys={len(parsed)}")
        self._enforce_monotonic(parsed)
        # Remove EMS raw battery state before update: bp_chg_dsg_sta reports the
        # controller MODE ("discharging" even at 0W/100% SoC), not the physical
        # state. The derivation below sets the correct value from actual power.
        # Without this, the parser overwrites the derived state on every EMS
        # report, causing ~250 false transitions/day (#50).
        parsed.pop("batt_charge_discharge_state", None)
        # Track the arrival of a fresh EmsParamChangeReport so the auto-sync
        # below can distinguish a real app-side change from a stale frame
        # whose value the user has since superseded.
        if "ems_app_surplus_pct" in parsed:
            self._last_ems_param_change_ts = now
        self._resolve_unit_power(parsed, own_connection=own_connection, now=now)
        if not own_connection and not parsed:
            # A fully held hand-over (every block it carried was dropped in
            # favour of a fresher own reading) has nothing left to apply -
            # no listener call, no snapshot (PLAN-145).
            return
        self._resolve_soc(parsed)
        self._resolve_schedule_armed(parsed)
        self._resolve_wallbox_action(parsed)

        # WAVE 3 (#161): the RuntimePropertyUpload carries its own firmware
        # revision, the first device in this integration to report one over
        # MQTT rather than only through the HTTP quota. Surface it the same
        # way the quota path does (self._firmware, read by diagnostics) and
        # push it to the device registry so the HA device page shows it.
        # Popped (and scoped to this device type) before `_note_value_change`
        # runs below, so a repeated firmware value is not itself counted as
        # a device-carried change, and the key is not swallowed for whatever
        # future parser next emits it (PLAN-047 review F1, F4).
        if self.device_type == DEVICE_TYPE_WAVE3:
            firmware = parsed.pop("firmware_version", None)
            if isinstance(firmware, str) and firmware:
                self._sw_version = firmware
                self._firmware["pd_firm_ver"] = {"decoded": firmware}
                registry = dr.async_get(self.hass)
                device = _registry_device(
                    registry, self.device_sn, self._entry.entry_id
                )
                # The registry's own state is the comparison (PLAN-047
                # review F2), not `_sw_version`: the device registry entry
                # is created when the platforms add their entities, which
                # can land after the first firmware frame. Gating on
                # `_sw_version` alone would then never retry for the life
                # of the config entry once that first race was lost.
                if device is not None and device.sw_version != firmware:
                    registry.async_update_device(device.id, sw_version=firmware)

        self._note_value_change(parsed)

        self._device_data.update(parsed)

        # WAVE 3 (#161): re-derive the four generic active-mode keys from
        # accumulated state, not from this message alone. A 2s incremental
        # frame can carry a mode switch without that mode's per-mode list
        # (the device only resends `514` on a full upload), so deriving from
        # `parsed` would leave the previous mode's setpoint standing for up
        # to 120s, the WAVE 3's idle push cadence.
        if self.device_type == DEVICE_TYPE_WAVE3 and (
            parsed.keys() & WAVE3_ACTIVE_MODE_INPUTS
        ):
            self._device_data.update(resolve_active_mode(self._device_data))

        # Re-aggregate bp_remain_watth from accumulated device_data (#10).
        # Each proto heartbeat may only contain a subset of battery packs.
        # Computing the sum from _device_data (not the current message) ensures
        # all known packs contribute even if only one pack reported this tick.
        if any(k.endswith("_remain_watth") and k.startswith("pack") for k in parsed):
            self._device_data["bp_remain_watth"] = sum(
                v
                for k, v in self._device_data.items()
                if k.startswith("pack")
                and k.endswith("_remain_watth")
                and isinstance(v, (int, float))
            )

        # Derive battery charge/discharge state from actual power (#50).
        self._derive_battery_state()

        # Integrate power → energy via Riemann sum
        self._integrate_energy(parsed)
        # Throttle flush scheduling: at most once per 60s (matches integrator's
        # SAVE_INTERVAL_S)
        if now - self._last_flush_ts > 60:
            self._last_flush_ts = now
            self.hass.async_create_task(self._async_flush_energy_state())

        self._snapshot = DeviceSnapshot(
            data=dict(self._device_data),
            captured_at=time.monotonic(),
            source="mqtt",
            key_count=len(self._device_data),
        )
        self.async_set_updated_data(dict(self._device_data))

        # PowerOcean Enhanced Mode: detect cloud-only app changes to the
        # solar-surplus slider via the EmsParamChangeReport.dev_soc echo
        # (cmd_id=13 wire field 10) and push a corrective both-field SET so
        # the EMS-side sys_bat_backup_ratio catches up to what the app set.
        if self._enhanced_mode and self.device_type == DEVICE_TYPE_POWEROCEAN:
            self._maybe_schedule_surplus_sync()

    def _maybe_schedule_surplus_sync(self) -> None:
        """Schedule an auto-sync SET if the app's dev_soc echo diverges from
        the EMS internal sys_bat_backup_ratio.

        The EcoFlow app sets the surplus slider via cmd_id=112 with only
        wire field 4 (`dev_soc`). The device acknowledges (result=0) but
        does not propagate it into the EMS-side `sys_bat_backup_ratio`
        (field 3). The device does, however, echo the app-set value back
        via the cmd_id=13 EmsParamChangeReport message (`dev_soc`, mapped
        here to `ems_app_surplus_pct`). When that value diverges from
        `ems_backup_ratio_pct`, this method schedules a corrective
        both-field SET that brings the EMS in line.

        ADR-013: a divergent (app, ems) pair gets at most
        `_SURPLUS_SYNC_MAX_WRITES` writes per process lifetime. A boundary
        value is not special-cased any more - it is simply the first
        measured instance of a pair the device will never reconcile: at
        app_int == 100 the EMS internally clamps `sys_bat_backup_ratio` to
        ~90 by design even though dev_soc / socDev hold the user value, and
        reissuing a SET would never close that gap. The general bound
        covers it the same way it covers every other unreconciled pair -
        two writes, then silence, until the app value, the EMS value or a
        user setting changes.
        """
        if self._shutdown:
            return
        app_val = self._device_data.get("ems_app_surplus_pct")
        ems_val = self._device_data.get("ems_backup_ratio_pct")
        if app_val is None or ems_val is None:
            return
        try:
            app_int = int(app_val)
            ems_int = int(ems_val)
        except (TypeError, ValueError):
            return

        record = self._surplus_sync_record
        # A changed app value abandons whatever pair the record was
        # tracking, even when the new report has already converged: a move
        # in the EcoFlow app to the value the EMS already holds is new
        # intent, not a continuation of a suppressed pair.
        if record is not None and record["app"] != app_int:
            record = None
            self._surplus_sync_record = None

        if app_int == ems_int:
            # Converged. An intact record is left alone here: an EMS that
            # echoes the written value once and then reverts must not
            # re-arm the loop at double the rate.
            return

        # A divergent pair with a different EMS value than the one on
        # record is new information - the device moved - so it is tracked
        # fresh even though the app value did not change.
        if record is not None and record["ems"] != ems_int:
            record = None
            self._surplus_sync_record = None

        if record is not None and record["writes"] >= _SURPLUS_SYNC_MAX_WRITES:
            if not record["stopped"]:
                record["stopped"] = True
                _LOGGER.info(
                    "PowerOcean surplus auto-sync (%s): EMS still reports "
                    "%d after %d writes of %d; no further writes until the "
                    "app value, the EMS value or a user setting changes",
                    self.device_tag,
                    ems_int,
                    record["writes"],
                    app_int,
                )
                self._log_event(
                    "surplus_auto_sync_stopped",
                    f"app={app_int} ems={ems_int} writes={record['writes']}",
                )
            return

        # Suppress sync if the latest EmsParamChangeReport carrying the
        # `dev_soc` value is older than the user's most recent SET. The
        # ParamChange echo is event-driven and lags the EmsChangeReport
        # echo - if the user just pushed a new value in HA, the
        # ParamChange we still see may be the obsolete app-side mirror
        # of a value the user has now superseded. Without this guard the
        # auto-sync would reissue the *old* app value as a both-field
        # SET, dragging HA back to the value the user just left.
        if self._last_ems_param_change_ts <= self._last_user_surplus_set_ts:
            return
        now = time.monotonic()
        if now - self._last_app_surplus_sync_ts < APP_SURPLUS_SYNC_MIN_INTERVAL_S:
            return
        if now - self._last_user_surplus_set_ts < APP_SURPLUS_SYNC_USER_GRACE_S:
            return

        # A missing, None or non-numeric backup limit means the device's
        # own discharge floor is unknown. Guessing 0 here would move it to
        # the value that lets the battery discharge fully - a setting
        # nobody chose (ADR-011 decision 4, applied to this, its third
        # consumer of the pair) - so the write is refused the same way the
        # guards above refuse an unreadable app or ems value.
        backup_val = self._device_data.get("ems_discharge_lower_limit_pct")
        if backup_val is None:
            return
        try:
            backup_int = int(backup_val)
        except (TypeError, ValueError):
            return
        target_backup = min(backup_int, app_int)

        task = self._schedule_powerocean_soc_write(
            target_backup,
            app_int,
            name=f"PowerOcean surplus auto-sync {self.device_tag}",
        )
        if task is None:
            return
        self._last_app_surplus_sync_ts = now
        if record is None:
            record = {"app": app_int, "ems": ems_int, "writes": 0, "stopped": False}
            self._surplus_sync_record = record
        record["writes"] += 1
        _LOGGER.info(
            "PowerOcean surplus auto-sync (%s): app=%d ems=%d -> SET both=%d",
            self.device_tag,
            app_int,
            ems_int,
            app_int,
        )
        self._log_event(
            "surplus_auto_sync",
            f"app={app_int} ems={ems_int}",
        )

    def mark_user_surplus_set(self) -> None:
        """Record a user-initiated surplus/backup change.

        The surplus auto-sync uses this timestamp to suppress stale
        app-side echoes, and this call also clears any auto-sync record
        (ADR-013 decision 2a): a user who corrects the sliders from Home
        Assistant starts fresh.
        """
        self._last_user_surplus_set_ts = time.monotonic()
        self._surplus_sync_record = None

    async def _async_flush_energy_state(self) -> None:
        """Flush energy integrator state to disk (non-blocking)."""
        await self.hass.async_add_executor_job(self._energy_integrator.flush)

    def seed_energy_total(self, key: str, total_kwh: float) -> None:
        """Seed the energy integrator with a restored HA sensor state.

        Recovers energy totals when the integrator state file was lost or
        corrupted: HA restores the sensor value across restarts, so the
        integrator continues from the restored total instead of zero. Calls
        restore_total, not set_total: a restored sensor state is not a
        device reading (ADR-010 addendum A1), so a restored value above the
        stored total is taken at once instead of becoming an unconfirmed
        candidate that only a device reading could later promote. A restored
        value at or below the stored total is ignored, so a stale restored
        value can never lower a live total. Keys that are not integrator
        metrics (e.g. cycle counters) are ignored.
        """
        if key not in self._energy_integrator_keys:
            return
        self._energy_integrator.restore_total(key, total_kwh)

    @property
    def _energy_integrator_keys(self) -> set[str]:
        """Keys the energy integrator owns end to end (ADR-010).

        `_integrate_energy` writes `_device_data[k]` for each of these keys
        from the return value of the `integrate`/`set_total` call that just
        ran for it: the resolved total when one exists, popped (not left at
        a stale value) when the call reports none. `_enforce_monotonic` must
        never touch these keys: the integrator is the sole monotonic
        authority for them, and letting the plain monotonic filter drop a
        lower device reading first would keep it from ever reaching
        `set_total`.
        """
        return set(self._power_to_energy.values()) | {
            energy_key for _, energy_key in self._energy_from_api
        }

    # Battery state derivation parameters (#63, #50).
    # These are class-level so tests can override without touching instance state.
    BATT_WINDOW_S = 120  # 2-minute rolling window (confirmation does the rest)
    BATT_MIN_SAMPLES = 10  # minimum samples before derivation is trusted
    BATT_OUTER_W = 150  # |avg| > 150W -> charging/discharging
    BATT_INNER_W = 50  # |avg| < 50W  -> standby
    BATT_MIN_HOLD_S = 120  # min seconds a state must be held before it can change
    BATT_CONFIRM_S = 600  # a diverging candidate must persist this long to commit

    def _derive_battery_state(self) -> None:
        """Derive battery charge/discharge state from a rolling-average power
        (#63, #50).

        The raw EMS field bp_chg_dsg_sta reports the controller MODE, not the
        physical state, so we override it from signed batt_w. Using the
        instantaneous value causes rapid flipping when solar and house load
        balance (morning/evening): batt_w swings between +1000W and -300W
        within seconds, and any threshold check flips with each sample.

        Strategy:
          1. Append current batt_w to a rolling buffer (timestamp, value).
          2. Drop samples older than BATT_WINDOW_S seconds.
          3. Compute the mean over the buffer.
          4. Apply thresholds to the mean, not the raw sample.
          5. A deadband between BATT_INNER_W and BATT_OUTER_W keeps prev state
             (and deliberately leaves any pending candidate untouched, so a
             brief dip into the deadband does not restart the confirmation).
          6. A transition requires the previous state to have been held for
             at least BATT_MIN_HOLD_S seconds.
          7. Confirmation gate: a candidate state that differs from the
             previous state must persist for BATT_CONFIRM_S seconds before
             the change is committed. Only the average returning to the
             previous state's band drops the pending candidate. The very
             first derived state (no previous state) commits immediately.

        Prefers signed `batt_w` when available, falls back to the derived
        charge/discharge power split for HTTP-only paths that never expose
        signed power directly.
        """
        batt_w = self._device_data.get("batt_w")
        if batt_w is None:
            charge_w = self._device_data.get("batt_charge_power_w")
            discharge_w = self._device_data.get("batt_discharge_power_w")
            if charge_w is None or discharge_w is None:
                return
            batt_w = charge_w - discharge_w

        now_mono = time.monotonic()
        self._batt_w_samples.append((now_mono, float(batt_w)))
        cutoff = now_mono - self.BATT_WINDOW_S
        self._batt_w_samples = [(t, v) for t, v in self._batt_w_samples if t >= cutoff]

        if len(self._batt_w_samples) < self.BATT_MIN_SAMPLES:
            return

        avg = sum(v for _, v in self._batt_w_samples) / len(self._batt_w_samples)
        prev = self._device_data.get("batt_charge_discharge_state")

        if avg > self.BATT_OUTER_W:
            derived = "charging"
        elif avg < -self.BATT_OUTER_W:
            derived = "discharging"
        elif abs(avg) < self.BATT_INNER_W:
            derived = "standby"
        else:
            return  # deadband: keep previous state

        if derived == prev:
            # Settled back into the previous state's band: drop any pending
            # candidate so a later divergence starts a fresh confirmation.
            self._batt_pending_state = None
            return

        hold_elapsed = now_mono - self._batt_state_changed_at
        if prev is not None and hold_elapsed < self.BATT_MIN_HOLD_S:
            return

        if prev is not None:
            # Confirmation gate (#50): the diverging candidate must persist
            # BATT_CONFIRM_S before the transition commits. Deadband samples
            # neither reset nor clear the pending timer (noreset semantics).
            if self._batt_pending_state != derived:
                self._batt_pending_state = derived
                self._batt_pending_since = now_mono
                _LOGGER.debug(
                    "Battery state for %s: avg(%ds)=%.1fW pending %s "
                    "(was %s, commit in %ds, n=%d)",
                    self.device_tag,
                    self.BATT_WINDOW_S,
                    avg,
                    derived,
                    prev,
                    self.BATT_CONFIRM_S,
                    len(self._batt_w_samples),
                )
                return
            confirm_elapsed = now_mono - self._batt_pending_since
            if confirm_elapsed < self.BATT_CONFIRM_S:
                return
            self._batt_pending_state = None

        self._device_data["batt_charge_discharge_state"] = derived
        self._batt_state_changed_at = now_mono
        _LOGGER.debug(
            "Battery state for %s: avg(%ds)=%.1fW -> %s "
            "(was %s, held %.0fs, confirmed %.0fs, n=%d)",
            self.device_tag,
            self.BATT_WINDOW_S,
            avg,
            derived,
            prev,
            hold_elapsed,
            now_mono - self._batt_pending_since if prev is not None else 0.0,
            len(self._batt_w_samples),
        )

    # ------------------------------------------------------------------
    # Energy integration (Riemann sum)
    # ------------------------------------------------------------------

    def _integrate_energy(self, parsed: dict[str, Any]) -> None:
        """Integrate power readings into energy totals via Riemann sum.

        Uses device-specific power → energy mappings from const.py.
        For API-provided energy totals, prefer those over Riemann sum.

        Each call site writes `_device_data` from its own return value
        (ADR-010), not from a lookup afterwards: `integrate` returns None on
        the seed-only first reading for a metric even though the integrator
        now holds an internal 0.0 for it (state_snapshot needs that 0.0 for
        diagnostics), so a value must be popped rather than resurrected at
        that placeholder. `set_total` mirrors the same contract - the
        resolved total, or None only when nothing has ever been established
        for that metric - so a reading it rejects or holds as an
        unconfirmed candidate restores the previous good value instead of
        letting the raw parsed value stand.
        """

        for power_key, energy_key in self._power_to_energy.items():
            power_w = parsed.get(power_key)
            if power_w is not None:
                total = self._energy_integrator.integrate(energy_key, abs(power_w))
            elif energy_key in parsed:
                # F5 (plan-051-052-energy-guards review): the energy key can
                # arrive without its power key (e.g.
                # energy_stream.solarTotalEnergy without mpptPwr, since each
                # power key is itself conditional on its own HTTP source).
                # The integrator is still the sole monotonic authority for
                # this key (ADR-010 decision 3, _enforce_monotonic skips
                # it), so the frame's raw value must never stand unfiltered
                # - rewrite from get_total instead.
                total = self._energy_integrator.get_total(energy_key)
            else:
                continue
            if total is not None:
                self._device_data[energy_key] = round(total, 2)
            else:
                self._device_data.pop(energy_key, None)

        # API totals: prefer over Riemann sum (more accurate when available)
        for power_key, energy_key in self._energy_from_api:
            if energy_key in parsed:
                # API provided a total - use it (already set by parser)
                total = self._energy_integrator.set_total(
                    energy_key, parsed[energy_key]
                )
                if total is not None:
                    self._device_data[energy_key] = round(total, 2)
                else:
                    self._device_data.pop(energy_key, None)
            else:
                # No API total - integrate from power
                power_w = parsed.get(power_key)
                if power_w is not None:
                    total = self._energy_integrator.integrate(energy_key, abs(power_w))
                    if total is not None:
                        self._device_data[energy_key] = round(total, 2)
                    else:
                        self._device_data.pop(energy_key, None)
