"""Entity-set tests for the STREAM AC 5000 (ES22).

Whether this device has PV or a per-phase smart meter is a wiring choice,
not a model difference, so those entities are accessory-gated rather than
listed per serial prefix: they appear once the device actually reports the
reading. Home Assistant keeps an entity in the registry after a later
release stops creating it, so an entity created on a unit that will never
fill it is permanent for that owner.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.binary_sensor import (
    async_setup_entry as binary_sensor_setup,
)
from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_STREAM_AC5000,
    DOMAIN,
    MODE_ENHANCED,
    STREAMAC5000_POWER_TO_ENERGY,
    STREAMAC5000_SENSORS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.const import (
    get_device_name,
    get_device_type,
)
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

ES22_DEVICE: dict[str, Any] = {
    "sn": "ES22TEST00000001",
    "name": "",
    "product_name": "",
    "device_type": DEVICE_TYPE_STREAM_AC5000,
    "online": 1,
}

# Only reported by a unit with PV wired to the EcoFlow itself, or with an
# EcoFlow P1 meter instead of a single-total meter such as a Tibber Pulse.
ACCESSORY_KEYS = {
    "solar_w",
    "solar_energy_kwh",
    "home_from_solar_w",
    "ac_frequency_hz",
    "grid_phase_a_active_power_w",
    "grid_phase_b_active_power_w",
    "grid_phase_c_active_power_w",
    "grid_phase_a_voltage_v",
    "grid_phase_b_voltage_v",
    "grid_phase_c_voltage_v",
    "grid_phase_a_current_a",
    "grid_phase_b_current_a",
    "grid_phase_c_current_a",
}


def _entry(device: dict[str, Any]) -> MockConfigEntry:
    """Build an Enhanced-mode entry for one ES22."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [device],
        },
        unique_id="test@example.com",
    )


async def _setup_keys(
    hass: HomeAssistant,
    platform_setup,
    reported: dict[str, Any] | None = None,
) -> set[str]:
    """Run one platform's setup and return the entity keys it created."""
    entry = _entry(ES22_DEVICE)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, ES22_DEVICE)
    if reported:
        coordinator.async_set_updated_data(dict(reported))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {ES22_DEVICE["sn"]: coordinator}

    created: list[Any] = []
    await platform_setup(hass, entry, created.extend)
    return {
        entity._definition.key for entity in created if hasattr(entity, "_definition")
    }


class TestStreamAC5000Routing:
    def test_es22_is_its_own_device_type(self) -> None:
        assert get_device_type("", "ES22TEST00000001") == DEVICE_TYPE_STREAM_AC5000

    def test_es22_display_name(self) -> None:
        assert get_device_name("", "ES22TEST00001234") == "STREAM AC 5000 (1234)"


class TestStreamAC5000EntitySet:
    async def test_core_sensors_exist(self, hass: HomeAssistant) -> None:
        keys = await _setup_keys(hass, sensor_setup)

        assert {
            "soc_pct",
            "batt_w",
            "batt_charge_power_w",
            "batt_discharge_power_w",
            "home_w",
            "grid_w",
            "grid_import_power_w",
            "grid_export_power_w",
            "work_mode",
            "max_grid_output_power_w",
        } <= keys

    async def test_accessory_sensors_are_absent_until_reported(
        self, hass: HomeAssistant
    ) -> None:
        keys = await _setup_keys(hass, sensor_setup)

        assert not keys & ACCESSORY_KEYS

    async def test_accessory_sensor_appears_once_the_device_reports_it(
        self, hass: HomeAssistant
    ) -> None:
        keys = await _setup_keys(hass, sensor_setup, reported={"solar_w": 1556.0})

        assert "solar_w" in keys
        # Still nothing for the meter variant this unit does not have.
        assert "grid_phase_a_active_power_w" not in keys

    async def test_binary_sensor(self, hass: HomeAssistant) -> None:
        keys = await _setup_keys(hass, binary_sensor_setup)

        assert keys == {"backup_reserve_enabled", "backup_socket_enabled"}


class TestStreamAC5000Definitions:
    def test_exactly_one_battery_device_class(self) -> None:
        battery = [s for s in STREAMAC5000_SENSORS if s.device_class == "battery"]
        assert [s.key for s in battery] == ["soc_pct"]

    def test_energy_counters_have_a_source_and_a_sensor(self) -> None:
        keys = {s.key for s in STREAMAC5000_SENSORS}
        for power_key, energy_key in STREAMAC5000_POWER_TO_ENERGY.items():
            assert power_key in keys, power_key
            assert energy_key in keys, energy_key

    def test_energy_counters_are_total_increasing(self) -> None:
        for definition in STREAMAC5000_SENSORS:
            if definition.unit == "kWh":
                assert definition.state_class == "total_increasing", definition.key

    def test_an_accessory_energy_counter_follows_its_power_source(self) -> None:
        """solar_energy_kwh is integrated from solar_w, so gating one without
        the other would create a counter that can never move."""
        by_key = {s.key: s for s in STREAMAC5000_SENSORS}
        for power_key, energy_key in STREAMAC5000_POWER_TO_ENERGY.items():
            assert by_key[energy_key].accessory == by_key[power_key].accessory, energy_key
