"""State application, battery state derivation, and energy integration."""

from __future__ import annotations

import logging
import time
from typing import Any

from ..const import (
    APP_SURPLUS_SYNC_MIN_INTERVAL_S,
    APP_SURPLUS_SYNC_USER_GRACE_S,
    DEVICE_TYPE_POWEROCEAN,
)

_LOGGER = logging.getLogger(__name__)


class StateApplyMixin:
    """Mixin applying parsed data to coordinator state."""

    def _apply_data(self, parsed: dict[str, Any]) -> None:
        """Apply parsed data and notify listeners (HA event loop)."""
        from .core import DeviceSnapshot

        now = time.monotonic()
        self._last_mqtt_ts = now
        self._device_available = True
        # MQTT data proves credentials are valid — prevent false reauth (#2)
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
        self._device_data.update(parsed)

        # Re-aggregate bp_remain_watth from accumulated device_data (#10).
        # Each proto heartbeat may only contain a subset of battery packs.
        # Computing the sum from _device_data (not the current message) ensures
        # all known packs contribute even if only one pack reported this tick.
        if any(k.endswith("_remain_watth") and k.startswith("pack") for k in parsed):
            self._device_data["bp_remain_watth"] = sum(
                v for k, v in self._device_data.items()
                if k.startswith("pack") and k.endswith("_remain_watth")
                and isinstance(v, (int, float))
            )

        # Derive battery charge/discharge state from actual power (#50).
        self._derive_battery_state()

        # Integrate power → energy via Riemann sum
        self._integrate_energy(parsed)
        # Throttle flush scheduling: at most once per 60s (matches integrator's SAVE_INTERVAL_S)
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
        """
        app_val = self._device_data.get("ems_app_surplus_pct")
        ems_val = self._device_data.get("ems_backup_ratio_pct")
        if app_val is None or ems_val is None:
            return
        try:
            app_int = int(app_val)
            ems_int = int(ems_val)
        except (TypeError, ValueError):
            return
        if app_int == ems_int:
            return

        # Edge-case suppress: at app_int == 100 (and likely 0), the EMS
        # internally clamps `sys_bat_backup_ratio` to ~90 by design even
        # though dev_soc / socDev hold the user value. Reissuing a SET
        # would never reconcile the two - it would just generate periodic
        # write traffic. The user-side mirror (ems_app_surplus_pct) is
        # the source of truth for the slider; the EMS-side divergence at
        # the boundaries is expected device behaviour.
        if app_int in (0, 100):
            return

        now = time.monotonic()
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
        if now - self._last_app_surplus_sync_ts < APP_SURPLUS_SYNC_MIN_INTERVAL_S:
            return
        if now - self._last_user_surplus_set_ts < APP_SURPLUS_SYNC_USER_GRACE_S:
            return

        backup_val = self._device_data.get("ems_discharge_lower_limit_pct", 0)
        try:
            backup_int = int(backup_val)
        except (TypeError, ValueError):
            backup_int = 0
        target_backup = min(backup_int, app_int)

        self._last_app_surplus_sync_ts = now
        _LOGGER.info(
            "PowerOcean surplus auto-sync (%s): app=%d ems=%d -> SET both=%d",
            self.device_sn, app_int, ems_int, app_int,
        )
        self._log_event(
            "surplus_auto_sync",
            f"app={app_int} ems={ems_int}",
        )
        self.hass.async_create_task(
            self.async_set_powerocean_soc(target_backup, app_int)
        )

    def mark_user_surplus_set(self) -> None:
        """Record a user-initiated surplus/backup change.

        The surplus auto-sync uses this timestamp to suppress stale
        app-side echoes.
        """
        self._last_user_surplus_set_ts = time.monotonic()

    async def _async_flush_energy_state(self) -> None:
        """Flush energy integrator state to disk (non-blocking)."""
        await self.hass.async_add_executor_job(self._energy_integrator.flush)

    def seed_energy_total(self, key: str, total_kwh: float) -> None:
        """Seed the energy integrator with a restored HA sensor state.

        Recovers energy totals when the integrator state file was lost or
        corrupted: HA restores the sensor value across restarts, so the
        integrator continues from the restored total instead of zero.
        set_total is monotonic-guarded (lower values are ignored), so a
        stale restored value can never lower a live total. Keys that are
        not integrator metrics (e.g. cycle counters) are ignored.
        """
        energy_keys = set(self._power_to_energy.values()) | {
            energy_key for _, energy_key in self._energy_from_api
        }
        if key not in energy_keys:
            return
        self._energy_integrator.set_total(key, total_kwh)

    # Battery state derivation parameters (#63, #50).
    # These are class-level so tests can override without touching instance state.
    BATT_WINDOW_S = 120       # 2-minute rolling window (confirmation does the rest)
    BATT_MIN_SAMPLES = 10     # minimum samples before derivation is trusted
    BATT_OUTER_W = 150        # |avg| > 150W -> charging/discharging
    BATT_INNER_W = 50         # |avg| < 50W  -> standby
    BATT_MIN_HOLD_S = 120     # min seconds a state must be held before it can change
    BATT_CONFIRM_S = 600      # a diverging candidate must persist this long to commit

    def _derive_battery_state(self) -> None:
        """Derive battery charge/discharge state from a rolling-average power (#63, #50).

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
        self._batt_w_samples = [
            (t, v) for t, v in self._batt_w_samples if t >= cutoff
        ]

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
                    self.device_sn,
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
            self.device_sn,
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
        """

        for power_key, energy_key in self._power_to_energy.items():
            power_w = parsed.get(power_key)
            if power_w is not None:
                total = self._energy_integrator.integrate(energy_key, abs(power_w))
                if total is not None:
                    self._device_data[energy_key] = round(total, 2)

        # API totals: prefer over Riemann sum (more accurate when available)
        for power_key, energy_key in self._energy_from_api:
            if energy_key in parsed:
                # API provided a total — use it (already set by parser)
                self._energy_integrator.set_total(energy_key, parsed[energy_key])
            else:
                # No API total — integrate from power
                power_w = parsed.get(power_key)
                if power_w is not None:
                    total = self._energy_integrator.integrate(energy_key, abs(power_w))
                    if total is not None:
                        self._device_data[energy_key] = round(total, 2)

