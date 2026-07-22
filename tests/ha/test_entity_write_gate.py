"""Tests for the shared state-write gate (EcoFlowWriteGateMixin).

Regression coverage for the availability-seed bug: entities added while
their device was silent (HA wrote "unavailable" as initial state) must
recover as soon as the device becomes available again, even when the
entity value is unchanged (e.g. an idle Delta 3 restored at a constant
SoC). The lazily seeded sentinel classified exactly that flip as
"unchanged" and stranded the entity on unavailable.
"""

from __future__ import annotations

import pytest

from custom_components.ecoflow_energy.entity import EcoFlowWriteGateMixin


class _EntityBase:
    """Minimal stand-in for the HA entity base classes in the MRO."""

    async def async_added_to_hass(self) -> None:
        return None


class _GateProbe(EcoFlowWriteGateMixin, _EntityBase):
    """Probe entity exposing write counts and a controllable availability."""

    def __init__(self, available: bool = True) -> None:
        self._available = available
        self.writes = 0

    @property
    def available(self) -> bool:
        return self._available

    def async_write_ha_state(self) -> None:
        self.writes += 1


@pytest.mark.asyncio
async def test_recovery_without_value_change_writes() -> None:
    """Prod scenario: added while unavailable, restored value, device recovers.

    The restored value equals the live value, so the availability flip is
    the only observable change - it must reach the state machine.
    """
    probe = _GateProbe(available=False)
    await probe.async_added_to_hass()  # seeds sentinel with False
    probe._last_written_value = 99  # restore path seeds the value sentinel

    probe._available = True  # device sends again, value unchanged
    probe._write_state_if_changed(99)

    assert probe.writes == 1


@pytest.mark.asyncio
async def test_degradation_without_value_change_writes() -> None:
    """Mirror case: added available, device goes silent, value unchanged."""
    probe = _GateProbe(available=True)
    await probe.async_added_to_hass()
    probe._last_written_value = 99

    probe._available = False
    probe._write_state_if_changed(99)

    assert probe.writes == 1


@pytest.mark.asyncio
async def test_unchanged_value_and_availability_skips_write() -> None:
    """The dedup purpose of the gate is preserved."""
    probe = _GateProbe(available=True)
    await probe.async_added_to_hass()

    probe._write_state_if_changed(42)  # first write: value None -> 42
    probe._write_state_if_changed(42)  # unchanged: skipped
    probe._write_state_if_changed(42)  # unchanged: skipped

    assert probe.writes == 1


@pytest.mark.asyncio
async def test_value_change_writes() -> None:
    probe = _GateProbe(available=True)
    await probe.async_added_to_hass()

    probe._write_state_if_changed(42)
    probe._write_state_if_changed(43)

    assert probe.writes == 2


def test_unseeded_sentinel_always_writes() -> None:
    """Fail-safe: before async_added_to_hass ran, never skip a write."""
    probe = _GateProbe(available=True)
    probe._last_written_value = 99  # even a value match must not skip

    probe._write_state_if_changed(99)

    assert probe.writes == 1


@pytest.mark.asyncio
async def test_write_state_always_bypasses_gate() -> None:
    """Optimistic SET feedback path writes unconditionally and re-seeds."""
    probe = _GateProbe(available=True)
    await probe.async_added_to_hass()

    probe._write_state_always(42)
    probe._write_state_if_changed(42)  # now deduped against the forced write

    assert probe.writes == 1
