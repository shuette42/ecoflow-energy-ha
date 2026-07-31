"""Entity-set tests for the Stream Micro (BK01).

The Stream Micro shares the Stream device type, parser and entity lists with
the rest of the BK series, but it is a grid-tie PV inverter without a battery
or AC outlets. These tests pin the reduced entity set down at the platform
setup level, because Home Assistant keeps an entity in the registry after a
later release stops creating it - a wrongly created entity is permanent for
that owner.
"""

from __future__ import annotations

from typing import Any

import pytest
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
    DEVICE_TYPE_STREAM,
    DOMAIN,
    MODE_ENHANCED,
    STREAM_MICRO_EXCLUDED_KEYS,
    excluded_keys_for_serial,
    filter_defs_for_serial,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.const import (
    get_device_name,
    get_device_type,
)
from custom_components.ecoflow_energy.number import async_setup_entry as number_setup
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

BK01_DEVICE: dict[str, Any] = {
    "sn": "BK01TEST00000001",
    "name": "",
    "product_name": "",
    "device_type": DEVICE_TYPE_STREAM,
    "online": 1,
}

BK31_DEVICE: dict[str, Any] = {
    "sn": "BK31TEST00000001",
    "name": "Stream AC Pro",
    "product_name": "Stream AC Pro",
    "device_type": DEVICE_TYPE_STREAM,
    "online": 1,
}


def _entry(device: dict[str, Any]) -> MockConfigEntry:
    """Build an Enhanced-mode entry for one Stream device."""
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
    hass: HomeAssistant, device: dict[str, Any], platform_setup
) -> set[str]:
    """Run one platform's setup and return the entity keys it created."""
    entry = _entry(device)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {device["sn"]: coordinator}

    created: list[Any] = []
    await platform_setup(hass, entry, created.extend)
    return {
        entity._definition.key
        for entity in created
        if hasattr(entity, "_definition")
    }


class TestStreamMicroRouting:
    def test_bk01_is_a_stream_device(self) -> None:
        """The app reports an empty product name for BK devices, so the type
        can only come from the serial prefix."""
        assert get_device_type("", "BK01TEST00000001") == DEVICE_TYPE_STREAM

    def test_bk01_display_name(self) -> None:
        assert get_device_name("", "BK01TESTAAAAAAAA") == "Stream Micro"

    def test_bk01_display_name_with_numeric_serial_tail(self) -> None:
        assert get_device_name("", "BK01TEST00001234") == "Stream Micro (1234)"

    def test_a_reported_product_name_still_wins(self) -> None:
        assert get_device_name("Stream Micro X", "BK01TEST00000001") == "Stream Micro X"


class TestPrefixFilterHelper:
    def test_bk01_has_an_exclusion_set(self) -> None:
        assert excluded_keys_for_serial("BK01TEST00000001") is STREAM_MICRO_EXCLUDED_KEYS

    def test_other_stream_prefixes_are_unfiltered(self) -> None:
        assert excluded_keys_for_serial("BK31TEST00000001") == frozenset()

    def test_unknown_and_empty_serials_are_unfiltered(self) -> None:
        assert excluded_keys_for_serial("") == frozenset()
        assert excluded_keys_for_serial("ZZZZ000000000000") == frozenset()

    def test_filter_matches_a_number_state_key_too(self) -> None:
        """The backup reserve number is keyed `backup_reserve` but reads
        `backup_reserve_pct`; both must be caught."""
        from custom_components.ecoflow_energy.const import STREAM_NUMBERS

        assert filter_defs_for_serial(STREAM_NUMBERS, "BK01TEST00000001") == []
        assert filter_defs_for_serial(STREAM_NUMBERS, "BK31TEST00000001") == list(
            STREAM_NUMBERS
        )


class TestStreamMicroEntitySet:
    async def test_no_battery_sensors(self, hass: HomeAssistant) -> None:
        keys = await _setup_keys(hass, BK01_DEVICE, sensor_setup)

        assert not keys & STREAM_MICRO_EXCLUDED_KEYS
        assert not {key for key in keys if key.startswith("batt_")}
        assert "soc_pct" not in keys
        assert "bms_soh_pct" not in keys
        assert "backup_reserve_pct" not in keys

    async def test_pv_and_grid_sensors_are_present(self, hass: HomeAssistant) -> None:
        keys = await _setup_keys(hass, BK01_DEVICE, sensor_setup)

        assert {
            "pv1_w",
            "pv2_w",
            "pv_voltage_v",
            "pv_current_a",
            "pv2_voltage_v",
            "pv2_current_a",
            "ac_voltage_v",
            "ac_current_a",
            "ac_frequency_hz",
            "grid_connection_power_w",
            "grid_connection_state",
            "feed_grid_power_limit_w",
            "wifi_rssi_dbm",
        } <= keys

    async def test_meter_dependent_solar_total_is_dropped(
        self, hass: HomeAssistant
    ) -> None:
        """PV is reported per string here, not through the system solar path.

        Keeping the meter-dependent total would add a lifetime energy counter
        that can never move off zero on this unit.
        """
        keys = await _setup_keys(hass, BK01_DEVICE, sensor_setup)

        assert {"pv1_w", "pv2_w"} <= keys
        assert "solar_w" not in keys
        assert "solar_energy_kwh" not in keys

    async def test_no_outlet_binary_sensors(self, hass: HomeAssistant) -> None:
        assert await _setup_keys(hass, BK01_DEVICE, binary_sensor_setup) == set()

    async def test_no_backup_reserve_number(self, hass: HomeAssistant) -> None:
        assert await _setup_keys(hass, BK01_DEVICE, number_setup) == set()


class TestStreamAcProKeepsItsEntities:
    """The correction must not shrink the entity set of the device family it
    was derived from."""

    async def test_battery_sensors_still_created(self, hass: HomeAssistant) -> None:
        keys = await _setup_keys(hass, BK31_DEVICE, sensor_setup)

        assert {
            "soc_pct",
            "batt_w",
            "batt_charge_power_w",
            "batt_discharge_power_w",
            "batt_charge_energy_kwh",
            "batt_discharge_energy_kwh",
            "bms_soh_pct",
            "backup_reserve_pct",
        } <= keys

    async def test_new_sensors_reach_the_whole_family(
        self, hass: HomeAssistant
    ) -> None:
        keys = await _setup_keys(hass, BK31_DEVICE, sensor_setup)

        assert {"ac_current_a", "wifi_rssi_dbm", "grid_connection_state"} <= keys

    async def test_solar_total_is_only_dropped_for_the_micro(
        self, hass: HomeAssistant
    ) -> None:
        keys = await _setup_keys(hass, BK31_DEVICE, sensor_setup)

        assert {"solar_w", "solar_energy_kwh"} <= keys

    async def test_outlet_binary_sensors_still_created(
        self, hass: HomeAssistant
    ) -> None:
        keys = await _setup_keys(hass, BK31_DEVICE, binary_sensor_setup)

        assert keys == {"ac_outlet_1_enabled", "ac_outlet_2_enabled"}

    async def test_backup_reserve_number_still_created(
        self, hass: HomeAssistant
    ) -> None:
        keys = await _setup_keys(hass, BK31_DEVICE, number_setup)

        assert keys == {"backup_reserve"}


@pytest.mark.parametrize("key", sorted(STREAM_MICRO_EXCLUDED_KEYS))
def test_every_excluded_key_exists_somewhere(key: str) -> None:
    """An exclusion that matches no definition is dead weight and hides a
    typo: the entity it was meant to suppress would still be created."""
    from custom_components.ecoflow_energy.const import (
        STREAM_BINARY_SENSORS,
        STREAM_NUMBERS,
        STREAM_SENSORS,
    )

    defined = {definition.key for definition in STREAM_SENSORS}
    defined |= {definition.key for definition in STREAM_BINARY_SENSORS}
    defined |= {definition.key for definition in STREAM_NUMBERS}
    defined |= {definition.state_key for definition in STREAM_NUMBERS}

    assert key in defined
