"""Entity-set tests for the EcoFlow PowerPulse 2 (C376) wallbox (PLAN-132).

The parser tests (test_powerpulse_proto.py) prove the field map against the
captured session. This file proves the part a parser test cannot cover: that
the PowerPulse 2 device type gets its own sensor and binary sensor set, that
the four keys migrated from POWEROCEAN_SENSORS left it, and that the one
sensor with a Home Assistant-typed value (`ev_session_start_ts`, device class
`timestamp`) converts the raw Unix-seconds wire value correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
    DEVICE_TYPE_POWERPULSE2,
    DOMAIN,
    MODE_ENHANCED,
    POWEROCEAN_SENSORS,
    POWERPULSE2_BINARY_SENSORS,
    POWERPULSE2_SENSORS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.const import (
    get_device_type,
)
from custom_components.ecoflow_energy.sensor import EcoFlowSensor
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

POWERPULSE2_DEVICE: dict[str, Any] = {
    "sn": "C376TEST00000007",
    "name": "",
    "product_name": "",
    "device_type": DEVICE_TYPE_POWERPULSE2,
    "online": 1,
}

# One reading per POWERPULSE2_SENSORS key, so a coordinator seeded with this
# dict has every sensor reporting - the accessory binary sensor's own report
# is added separately per test, since it is the one entity gated behind
# `reading_reported`.
_ALL_READINGS: dict[str, Any] = {
    "ev_charge_power_w": 7360.0,
    "ev_voltage_l1_v": 231.4,
    "ev_voltage_l2_v": 230.9,
    "ev_voltage_l3_v": 231.1,
    "ev_current_l1_a": 16.0,
    "ev_current_l2_a": 16.0,
    "ev_current_l3_a": 16.0,
    "ev_max_current_a": 16.0,
    "ev_phase_mode": "three_phase",
    "ev_session_status": "charging",
    "ev_session_duration_s": 1800,
    "ev_charge_status": "charging",
    "ev_session_start_ts": 1757320000,
    "ev_session_energy_wh": 3200,
    "ev_session_start_energy_wh": 101480,
    "ev_total_energy_wh": 104680,
}


def _entry(device: dict[str, Any]) -> MockConfigEntry:
    """Build an Enhanced-mode entry for one device."""
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


async def _setup_entities(
    hass: HomeAssistant,
    platform_setup,
    device: dict[str, Any],
    reported: dict[str, Any] | None = None,
) -> list[Any]:
    """Run one platform's setup and return the definition-driven entities."""
    entry = _entry(device)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    if reported:
        coordinator.async_set_updated_data(dict(reported))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {device["sn"]: coordinator}

    created: list[Any] = []
    await platform_setup(hass, entry, created.extend)
    return [entity for entity in created if hasattr(entity, "_definition")]


class TestPowerpulse2Routing:
    def test_c376_is_its_own_device_type(self) -> None:
        assert get_device_type("", "C376TEST00000007") == DEVICE_TYPE_POWERPULSE2


class TestPowerpulse2EntitySet:
    async def test_a_reporting_device_gets_the_full_sensor_and_binary_sensor_set(
        self, hass: HomeAssistant
    ) -> None:
        reported = dict(_ALL_READINGS)
        reported["ev_cable_lock_enabled"] = True

        entities = await _setup_entities(
            hass, sensor_setup, POWERPULSE2_DEVICE, reported
        )
        keys = {entity._definition.key for entity in entities}
        assert keys == {sensor.key for sensor in POWERPULSE2_SENSORS}

        binary_entities = await _setup_entities(
            hass, binary_sensor_setup, POWERPULSE2_DEVICE, reported
        )
        binary_keys = {entity._definition.key for entity in binary_entities}
        assert binary_keys == {sensor.key for sensor in POWERPULSE2_BINARY_SENSORS}

    async def test_the_cable_lock_is_absent_from_a_property_only_session(
        self, hass: HomeAssistant
    ) -> None:
        """`ev_cable_lock_enabled` only ever arrives inside a get_reply
        bundle (ParamReport, 2/34); a session that only sees the property
        push never reports it, so the entity must not be created yet."""
        binary_entities = await _setup_entities(
            hass, binary_sensor_setup, POWERPULSE2_DEVICE, dict(_ALL_READINGS)
        )
        assert binary_entities == []

    async def test_no_migrated_key_is_gated_behind_accessory_anymore(self) -> None:
        """The four keys that moved from POWEROCEAN_SENSORS were an
        accessory reading there (the wallbox might not be attached to the
        PowerOcean at all). On its own device type every unit has these
        readings, so none of them should wait for a first report."""
        migrated = {
            "ev_charge_power_w",
            "ev_session_energy_wh",
            "ev_session_duration_s",
            "ev_charge_status",
        }
        for definition in POWERPULSE2_SENSORS:
            if definition.key in migrated:
                assert definition.accessory is False, definition.key

    async def test_the_cable_lock_binary_sensor_still_gates_on_accessory(self) -> None:
        cable_lock = next(
            d for d in POWERPULSE2_BINARY_SENSORS if d.key == "ev_cable_lock_enabled"
        )
        assert cable_lock.accessory is True


class TestTheFourKeysLiveOnBothLists:
    """The four wallbox keys are defined twice on purpose, once per wallbox.

    The PowerPulse 2 reads them off its own channel; the PowerPulse 1
    (`AC31`) still reports them through the PowerOcean on `(209, 8)`, a path
    PLAN-132 does not touch. Removing them from `POWEROCEAN_SENSORS` when the
    `(241, 3)` relay was retired would have left every `AC31` owner without a
    single wallbox entity, with nothing in the tree going red - the keys keep
    being filled, they just would have had no definition to render.

    A unique id is `<serial>_<key>`, so one key on two device types is two
    entities on two devices, not a collision.
    """

    def test_the_four_keys_are_defined_on_both_lists(self) -> None:
        shared = {
            "ev_charge_power_w",
            "ev_session_energy_wh",
            "ev_session_duration_s",
            "ev_charge_status",
        }
        assert shared <= {d.key for d in POWEROCEAN_SENSORS}
        assert shared <= {d.key for d in POWERPULSE2_SENSORS}

    def test_the_vehicle_stays_a_powerocean_reading(self) -> None:
        """The PowerPulse 2's own channel carries no vehicle identity at all,
        so `ev_vehicle_id` belongs to the PowerPulse 1 alone."""
        assert "ev_vehicle_id" in {d.key for d in POWEROCEAN_SENSORS}
        assert "ev_vehicle_id" not in {d.key for d in POWERPULSE2_SENSORS}

    def test_the_migrated_keys_landed_on_powerpulse2_instead(self) -> None:
        powerpulse2_keys = {d.key for d in POWERPULSE2_SENSORS}
        migrated = {
            "ev_charge_power_w",
            "ev_session_energy_wh",
            "ev_session_duration_s",
            "ev_charge_status",
        }
        assert migrated <= powerpulse2_keys


class TestSessionStartTimestamp:
    """`ev_session_start_ts` is Unix seconds on the wire but a Home Assistant
    `timestamp` sensor requires a timezone-aware datetime; the platform layer
    converts it (sensor.py native_value), since the parser is not touched by
    this phase."""

    def _entity(self, hass: HomeAssistant, value: Any) -> EcoFlowSensor:
        entry = _entry(POWERPULSE2_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, POWERPULSE2_DEVICE)
        coordinator.async_set_updated_data({"ev_session_start_ts": value})
        definition = next(
            d for d in POWERPULSE2_SENSORS if d.key == "ev_session_start_ts"
        )
        return EcoFlowSensor(coordinator, definition)

    async def test_the_raw_epoch_seconds_become_a_utc_datetime(
        self, hass: HomeAssistant
    ) -> None:
        entity = self._entity(hass, 1757320000)
        assert entity.native_value == datetime(2025, 9, 8, 8, 26, 40, tzinfo=UTC)

    async def test_a_missing_reading_falls_back_to_none(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(POWERPULSE2_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, POWERPULSE2_DEVICE)
        coordinator.async_set_updated_data({})
        definition = next(
            d for d in POWERPULSE2_SENSORS if d.key == "ev_session_start_ts"
        )
        entity = EcoFlowSensor(coordinator, definition)
        assert entity.native_value is None


class TestEnergyStateClasses:
    """ADR / house rule: a counter that can fall must not be
    `total_increasing`, or the recorder reads the drop as a meter reset."""

    def test_the_lifetime_counter_is_total_increasing(self) -> None:
        definition = next(
            d for d in POWERPULSE2_SENSORS if d.key == "ev_total_energy_wh"
        )
        assert definition.state_class == "total_increasing"

    def test_the_session_start_snapshot_is_not_total_increasing(self) -> None:
        """Jumps to the then-current lifetime counter on every new session,
        so it is not monotonic across the device's life."""
        definition = next(
            d for d in POWERPULSE2_SENSORS if d.key == "ev_session_start_energy_wh"
        )
        assert definition.state_class != "total_increasing"

    def test_the_session_energy_counter_is_still_not_total_increasing(self) -> None:
        """Pre-existing entity (unchanged by this phase): falls back to a
        small value on every new session."""
        definition = next(
            d for d in POWERPULSE2_SENSORS if d.key == "ev_session_energy_wh"
        )
        assert definition.state_class != "total_increasing"
