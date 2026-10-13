"""Entity gating for the Ocean 2's three EMS status sensors (#510).

Work mode, system state and control mode come from field 3 of the full
snapshot (the change-only pushes carry it only rarely), so they are
accessories: created on the first report that carries the key, never up front.
Work mode and system state use the PowerOcean's sensor keys, state names and
translations, because the states mean the same; the control mode is this
device's alone.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
    POWEROCEAN_SENSORS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.parsers.ocean2_proto import (
    OCEAN2_CONTROL_MODES,
    OCEAN2_WORK_MODES,
)
from custom_components.ecoflow_energy.ecoflow.parsers.powerocean import (
    EMS_WORK_STATES,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
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
    "ems_work_mode": OCEAN2_WORK_MODES,
    "ems_work_state": EMS_WORK_STATES,
    "ems_control_mode": OCEAN2_CONTROL_MODES,
}
EMS_KEYS = set(EMS_TABLES)

# A push: readings, no field 3.
PUSH_REPORT: dict[str, Any] = {"soc_pct": 62.0, "home_w": 410.0}

# The full snapshot, with the explicit "none" the device sends at rest.
SNAPSHOT_REPORT: dict[str, Any] = {
    "soc_pct": 62.0,
    "ems_work_mode": "self_use",
    "ems_work_state": "running",
    "ems_control_mode": "none",
}

# The field-3 record of a get_reply full upload from an RE11 (tag and length
# included), the same bytes as `_EMS_RECORD` in tests/test_ocean2_proto_parser.py:
# work mode 1, state 7, and an explicit 3.10 = 0 among seventeen sub-fields.
EMS_RECORD = bytes.fromhex(
    "1a250800100118072000286430023800400148005000583260016810700178008001810488010e"
)


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


def _telemetry_frame(pdata: bytes) -> bytes:
    """A `254/39` telemetry frame around `pdata`, as the MQTT client delivers it."""
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 254)
        + encode_field_varint(9, 39)
    )
    return encode_field_bytes(1, header)


async def _setup(
    hass: HomeAssistant,
    device_data: dict[str, Any],
    frame: bytes | None = None,
) -> tuple[EcoFlowDeviceCoordinator, list[Any]]:
    """Run the sensor platform setup and return coordinator plus entities.

    `device_data` is written straight into the store; `frame`, when given,
    goes through the coordinator's own ingest first, like a real report.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, OCEAN2_DEVICE)
    for key, value in device_data.items():
        coordinator.set_device_value(key, value)
    if frame is not None:
        coordinator._on_mqtt_message(
            f"/app/device/property/{coordinator.device_sn}", frame
        )
        await hass.async_block_till_done()
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

    def test_the_work_state_options_are_the_powerocean_state_names(self) -> None:
        # One distinction, one registration: the Ocean 2's system state is the
        # PowerOcean's `ems_work_state` enum, so it shows the same states under
        # the same translations. A renamed or re-labelled copy would give a user
        # with both devices two vocabularies for one enum.
        ocean2 = {sensor.key: sensor for sensor in OCEAN2_SENSORS}["ems_work_state"]
        powerocean = {sensor.key: sensor for sensor in POWEROCEAN_SENSORS}[
            "ems_work_state"
        ]

        assert ocean2.options == list(EMS_WORK_STATES.values())
        assert ocean2.options == powerocean.options

    def test_the_work_mode_options_are_states_the_powerocean_already_names(
        self,
    ) -> None:
        # The wire numbers differ (the PowerOcean sends 0 and 12), the names do
        # not: the Ocean 2 offers a subset of the PowerOcean's `ems_work_mode`
        # states, so the shared translation block covers every one of them.
        ocean2 = {sensor.key: sensor for sensor in OCEAN2_SENSORS}["ems_work_mode"]
        powerocean = {sensor.key: sensor for sensor in POWEROCEAN_SENSORS}[
            "ems_work_mode"
        ]

        assert ocean2.options
        assert powerocean.options
        assert set(ocean2.options) <= set(powerocean.options)


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
        _, created = await _setup(hass, {"soc_pct": 62.0, "ems_control_mode": "none"})

        assert _keys(created) & EMS_KEYS == {"ems_control_mode"}


class TestRenderedStates:
    async def test_the_real_field_3_record_renders_running_self_use_none(
        self, hass: HomeAssistant
    ) -> None:
        # The recorded field-3 bytes go in through the coordinator's own
        # ingest, not into the store, so the whole path is exercised: frame,
        # parser, merge into the device data, entity creation, rendered state.
        _, created = await _setup(
            hass, {"soc_pct": 62.0}, frame=_telemetry_frame(EMS_RECORD)
        )

        by_key = {
            entity._definition.key: entity
            for entity in created
            if hasattr(entity, "_definition") and entity._definition.key in EMS_KEYS
        }
        assert set(by_key) == EMS_KEYS
        # `native_value` is what the entity hands to the state machine; the
        # entity is not attached to a platform here, so `.state` is out of reach.
        assert by_key["ems_work_state"].native_value == "running"
        assert by_key["ems_work_mode"].native_value == "self_use"
        assert by_key["ems_control_mode"].native_value == "none"
        # Each value is one of the options the entity advertises, which is what
        # makes the state machine accept it for an enum sensor.
        for key, entity in by_key.items():
            assert entity.native_value in (entity.options or []), key
