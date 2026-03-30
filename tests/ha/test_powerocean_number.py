"""Tests for PowerOcean number entities — SoC limit SET via Enhanced Mode."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    DEVICE_TYPE_POWEROCEAN,
    DOMAIN,
    POWEROCEAN_NUMBERS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.number import (
    EcoFlowNumber,
    _get_number_defs,
)

from .conftest import (
    MOCK_POWEROCEAN_DEVICE,
)


# ===========================================================================
# Number definition routing
# ===========================================================================


class TestGetNumberDefs:
    def test_powerocean_returns_powerocean_numbers(self):
        defs = _get_number_defs(DEVICE_TYPE_POWEROCEAN)
        assert defs is POWEROCEAN_NUMBERS
        assert len(defs) == 2

    def test_powerocean_number_keys(self):
        defs = _get_number_defs(DEVICE_TYPE_POWEROCEAN)
        keys = {d.key for d in defs}
        assert keys == {"max_charge_soc", "min_discharge_soc"}

    def test_powerocean_numbers_are_enhanced_only(self):
        defs = _get_number_defs(DEVICE_TYPE_POWEROCEAN)
        assert all(d.enhanced_only for d in defs)

    def test_powerocean_max_charge_soc_range(self):
        defs = _get_number_defs(DEVICE_TYPE_POWEROCEAN)
        max_soc = next(d for d in defs if d.key == "max_charge_soc")
        assert max_soc.min_value == 50
        assert max_soc.max_value == 100
        assert max_soc.step == 5

    def test_powerocean_min_discharge_soc_range(self):
        defs = _get_number_defs(DEVICE_TYPE_POWEROCEAN)
        min_soc = next(d for d in defs if d.key == "min_discharge_soc")
        assert min_soc.min_value == 0
        assert min_soc.max_value == 30
        assert min_soc.step == 5


# ===========================================================================
# Coordinator async_set_soc_limits
# ===========================================================================


class TestAsyncSetSocLimits:
    async def test_set_soc_limits_success(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Enhanced Mode coordinator sends SoC limits via proto SET."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE,
        )
        assert coordinator.enhanced_mode is True

        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        mock_mqtt.send_proto_set.return_value = True
        coordinator._mqtt_client = mock_mqtt

        result = await coordinator.async_set_soc_limits(95, 10)

        assert result is True
        mock_mqtt.send_proto_set.assert_called_once()
        payload = mock_mqtt.send_proto_set.call_args[0][0]
        assert isinstance(payload, bytes)

    async def test_set_soc_limits_fails_standard_mode(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Standard Mode coordinator rejects SoC limit SET."""
        standard_config_entry.add_to_hass(hass)

        from .conftest import MOCK_DELTA_DEVICE
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE,
        )
        assert coordinator.enhanced_mode is False

        result = await coordinator.async_set_soc_limits(95, 10)
        assert result is False

    async def test_set_soc_limits_fails_mqtt_disconnected(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Enhanced Mode with disconnected MQTT rejects SoC limit SET."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE,
        )

        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = False
        coordinator._mqtt_client = mock_mqtt

        result = await coordinator.async_set_soc_limits(95, 10)
        assert result is False

    async def test_set_soc_limits_fails_no_mqtt(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Enhanced Mode with no MQTT client rejects SoC limit SET."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE,
        )
        coordinator._mqtt_client = None

        result = await coordinator.async_set_soc_limits(95, 10)
        assert result is False


# ===========================================================================
# Number entity SET value routing
# ===========================================================================


class TestPowerOceanNumberSet:
    def _make_number_entity(
        self, hass, entry, key="max_charge_soc",
    ) -> tuple[EcoFlowNumber, EcoFlowDeviceCoordinator]:
        """Create a PowerOcean number entity with mocked coordinator."""
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE,
        )
        # Seed device data with current limits
        coordinator._device_data = {
            "ems_charge_upper_limit_pct": 100,
            "ems_discharge_lower_limit_pct": 0,
        }
        coordinator.async_set_updated_data(dict(coordinator._device_data))

        defn = next(d for d in POWEROCEAN_NUMBERS if d.key == key)
        entity = EcoFlowNumber(coordinator, defn)
        return entity, coordinator

    async def test_set_max_charge_soc(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Setting max_charge_soc sends both limits with current min."""
        entity, coordinator = self._make_number_entity(
            hass, enhanced_config_entry, "max_charge_soc",
        )
        coordinator.async_set_soc_limits = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        await entity.async_set_native_value(95.0)

        coordinator.async_set_soc_limits.assert_called_once_with(95, 0)
        # Optimistic update
        assert coordinator.data["ems_charge_upper_limit_pct"] == 95.0

    async def test_set_min_discharge_soc(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Setting min_discharge_soc sends both limits with current max."""
        entity, coordinator = self._make_number_entity(
            hass, enhanced_config_entry, "min_discharge_soc",
        )
        coordinator.async_set_soc_limits = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        await entity.async_set_native_value(10.0)

        coordinator.async_set_soc_limits.assert_called_once_with(100, 10)
        # Optimistic update
        assert coordinator.data["ems_discharge_lower_limit_pct"] == 10.0

    async def test_set_failed_no_optimistic_update(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Failed SET does not optimistically update coordinator data."""
        entity, coordinator = self._make_number_entity(
            hass, enhanced_config_entry, "max_charge_soc",
        )
        coordinator.async_set_soc_limits = AsyncMock(return_value=False)

        await entity.async_set_native_value(90.0)

        coordinator.async_set_soc_limits.assert_called_once_with(90, 0)
        # No optimistic update — original value retained
        assert coordinator.data["ems_charge_upper_limit_pct"] == 100

    async def test_native_value_reads_state_key(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Number entity reads current value from coordinator data via state_key."""
        entity, coordinator = self._make_number_entity(
            hass, enhanced_config_entry, "max_charge_soc",
        )
        assert entity.native_value == 100.0

    async def test_native_value_none_when_no_data(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Number entity returns None when coordinator has no data."""
        entity, coordinator = self._make_number_entity(
            hass, enhanced_config_entry, "max_charge_soc",
        )
        coordinator.async_set_updated_data(None)
        assert entity.native_value is None
