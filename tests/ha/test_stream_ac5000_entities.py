"""Entity-set tests for the STREAM AC 5000 (ES22).

Whether this device has PV or a per-phase smart meter is a wiring choice,
not a model difference, so those entities are accessory-gated rather than
listed per serial prefix: they appear once the device actually reports the
reading. Home Assistant keeps an entity in the registry after a later
release stops creating it, so an entity created on a unit that will never
fill it is permanent for that owner.
"""

from __future__ import annotations

import json
from pathlib import Path
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
    STREAMAC5000_NUMBERS,
    STREAMAC5000_POWER_TO_ENERGY,
    STREAMAC5000_SELECTS,
    STREAMAC5000_SENSORS,
    STREAMAC5000_SWITCHES,
    filter_defs_for_serial,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.const import (
    get_device_name,
    get_device_type,
)
from custom_components.ecoflow_energy.number import async_setup_entry as number_setup
from custom_components.ecoflow_energy.select import async_setup_entry as select_setup
from custom_components.ecoflow_energy.sensor import (
    _get_sensor_defs,
    _reported,
    async_setup_entry as sensor_setup,
)
from custom_components.ecoflow_energy.switch import async_setup_entry as switch_setup

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
    "pv_total_w",
    "pv1_w",
    "pv2_w",
    "pv3_w",
    "pv4_w",
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

    async def test_controls(self, hass: HomeAssistant) -> None:
        assert await _setup_keys(hass, number_setup) == {
            "scheduled_discharge_power_w",
            "scheduled_charge_power_w",
            "max_charge_soc_pct",
            "min_discharge_soc_pct",
            "backup_reserve",
        }
        assert await _setup_keys(hass, select_setup) == {"work_mode"}

    async def test_switches(self, hass: HomeAssistant) -> None:
        """Both were captured from the app, so both are writable.

        Enhanced Mode only, like every other control here, which is what this
        entry is.
        """
        assert await _setup_keys(hass, switch_setup) == {
            "backup_reserve_switch",
            "backup_socket_switch",
        }


class TestStreamAC5000TaskReadback:
    """A setpoint number must not outlive the task it reports."""

    async def test_a_deleted_task_clears_its_setpoint(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(ES22_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, ES22_DEVICE)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            ES22_DEVICE["sn"]: coordinator
        }
        created: list[Any] = []
        await number_setup(hass, entry, created.extend)
        number = next(
            entity
            for entity in created
            if entity._definition.key == "scheduled_discharge_power_w"
        )
        # A value the entity would otherwise fall back to, which is what the
        # restore path leaves behind after a reload.
        number._restored_value = 1200.0

        coordinator.async_set_updated_data({"scheduled_discharge_power_w": 1200})
        assert number.native_value == 1200

        coordinator.async_set_updated_data({"scheduled_discharge_power_w": None})
        assert number.native_value is None


class TestStreamAC5000Definitions:
    def test_documented_totals(self) -> None:
        """The counts the README and the entity reference publish."""
        assert len(STREAMAC5000_SENSORS) == 56
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

    def test_every_control_is_enhanced_only(self) -> None:
        """This device has no Developer API at all, so with developer keys a
        control would be created that can neither write nor read back."""
        for definition in (
            *STREAMAC5000_NUMBERS,
            *STREAMAC5000_SWITCHES,
            *STREAMAC5000_SELECTS,
        ):
            assert definition.enhanced_only is True, definition.key

    def test_a_control_is_named_after_the_reading_it_writes(self) -> None:
        """One value, one wording.

        Home Assistant gives a number no description field, so its name is the
        only text an owner reads while dragging the slider. A control and the
        sensor reporting the same value under a different name reads as two
        settings.
        """
        translations = json.loads(
            (
                Path(__file__).parents[2]
                / "custom_components/ecoflow_energy/translations/en.json"
            ).read_text(encoding="utf-8")
        )["entity"]
        sensor_keys = {s.key for s in STREAMAC5000_SENSORS}
        for definition in STREAMAC5000_NUMBERS:
            if definition.state_key not in sensor_keys:
                continue
            assert (
                translations["number"][definition.key]["name"]
                == translations["sensor"][definition.state_key]["name"]
            ), definition.key


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
