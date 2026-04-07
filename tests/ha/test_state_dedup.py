"""Tests for entity-level state update deduplication.

Verifies that _handle_coordinator_update() only calls async_write_ha_state()
when the entity value has actually changed, reducing unnecessary recorder writes.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    DOMAIN,
    EcoFlowBinarySensorDef,
    EcoFlowNumberDef,
    EcoFlowSensorDef,
    EcoFlowSwitchDef,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.sensor import EcoFlowDiagnosticSensor, EcoFlowSensor
from custom_components.ecoflow_energy.binary_sensor import EcoFlowBinarySensor
from custom_components.ecoflow_energy.switch import EcoFlowSwitch
from custom_components.ecoflow_energy.number import EcoFlowNumber

from .conftest import MOCK_DELTA_DEVICE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_coordinator(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> EcoFlowDeviceCoordinator:
    """Create a coordinator without calling async_setup."""
    return EcoFlowDeviceCoordinator(hass, entry, MOCK_DELTA_DEVICE)


# ===========================================================================
# EcoFlowSensor deduplication
# ===========================================================================


class TestSensorDedup:
    async def test_sensor_no_write_on_same_value(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """async_write_ha_state is NOT called when value has not changed."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"soc": 85.0})

        defn = EcoFlowSensorDef(key="soc", name="SoC", unit="%")
        sensor = EcoFlowSensor(coordinator, defn)

        with patch.object(sensor, "async_write_ha_state") as mock_write:
            # First call: value transitions from None to 85.0 → writes
            sensor._handle_coordinator_update()
            assert mock_write.call_count == 1

            # Second call: same value 85.0 → no write
            sensor._handle_coordinator_update()
            assert mock_write.call_count == 1

    async def test_sensor_writes_on_value_change(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """async_write_ha_state IS called when value changes."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"soc": 85.0})

        defn = EcoFlowSensorDef(key="soc", name="SoC", unit="%")
        sensor = EcoFlowSensor(coordinator, defn)

        with patch.object(sensor, "async_write_ha_state") as mock_write:
            # First update: None → 85.0
            sensor._handle_coordinator_update()
            assert mock_write.call_count == 1

            # Value changes to 90.0
            coordinator.async_set_updated_data({"soc": 90.0})
            sensor._handle_coordinator_update()
            assert mock_write.call_count == 2

    async def test_sensor_writes_on_first_update(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """First update always writes (transitions from None)."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"soc": 42.0})

        defn = EcoFlowSensorDef(key="soc", name="SoC", unit="%")
        sensor = EcoFlowSensor(coordinator, defn)
        assert sensor._last_written_value is None

        with patch.object(sensor, "async_write_ha_state") as mock_write:
            sensor._handle_coordinator_update()
            assert mock_write.call_count == 1
            assert sensor._last_written_value == 42.0

    async def test_sensor_restore_then_same_value(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """No write if coordinator provides the same value as restored."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        defn = EcoFlowSensorDef(key="soc", name="SoC", unit="%")
        sensor = EcoFlowSensor(coordinator, defn)

        # Simulate restore: _restored_value and _last_written_value are both set
        sensor._restored_value = 75.0
        sensor._last_written_value = 75.0

        # Coordinator delivers the same value as restored
        coordinator.async_set_updated_data({"soc": 75.0})

        with patch.object(sensor, "async_write_ha_state") as mock_write:
            sensor._handle_coordinator_update()
            assert mock_write.call_count == 0

    async def test_sensor_restore_then_different_value(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Write if coordinator provides a different value than restored."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        defn = EcoFlowSensorDef(key="soc", name="SoC", unit="%")
        sensor = EcoFlowSensor(coordinator, defn)

        # Simulate restore
        sensor._restored_value = 75.0
        sensor._last_written_value = 75.0

        # Coordinator delivers a different value
        coordinator.async_set_updated_data({"soc": 80.0})

        with patch.object(sensor, "async_write_ha_state") as mock_write:
            sensor._handle_coordinator_update()
            assert mock_write.call_count == 1
            assert sensor._last_written_value == 80.0


# ===========================================================================
# EcoFlowDiagnosticSensor deduplication
# ===========================================================================


class TestDiagnosticSensorDedup:
    async def test_diagnostic_sensor_dedup(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Diagnostic sensor only writes when its value changes."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        sensor = EcoFlowDiagnosticSensor(coordinator, "mqtt_status")

        with patch.object(sensor, "async_write_ha_state") as mock_write:
            # First call: None → actual value (writes)
            sensor._handle_coordinator_update()
            first_count = mock_write.call_count

            # Second call: same value → no additional write
            sensor._handle_coordinator_update()
            assert mock_write.call_count == first_count


# ===========================================================================
# EcoFlowBinarySensor deduplication
# ===========================================================================


class TestBinarySensorDedup:
    async def test_binary_sensor_dedup(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Binary sensor only writes when is_on changes."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"ac_enabled": 1})

        defn = EcoFlowBinarySensorDef(key="ac_enabled", name="AC Enabled")
        sensor = EcoFlowBinarySensor(coordinator, defn)

        with patch.object(sensor, "async_write_ha_state") as mock_write:
            # First: None → True
            sensor._handle_coordinator_update()
            assert mock_write.call_count == 1

            # Same value → no write
            sensor._handle_coordinator_update()
            assert mock_write.call_count == 1

            # Value changes → writes
            coordinator.async_set_updated_data({"ac_enabled": 0})
            sensor._handle_coordinator_update()
            assert mock_write.call_count == 2
            assert sensor._last_written_value is False


# ===========================================================================
# EcoFlowSwitch deduplication
# ===========================================================================


class TestSwitchDedup:
    async def test_switch_dedup(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Switch only writes on coordinator update when is_on changes."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"ac_enabled": 1})

        defn = EcoFlowSwitchDef(key="ac_switch", name="AC Output", state_key="ac_enabled")
        switch = EcoFlowSwitch(coordinator, defn)

        with patch.object(switch, "async_write_ha_state") as mock_write:
            # First: None → True
            switch._handle_coordinator_update()
            assert mock_write.call_count == 1

            # Same value → no write
            switch._handle_coordinator_update()
            assert mock_write.call_count == 1

            # Value changes → writes
            coordinator.async_set_updated_data({"ac_enabled": 0})
            switch._handle_coordinator_update()
            assert mock_write.call_count == 2

    async def test_switch_optimistic_write_always_works(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """_send_command always calls async_write_ha_state (optimistic update)."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"ac_enabled": 1})

        defn = EcoFlowSwitchDef(key="ac_switch", name="AC Output", state_key="ac_enabled")
        switch = EcoFlowSwitch(coordinator, defn)

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock),
            patch.object(switch, "async_write_ha_state") as mock_write,
        ):
            # _send_command always writes — it bypasses _handle_coordinator_update
            await switch._send_command(False)
            assert mock_write.call_count == 1


# ===========================================================================
# EcoFlowNumber deduplication
# ===========================================================================


class TestNumberDedup:
    async def test_number_dedup(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Number only writes on coordinator update when native_value changes."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"pd.soc": 80})

        defn = EcoFlowNumberDef(
            key="max_charge_soc", name="Max Charge SoC",
            state_key="pd.soc", unit="%",
        )
        number = EcoFlowNumber(coordinator, defn)

        with patch.object(number, "async_write_ha_state") as mock_write:
            # First: None → 80.0
            number._handle_coordinator_update()
            assert mock_write.call_count == 1

            # Same value → no write
            number._handle_coordinator_update()
            assert mock_write.call_count == 1

            # Value changes → writes
            coordinator.async_set_updated_data({"pd.soc": 90})
            number._handle_coordinator_update()
            assert mock_write.call_count == 2

    async def test_number_optimistic_write_always_works(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """async_set_native_value always calls async_write_ha_state (optimistic)."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"pd.soc": 80})

        defn = EcoFlowNumberDef(
            key="max_charge_soc", name="Max Charge SoC",
            state_key="pd.soc", unit="%",
        )
        number = EcoFlowNumber(coordinator, defn)

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock),
            patch.object(number, "async_write_ha_state") as mock_write,
        ):
            # async_set_native_value directly calls async_write_ha_state
            await number.async_set_native_value(95.0)
            assert mock_write.call_count == 1


# ===========================================================================
# Energy rounding precision
# ===========================================================================


class TestEnergyRounding:
    async def test_energy_rounding_precision(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Energy values are rounded to 2 decimal places (0.01 kWh resolution)."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        # Mock the energy integrator to return a value with 3+ decimal places
        mock_integrator = MagicMock()
        mock_integrator.integrate.return_value = 15.12345
        mock_integrator.flush.return_value = None
        coordinator._energy_integrator = mock_integrator

        # Set up a power→energy mapping for testing
        coordinator._power_to_energy = {"test_power": "test_energy"}
        coordinator._energy_from_api = []

        # Call _integrate_energy with a parsed dict containing power
        coordinator._integrate_energy({"test_power": 500.0})

        # Verify the energy value is rounded to 2 decimal places
        assert coordinator._device_data["test_energy"] == 15.12
        assert coordinator._device_data["test_energy"] != 15.123

    async def test_energy_rounding_api_fallback(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Energy from API fallback integration also rounds to 2 decimals."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        mock_integrator = MagicMock()
        mock_integrator.integrate.return_value = 7.98765
        mock_integrator.flush.return_value = None
        coordinator._energy_integrator = mock_integrator

        # Set up energy_from_api mapping (no API total in parsed → falls back to integrate)
        coordinator._power_to_energy = {}
        coordinator._energy_from_api = [("fallback_power", "fallback_energy")]

        coordinator._integrate_energy({"fallback_power": 200.0})

        assert coordinator._device_data["fallback_energy"] == 7.99
        assert coordinator._device_data["fallback_energy"] != 7.988


# ===========================================================================
# Dedup sync after optimistic writes (no double-write on next coordinator tick)
# ===========================================================================


class TestOptimisticDedupSync:
    async def test_switch_no_double_write_after_optimistic(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """_handle_coordinator_update must not re-write the value that _send_command already wrote."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"ac_enabled": 1})

        defn = EcoFlowSwitchDef(key="ac_switch", name="AC Output", state_key="ac_enabled")
        switch = EcoFlowSwitch(coordinator, defn)

        # Establish baseline: set _last_written_value directly (entity has no hass yet)
        switch._last_written_value = True

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock),
            patch.object(switch, "async_write_ha_state") as mock_write,
        ):
            # User turns off: optimistic write (count = 1), syncs _last_written_value = False
            with patch("time.monotonic", return_value=1000.0):
                await switch._send_command(False)
            assert mock_write.call_count == 1
            assert switch._last_written_value is False

            # Coordinator tick during lock window — is_on still returns False (lock active).
            # Because _last_written_value is already False, no second write.
            with patch("time.monotonic", return_value=1001.0):  # still within 5 s lock
                switch._handle_coordinator_update()
            assert mock_write.call_count == 1  # no additional write

    async def test_number_no_double_write_after_optimistic(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """_handle_coordinator_update must not re-write the value that async_set_native_value already wrote."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"pd.soc": 80})

        defn = EcoFlowNumberDef(
            key="max_charge_soc", name="Max Charge SoC",
            state_key="pd.soc", unit="%",
        )
        number = EcoFlowNumber(coordinator, defn)

        # Establish baseline: set _last_written_value directly (entity has no hass yet)
        number._last_written_value = 80.0

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock),
            patch.object(number, "async_write_ha_state") as mock_write,
        ):
            # User sets 95: optimistic write (count = 1), mutates data, syncs _last_written_value = 95.0
            await number.async_set_native_value(95.0)
            assert mock_write.call_count == 1
            assert number._last_written_value == 95.0

            # Next coordinator tick delivers the same 95.0 (from mutated data).
            # Because _last_written_value is already 95.0, no second write.
            number._handle_coordinator_update()
            assert mock_write.call_count == 1  # no additional write


# ===========================================================================
# Enum sensor restore discard
# ===========================================================================


class TestEnumRestoreDiscard:
    """Verify that restored values not in the options list are discarded.

    Uses direct patch on super().async_added_to_hass to avoid CoordinatorEntity
    listener registration which leaves lingering timers.
    """

    async def test_enum_sensor_discards_invalid_restored_value(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Old numeric restored value '0' is discarded for enum sensor."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        definition = EcoFlowSensorDef(
            "ems_feed_mode", "EMS Feed Mode", None, "enum", None,
            "mdi:cog", "diagnostic", options=["off", "no_limit", "zero", "limit"],
        )
        sensor = EcoFlowSensor(coordinator, definition)
        sensor.hass = hass

        mock_last = MagicMock()
        mock_last.native_value = "0"
        with (
            patch.object(type(sensor).__mro__[1], "async_added_to_hass", new_callable=AsyncMock),
            patch.object(sensor, "async_get_last_sensor_data", new_callable=AsyncMock, return_value=mock_last),
        ):
            await sensor.async_added_to_hass()

        assert sensor._restored_value is None

    async def test_enum_sensor_discards_old_workmode_string(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Old string restored value 'WORKMODE_SELFUSE' is discarded for enum sensor."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        definition = EcoFlowSensorDef(
            "ems_work_mode", "EMS Work Mode", None, "enum", None,
            "mdi:cog", "diagnostic", options=["self_use", "time_of_use", "backup"],
        )
        sensor = EcoFlowSensor(coordinator, definition)
        sensor.hass = hass

        mock_last = MagicMock()
        mock_last.native_value = "WORKMODE_SELFUSE"
        with (
            patch.object(type(sensor).__mro__[1], "async_added_to_hass", new_callable=AsyncMock),
            patch.object(sensor, "async_get_last_sensor_data", new_callable=AsyncMock, return_value=mock_last),
        ):
            await sensor.async_added_to_hass()

        assert sensor._restored_value is None

    async def test_enum_sensor_accepts_valid_restored_value(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Valid restored value 'limit' is accepted for enum sensor."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        definition = EcoFlowSensorDef(
            "ems_feed_mode", "EMS Feed Mode", None, "enum", None,
            "mdi:cog", "diagnostic", options=["off", "no_limit", "zero", "limit"],
        )
        sensor = EcoFlowSensor(coordinator, definition)
        sensor.hass = hass

        mock_last = MagicMock()
        mock_last.native_value = "limit"
        with (
            patch.object(type(sensor).__mro__[1], "async_added_to_hass", new_callable=AsyncMock),
            patch.object(sensor, "async_get_last_sensor_data", new_callable=AsyncMock, return_value=mock_last),
        ):
            await sensor.async_added_to_hass()

        assert sensor._restored_value == "limit"

    async def test_non_enum_sensor_restores_any_value(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Non-enum sensor restores any value without filtering."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        definition = EcoFlowSensorDef(
            "soc_pct", "Battery SoC", "%", "battery", "measurement", "mdi:battery", None,
        )
        sensor = EcoFlowSensor(coordinator, definition)
        sensor.hass = hass

        mock_last = MagicMock()
        mock_last.native_value = 85.0
        with (
            patch.object(type(sensor).__mro__[1], "async_added_to_hass", new_callable=AsyncMock),
            patch.object(sensor, "async_get_last_sensor_data", new_callable=AsyncMock, return_value=mock_last),
        ):
            await sensor.async_added_to_hass()

        assert sensor._restored_value == 85.0
