"""Stream Ultra (BK11) AC outlet switches in Home Assistant, issue #677.

The BK11 gets the two AC outlet switches the BK31 has, and nothing else of the
BK31 write set: no SoC limits, LED brightness or Backup Reserve. The switch
follows the device's read-back push, taken here from a real BK11 frame.
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
    AUTH_METHOD_APP,
    AUTH_METHOD_DEVELOPER,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SECRET_KEY,
    CONF_USER_ID,
    DEVICE_TYPE_STREAM,
    DOMAIN,
    MODE_ENHANCED,
    MODE_STANDARD,
    STREAM_SWITCHES,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.number import _get_number_defs
from custom_components.ecoflow_energy.switch import (
    EcoFlowSwitch,
    _get_switch_defs,
)
from custom_components.ecoflow_energy.switch import (
    async_setup_entry as switch_setup,
)

from .conftest import add_entities_collector

OUTLET_KEYS = {"ac_outlet_1_switch", "ac_outlet_2_switch"}
FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "stream" / "bk11_outlet_toggle_677.json"
)


def _sn(prefix: str) -> str:
    return f"{prefix}TEST00000001"


def _coordinator(
    hass: HomeAssistant, prefix: str, enhanced: bool
) -> tuple[EcoFlowDeviceCoordinator, MockConfigEntry]:
    device = {
        "sn": _sn(prefix),
        "name": f"Test {prefix}",
        "product_name": f"Test {prefix}",
        "device_type": DEVICE_TYPE_STREAM,
        "online": 1,
    }
    if enhanced:
        data: dict[str, Any] = {
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [device],
        }
        unique_id = "test@example.com"
    else:
        data = {
            CONF_AUTH_METHOD: AUTH_METHOD_DEVELOPER,
            CONF_ACCESS_KEY: "test_ak",
            CONF_SECRET_KEY: "test_sk",
            CONF_MODE: MODE_STANDARD,
            CONF_DEVICES: [device],
        }
        unique_id = "test_ak"
    entry = MockConfigEntry(
        domain=DOMAIN, title="EcoFlow Energy", data=data, unique_id=unique_id
    )
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    coordinator._enhanced_mode = enhanced
    coordinator._auth_method = AUTH_METHOD_APP if enhanced else AUTH_METHOD_DEVELOPER
    coordinator.async_set_updated_data({})
    coordinator.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    return coordinator, entry


async def _created_switch_keys(
    hass: HomeAssistant, prefix: str, enhanced: bool
) -> set[str]:
    coordinator, entry = _coordinator(hass, prefix, enhanced)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        coordinator.device_sn: coordinator
    }
    entities: list[Any] = []
    await switch_setup(hass, entry, add_entities_collector(entities))
    return {e._definition.key for e in entities}


class TestSwitchSetup:
    async def test_bk11_enhanced_creates_both_outlet_switches(
        self, hass: HomeAssistant
    ) -> None:
        assert await _created_switch_keys(hass, "BK11", True) >= OUTLET_KEYS

    async def test_bk11_standard_does_not(self, hass: HomeAssistant) -> None:
        assert not OUTLET_KEYS & await _created_switch_keys(hass, "BK11", False)

    async def test_bk31_enhanced_keeps_both(self, hass: HomeAssistant) -> None:
        assert await _created_switch_keys(hass, "BK31", True) >= OUTLET_KEYS

    @pytest.mark.parametrize("prefix", ["BK41", "BK51", "BK61"])
    async def test_other_streams_enhanced_do_not(
        self, hass: HomeAssistant, prefix: str
    ) -> None:
        assert not OUTLET_KEYS & await _created_switch_keys(hass, prefix, True)


class TestBk31OnlyControlsStayOff:
    def test_bk11_gets_no_stream_numbers(self) -> None:
        """No SoC limits, LED brightness or Backup Reserve for the BK11."""
        assert _get_number_defs(DEVICE_TYPE_STREAM, _sn("BK11")) == []

    def test_bk31_numbers_are_unchanged(self) -> None:
        assert _get_number_defs(DEVICE_TYPE_STREAM, _sn("BK31"))

    def test_bk11_switches_are_the_outlets_only(self) -> None:
        defs = _get_switch_defs(DEVICE_TYPE_STREAM, _sn("BK11"))
        assert {d.key for d in defs} == OUTLET_KEYS


class TestReadBack:
    async def test_outlet_1_follows_the_recorded_pushes_through_ingest(
        self, hass: HomeAssistant
    ) -> None:
        """The recorded on and off pushes take the MQTT path to the switch.

        Each frame enters where the client hands it over (``_on_mqtt_message``)
        and the update it posts to the event loop is run as posted, so parser
        choice, dispatch and ``_apply_data`` are the product's own.
        """
        frames = {
            f["label"]: bytes.fromhex(f["hex"])
            for f in json.loads(FIXTURE.read_text())["frames"]
        }
        coordinator, _ = _coordinator(hass, "BK11", True)
        defn = next(d for d in STREAM_SWITCHES if d.key == "ac_outlet_1_switch")
        switch = EcoFlowSwitch(coordinator, defn)
        switch.async_write_ha_state = MagicMock()  # type: ignore[misc]
        topic = f"/app/device/property/{coordinator.device_sn}"

        seen: list[bool | None] = []
        for label in ("readback_outlet_1_on", "readback_outlet_1_off"):
            with patch.object(hass.loop, "call_soon_threadsafe") as post:
                coordinator._on_mqtt_message(topic, frames[label])
            post.assert_called_once()
            callback, *args = post.call_args.args
            callback(*args)
            seen.append(switch.is_on)

        assert seen == [True, False]
