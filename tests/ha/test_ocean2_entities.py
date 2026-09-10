"""Entity-set and routing tests for the EcoFlow Ocean 2 (RE11/RE17), PLAN-135.

The parser tests (test_ocean2_proto.py) prove the field map against the
captured frames. This file proves the part a parser test cannot: that the
Ocean 2 is its own device type under both serial prefixes, that it gets the
sensor set defined for it with the right entities enabled by default, that
the coordinator actually routes its frames to that parser on both topics,
and that the feed-in limit the display upload carries never became a sensor.
"""

from __future__ import annotations

import json
from pathlib import Path
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
    DEVICE_TYPE_DISPLAY_NAMES,
    DEVICE_TYPE_OCEAN2,
    DOMAIN,
    ENHANCED_ONLY_DEVICE_TYPES,
    MODE_ENHANCED,
    OCEAN2_POWER_TO_ENERGY,
    OCEAN2_SENSORS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.const import (
    get_device_name,
    get_device_type,
)
from custom_components.ecoflow_energy.sensor import _get_sensor_defs
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "ocean2" / "re11_frames_plan135.json"
)

OCEAN2_DEVICE: dict[str, Any] = {
    "sn": "RE11TEST00000001",
    "name": "",
    "product_name": "",
    "device_type": DEVICE_TYPE_OCEAN2,
    "online": 1,
}

_ENABLED_BY_DEFAULT: set[str] = (
    {
        "solar_w",
        "home_w",
        "grid_w",
        "batt_w",
        "batt_charge_power_w",
        "batt_discharge_power_w",
        "grid_import_power_w",
        "grid_export_power_w",
        "soc_pct",
        "bp_remain_watth",
        "pcs_ac_power_w",
        "pcs_ac_freq_hz",
        "solar_energy_kwh",
        "home_energy_kwh",
        "grid_import_energy_kwh",
        "grid_export_energy_kwh",
        "batt_charge_energy_kwh",
        "batt_discharge_energy_kwh",
    }
    | {
        f"grid_phase_{p}_{q}"
        for p in "abc"
        for q in ("voltage_v", "current_a", "active_power_w")
    }
    | {f"mppt_pv{n}_{q}" for n in (1, 2) for q in ("voltage_v", "current_a", "power_w")}
    | {
        f"pack{n}_{q}"
        for n in (1, 2)
        for q in ("soc", "power_w", "soh", "cycles", "remain_watth")
    }
)


def _frame(index: int) -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())["frames"][index]


def _topic(sn: str, frame_topic: str) -> str:
    if frame_topic == "get_reply":
        return f"/app/user123/{sn}/thing/property/get_reply"
    return f"/app/device/property/{sn}"


def _coordinator(
    hass: HomeAssistant, device: dict[str, Any]
) -> EcoFlowDeviceCoordinator:
    entry = MockConfigEntry(
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
    entry.add_to_hass(hass)
    return EcoFlowDeviceCoordinator(hass, entry, device)


class TestOcean2Routing:
    def test_both_prefixes_are_one_type_with_one_name(self) -> None:
        for prefix in ("RE11", "RE17"):
            sn = f"{prefix}TEST00000001"
            assert get_device_type("", sn) == DEVICE_TYPE_OCEAN2, prefix
            assert get_device_name("", sn).startswith("Ocean 2"), prefix
        assert DEVICE_TYPE_DISPLAY_NAMES[DEVICE_TYPE_OCEAN2] == "Ocean 2"

    def test_the_type_is_enhanced_only(self) -> None:
        assert DEVICE_TYPE_OCEAN2 in ENHANCED_ONLY_DEVICE_TYPES
        assert all(sensor.enhanced_only for sensor in OCEAN2_SENSORS)

    def test_the_sensor_list_is_the_ocean2_one(self) -> None:
        assert _get_sensor_defs(DEVICE_TYPE_OCEAN2) is OCEAN2_SENSORS


class TestOcean2EntitySet:
    def test_the_defaults_are_exactly_the_documented_set(self) -> None:
        enabled = {s.key for s in OCEAN2_SENSORS if not s.disabled_by_default}
        assert enabled == _ENABLED_BY_DEFAULT
        assert len(OCEAN2_SENSORS) == len({s.key for s in OCEAN2_SENSORS})

    def test_the_feed_in_limit_is_not_a_sensor(self) -> None:
        """ADR-028 decision 5: the display upload's feed-in ceiling is the
        app's own setting echoed back, never a reading."""
        for sensor in OCEAN2_SENSORS:
            lowered = (sensor.key + " " + sensor.name).lower()
            assert "feed" not in lowered and "ceiling" not in lowered, sensor.key

    def test_every_integrated_energy_key_has_a_power_source_in_the_list(
        self,
    ) -> None:
        keys = {s.key for s in OCEAN2_SENSORS}
        for power_key, energy_key in OCEAN2_POWER_TO_ENERGY.items():
            assert power_key in keys, power_key
            assert energy_key in keys, energy_key

    async def test_a_reporting_device_gets_the_full_sensor_set(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(hass, OCEAN2_DEVICE)
        coordinator.async_set_updated_data({s.key: 1.0 for s in OCEAN2_SENSORS})
        entry = coordinator.config_entry
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            OCEAN2_DEVICE["sn"]: coordinator
        }
        created: list[Any] = []
        await sensor_setup(hass, entry, created.extend)
        keys = {e._definition.key for e in created if hasattr(e, "_definition")}
        assert keys == {s.key for s in OCEAN2_SENSORS}


class TestOcean2Ingest:
    """The coordinator reaches `parse_ocean2_message` for this device type on
    both topics, from the real capture; the parser's own tests prove the
    values, this only proves the routing."""

    async def test_a_get_reply_bundle_lands_the_display_upload(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(hass, OCEAN2_DEVICE)
        frame = _frame(18)
        assert frame["topic"] == "get_reply"
        parsed = coordinator._parse_message(
            _topic(OCEAN2_DEVICE["sn"], frame["topic"]), bytes.fromhex(frame["hex"])
        )
        assert parsed is not None
        assert parsed["solar_w"] == 6687.0
        assert "pcs_ac_power_w" in parsed
        # A get_reply carries the richer module form the parser does not
        # read, so no pack key arrives on this topic.
        assert "pack1_soc" not in parsed

    async def test_a_property_module_bundle_lands_two_packs(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(hass, OCEAN2_DEVICE)
        frame = _frame(1)
        assert frame["topic"] == "property"
        parsed = coordinator._parse_message(
            _topic(OCEAN2_DEVICE["sn"], frame["topic"]), bytes.fromhex(frame["hex"])
        )
        assert parsed is not None
        assert "pack1_soc" in parsed
        assert "pack2_soc" in parsed
        assert "pack3_soc" not in parsed

    async def test_an_incremental_push_lands_only_what_it_carries(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(hass, OCEAN2_DEVICE)
        frame = _frame(0)
        parsed = coordinator._parse_message(
            _topic(OCEAN2_DEVICE["sn"], frame["topic"]), bytes.fromhex(frame["hex"])
        )
        assert parsed is not None
        assert parsed["home_w"] == 220.0
        assert "solar_w" not in parsed
