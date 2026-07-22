"""Shared entity helpers for EcoFlow Energy platforms."""

from __future__ import annotations

from typing import Any


class EcoFlowWriteGateMixin:
    """State-write gate shared by all EcoFlow entity platforms.

    Coordinator ticks arrive every few seconds, but the HA recorder writes
    on every state update. The gate skips ``async_write_ha_state()`` when
    nothing observable changed. Both the entity VALUE and its AVAILABILITY
    are part of the comparison: an availability flip with an unchanged
    value must still reach the state machine, otherwise entities appear
    available long after the connection degraded (and vice versa).
    """

    _last_written_value: Any = None
    _last_written_available: bool | None = None

    def _write_state_if_changed(self, new_value: Any) -> None:
        """Write HA state when the value or the availability changed."""
        new_available = self.available  # type: ignore[attr-defined]
        if self._last_written_available is None:
            # First observation: seed the availability sentinel without
            # forcing a write - HA already wrote the initial state when the
            # entity was added. Later flips are compared against this seed.
            self._last_written_available = new_available
        if (
            new_value == self._last_written_value
            and new_available == self._last_written_available
        ):
            return
        self._last_written_value = new_value
        self._last_written_available = new_available
        self.async_write_ha_state()  # type: ignore[attr-defined]

    def _write_state_always(self, new_value: Any) -> None:
        """Write HA state unconditionally (optimistic SET feedback path)."""
        self._last_written_value = new_value
        self._last_written_available = self.available  # type: ignore[attr-defined]
        self.async_write_ha_state()  # type: ignore[attr-defined]
