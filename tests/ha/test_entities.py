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
    DEVICE_TYPE_SMARTPLUG,
    DOMAIN,
    EcoFlowBinarySensorDef,
    EcoFlowNumberDef,
    EcoFlowSensorDef,
    EcoFlowSwitchDef,
    POWEROCEAN_BINARY_SENSORS,
    POWEROCEAN_SENSORS,
    SMARTPLUG_NUMBERS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.sensor import (
    EcoFlowDiagnosticSensor,
    EcoFlowSensor,
    _get_sensor_defs,
)
from custom_components.ecoflow_energy.binary_sensor import (
    EcoFlowBinarySensor,
    _get_binary_sensor_defs,
)
from custom_components.ecoflow_energy.switch import (
    EcoFlowSwitch,
    OPTIMISTIC_LOCK_S,
    SWITCH_COMMANDS_R351 as SWITCH_COMMANDS,
    SWITCH_DECLARATIVE_R351 as SWITCH_DECLARATIVE,
    _get_switch_defs,
)
from custom_components.ecoflow_energy.number import (
    EcoFlowNumber,
    NUMBER_COMMANDS,
    SMARTPLUG_NUMBER_COMMANDS,
    _get_number_defs,
)

from .conftest import MOCK_DELTA_DEVICE, MOCK_POWEROCEAN_DEVICE, MOCK_SMARTPLUG_DEVICE


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

    def test_smartplug_numbers(self):
        assert _get_number_defs(DEVICE_TYPE_SMARTPLUG) is SMARTPLUG_NUMBERS

    def test_powerocean_numbers(self):
        from custom_components.ecoflow_energy.const import POWEROCEAN_NUMBERS
        assert _get_number_defs(DEVICE_TYPE_POWEROCEAN) is POWEROCEAN_NUMBERS


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

    async def test_sensor_native_value_precision_rounding(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Sensor rounds value based on suggested_display_precision."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({
            "power_w": 2347.28399,
            "energy_kwh": 15.23456,
            "soc_pct": 76.8,
            "raw_val": 3.14159,
            "int_val": 500,
            "str_val": "online",
        })

        # precision=0 → integer
        defn_w = EcoFlowSensorDef(key="power_w", name="Power", suggested_display_precision=0)
        assert EcoFlowSensor(coordinator, defn_w).native_value == 2347
        assert isinstance(EcoFlowSensor(coordinator, defn_w).native_value, int)

        # precision=2 → 2 decimal places
        defn_kwh = EcoFlowSensorDef(key="energy_kwh", name="Energy", suggested_display_precision=2)
        assert EcoFlowSensor(coordinator, defn_kwh).native_value == 15.23

        # precision=1
        defn_soc = EcoFlowSensorDef(key="soc_pct", name="SoC", suggested_display_precision=1)
        assert EcoFlowSensor(coordinator, defn_soc).native_value == 76.8

        # no precision → raw value
        defn_raw = EcoFlowSensorDef(key="raw_val", name="Raw")
        assert EcoFlowSensor(coordinator, defn_raw).native_value == 3.14159

        # already-int with precision=0 → stays int (no fractional rounding artifacts)
        defn_int = EcoFlowSensorDef(key="int_val", name="Int", suggested_display_precision=0)
        assert EcoFlowSensor(coordinator, defn_int).native_value == 500
        assert isinstance(EcoFlowSensor(coordinator, defn_int).native_value, int)

        # string value with precision set → passes through unchanged
        defn_str = EcoFlowSensorDef(key="str_val", name="Str", suggested_display_precision=0)
        assert EcoFlowSensor(coordinator, defn_str).native_value == "online"

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
        switch._optimistic_lock_until = time.monotonic() + 10.0  # far in the future
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
        switch._optimistic_lock_until = time.monotonic() - 1.0
        assert switch.is_on is True  # back to coordinator value

    async def test_switch_commands_templates(self) -> None:
        """All switch defs have matching command templates (legacy or declarative)."""
        for defn in DELTA2MAX_SWITCHES:
            has_legacy = defn.key in SWITCH_COMMANDS
            has_declarative = defn.key in SWITCH_DECLARATIVE
            assert has_legacy or has_declarative, f"No command template for {defn.key}"
            if has_legacy:
                assert "on" in SWITCH_COMMANDS[defn.key]
                assert "off" in SWITCH_COMMANDS[defn.key]
            if has_declarative:
                decl = SWITCH_DECLARATIVE[defn.key]
                assert "moduleType" in decl
                assert "operateType" in decl
                assert "param_key" in decl

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
            assert switch._optimistic_lock_until > time.monotonic()

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

    async def test_beeper_switch_on_sends_inverted(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Beeper ON sends quietCfg enabled=0 (quiet mode OFF)."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        defn = EcoFlowSwitchDef(key="beeper_switch", name="Beeper", state_key="beep_enabled")
        switch = EcoFlowSwitch(coordinator, defn)

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd,
            patch.object(switch, "async_write_ha_state"),
        ):
            await switch.async_turn_on()
            mock_cmd.assert_called_once()
            cmd = mock_cmd.call_args[0][0]
            assert cmd["moduleType"] == 1
            assert cmd["operateType"] == "quietCfg"
            assert cmd["params"]["enabled"] == 0  # inverted: ON = quiet OFF

    async def test_beeper_switch_off_sends_inverted(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Beeper OFF sends quietCfg enabled=1 (quiet mode ON)."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        defn = EcoFlowSwitchDef(key="beeper_switch", name="Beeper", state_key="beep_enabled")
        switch = EcoFlowSwitch(coordinator, defn)

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd,
            patch.object(switch, "async_write_ha_state"),
        ):
            await switch.async_turn_off()
            mock_cmd.assert_called_once()
            cmd = mock_cmd.call_args[0][0]
            assert cmd["params"]["enabled"] == 1  # inverted: OFF = quiet ON

    async def test_xboost_switch_on(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """X-Boost ON sends acOutCfg xboost=1."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        defn = EcoFlowSwitchDef(key="xboost_switch", name="X-Boost", state_key="ac_xboost")
        switch = EcoFlowSwitch(coordinator, defn)

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd,
            patch.object(switch, "async_write_ha_state"),
        ):
            await switch.async_turn_on()
            cmd = mock_cmd.call_args[0][0]
            assert cmd["moduleType"] == 3
            assert cmd["operateType"] == "acOutCfg"
            assert cmd["params"]["xboost"] == 1

    async def test_ac_auto_on_switch_on(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """AC Auto Restart ON sends extra_params."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        defn = EcoFlowSwitchDef(key="ac_auto_on_switch", name="AC Auto Restart", state_key="ac_auto_on")
        switch = EcoFlowSwitch(coordinator, defn)

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd,
            patch.object(switch, "async_write_ha_state"),
        ):
            await switch.async_turn_on()
            cmd = mock_cmd.call_args[0][0]
            assert cmd["params"]["enabled"] == 1
            assert cmd["params"]["minAcSoc"] == 5

    async def test_backup_reserve_switch_off(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Backup Reserve OFF sends isConfig=0 with extra_params."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)

        defn = EcoFlowSwitchDef(
            key="backup_reserve_switch", name="Backup Reserve",
            state_key="backup_reserve_enabled",
        )
        switch = EcoFlowSwitch(coordinator, defn)

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd,
            patch.object(switch, "async_write_ha_state"),
        ):
            await switch.async_turn_off()
            cmd = mock_cmd.call_args[0][0]
            assert cmd["moduleType"] == 1
            assert cmd["operateType"] == "watthConfig"
            assert cmd["params"]["isConfig"] == 0
            assert cmd["params"]["bpPowerSoc"] == 50
            assert cmd["params"]["minChgSoc"] == 0
            assert cmd["params"]["minDsgSoc"] == 0


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

        with patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd, \
             patch.object(number, "async_write_ha_state"):
            await number.async_set_native_value(90.0)
            mock_cmd.assert_called_once()
            cmd = mock_cmd.call_args[0][0]
            assert cmd["moduleType"] == 2
            assert cmd["operateType"] == "upsConfig"
            assert cmd["params"]["maxChgSoc"] == 90

    async def test_smartplug_number_command_templates(self) -> None:
        """All Smart Plug number defs have matching command templates."""
        for defn in SMARTPLUG_NUMBERS:
            assert defn.key in SMARTPLUG_NUMBER_COMMANDS, f"No command for {defn.key}"
            cmd = SMARTPLUG_NUMBER_COMMANDS[defn.key]
            assert "cmdCode" in cmd
            assert "param_key" in cmd

    async def test_smartplug_led_brightness_set(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Smart Plug LED brightness sends cmdCode format command (% -> raw)."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry, MOCK_SMARTPLUG_DEVICE)
        coordinator.async_set_updated_data({"led_brightness": 50.0})  # 50%

        defn = EcoFlowNumberDef(
            key="led_brightness", name="LED Brightness",
            state_key="led_brightness",
            min_value=0, max_value=100, step=5, unit="%",
        )
        number = EcoFlowNumber(coordinator, defn)

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd,
            patch.object(number, "async_write_ha_state"),
        ):
            await number.async_set_native_value(80.0)  # 80%
            mock_cmd.assert_called_once()
            cmd = mock_cmd.call_args[0][0]
            assert cmd["cmdCode"] == "WN511_SOCKET_SET_BRIGHTNESS_PACK"
            # 80% * 1023/100 = 818.4 -> int = 818
            assert cmd["params"]["brightness"] == 818
            assert cmd["sn"] == coordinator.device_sn

    async def test_smartplug_max_watts_set(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Smart Plug max watts sends cmdCode format command."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry, MOCK_SMARTPLUG_DEVICE)
        coordinator.async_set_updated_data({"max_power_w": 2500})

        defn = EcoFlowNumberDef(
            key="max_watts", name="Max Power Limit",
            state_key="max_power_w", unit="W",
            min_value=0, max_value=2500, step=100,
        )
        number = EcoFlowNumber(coordinator, defn)

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd,
            patch.object(number, "async_write_ha_state"),
        ):
            await number.async_set_native_value(2000.0)
            mock_cmd.assert_called_once()
            cmd = mock_cmd.call_args[0][0]
            assert cmd["cmdCode"] == "WN511_SOCKET_SET_MAX_WATTS"
            assert cmd["params"]["maxWatts"] == 2000
            assert cmd["sn"] == coordinator.device_sn

    async def test_smartplug_number_optimistic_update(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Smart Plug number entity updates state optimistically after SET."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry, MOCK_SMARTPLUG_DEVICE)
        coordinator.async_set_updated_data({"led_brightness": 0})

        defn = EcoFlowNumberDef(
            key="led_brightness", name="LED Brightness",
            state_key="led_brightness",
            min_value=0, max_value=100, step=5, unit="%",
        )
        number = EcoFlowNumber(coordinator, defn)
        assert number.native_value == 0.0

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock),
            patch.object(number, "async_write_ha_state"),
        ):
            await number.async_set_native_value(50.0)  # 50%
            # Optimistic update persists in device_data (survives coordinator refresh)
            assert coordinator.device_data["led_brightness"] == 50.0

    async def test_screen_brightness_command(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Screen brightness sends lcdCfg with brighLevel and delayOff=0."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"screen_brightness": 50})

        defn = EcoFlowNumberDef(
            key="screen_brightness", name="Screen Brightness",
            state_key="screen_brightness", unit="%",
            min_value=0, max_value=100, step=10,
        )
        number = EcoFlowNumber(coordinator, defn)

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd,
            patch.object(number, "async_write_ha_state"),
        ):
            await number.async_set_native_value(80.0)
            cmd = mock_cmd.call_args[0][0]
            assert cmd["moduleType"] == 1
            assert cmd["operateType"] == "lcdCfg"
            assert cmd["params"]["brighLevel"] == 80
            assert cmd["params"]["delayOff"] == 0

    async def test_screen_timeout_command(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Screen timeout sends lcdCfg with delayOff and brighLevel=255."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"screen_timeout_sec": 30})

        defn = EcoFlowNumberDef(
            key="screen_timeout", name="Screen Timeout",
            state_key="screen_timeout_sec", unit="s",
            min_value=0, max_value=1800, step=10,
        )
        number = EcoFlowNumber(coordinator, defn)

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd,
            patch.object(number, "async_write_ha_state"),
        ):
            await number.async_set_native_value(300.0)
            cmd = mock_cmd.call_args[0][0]
            assert cmd["params"]["delayOff"] == 300
            assert cmd["params"]["brighLevel"] == 255

    async def test_backup_reserve_soc_command(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Backup reserve level sends watthConfig with extra_params."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"backup_reserve_soc": 50})

        defn = EcoFlowNumberDef(
            key="backup_reserve_soc", name="Backup Reserve Level",
            state_key="backup_reserve_soc", unit="%",
            min_value=5, max_value=100, step=5,
        )
        number = EcoFlowNumber(coordinator, defn)

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd,
            patch.object(number, "async_write_ha_state"),
        ):
            await number.async_set_native_value(75.0)
            cmd = mock_cmd.call_args[0][0]
            assert cmd["moduleType"] == 1
            assert cmd["operateType"] == "watthConfig"
            assert cmd["params"]["bpPowerSoc"] == 75
            assert cmd["params"]["isConfig"] == 1
            assert cmd["params"]["minChgSoc"] == 0
            assert cmd["params"]["minDsgSoc"] == 0

    async def test_car_standby_timeout_command(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """12V port timeout sends standbyTime to moduleType 5."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"car_standby_min": 120})

        defn = EcoFlowNumberDef(
            key="car_standby_timeout", name="12V Port Timeout",
            state_key="car_standby_min", unit="min",
            min_value=0, max_value=720, step=30,
        )
        number = EcoFlowNumber(coordinator, defn)

        with (
            patch.object(coordinator, "async_send_set_command", new_callable=AsyncMock) as mock_cmd,
            patch.object(number, "async_write_ha_state"),
        ):
            await number.async_set_native_value(240.0)
            cmd = mock_cmd.call_args[0][0]
            assert cmd["moduleType"] == 5
            assert cmd["operateType"] == "standbyTime"
            assert cmd["params"]["standbyMins"] == 240


# ===========================================================================
# EcoFlowDiagnosticSensor
# ===========================================================================


class TestEcoFlowDiagnosticSensor:
    async def test_diagnostic_sensor_mqtt_status(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Diagnostic sensor returns mqtt_status from coordinator."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        sensor = EcoFlowDiagnosticSensor(coordinator, "mqtt_status")

        assert sensor.unique_id == "DAEBK5ZZ12340001_mqtt_status"
        assert sensor.translation_key == "mqtt_status"
        assert sensor.native_value == "not_configured"
        assert sensor.entity_registry_enabled_default is False

    async def test_diagnostic_sensor_connection_mode(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Diagnostic sensor returns connection_mode from coordinator."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        sensor = EcoFlowDiagnosticSensor(coordinator, "connection_mode")

        assert sensor.unique_id == "DAEBK5ZZ12340001_connection_mode"
        assert sensor.native_value == "standard"

    async def test_diagnostic_sensor_entity_category(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Diagnostic sensors have entity_category=DIAGNOSTIC."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        sensor = EcoFlowDiagnosticSensor(coordinator, "mqtt_status")
        from homeassistant.const import EntityCategory
        assert sensor.entity_category is EntityCategory.DIAGNOSTIC

    async def test_diagnostic_sensor_device_info(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Diagnostic sensor provides correct device info."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        sensor = EcoFlowDiagnosticSensor(coordinator, "mqtt_status")
        info = sensor.device_info
        assert (DOMAIN, "DAEBK5ZZ12340001") in info["identifiers"]

    async def test_diagnostic_sensor_unknown_key(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Unknown diagnostic key returns None."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        sensor = EcoFlowDiagnosticSensor(coordinator, "nonexistent")
        assert sensor.native_value is None

    async def test_diagnostic_sensor_mqtt_connected(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """mqtt_status sensor reflects connected MQTT client."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt

        sensor = EcoFlowDiagnosticSensor(coordinator, "mqtt_status")
        assert sensor.native_value == "connected"

    async def test_diagnostic_sensor_enhanced_mode(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """connection_mode sensor returns 'enhanced' for Enhanced Mode."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(
            hass, enhanced_config_entry, device=MOCK_POWEROCEAN_DEVICE
        )
        sensor = EcoFlowDiagnosticSensor(coordinator, "connection_mode")
        assert sensor.native_value == "enhanced"
