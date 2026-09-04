"""PLAN-118: the test harness gives every test its own config directory.

``hass.config.path()`` used to resolve to the pytest plugin's shared
package directory (``testing_config/``), so a state file the energy
integrator flushed in one test was still on disk for the next run of any
other test - in the same session, or a later one, since the file survives
between invocations. These tests pin the fix from ADR-012: the
``hass_config_dir`` override in ``tests/ha/conftest.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
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
    site-packages path instead of the test's own tmp_path.
    """
    resolved = hass.config.path(".storage/x.json")
    assert resolved.startswith(str(tmp_path))


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
        """Run after its partner above: a new coordinator must start empty.

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


def test_leftovers_in_the_plugin_dir_are_not_copied_in(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``.storage/`` left in the plugin's own package dir must not leak in.

    ``hass_tmp_config_dir`` copies the whole ``testing_config/`` directory,
    so a ``.storage/`` a pre-fix run left behind there would otherwise be
    copied into every test's own tmp directory. The ``hass_config_dir``
    fixture in ``conftest.py`` removes the copy after that ``copytree`` runs.

    This calls both fixture functions directly, in that order, instead of
    going through the ``hass`` fixture: measured on 2026-09-04, the ``hass``
    graph resolves ``hass_tmp_config_dir`` (and its ``copytree``) before a
    same-scope, no-dependency fixture placed earlier in a test's parameter
    list, even though nothing documents that ordering as reliable. A
    monkeypatch applied through such a fixture would silently miss the
    ``copytree`` call it exists to influence - which this test's own first
    draft did: it seeded a fake plugin directory, asserted a clean result,
    and passed whether or not the ``.storage`` removal in ``conftest.py``
    was even present, because ``get_test_config_dir`` had already been
    called before the monkeypatch took hold.
    """
    fake_plugin_dir = tmp_path_factory.mktemp("fake_plugin_dir")
    storage = fake_plugin_dir / ".storage"
    storage.mkdir()
    (storage / _STATE_FILE_NAME).write_text(
        '{"solar_energy_kwh": [200.004, 500.0, 0.0]}'
    )
    monkeypatch.setattr(
        "pytest_homeassistant_custom_component.plugins.get_test_config_dir",
        lambda: str(fake_plugin_dir),
    )

    from pytest_homeassistant_custom_component.plugins import hass_tmp_config_dir

    from .conftest import hass_config_dir as hass_config_dir_fixture

    dest = tmp_path_factory.mktemp("test_config_dir")
    copied_dir = hass_tmp_config_dir.__wrapped__(dest)
    config_dir = hass_config_dir_fixture.__wrapped__(copied_dir)

    assert not (Path(config_dir) / ".storage").exists()
