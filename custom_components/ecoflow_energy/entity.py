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

    async def async_added_to_hass(self) -> None:
        """Seed the availability sentinel with the state HA writes on add.

        The sentinel must capture what actually reached the state machine,
        not what the first gate pass observes. If the device is silent while
        Home Assistant starts, HA writes ``unavailable`` on add; once data
        resumes with unchanged values, the availability flip is the only
        difference. A lazily seeded sentinel would classify that flip as
        "unchanged" and the entity would stay unavailable until its value
        happens to change.
        """
        await super().async_added_to_hass()  # type: ignore[misc]
        self._last_written_available = self.available  # type: ignore[attr-defined]

    def _write_state_if_changed(self, new_value: Any) -> None:
        """Write HA state when the value or the availability changed."""
        new_available = self.available  # type: ignore[attr-defined]
        if (
            # Unseeded sentinel (entity not fully added yet): write, never
            # skip - a spurious write is harmless, a swallowed availability
            # flip strands the entity.
            self._last_written_available is not None
            and new_value == self._last_written_value
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
