"""Tests for the PowerOcean Select entity (Work Mode)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    DEVICE_TYPE_POWEROCEAN,
    POWEROCEAN_SELECTS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.select import (
    EcoFlowSelect,
    WORK_MODE_TO_INT,
    _get_select_defs,
)

from .conftest import MOCK_POWEROCEAN_DEVICE


class TestSelectDefs:
    def test_powerocean_returns_powerocean_selects(self):
        defs = _get_select_defs(DEVICE_TYPE_POWEROCEAN)
        assert defs is POWEROCEAN_SELECTS

    def test_powerocean_select_keys(self):
        defs = _get_select_defs(DEVICE_TYPE_POWEROCEAN)
        keys = {d.key for d in defs}
        assert keys == {"work_mode"}

    def test_work_mode_options(self):
        defs = _get_select_defs(DEVICE_TYPE_POWEROCEAN)
        wm = next(d for d in defs if d.key == "work_mode")
        assert wm.options == ("self_use", "ai_schedule")

    def test_work_mode_enhanced_only(self):
        defs = _get_select_defs(DEVICE_TYPE_POWEROCEAN)
        assert all(d.enhanced_only for d in defs)

    def test_unknown_device_type_returns_empty(self):
        defs = _get_select_defs("unknown")
        assert defs == []


class TestWorkModeMapping:
    """Maps the user-facing options to wire-level integers."""

    def test_self_use_maps_to_zero(self):
        assert WORK_MODE_TO_INT["self_use"] == 0

    def test_ai_schedule_maps_to_twelve(self):
        assert WORK_MODE_TO_INT["ai_schedule"] == 12

    def test_only_phase_1_modes_exposed(self):
        # TOU and Backup require sub-params; they must NOT be in the map
        assert "time_of_use" not in WORK_MODE_TO_INT
        assert "backup" not in WORK_MODE_TO_INT


class TestEcoFlowSelect:
    def _make_entity(self, hass, entry):
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE,
        )
        coordinator._device_data = {"ems_work_mode": "self_use"}
        coordinator.async_set_updated_data(dict(coordinator._device_data))
        defn = next(d for d in POWEROCEAN_SELECTS if d.key == "work_mode")
        entity = EcoFlowSelect(coordinator, defn)
        return entity, coordinator

    async def test_current_option_self_use(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        entity, _ = self._make_entity(hass, enhanced_config_entry)
        assert entity.current_option == "self_use"

    async def test_current_option_unknown_value_returns_none(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Modes not in the exposed options return None (not a crash)."""
        entity, coordinator = self._make_entity(hass, enhanced_config_entry)
        coordinator._device_data["ems_work_mode"] = "time_of_use"
        coordinator.async_set_updated_data(dict(coordinator._device_data))
        assert entity.current_option is None

    async def test_async_select_option_self_use(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        entity, coordinator = self._make_entity(hass, enhanced_config_entry)
        coordinator.async_set_powerocean_work_mode = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        await entity.async_select_option("self_use")

        coordinator.async_set_powerocean_work_mode.assert_called_once_with(0)

    async def test_async_select_option_ai_schedule(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        entity, coordinator = self._make_entity(hass, enhanced_config_entry)
        coordinator.async_set_powerocean_work_mode = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        await entity.async_select_option("ai_schedule")

        coordinator.async_set_powerocean_work_mode.assert_called_once_with(12)

    async def test_async_select_option_invalid_ignored(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Options not in the exposed list are rejected without calling SET."""
        entity, coordinator = self._make_entity(hass, enhanced_config_entry)
        coordinator.async_set_powerocean_work_mode = AsyncMock()

        await entity.async_select_option("backup")

        coordinator.async_set_powerocean_work_mode.assert_not_called()

    async def test_set_failed_no_optimistic_update(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        entity, coordinator = self._make_entity(hass, enhanced_config_entry)
        coordinator.async_set_powerocean_work_mode = AsyncMock(return_value=False)

        await entity.async_select_option("ai_schedule")

        # Failed SET: optimistic value not applied, current_option stays
        assert coordinator._device_data["ems_work_mode"] == "self_use"
