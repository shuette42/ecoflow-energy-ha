"""Tests for EcoFlow entity platforms — sensor, binary_sensor, switch, number."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    DELTA2MAX_BINARY_SENSORS,
    DELTA2MAX_NUMBERS,
    DELTA2MAX_SENSORS,
    DELTA2MAX_SWITCHES,
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_POWEROCEAN,
    DOMAIN,
    EcoFlowBinarySensorDef,
    EcoFlowNumberDef,
    EcoFlowSensorDef,
    EcoFlowSwitchDef,
    POWEROCEAN_BINARY_SENSORS,
    POWEROCEAN_SENSORS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.sensor import EcoFlowSensor, _get_sensor_defs
from custom_components.ecoflow_energy.binary_sensor import (
    EcoFlowBinarySensor,
    _get_binary_sensor_defs,
)
from custom_components.ecoflow_energy.switch import (
    EcoFlowSwitch,
    OPTIMISTIC_LOCK_S,
    SWITCH_COMMANDS,
    _get_switch_defs,
)
from custom_components.ecoflow_energy.number import (
    EcoFlowNumber,
    NUMBER_COMMANDS,
    _get_number_defs,
)

from .conftest import MOCK_DELTA_DEVICE, MOCK_POWEROCEAN_DEVICE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_coordinator(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    device: dict | None = None,
) -> EcoFlowDeviceCoordinator:
    """Create a coordinator without calling async_setup."""
    return EcoFlowDeviceCoordinator(
        hass, entry, device or MOCK_DELTA_DEVICE
    )


# ===========================================================================
# Sensor definitions routing
# ===========================================================================


class TestSensorDefsRouting:
    def test_delta_sensors(self):
        assert _get_sensor_defs(DEVICE_TYPE_DELTA) is DELTA2MAX_SENSORS

    def test_powerocean_sensors(self):
        assert _get_sensor_defs(DEVICE_TYPE_POWEROCEAN) is POWEROCEAN_SENSORS

    def test_unknown_sensors_empty(self):
        assert _get_sensor_defs("unknown") == []


class TestBinarySensorDefsRouting:
    def test_delta_binary_sensors(self):
        assert _get_binary_sensor_defs(DEVICE_TYPE_DELTA) is DELTA2MAX_BINARY_SENSORS

    def test_powerocean_binary_sensors(self):
        assert _get_binary_sensor_defs(DEVICE_TYPE_POWEROCEAN) is POWEROCEAN_BINARY_SENSORS

    def test_unknown_binary_sensors_empty(self):
        assert _get_binary_sensor_defs("unknown") == []


class TestSwitchDefsRouting:
    def test_delta_switches(self):
        assert _get_switch_defs(DEVICE_TYPE_DELTA) is DELTA2MAX_SWITCHES

    def test_powerocean_switches_empty(self):
        assert _get_switch_defs(DEVICE_TYPE_POWEROCEAN) == []


class TestNumberDefsRouting:
    def test_delta_numbers(self):
        assert _get_number_defs(DEVICE_TYPE_DELTA) is DELTA2MAX_NUMBERS

    def test_powerocean_numbers_empty(self):
        assert _get_number_defs(DEVICE_TYPE_POWEROCEAN) == []


# ===========================================================================
# EcoFlowSensor
# ===========================================================================


class TestEcoFlowSensor:
    async def test_sensor_attributes(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Sensor entity has correct attributes."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        defn = EcoFlowSensorDef(
            key="soc", name="SoC", unit="%", device_class="battery",
            state_class="measurement", icon="mdi:battery",
        )
        sensor = EcoFlowSensor(coordinator, defn)
        assert sensor.unique_id == "DAEBK5ZZ12340001_soc"
        assert sensor.translation_key == "soc"
        assert sensor.native_unit_of_measurement == "%"
        assert sensor.icon == "mdi:battery"

    async def test_sensor_native_value_from_data(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Sensor returns value from coordinator.data."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"soc": 85.0})

        defn = EcoFlowSensorDef(key="soc", name="SoC", unit="%")
        sensor = EcoFlowSensor(coordinator, defn)
        assert sensor.native_value == 85.0

    async def test_sensor_native_value_none_when_no_data(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Sensor returns None when coordinator.data is None."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        # data is None by default (no refresh yet)

        defn = EcoFlowSensorDef(key="soc", name="SoC")
        sensor = EcoFlowSensor(coordinator, defn)
        assert sensor.native_value is None

    async def test_sensor_device_info(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Sensor provides correct device info."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        defn = EcoFlowSensorDef(key="soc", name="SoC")
        sensor = EcoFlowSensor(coordinator, defn)
        info = sensor.device_info
        assert (DOMAIN, "DAEBK5ZZ12340001") in info["identifiers"]
        assert info["manufacturer"] == "EcoFlow"

    async def test_sensor_entity_category(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Sensor with entity_category='diagnostic' maps correctly."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        defn = EcoFlowSensorDef(
            key="pd_err_code", name="PD Error", entity_category="diagnostic"
        )
        sensor = EcoFlowSensor(coordinator, defn)
        from homeassistant.const import EntityCategory

        assert sensor.entity_category is EntityCategory.DIAGNOSTIC


# ===========================================================================
# EcoFlowBinarySensor
# ===========================================================================


class TestEcoFlowBinarySensor:
    async def test_binary_sensor_is_on_numeric(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Numeric 1 → True, 0 → False."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        defn = EcoFlowBinarySensorDef(key="ac_enabled", name="AC Enabled")

        coordinator.async_set_updated_data({"ac_enabled": 1})
        sensor = EcoFlowBinarySensor(coordinator, defn)
        assert sensor.is_on is True

        coordinator.async_set_updated_data({"ac_enabled": 0})
        assert sensor.is_on is False

    async def test_binary_sensor_is_on_none(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Missing key returns None."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({})

        defn = EcoFlowBinarySensorDef(key="ac_enabled", name="AC Enabled")
        sensor = EcoFlowBinarySensor(coordinator, defn)
        assert sensor.is_on is None

    async def test_binary_sensor_string_on(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """String 'ON' → True, 'OFF' → False."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        defn = EcoFlowBinarySensorDef(key="status", name="Status")

        coordinator.async_set_updated_data({"status": "ON"})
        sensor = EcoFlowBinarySensor(coordinator, defn)
        assert sensor.is_on is True

        coordinator.async_set_updated_data({"status": "OFF"})
        assert sensor.is_on is False


# ===========================================================================
# EcoFlowSwitch
# ===========================================================================


class TestEcoFlowSwitch:
    async def test_switch_is_on_from_state_key(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Switch reads is_on from state_key in coordinator data."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"ac_enabled": 1})

        defn = EcoFlowSwitchDef(key="ac_switch", name="AC Output", state_key="ac_enabled")
        switch = EcoFlowSwitch(coordinator, defn)
        assert switch.is_on is True

    async def test_switch_optimistic_lock(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """During optimistic lock, is_on returns the locally set value."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"ac_enabled": 1})

        defn = EcoFlowSwitchDef(key="ac_switch", name="AC Output", state_key="ac_enabled")
        switch = EcoFlowSwitch(coordinator, defn)

        # Simulate optimistic lock (turned off)
        switch._optimistic_value = False
        switch._optimistic_lock_until = time.time() + 10.0  # far in the future
        # Even though coordinator says 1 (on), optimistic lock says off
        assert switch.is_on is False

    async def test_switch_optimistic_lock_expires(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """After optimistic lock expires, is_on returns coordinator value."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"ac_enabled": 1})

        defn = EcoFlowSwitchDef(key="ac_switch", name="AC Output", state_key="ac_enabled")
        switch = EcoFlowSwitch(coordinator, defn)

        # Expired lock
        switch._optimistic_value = False
        switch._optimistic_lock_until = time.time() - 1.0
        assert switch.is_on is True  # back to coordinator value

    async def test_switch_commands_templates(self) -> None:
        """All switch defs have matching command templates."""
        for defn in DELTA2MAX_SWITCHES:
            assert defn.key in SWITCH_COMMANDS, f"No command template for {defn.key}"
            assert "on" in SWITCH_COMMANDS[defn.key]
            assert "off" in SWITCH_COMMANDS[defn.key]

    async def test_async_turn_on(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """async_turn_on sends ON command and applies optimistic lock."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        defn = EcoFlowSwitchDef(key="ac_switch", name="AC Output", state_key="ac_enabled")
        switch = EcoFlowSwitch(coordinator, defn)

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd,
            patch.object(switch, "async_write_ha_state"),
        ):
            await switch.async_turn_on()
            mock_cmd.assert_called_once()
            cmd = mock_cmd.call_args[0][0]
            assert cmd["params"]["enabled"] == 1
            assert switch._optimistic_value is True
            assert switch._optimistic_lock_until > time.time()

    async def test_async_turn_off(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """async_turn_off sends OFF command and applies optimistic lock."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        defn = EcoFlowSwitchDef(key="dc_switch", name="DC Output", state_key="dc_out_enabled")
        switch = EcoFlowSwitch(coordinator, defn)

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd,
            patch.object(switch, "async_write_ha_state"),
        ):
            await switch.async_turn_off()
            mock_cmd.assert_called_once()
            cmd = mock_cmd.call_args[0][0]
            assert cmd["params"]["enabled"] == 0
            assert switch._optimistic_value is False

    async def test_send_command_missing_template(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """_send_command with unknown key does nothing."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        defn = EcoFlowSwitchDef(key="nonexistent_switch", name="Bad", state_key="x")
        switch = EcoFlowSwitch(coordinator, defn)

        with patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd:
            await switch._send_command(True)
            mock_cmd.assert_not_called()

    async def test_switch_is_on_none_when_no_data(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Switch returns None when coordinator data is None."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        defn = EcoFlowSwitchDef(key="ac_switch", name="AC Output", state_key="ac_enabled")
        switch = EcoFlowSwitch(coordinator, defn)
        assert switch.is_on is None


# ===========================================================================
# EcoFlowNumber
# ===========================================================================


class TestEcoFlowNumber:
    async def test_number_native_value(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Number reads value from state_key."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"ac_chg_rated_power_w": 1200})

        defn = EcoFlowNumberDef(
            key="ac_charge_speed", name="AC Charge Speed",
            state_key="ac_chg_rated_power_w", unit="W",
            min_value=200, max_value=2400, step=100,
        )
        number = EcoFlowNumber(coordinator, defn)
        assert number.native_value == 1200.0

    async def test_number_none_when_missing(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Number returns None when state_key not in data."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({})

        defn = EcoFlowNumberDef(
            key="ac_charge_speed", name="AC Charge Speed",
            state_key="ac_chg_rated_power_w",
        )
        number = EcoFlowNumber(coordinator, defn)
        assert number.native_value is None

    async def test_number_non_numeric_returns_none(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Number returns None for non-numeric values."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"ac_chg_rated_power_w": "error"})

        defn = EcoFlowNumberDef(
            key="ac_charge_speed", name="AC Charge Speed",
            state_key="ac_chg_rated_power_w",
        )
        number = EcoFlowNumber(coordinator, defn)
        assert number.native_value is None

    async def test_number_attributes(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Number entity has correct min/max/step."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        defn = EcoFlowNumberDef(
            key="max_charge_soc", name="Max Charge SoC",
            state_key="max_charge_soc", unit="%",
            min_value=50, max_value=100, step=1,
        )
        number = EcoFlowNumber(coordinator, defn)
        assert number.native_min_value == 50
        assert number.native_max_value == 100
        assert number.native_step == 1
        assert number.native_unit_of_measurement == "%"

    async def test_number_command_templates(self) -> None:
        """All number defs have matching command templates."""
        for defn in DELTA2MAX_NUMBERS:
            assert defn.key in NUMBER_COMMANDS, f"No command for {defn.key}"
            cmd = NUMBER_COMMANDS[defn.key]
            assert "moduleType" in cmd
            assert "operateType" in cmd
            assert "param_key" in cmd

    async def test_set_native_value_builds_command(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """async_set_native_value sends the correct SET command."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        await coordinator.async_setup()

        defn = EcoFlowNumberDef(
            key="max_charge_soc", name="Max Charge SoC",
            state_key="max_charge_soc", unit="%",
            min_value=50, max_value=100, step=1,
        )
        number = EcoFlowNumber(coordinator, defn)

        with patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd:
            await number.async_set_native_value(90.0)
            mock_cmd.assert_called_once()
            cmd = mock_cmd.call_args[0][0]
            assert cmd["moduleType"] == 2
            assert cmd["operateType"] == "upsConfig"
            assert cmd["params"]["maxChgSoc"] == 90
