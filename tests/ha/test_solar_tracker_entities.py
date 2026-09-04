"""Entity-set tests for the EcoFlow Solar Tracker (HZ31 / S02F).

The parser tests prove the field map against the reporter's capture and the
diagnostics frames. These run one frame per prefix through the coordinator,
because the wiring this phase adds - the prefix map, the two dispatch
branches, the sensor block - is exactly the part a parser test cannot cover
on its own: a solar tracker frame that lands on the wrong device type, or a
dispatch branch missing at one of the two ingest sites, produces no
readings at all even though the parser itself is correct.
"""

from __future__ import annotations

import binascii
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
    DEVICE_TYPE_SOLAR_TRACKER,
    DOMAIN,
    MODE_ENHANCED,
    SOLARTRACKER_SENSORS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.const import (
    get_device_name,
    get_device_type,
)
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

CAPTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "solar_tracker"
    / "hz31_s02f_frames_issue339.json"
)

# One frame per prefix, taken straight from the diagnostics `raw_capture`
# (fixture tags "S02F" and "HZ31"), so both account serial prefixes are
# proven against an independently sourced frame, not just the reporter's
# own T1/T2 dataset.
S02F_FRAME_INDEX = 7
HZ31_FRAME_INDEX = 8

HZ31_DEVICE: dict[str, Any] = {
    "sn": "HZ31TEST00000001",
    "name": "",
    "product_name": "",
    "device_type": DEVICE_TYPE_SOLAR_TRACKER,
    "online": 1,
}

S02F_DEVICE: dict[str, Any] = {
    "sn": "S02FTEST00000001",
    "name": "",
    "product_name": "",
    "device_type": DEVICE_TYPE_SOLAR_TRACKER,
    "online": 1,
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
    device: dict[str, Any],
    reported: dict[str, Any] | None = None,
) -> list[Any]:
    """Run the sensor platform's setup and return the definition-driven entities."""
    entry = _entry(device)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    if reported:
        coordinator.async_set_updated_data(dict(reported))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {device["sn"]: coordinator}

    created: list[Any] = []
    await sensor_setup(hass, entry, created.extend)
    return [entity for entity in created if hasattr(entity, "_definition")]


def _frame(index: int) -> bytes:
    frames = json.loads(CAPTURE.read_text())["frames"]
    return binascii.unhexlify(frames[index]["hex"])


class TestSolarTrackerRouting:
    def test_both_prefixes_are_one_device_type(self) -> None:
        assert (
            get_device_type("", "HZ31TEST00000001")
            == get_device_type("", "S02FTEST00000001")
            == DEVICE_TYPE_SOLAR_TRACKER
        )

    def test_display_name_is_solar_tracker_for_both_prefixes(self) -> None:
        assert get_device_name("", "HZ31TEST0001") == "Solar Tracker (0001)"
        assert get_device_name("", "S02FTEST0002") == "Solar Tracker (0002)"


class TestSolarTrackerEntitySet:
    async def test_the_hz31_prefix_gets_its_six_sensors(
        self, hass: HomeAssistant
    ) -> None:
        entities = await _setup_entities(hass, HZ31_DEVICE)

        keys = {entity._definition.key for entity in entities}
        assert len(entities) == 6
        assert keys == {sensor.key for sensor in SOLARTRACKER_SENSORS}

    async def test_the_s02f_prefix_gets_the_same_six_sensors(
        self, hass: HomeAssistant
    ) -> None:
        entities = await _setup_entities(hass, S02F_DEVICE)

        keys = {entity._definition.key for entity in entities}
        assert len(entities) == 6
        assert keys == {sensor.key for sensor in SOLARTRACKER_SENSORS}


class TestTheCaptureReachesTheEntities:
    """The point of the whole phase: a frame each tracker sent, in, and
    Home Assistant states out."""

    async def test_the_hz31_frame_fills_the_sensors(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(HZ31_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, HZ31_DEVICE)

        parsed = coordinator._parse_message(
            f"/app/device/property/{HZ31_DEVICE['sn']}", _frame(HZ31_FRAME_INDEX)
        )
        assert parsed is not None
        coordinator.async_set_updated_data(dict(parsed))
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            HZ31_DEVICE["sn"]: coordinator
        }

        created: list[Any] = []
        await sensor_setup(hass, entry, created.extend)
        values = {
            entity._definition.key: entity.native_value
            for entity in created
            if hasattr(entity, "_definition")
        }

        assert values["tilt_angle_deg"] == 85
        assert values["target_angle_deg"] == 85
        # 0xFFFFFFFF sentinel: the key is present, the state reads unknown.
        assert "optimal_angle_deg" in values
        assert values["optimal_angle_deg"] is None
        assert values["light_level"] == 1278602
        assert values["tracking_mode"] == "manual"
        assert values["battery_pct"] == 98

    async def test_the_s02f_frame_fills_the_sensors(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(S02F_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, S02F_DEVICE)

        parsed = coordinator._parse_message(
            f"/app/device/property/{S02F_DEVICE['sn']}", _frame(S02F_FRAME_INDEX)
        )
        assert parsed is not None
        coordinator.async_set_updated_data(dict(parsed))
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            S02F_DEVICE["sn"]: coordinator
        }

        created: list[Any] = []
        await sensor_setup(hass, entry, created.extend)
        values = {
            entity._definition.key: entity.native_value
            for entity in created
            if hasattr(entity, "_definition")
        }

        assert values["tilt_angle_deg"] == 20
        assert values["target_angle_deg"] == 20
        assert "optimal_angle_deg" in values
        assert values["optimal_angle_deg"] is None
        assert values["light_level"] == 171878
        assert values["tracking_mode"] == "manual"
        assert values["battery_pct"] == 97
