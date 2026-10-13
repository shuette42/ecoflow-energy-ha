"""Entity gating for the Ocean 2's three EMS status sensors (#510).

Work mode, system state and control mode come from field 3 of the full
snapshot only (the change-only pushes never carry it), so they are accessories:
created on the first report that carries the key, never up front. Their keys
carry an `ocean2_` prefix because `ems_work_mode` and `ems_work_state` already
name the PowerOcean sensors, which have other state tables and other labels.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy import const as const_module
from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_OCEAN2,
    DOMAIN,
    MODE_ENHANCED,
    OCEAN2_SENSORS,
    EcoFlowSensorDef,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.parsers.ocean2_proto import (
    OCEAN2_CONTROL_MODES,
    OCEAN2_WORK_MODES,
    OCEAN2_WORK_STATES,
)
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

from .conftest import add_entities_collector

OCEAN2_DEVICE: dict[str, Any] = {
    "sn": "RE11TEST00000001",
    "name": "Ocean 2",
    "product_name": "Ocean 2",
    "device_type": DEVICE_TYPE_OCEAN2,
    "online": 1,
}

EMS_TABLES = {
    "ocean2_ems_work_mode": OCEAN2_WORK_MODES,
    "ocean2_ems_work_state": OCEAN2_WORK_STATES,
    "ocean2_ems_control_mode": OCEAN2_CONTROL_MODES,
}
EMS_KEYS = set(EMS_TABLES)

# A push: readings, no field 3.
PUSH_REPORT: dict[str, Any] = {"soc_pct": 62.0, "home_w": 410.0}

# The full snapshot, with the explicit "none" the device sends at rest.
SNAPSHOT_REPORT: dict[str, Any] = {
    "soc_pct": 62.0,
    "ocean2_ems_work_mode": "self_use",
    "ocean2_ems_work_state": "running",
    "ocean2_ems_control_mode": "none",
}


def _entry() -> MockConfigEntry:
    """Build an account sign-in entry for one Ocean 2 (Enhanced Mode only)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [OCEAN2_DEVICE],
        },
        unique_id="test@example.com",
    )


async def _setup(
    hass: HomeAssistant, device_data: dict[str, Any]
) -> tuple[EcoFlowDeviceCoordinator, list[Any]]:
    """Run the sensor platform setup and return coordinator plus entities."""
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, OCEAN2_DEVICE)
    for key, value in device_data.items():
        coordinator.set_device_value(key, value)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        OCEAN2_DEVICE["sn"]: coordinator
    }

    created: list[Any] = []
    await sensor_setup(hass, entry, add_entities_collector(created))
    return coordinator, created


def _keys(entities: list[Any]) -> set[str]:
    return {
        entity._definition.key for entity in entities if hasattr(entity, "_definition")
    }


class TestDefinitions:
    def test_the_three_sensors_are_diagnostic_enum_accessories(self) -> None:
        by_key = {sensor.key: sensor for sensor in OCEAN2_SENSORS}

        for key in EMS_KEYS:
            sensor = by_key[key]
            assert sensor.device_class == "enum", key
            assert sensor.entity_category == "diagnostic", key
            assert sensor.accessory, key
            assert not sensor.disabled_by_default, key

    def test_the_options_are_the_parsers_state_names_in_wire_order(self) -> None:
        by_key = {sensor.key: sensor for sensor in OCEAN2_SENSORS}

        for key, table in EMS_TABLES.items():
            assert by_key[key].options == [table[wire] for wire in sorted(table)], key

    def test_the_keys_do_not_reuse_another_devices(self) -> None:
        # `ems_work_mode` / `ems_work_state` are PowerOcean sensors with other
        # state tables; sharing a key would share a translation and its labels.
        # Every other sensor list is checked, not only the PowerOcean's, and
        # the control is that the PowerOcean names really are in that set.
        others = {
            definition.key
            for name, value in vars(const_module).items()
            if name != "OCEAN2_SENSORS" and isinstance(value, list)
            for definition in value
            if isinstance(definition, EcoFlowSensorDef)
        }

        # Found by their option tables, not by the expected key names, so a
        # definition renamed to a PowerOcean key is seen under its new name.
        tables = [list(table.values()) for table in EMS_TABLES.values()]
        ours = [sensor for sensor in OCEAN2_SENSORS if sensor.options in tables]

        assert {"ems_work_mode", "ems_work_state"} <= others
        assert len(ours) == len(EMS_TABLES)
        assert not {sensor.key for sensor in ours} & others


class TestGating:
    async def test_a_push_without_field_3_creates_none_of_them(
        self, hass: HomeAssistant
    ) -> None:
        _, created = await _setup(hass, PUSH_REPORT)

        assert not _keys(created) & EMS_KEYS

    async def test_a_snapshot_creates_all_three(self, hass: HomeAssistant) -> None:
        _, created = await _setup(hass, SNAPSHOT_REPORT)

        assert _keys(created) & EMS_KEYS == EMS_KEYS

    async def test_one_reported_key_creates_only_that_sensor(
        self, hass: HomeAssistant
    ) -> None:
        _, created = await _setup(
            hass, {"soc_pct": 62.0, "ocean2_ems_control_mode": "none"}
        )

        assert _keys(created) & EMS_KEYS == {"ocean2_ems_control_mode"}
