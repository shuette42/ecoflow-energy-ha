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
        assert keys == {"backup_reserve", "solar_surplus_threshold"}

    def test_powerocean_numbers_are_enhanced_only(self):
        defs = _get_number_defs(DEVICE_TYPE_POWEROCEAN)
        assert all(d.enhanced_only for d in defs)

    def test_powerocean_backup_reserve_range(self):
        defs = _get_number_defs(DEVICE_TYPE_POWEROCEAN)
        br = next(d for d in defs if d.key == "backup_reserve")
        assert br.min_value == 0
        assert br.max_value == 100
        assert br.step == 5

    def test_powerocean_solar_surplus_range(self):
        defs = _get_number_defs(DEVICE_TYPE_POWEROCEAN)
        ss = next(d for d in defs if d.key == "solar_surplus_threshold")
        assert ss.min_value == 0
        assert ss.max_value == 100
        assert ss.step == 5


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

        result = await coordinator.async_set_soc_limits(100, 10)

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

        result = await coordinator.async_set_soc_limits(100, 10)
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

        result = await coordinator.async_set_soc_limits(100, 10)
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

        result = await coordinator.async_set_soc_limits(100, 10)
        assert result is False


# ===========================================================================
# Number entity SET value routing
# ===========================================================================


class TestPowerOceanNumberBasic:
    """Basic native_value and failure-mode tests using backup_reserve."""

    def _make_number_entity(
        self, hass, entry,
    ) -> tuple[EcoFlowNumber, EcoFlowDeviceCoordinator]:
        """Create a PowerOcean backup_reserve entity with mocked coordinator."""
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE,
        )
        # Seed device data with current limits
        coordinator._device_data = {
            "ems_charge_upper_limit_pct": 100,
            "ems_discharge_lower_limit_pct": 0,
            "ems_backup_ratio_pct": 100,
        }
        coordinator.async_set_updated_data(dict(coordinator._device_data))

        defn = next(d for d in POWEROCEAN_NUMBERS if d.key == "backup_reserve")
        entity = EcoFlowNumber(coordinator, defn)
        return entity, coordinator

    async def test_set_failed_no_optimistic_update(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Failed SET does not optimistically update coordinator data."""
        entity, coordinator = self._make_number_entity(
            hass, enhanced_config_entry,
        )
        coordinator.async_set_powerocean_soc = AsyncMock(return_value=False)

        await entity.async_set_native_value(50.0)

        coordinator.async_set_powerocean_soc.assert_called_once_with(50, 100)
        # No optimistic update — original value retained
        assert coordinator.data["ems_discharge_lower_limit_pct"] == 0

    async def test_native_value_reads_state_key(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Number entity reads current value from coordinator data via state_key."""
        entity, coordinator = self._make_number_entity(
            hass, enhanced_config_entry,
        )
        assert entity.native_value == 0.0

    async def test_native_value_none_when_no_data(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Number entity returns None when coordinator has no data."""
        entity, coordinator = self._make_number_entity(
            hass, enhanced_config_entry,
        )
        coordinator.async_set_updated_data(None)
        assert entity.native_value is None


# ===========================================================================
# 3-field PowerOcean SoC SET (verified against live app traffic 2026-05-06)
# ===========================================================================


class TestAsyncSetPowerOceanSoc:
    async def test_3field_set_success(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE,
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        mock_mqtt.send_proto_set.return_value = True
        coordinator._mqtt_client = mock_mqtt

        result = await coordinator.async_set_powerocean_soc(25, 80)

        assert result is True
        mock_mqtt.send_proto_set.assert_called_once()
        payload = mock_mqtt.send_proto_set.call_args[0][0]
        # Inner pdata contains 3 fields: 1=100, 2=25, 3=80
        # Field 3 (0x18 tag) is sys_bat_backup_ratio per SysBatChgDsgSet proto.
        assert b"\x08\x64\x10\x19\x18\x50" in payload

    async def test_3field_set_rejects_backup_above_solar(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE,
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt

        result = await coordinator.async_set_powerocean_soc(80, 25)
        assert result is False
        mock_mqtt.send_proto_set.assert_not_called()

    async def test_3field_rejects_standard_mode(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        standard_config_entry.add_to_hass(hass)
        from .conftest import MOCK_DELTA_DEVICE
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE,
        )
        result = await coordinator.async_set_powerocean_soc(0, 100)
        assert result is False


class TestAsyncSetWorkMode:
    async def test_work_mode_self_use(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE,
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        mock_mqtt.send_proto_set.return_value = True
        coordinator._mqtt_client = mock_mqtt

        result = await coordinator.async_set_powerocean_work_mode(0)

        assert result is True
        payload = mock_mqtt.send_proto_set.call_args[0][0]
        # cmd_id=98 (0x62), inner field 1 = 0
        assert b"\x48\x62" in payload  # cmd_id=98

    async def test_work_mode_ai_schedule(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE,
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        mock_mqtt.send_proto_set.return_value = True
        coordinator._mqtt_client = mock_mqtt

        result = await coordinator.async_set_powerocean_work_mode(12)

        assert result is True
        payload = mock_mqtt.send_proto_set.call_args[0][0]
        # cmd_id=98 (0x62), inner field 1 = 12 (0x0c)
        assert b"\x08\x0c" in payload


class TestPowerOceanNumberSet3Field:
    def _make_entity(self, hass, entry, key: str):
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE,
        )
        coordinator._device_data = {
            "ems_charge_upper_limit_pct": 100,
            "ems_discharge_lower_limit_pct": 30,
            "ems_backup_ratio_pct": 80,
        }
        coordinator.async_set_updated_data(dict(coordinator._device_data))
        defn = next(d for d in POWEROCEAN_NUMBERS if d.key == key)
        entity = EcoFlowNumber(coordinator, defn)
        return entity, coordinator

    async def test_set_backup_reserve_holds_solar(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Setting backup_reserve sends backup=value, solar=current_solar."""
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "backup_reserve",
        )
        coordinator.async_set_powerocean_soc = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        await entity.async_set_native_value(50.0)

        coordinator.async_set_powerocean_soc.assert_called_once_with(50, 80)

    async def test_set_backup_reserve_clamps_solar_when_higher(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """If new backup > current solar, raise solar to backup."""
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "backup_reserve",
        )
        coordinator._device_data["ems_backup_ratio_pct"] = 40
        coordinator.async_set_updated_data(dict(coordinator._device_data))
        coordinator.async_set_powerocean_soc = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        await entity.async_set_native_value(60.0)

        coordinator.async_set_powerocean_soc.assert_called_once_with(60, 60)

    async def test_set_solar_surplus_holds_backup(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Setting solar_surplus_threshold sends backup=current, solar=value."""
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "solar_surplus_threshold",
        )
        coordinator.async_set_powerocean_soc = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        await entity.async_set_native_value(90.0)

        coordinator.async_set_powerocean_soc.assert_called_once_with(30, 90)

    async def test_set_solar_clamps_backup_when_lower(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """If new solar < current backup, lower backup to solar."""
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "solar_surplus_threshold",
        )
        coordinator.async_set_powerocean_soc = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        # current backup = 30, set solar to 20 -> backup must clamp to 20
        await entity.async_set_native_value(20.0)

        coordinator.async_set_powerocean_soc.assert_called_once_with(20, 20)
