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
    DEVICE_TYPE_STREAM,
    DEVICE_TYPE_STREAM_AC5000,
    DOMAIN,
    MODE_ENHANCED,
    STREAM_MICRO_EXCLUDED_KEYS,
    STREAMAC5000_POWER_TO_ENERGY,
    STREAMAC5000_SENSORS,
    filter_defs_for_serial,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.const import (
    get_device_name,
    get_device_type,
)
from custom_components.ecoflow_energy.sensor import (
    _get_sensor_defs,
    _reported,
    async_setup_entry as sensor_setup,
)

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
    def test_documented_totals(self) -> None:
        """The counts the README and the entity reference publish."""
        assert len(STREAMAC5000_SENSORS) == 50
        assert len(STREAMAC5000_POWER_TO_ENERGY) == 5

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
        """An energy counter is integrated from its power reading, so gating
        one without the other would create a counter that can never move."""
        by_key = {s.key: s for s in STREAMAC5000_SENSORS}
        for power_key, energy_key in STREAMAC5000_POWER_TO_ENERGY.items():
            assert by_key[energy_key].accessory == by_key[power_key].accessory, energy_key

    def test_a_zero_does_not_announce_an_accessory(self) -> None:
        """The gate itself, not just the flag that switches it on."""

        class _Stub:
            device_data = {"solar_w": 0.0, "grid_phase_a_active_power_w": 0.0}
            data: dict[str, Any] = {}

        stub = _Stub()
        assert _reported(stub, "solar_w") is True
        assert _reported(stub, "solar_w", needs_nonzero=True) is False
        stub.device_data["solar_w"] = 1556.0
        assert _reported(stub, "solar_w", needs_nonzero=True) is True
        # A key without the flag is unaffected by its own zero.
        assert _reported(stub, "grid_phase_a_active_power_w") is True
        assert _reported(stub, "missing_key", needs_nonzero=True) is False

    def test_the_solar_reading_waits_for_a_non_zero_value(self) -> None:
        """It is published as a zero, so presence alone cannot gate it.

        The node total is zero-filled to stop the reading latching at its
        last daylight value overnight. That fill would otherwise announce
        solar on the first frame of every unit, so the entity waits for a
        reading that is actually solar.
        """
        by_key = {d.key: d for d in STREAMAC5000_SENSORS}
        solar = by_key["solar_w"]
        assert solar.accessory is True
        assert solar.accessory_needs_nonzero is True

    def test_only_solar_needs_a_non_zero_reading(self) -> None:
        """A meter phase may legitimately sit at zero and must not wait."""
        for definition in STREAMAC5000_SENSORS:
            if definition.key == "solar_w":
                continue
            assert definition.accessory_needs_nonzero is False, definition.key

    def test_the_solar_reading_has_no_lifetime_counter(self) -> None:
        """The reading stays, the counter does not.

        This device works its solar figure out from the house flows and
        reports one on a unit with no PV wired to it at all, as frames 25 and
        26 of the push capture show. A total_increasing counter only ever
        counts up and cannot be corrected afterwards, so integrating an
        inferred figure would credit the Energy Dashboard with production
        that never happened. `solar_w` carries the same information and can
        simply be ignored.
        """
        keys = {s.key for s in STREAMAC5000_SENSORS}
        assert "solar_w" in keys
        assert "solar_energy_kwh" not in keys
        assert "solar_w" not in STREAMAC5000_POWER_TO_ENERGY


class TestBKSeriesSetIsUntouched:
    """The BK-series entity set must not move because of this device.

    An ES22 gets its own list precisely so the BK series keeps the set it
    was released with. The failure this guards against is subtractive: an
    ES22-only key added to `STREAM_SENSORS` instead, then excluded again per
    prefix, would create entities on every Stream in the field that can never
    fill, and Home Assistant keeps an entity once it exists. The Stream Micro
    (BK01) is the one prefix that legitimately gets less, and it gets it
    through `filter_defs_for_serial`, not through a shorter list.
    """

    def test_the_stream_list_keeps_its_size(self) -> None:
        assert len(_get_sensor_defs(DEVICE_TYPE_STREAM)) == 54

    def test_bk01_still_gets_the_micro_reduced_set(self) -> None:
        defs = _get_sensor_defs(DEVICE_TYPE_STREAM)
        micro = filter_defs_for_serial(defs, "BK01TEST00000001")

        assert len(micro) == 21
        assert not {d.key for d in micro} & STREAM_MICRO_EXCLUDED_KEYS

    def test_every_other_stream_prefix_gets_the_whole_set(self) -> None:
        defs = _get_sensor_defs(DEVICE_TYPE_STREAM)

        assert filter_defs_for_serial(defs, "BK31TEST00000001") == list(defs)
