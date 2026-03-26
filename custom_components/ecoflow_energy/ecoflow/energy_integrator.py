"""Riemann sum energy integrator for power → kWh conversion.

Calculates energy totals (kWh) from power readings (W) using trapezoidal
integration with persistent state. Designed for the HA Energy Dashboard.

Ported from EcoFlow main repo (src/service/logic/energy_integrator.py).

Features:
- Trapezoidal integration (average of last + current power)
- Gap detection: skip integration for gaps >7 minutes
- Jump detection: use min(last, current) for >50% power changes
- Monotonic: totals never decrease
- Persistent: state survives HA restarts via JSON file
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_GAP_S = 420.0  # Skip integration for gaps >7 minutes
MIN_DELTA_S = 0.1  # Ignore updates faster than 100ms
SAVE_INTERVAL_S = 60.0  # Save state to disk at most every 60s


class EnergyIntegrator:
    """Integrates power (W) readings into energy totals (kWh)."""

    def __init__(self, state_file: str) -> None:
        self._state_file = Path(state_file)
        # metric → (total_kwh, last_ts, last_power_w)
        self._state: Dict[str, Tuple[float, float, float]] = {}
        self._dirty: bool = False
        self._last_save_ts: float = 0.0
        self._loaded: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_state(self) -> None:
        """Load persisted state from disk (blocking I/O).

        Call from an executor job to avoid blocking the HA event loop.
        Safe to call multiple times — only loads once.
        """
        if self._loaded:
            return
        self._load_state()
        self._loaded = True

    def integrate(self, metric: str, power_w: float) -> Optional[float]:
        """Integrate a power reading into the running energy total.

        Args:
            metric: Sensor key (e.g. "solar_energy_kwh").
            power_w: Current power in Watts (always ≥ 0 for directional sensors).

        Returns:
            Updated energy total in kWh, or None if skipped.
        """
        if not self._loaded:
            self.load_state()
        now = time.time()

        if metric in self._state:
            total_kwh, last_ts, last_power_w = self._state[metric]
        else:
            # First reading: seed state, don't integrate yet
            self._state[metric] = (0.0, now, power_w)
            self._dirty = True
            return 0.0

        delta_t_s = now - last_ts

        # Gap too large → reset timestamp, keep total
        if delta_t_s > MAX_GAP_S:
            self._state[metric] = (total_kwh, now, power_w)
            self._dirty = True
            return total_kwh

        # Too fast → skip
        if delta_t_s < MIN_DELTA_S:
            return total_kwh

        # Jump detection: >50% change → use conservative lower bound
        power_diff = abs(power_w - last_power_w)
        power_avg = (abs(last_power_w) + abs(power_w)) / 2.0

        if power_avg > 0 and (power_diff / power_avg) > 0.5:
            avg_power_w = min(abs(last_power_w), abs(power_w))
        else:
            avg_power_w = (last_power_w + power_w) / 2.0

        # Energy = Power × Time (W → kWh)
        delta_kwh = abs(avg_power_w * delta_t_s) / 3_600_000.0
        new_total_kwh = total_kwh + delta_kwh

        self._state[metric] = (new_total_kwh, now, power_w)
        self._dirty = True
        return new_total_kwh

    def set_total(self, metric: str, total_kwh: float) -> None:
        """Set total directly from API (monotonic — only if higher)."""
        if not self._loaded:
            self.load_state()
        if metric in self._state:
            current = self._state[metric][0]
            if total_kwh < current:
                return
            last_power = self._state[metric][2]
        else:
            last_power = 0.0

        self._state[metric] = (total_kwh, time.time(), last_power)
        self._dirty = True

    def flush(self) -> None:
        """Save state to disk if dirty and enough time has passed.

        Call this from a non-async context (executor job) to avoid
        blocking the HA event loop.
        """
        if not self._dirty:
            return
        now = time.time()
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

    def get_total(self, metric: str) -> Optional[float]:
        """Return current total for a metric, or None."""
        if metric in self._state:
            return self._state[metric][0]
        return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                for metric, values in data.items():
                    if isinstance(values, list) and len(values) >= 3:
                        self._state[metric] = (
                            float(values[0]),
                            float(values[1]),
                            float(values[2]),
                        )
                logger.debug("Energy state loaded: %d metrics", len(self._state))
        except Exception as exc:
            logger.warning("Failed to load energy state: %s", exc)
            self._state = {}

    def _save_state(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            # Snapshot: dict() copy prevents RuntimeError if _state is mutated
            # concurrently from the event loop while this runs in executor.
            snapshot = dict(self._state)
            data: Dict[str, Any] = {
                m: [t, ts, p] for m, (t, ts, p) in snapshot.items()
            }
            self._state_file.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            logger.warning("Failed to save energy state: %s", exc)
