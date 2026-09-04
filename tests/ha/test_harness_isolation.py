"""PLAN-118: the test harness gives every test its own config directory.

``hass.config.path()`` used to resolve to the pytest plugin's shared
package directory (``testing_config/``), so a state file the energy
integrator flushed in one test was still on disk for the next run of any
other test - in the same session, or a later one, since the file survives
between invocations. These tests pin the fix from ADR-012 as amended
2026-09-04: the ``hass`` fixture override in ``tests/ha/conftest.py`` that
re-points ``hass.config.config_dir`` at pytest's own ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator

from .conftest import MOCK_POWEROCEAN_DEVICE

# Built from the shared mock rather than spelled out, so the serial lives in
# exactly one place and this file carries no identifier-shaped literal.
_STATE_FILE_NAME = f"ecoflow_energy_{MOCK_POWEROCEAN_DEVICE['sn']}.json"

_CLOCK = "custom_components.ecoflow_energy.ecoflow.energy_integrator.time.monotonic"


async def test_state_file_path_is_under_the_test_tmp_dir(
    hass: HomeAssistant, tmp_path: Path,
) -> None:
    """The integrator's state path lives under pytest's own tmp dir.

    Before the fix, ``hass.config.path()`` resolved to the pytest plugin's
    installed package directory, so this assertion failed against a real
    site-packages path instead of the test's own tmp_path. The second
    assertion pins that nothing is copied into ``tmp_path`` on the way in -
    unlike the plugin's own ``hass_tmp_config_dir`` hook, re-pointing
    ``config_dir`` after the fact never populates a ``.storage/`` the test
    did not write itself.
    """
    resolved = hass.config.path(".storage/x.json")
    assert resolved.startswith(str(tmp_path))
    assert not (tmp_path / ".storage").exists()


class TestStateDoesNotLeakAcrossTests:
    """An ordered pair: one test writes a total, the next must not see it."""

    async def test_a_flushed_total_lands_in_this_test_dir(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        tmp_path: Path,
    ) -> None:
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        integrator = coordinator._energy_integrator

        with patch(_CLOCK, return_value=1000.0):
            integrator.integrate("solar_energy_kwh", 1000.0)
        with patch(_CLOCK, return_value=1036.0):
            integrator.integrate("solar_energy_kwh", 1000.0)
        integrator.flush()

        state_file = Path(hass.config.path(f".storage/{_STATE_FILE_NAME}"))
        assert state_file.exists()
        assert str(state_file).startswith(str(tmp_path))

    async def test_a_fresh_coordinator_reads_nothing(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """A new coordinator must start empty.

        Under the fixture this holds on its own, because every test gets its
        own config directory. It is written next to the test that flushes a
        total because that pairing is what fails without the fixture, not
        because it depends on the order.

        Before the fix this read the total the previous test flushed, off
        the plugin's shared directory - the exact PLAN-118 failure, and it
        reproduces even when this test file runs alone on a machine with a
        leftover state file from an earlier pytest invocation.
        """
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._energy_integrator.load_state()
        assert coordinator._energy_integrator.state_snapshot() == {}
