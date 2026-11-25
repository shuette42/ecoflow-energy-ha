"""Stream Ultra (BK11) and Ultra X (BK61) AC outlet switches in Home Assistant,
issues #677 and #753.

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

    async def test_bk61_enhanced_creates_both_outlet_switches_and_no_feed_in(
        self, hass: HomeAssistant
    ) -> None:
        """Enhanced Mode: the outlets (#753); Feed-in is Standard Mode only."""
        keys = await _created_switch_keys(hass, "BK61", True)
        assert keys == OUTLET_KEYS

    async def test_bk61_standard_keeps_feed_in_and_gets_no_outlets(
        self, hass: HomeAssistant
    ) -> None:
        keys = await _created_switch_keys(hass, "BK61", False)
        assert keys == {"feed_in_control_switch"}

    async def test_bk12_enhanced_creates_both_outlet_switches_and_nothing_else(
        self, hass: HomeAssistant
    ) -> None:
        """A Stream Pro (#761): the outlets in Enhanced Mode, no Feed-in."""
        assert await _created_switch_keys(hass, "BK12", True) == OUTLET_KEYS

    async def test_bk12_standard_creates_no_switch(self, hass: HomeAssistant) -> None:
        assert await _created_switch_keys(hass, "BK12", False) == set()

    @pytest.mark.parametrize("prefix", ["BK41", "BK51"])
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

    def test_bk12_gets_no_stream_numbers(self) -> None:
        assert _get_number_defs(DEVICE_TYPE_STREAM, _sn("BK12")) == []

    def test_bk12_switches_are_the_outlets_only(self) -> None:
        defs = _get_switch_defs(DEVICE_TYPE_STREAM, _sn("BK12"))
        assert {d.key for d in defs} == OUTLET_KEYS

    def test_bk61_gets_no_stream_numbers(self) -> None:
        assert _get_number_defs(DEVICE_TYPE_STREAM, _sn("BK61")) == []

    def test_bk61_switch_lists_combine_outlets_and_feed_in(self) -> None:
        """Both lists reach the platform; the mode filter picks per entry."""
        defs = _get_switch_defs(DEVICE_TYPE_STREAM, _sn("BK61"))
        assert {d.key for d in defs} == OUTLET_KEYS | {"feed_in_control_switch"}


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


BK61_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "stream" / "bk61_outlet_toggle_753.json"
)


class TestBk61ReadBack:
    @staticmethod
    def _frames() -> dict[str, bytes]:
        return {
            f["label"]: bytes.fromhex(f["hex"])
            for f in json.loads(BK61_FIXTURE.read_text())["frames"]
        }

    @staticmethod
    def _switches(coordinator: Any) -> dict[str, EcoFlowSwitch]:
        switches = {}
        for key in ("ac_outlet_1_switch", "ac_outlet_2_switch"):
            defn = next(d for d in STREAM_SWITCHES if d.key == key)
            switch = EcoFlowSwitch(coordinator, defn)
            switch.async_write_ha_state = MagicMock()  # type: ignore[misc]
            switches[key] = switch
        return switches

    @staticmethod
    def _ingest(hass: HomeAssistant, coordinator: Any, frame: bytes) -> None:
        topic = f"/app/device/property/{coordinator.device_sn}"
        with patch.object(hass.loop, "call_soon_threadsafe") as post:
            coordinator._on_mqtt_message(topic, frame)
        post.assert_called_once()
        callback, *args = post.call_args.args
        callback(*args)

    async def test_outlet_1_follows_the_app_write_on_one_unit_in_both_directions(
        self, hass: HomeAssistant
    ) -> None:
        """KEV-github's unit on 2026-11-20: the push after the app's outlet 1
        on write, then the full push after the off write (#753)."""
        frames = self._frames()
        coordinator, _ = _coordinator(hass, "BK61", True)
        outlet_1 = self._switches(coordinator)["ac_outlet_1_switch"]

        seen = []
        for label in (
            "kev_1120_readback_outlet_1_on",
            "kev_1120_readback_outlet_1_off",
        ):
            self._ingest(hass, coordinator, frames[label])
            seen.append(outlet_1.is_on)

        assert seen == [True, False]

    async def test_outlet_2_push_leaves_outlet_1_as_it_was(
        self, hass: HomeAssistant
    ) -> None:
        """Two recordings of two units, not one recorded sequence: StudioCuda's
        full push (outlet 1 on, outlet 2 off), then the other unit's outlet 2
        push (on) that carries no outlet 1 field. The step shows only that a
        push without the field keeps the other outlet's state."""
        frames = self._frames()
        coordinator, _ = _coordinator(hass, "BK61", True)
        switches = self._switches(coordinator)

        seen: list[tuple[bool | None, bool | None]] = []
        for label in ("readback_outlet_1_on", "readback_outlet_2_on"):
            self._ingest(hass, coordinator, frames[label])
            seen.append(
                (
                    switches["ac_outlet_1_switch"].is_on,
                    switches["ac_outlet_2_switch"].is_on,
                )
            )

        assert seen == [(True, False), (True, True)]


BK12_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "stream" / "bk12_outlet_toggle_761.json"
)


class TestBk12ReadBack:
    async def test_each_push_switches_its_own_outlet_through_ingest(
        self, hass: HomeAssistant
    ) -> None:
        """A Stream Pro (#761): the push after the outlet 2 on acknowledgement
        turns outlet 2 on and leaves outlet 1 alone, the later push after the
        outlet 1 on acknowledgement turns outlet 1 on and keeps outlet 2."""
        frames = {
            f["label"]: bytes.fromhex(f["hex"])
            for f in json.loads(BK12_FIXTURE.read_text())["frames"]
        }
        coordinator, _ = _coordinator(hass, "BK12", True)
        switches = {}
        for key in ("ac_outlet_1_switch", "ac_outlet_2_switch"):
            defn = next(d for d in STREAM_SWITCHES if d.key == key)
            switch = EcoFlowSwitch(coordinator, defn)
            switch.async_write_ha_state = MagicMock()  # type: ignore[misc]
            switches[key] = switch
        topic = f"/app/device/property/{coordinator.device_sn}"

        seen: list[tuple[bool | None, bool | None]] = []
        for label in ("push_outlet_2_on", "push_outlet_1_on"):
            with patch.object(hass.loop, "call_soon_threadsafe") as post:
                coordinator._on_mqtt_message(topic, frames[label])
            post.assert_called_once()
            callback, *args = post.call_args.args
            callback(*args)
            seen.append(
                (
                    switches["ac_outlet_1_switch"].is_on,
                    switches["ac_outlet_2_switch"].is_on,
                )
            )

        assert seen == [(None, True), (True, True)]
