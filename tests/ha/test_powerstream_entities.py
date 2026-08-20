"""End-to-end checks for the PowerStream (HW51) in Standard Mode.

The parser tests prove the field map against the reporter capture. These
run the same capture through the coordinator's own HTTP path, because the
failure this device already had once was never in a parser: in #188 it was
routed to the wrong one, and every reading it produced was correct for a
device that was not there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    CONF_ACCESS_KEY,
    CONF_DEVICES,
    CONF_MODE,
    CONF_SECRET_KEY,
    DEVICE_TYPE_POWERSTREAM,
    DEVICE_TYPE_UNKNOWN,
    DOMAIN,
    MODE_STANDARD,
    POWERSTREAM_POWER_TO_ENERGY,
    POWERSTREAM_SENSORS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

CAPTURE = (
    Path(__file__).parent.parent / "fixtures" / "powerstream" / "hw51_quota_masked.json"
)

MOCK_POWERSTREAM_DEVICE: dict[str, Any] = {
    "sn": "HW51TEST00000001",
    "name": "PowerStream",
    "product_name": "PowerStream",
    "device_type": DEVICE_TYPE_POWERSTREAM,
    "online": 1,
}


@pytest.fixture
def powerstream_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_ACCESS_KEY: "test_ak",
            CONF_SECRET_KEY: "test_sk",
            CONF_MODE: MODE_STANDARD,
            CONF_DEVICES: [MOCK_POWERSTREAM_DEVICE],
        },
        unique_id="test_ak",
    )


@pytest.fixture
def coordinator(
    hass: HomeAssistant, powerstream_entry: MockConfigEntry
) -> EcoFlowDeviceCoordinator:
    powerstream_entry.add_to_hass(hass)
    instance = EcoFlowDeviceCoordinator(
        hass, powerstream_entry, MOCK_POWERSTREAM_DEVICE
    )
    instance._http_client = MagicMock()
    instance._http_client.get_quota_all = AsyncMock(
        return_value=json.loads(CAPTURE.read_text())
    )
    return instance


class TestTheStandardModePoll:
    async def test_the_capture_reaches_the_device_data(
        self, coordinator: EcoFlowDeviceCoordinator
    ) -> None:
        """The whole point: a real quota in, readings out."""
        data = await coordinator._async_update_data()

        assert data["solar_w"] == 109.0
        assert data["pv1_w"] == 51.0
        assert data["pv2_w"] == 58.0
        assert data["inv_output_w"] == 109.0
        assert data["grid_w"] == -108.0
        assert data["soc_pct"] == 87
        assert data["supply_priority"] == "battery_storage"
        assert coordinator.snapshot.source == "http"

    async def test_no_raw_quota_key_reaches_the_store(
        self, coordinator: EcoFlowDeviceCoordinator
    ) -> None:
        """An unparsed key in the device data is what a missing parser looks
        like, and it is how #139 stayed invisible on the Stream."""
        data = await coordinator._async_update_data()

        assert not [key for key in data if key.startswith("20_1.")]

    async def test_the_raw_quota_is_kept_for_diagnostics(
        self, coordinator: EcoFlowDeviceCoordinator
    ) -> None:
        """The readings this support left out are settled by a second dump,
        so a diagnostics download has to carry the raw keys (#230)."""
        await coordinator._async_update_data()

        assert coordinator.raw_quota_captured_at > 0
        assert coordinator.raw_quota["20_1.pv1InputCur"] == 17

    async def test_the_energy_map_is_the_powerstream_one(
        self, coordinator: EcoFlowDeviceCoordinator
    ) -> None:
        assert coordinator._power_to_energy == POWERSTREAM_POWER_TO_ENERGY


class TestThePlatform:
    async def test_the_sensor_platform_creates_the_whole_set(
        self, hass: HomeAssistant, powerstream_entry: MockConfigEntry
    ) -> None:
        """Entity definitions routing is one thing, entities another.

        Home Assistant keeps an entity in the registry once it has been
        created, so what the platform builds on the first release is what
        that owner is stuck with.
        """
        powerstream_entry.add_to_hass(hass)
        instance = EcoFlowDeviceCoordinator(
            hass, powerstream_entry, MOCK_POWERSTREAM_DEVICE
        )
        hass.data.setdefault(DOMAIN, {})[powerstream_entry.entry_id] = {
            MOCK_POWERSTREAM_DEVICE["sn"]: instance
        }

        created: list[Any] = []
        await sensor_setup(hass, powerstream_entry, created.extend)
        keys = {
            entity._definition.key
            for entity in created
            if hasattr(entity, "_definition")
        }

        assert keys == {definition.key for definition in POWERSTREAM_SENSORS}

    async def test_the_readings_reach_the_entities(
        self, hass: HomeAssistant, coordinator: EcoFlowDeviceCoordinator
    ) -> None:
        """The end of the line: a real quota in, entity states out."""
        hass.data.setdefault(DOMAIN, {})[coordinator._entry.entry_id] = {
            MOCK_POWERSTREAM_DEVICE["sn"]: coordinator
        }
        await coordinator.async_refresh()

        created: list[Any] = []
        await sensor_setup(hass, coordinator._entry, created.extend)
        states = {
            entity._definition.key: entity.native_value
            for entity in created
            if hasattr(entity, "_definition")
        }

        assert states["solar_w"] == 109.0
        assert states["grid_w"] == -108.0
        assert states["soc_pct"] == 87
        assert states["rated_power_w"] == 800.0
        assert states["supply_priority"] == "battery_storage"


class TestTheStandardModePush:
    async def test_a_quota_push_is_read_by_the_same_parser(
        self, coordinator: EcoFlowDeviceCoordinator
    ) -> None:
        """The vendor documents the report as cmdId 1 / cmdFunc 20 carrying
        the same `20_1.` keys, so it goes through the same field map."""
        payload = json.dumps(
            {
                "cmdId": 1,
                "cmdFunc": 20,
                "param": {"20_1.invOutputWatts": 1090, "20_1.batSoc": 87},
            }
        ).encode()

        parsed = coordinator._parse_message(
            "/open/test_account/HW51TEST00000001/quota", payload
        )

        assert parsed == {"inv_output_w": 109.0, "soc_pct": 87}

    async def test_a_push_with_nothing_we_read_is_dropped(
        self, coordinator: EcoFlowDeviceCoordinator
    ) -> None:
        """Returning the raw dict here is how unmapped keys reached the
        device data store on the Stream (#139)."""
        payload = json.dumps({"param": {"20_1.somethingNew": 1}}).encode()

        assert (
            coordinator._parse_message(
                "/open/test_account/HW51TEST00000001/quota", payload
            )
            is None
        )


class TestTheMqttSubscription:
    async def test_standard_mode_subscribes_to_the_quota_topic(
        self,
        hass: HomeAssistant,
        powerstream_entry: MockConfigEntry,
        mock_iot_api,
        mock_http_client,
    ) -> None:
        """Without this the push parser above is unreachable code.

        The subscription is safe even if this device never pushes: with
        developer keys the HTTP poll is the primary source, and a silent
        MQTT only keeps that poll running rather than degrading the device.
        """
        powerstream_entry.add_to_hass(hass)
        with (
            patch("custom_components.ecoflow_energy.device_probe.EcoFlowMQTTClient"),
            patch(
                "custom_components.ecoflow_energy.coordinator.setup.EcoFlowMQTTClient"
            ) as client_cls,
        ):
            client_cls.return_value.create_client.return_value = True
            client_cls.return_value.connect.return_value = True
            instance = EcoFlowDeviceCoordinator(
                hass, powerstream_entry, MOCK_POWERSTREAM_DEVICE
            )
            await instance.async_setup()

        assert client_cls.call_args.kwargs["subscribe_data"] is True
        assert client_cls.call_args.kwargs["wss_mode"] is False


class TestDetection:
    async def test_the_serial_alone_routes_the_device(
        self, hass: HomeAssistant, powerstream_entry: MockConfigEntry
    ) -> None:
        """An entry stored as unsupported is reclassified on load.

        Every owner who set the integration up before this release has the
        PowerStream sitting in their config entry as an unknown device, so
        the serial has to be enough to route it without a fresh setup.
        """
        powerstream_entry.add_to_hass(hass)
        device = dict(MOCK_POWERSTREAM_DEVICE)
        device["product_name"] = ""
        device["device_type"] = DEVICE_TYPE_UNKNOWN

        instance = EcoFlowDeviceCoordinator(hass, powerstream_entry, device)

        assert instance.device_type == DEVICE_TYPE_POWERSTREAM
