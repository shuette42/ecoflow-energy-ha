"""Riemann sum energy integrator for power → kWh conversion.

Calculates energy totals (kWh) from power readings (W) using trapezoidal
integration with persistent state. Designed for the HA Energy Dashboard.

Ported from EcoFlow main repo (src/service/logic/energy_integrator.py).

Features:
- Trapezoidal integration (average of last + current power)
- Gap detection: skip integration for gaps >7 minutes
- Plausibility bounds: a reading wrong by orders of magnitude is rejected
- Monotonic: totals never decrease
- Persistent: state survives HA restarts via JSON file
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

MAX_GAP_S = 420.0  # Skip integration for gaps >7 minutes
MIN_DELTA_S = 0.1  # Ignore updates faster than 100ms
SAVE_INTERVAL_S = 60.0  # Save state to disk at most every 60s

# Physical plausibility bounds.
#
# A decoding fault can produce readings many orders of magnitude beyond
# anything a real device delivers. Because totals here are monotonic, a single
# such value becomes a permanent floor: every correct reading afterwards is
# lower and gets discarded, so the counter can never recover on its own. That
# is what happened on a PowerOcean Plus reporting 5.45e7 kWh against an actual
# 2.6e3 kWh, months after the decoding fault itself had been fixed.
#
# The bounds are deliberately far above any real installation rather than
# tuned per device. They exist to catch the wrong-by-orders-of-magnitude class
# of fault, not to second-guess a plausible reading.
MAX_POWER_W = 1_000_000.0  # 1 MW; the largest EcoFlow unit is rated 30 kW
MAX_TOTAL_KWH = 10_000_000.0  # 10 GWh; 30 kW running flat out for 38 years

# Tolerance band for set_total, around the stored total (ADR-010).
#
# A device-reported total is allowed to move by this much between two
# readings without going through the confirmation gate below. The floor is
# derived from the interval this integrator itself credits per step: the
# largest EcoFlow unit is rated 30 kW and MAX_GAP_S caps one interval at 7
# minutes, so the largest jump the device's own counter could legitimately
# make between two live readings is under 4 kWh (30 kW * 7 min / 60).
# 5 kWh clears that with headroom. The percentage half keeps the same rule
# proportionate on a large installation, where 10% of the standing total
# dwarfs the floor.
SET_TOTAL_TOLERANCE_PCT = 0.10
SET_TOTAL_TOLERANCE_FLOOR_KWH = 5.0


class EnergyIntegrator:
    """Integrates power (W) readings into energy totals (kWh)."""

    def __init__(self, state_file: str) -> None:
        self._state_file = Path(state_file)
        # metric → (total_kwh, last_ts, last_power_w)
        self._state: dict[str, tuple[float, float, float]] = {}
        self._dirty: bool = False
        self._last_save_ts: float = 0.0
        self._loaded: bool = False
        # Metrics already reported as implausible, so a device stuck on a bad
        # reading warns once instead of on every push.
        self._rejected: set[str] = set()
        # A device-reported total that moved beyond the tolerance band from
        # the stored value, waiting for a second reading to confirm it
        # (ADR-010). Memory-only by design - see set_total's docstring.
        self._candidates: dict[str, float] = {}

    def _reject(self, metric: str, what: str, value: float) -> None:
        """Report an implausible reading once per metric, then stay quiet."""
        if metric in self._rejected:
            _LOGGER.debug("Ignoring implausible %s for %s: %r", what, metric, value)
            return
        self._rejected.add(metric)
        _LOGGER.warning(
            "Ignoring implausible %s for %s: %r. The energy total is kept at "
            "its last good value; further occurrences are logged at debug level",
            what, metric, value,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_state(self) -> None:
        """Load persisted state from disk (blocking I/O).

        Call from an executor job to avoid blocking the HA event loop.
        Safe to call multiple times - only loads once.
        """
        if self._loaded:
            return
        self._load_state()
        self._loaded = True

    def integrate(self, metric: str, power_w: float) -> float | None:
        """Integrate a power reading into the running energy total.

        Args:
            metric: Sensor key (e.g. "solar_energy_kwh").
            power_w: Current power in Watts (always ≥ 0 for directional sensors).

        Returns:
            Updated energy total in kWh, or None when this call produced no
            total: the very first reading for a metric (which only seeds the
            state) and any rejected reading. Callers must treat None as "leave
            the sensor alone", never as zero.
        """
        if not self._loaded:
            self.load_state()

        if not math.isfinite(power_w) or abs(power_w) > MAX_POWER_W:
            self._reject(metric, "power reading", power_w)
            existing = self._state.get(metric)
            return existing[0] if existing else None

        now = time.monotonic()

        if metric in self._state:
            total_kwh, last_ts, last_power_w = self._state[metric]
        else:
            # First reading: seed the state and report nothing yet.
            #
            # Returning 0.0 here would claim a total this call has no basis
            # for. The caller writes whatever it gets into the sensor, and the
            # energy sensors are total_increasing: a 0.0 published while Home
            # Assistant still holds a restored total reads as a meter reset,
            # and the restored value is then counted a second time when the
            # next reading brings it back. Reporting None instead leaves the
            # key absent, which the sensor resolves to its restored value.
            self._state[metric] = (0.0, now, power_w)
            self._dirty = True
            return None

        delta_t_s = now - last_ts

        # Gap too large → reset timestamp, keep total
        if delta_t_s > MAX_GAP_S:
            self._state[metric] = (total_kwh, now, power_w)
            self._dirty = True
            return total_kwh

        # Too fast → skip
        if delta_t_s < MIN_DELTA_S:
            return total_kwh

        # Trapezoidal rule: the interval is credited with the mean of the two
        # readings that bound it.
        #
        # A change of more than 50% used to be credited with
        # min(last, current) instead, on the reasoning that a large step makes
        # the straight line between the readings untrustworthy, so the least
        # the interval could have been is the safe answer. It is not the safe
        # answer, it is the lowest one. When a load steps at an unknown moment
        # inside the interval, the mean of the endpoints is the expected
        # energy. Each switching edge can be off by at most half an interval
        # at the load's power, the on-edge errs opposite to the off-edge, and
        # neither direction is favoured, so over many cycles the errors cancel
        # on average instead of piling up. Taking the minimum lost both halves
        # outright.
        #
        # Where one of the two readings is zero the minimum is zero, so the
        # interval contributed nothing at all. That is how a STREAM AC 5000
        # came to report four energy counters at zero next to correct live
        # power (#177): its readings are edges of a sparse flow matrix, an
        # edge that is not flowing is a real zero, and the readings alternate
        # between zero and a value all day. Any metric that returns to zero
        # between samples was losing energy the same way, in proportion to how
        # often it did so.
        #
        # A single spurious spike is the one thing the minimum did guard
        # against. It now costs one interval at the spike's own value, which
        # the bounds above cap; the wrong-by-orders-of-magnitude class that
        # froze a counter for good in #88 is still rejected outright.
        avg_power_w = (last_power_w + power_w) / 2.0

        # Energy = Power × Time (W → kWh)
        delta_kwh = abs(avg_power_w * delta_t_s) / 3_600_000.0
        new_total_kwh = total_kwh + delta_kwh

        # A total beyond the ceiling can only come from bad input, and letting
        # it persist would freeze the counter for good.
        if new_total_kwh > MAX_TOTAL_KWH:
            self._reject(metric, "energy total", new_total_kwh)
            self._state[metric] = (total_kwh, now, power_w)
            self._dirty = True
            return total_kwh

        self._state[metric] = (new_total_kwh, now, power_w)
        self._dirty = True
        return new_total_kwh

    def set_total(self, metric: str, total_kwh: float) -> float | None:
        """Set total directly from a device-reported counter (ADR-010).

        Relative to the stored total, a reading falls into one of three
        bands, with tolerance ``max(SET_TOTAL_TOLERANCE_PCT * stored,
        SET_TOTAL_TOLERANCE_FLOOR_KWH)``:

        - a rise within tolerance is accepted at once (an ordinary
          increment);
        - a dip within tolerance is ignored - the class of glitch this
          guard exists for (e.g. 4408.259 -> 4408.258);
        - a step beyond tolerance, in either direction, becomes a
          memory-only candidate. The next device reading within tolerance
          of that candidate confirms it and becomes the new stored total; a
          reading that agrees with the stored total drops the candidate
          instead; anything else replaces it.

        A single wrong reading can therefore never move the stored total on
        its own, and a sustained wrong reading recovers in two device
        readings without a magnitude ceiling low enough to risk rejecting a
        genuine lifetime counter. Candidates are never persisted: a
        poisoned restored state (seed_energy_total) becomes a candidate
        here and can only take effect if a live device reading confirms it,
        so it can no longer re-poison a clean integrator across a restart.

        Returns:
            The total a caller should display in kWh: the stored total
            after this call, whatever it resolved to (accepted, unchanged
            on an ignored dip, or unchanged on an unconfirmed candidate).
            None only when this metric has never had a total established at
            all - the same "leave the sensor alone" contract as
            ``integrate``.
        """
        if not self._loaded:
            self.load_state()

        # Guard before the monotonic comparison, not after: an implausible
        # value is always higher than the real one, so it would win every time
        # and then block every correct reading that follows.
        if (
            not math.isfinite(total_kwh)
            or total_kwh < 0
            or total_kwh > MAX_TOTAL_KWH
        ):
            self._reject(metric, "energy total", total_kwh)
            existing = self._state.get(metric)
            return existing[0] if existing else None

        if metric not in self._state:
            # No reference point yet: accept directly. There is nothing to
            # measure a jump against, and no candidate can exist for a
            # metric that has never been seen.
            self._state[metric] = (total_kwh, time.monotonic(), 0.0)
            self._dirty = True
            self._candidates.pop(metric, None)
            return total_kwh

        current, _last_ts, last_power = self._state[metric]
        tolerance = max(
            current * SET_TOTAL_TOLERANCE_PCT, SET_TOTAL_TOLERANCE_FLOOR_KWH
        )
        diff = total_kwh - current

        if abs(diff) <= tolerance:
            # Agrees with the stored total, so a pending candidate from an
            # earlier, unconfirmed step is now stale.
            self._candidates.pop(metric, None)
            if diff > 0:
                self._state[metric] = (total_kwh, time.monotonic(), last_power)
                self._dirty = True
                return total_kwh
            return current

        candidate = self._candidates.get(metric)
        if candidate is not None:
            candidate_tolerance = max(
                candidate * SET_TOTAL_TOLERANCE_PCT,
                SET_TOTAL_TOLERANCE_FLOOR_KWH,
            )
            if abs(total_kwh - candidate) <= candidate_tolerance:
                # Confirmed: two device readings close to each other, both
                # far from the old stored total. The confirming reading
                # itself becomes the new total.
                if total_kwh < current:
                    _LOGGER.warning(
                        "Energy total for %s confirmed lower: %.3f -> "
                        "%.3f kWh. Home Assistant will record this as a "
                        "meter reset",
                        metric, current, total_kwh,
                    )
                self._state[metric] = (total_kwh, time.monotonic(), last_power)
                self._dirty = True
                del self._candidates[metric]
                return total_kwh

        self._candidates[metric] = total_kwh
        return current

    def flush(self) -> None:
        """Save state to disk if dirty and enough time has passed.

        Call this from a non-async context (executor job) to avoid
        blocking the HA event loop.
        """
        if not self._dirty:
            return
        now = time.monotonic()
        if now - self._last_save_ts < SAVE_INTERVAL_S:
            return
        self._save_state()
        self._last_save_ts = now
        self._dirty = False

    def force_flush(self) -> None:
        """Save state immediately (for shutdown)."""
        if self._dirty:
            self._save_state()
            self._dirty = False

    def get_total(self, metric: str) -> float | None:
        """Return current total for a metric, or None."""
        if metric in self._state:
            return self._state[metric][0]
        return None

    def state_snapshot(self) -> dict[str, tuple[float, float, float]]:
        """Return a copy of the running state, keyed by metric.

        Each value is the same triple that gets persisted:
        ``(total_kwh, last_monotonic_ts, last_power_w)``.

        Read-only by construction - the dict is a copy and the values are
        tuples, so a reader cannot disturb the totals. Deliberately does not
        load from disk: this reports what the integrator is doing now, and
        the file is only written every ``SAVE_INTERVAL_S``, so a snapshot
        taken from it can be a minute behind the running counters.
        """
        return dict(self._state)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                now = time.monotonic()
                for metric, values in data.items():
                    if isinstance(values, list) and len(values) >= 3:
                        total = float(values[0])
                        # An installation that stored an implausible total
                        # before this guard existed would otherwise stay stuck
                        # on it forever. Dropping the metric restarts the
                        # counter, which Home Assistant reads as a counter
                        # reset - the honest outcome, since the stored value
                        # was never real.
                        if not math.isfinite(total) or total > MAX_TOTAL_KWH:
                            _LOGGER.warning(
                                "Discarding implausible stored energy total "
                                "for %s: %r. The counter restarts from zero",
                                metric, total,
                            )
                            continue
                        last_ts = float(values[1])
                        # Migrate epoch timestamps from pre-v1.5.1 state files
                        if last_ts > 1e9:
                            last_ts = now
                        # Monotonic clock reset after host reboot
                        elif last_ts > now:
                            _LOGGER.debug(
                                "Monotonic reset for %s: saved=%.1f > now=%.1f",
                                metric, last_ts, now,
                            )
                            last_ts = now
                        self._state[metric] = (
                            total,
                            last_ts,
                            float(values[2]),
                        )
                _LOGGER.debug("Energy state loaded: %d metrics", len(self._state))
        except Exception as exc:
            _LOGGER.warning("Failed to load energy state: %s", exc)
            self._state = {}

    def _save_state(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            # Snapshot: dict() copy prevents RuntimeError if _state is mutated
            # concurrently from the event loop while this runs in executor.
            snapshot = dict(self._state)
            data: dict[str, Any] = {
                m: [t, ts, p] for m, (t, ts, p) in snapshot.items()
            }
            self._state_file.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            _LOGGER.warning("Failed to save energy state: %s", exc)
