"""Entity-level behaviour of Delta 3 port priority.

The wire item carries the non-essential flag and the cutoff level together, so
writing either one means resending the other. Everything delicate about this
feature lives in that coupling, and it lives in the entities rather than in the
payload builder: the switch has to fetch the cutoff the device last reported,
and the number has to fetch the flag. A default on either side would silently
change a setting the user never touched - move a port between essential and
non-essential, or reset a threshold set in the app.

The payload builder is covered in tests/test_delta3_port_priority.py. What is
covered here is the wiring around it, plus the platform-level gating that the
switch platform gained together with this feature.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

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
    DELTA2MAX_SWITCHES,
    DELTA3_NUMBERS,
    DELTA3_SWITCHES,
    DEVICE_TYPE_DELTA3,
    DOMAIN,
    MODE_ENHANCED,
    MODE_STANDARD,
    STREAM_SWITCHES,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.number import EcoFlowNumber
from custom_components.ecoflow_energy.switch import (
    EcoFlowSwitch,
    async_setup_entry as switch_setup,
)

from .conftest import MOCK_DELTA_DEVICE, MOCK_STREAM_DEVICE

DELTA3_MAX_PLUS: dict[str, Any] = {
    "sn": "D3M1TEST00000001",
    "name": "Delta 3 Max Plus",
    "product_name": "DELTA 3 Max Plus",
    "device_type": DEVICE_TYPE_DELTA3,
    "online": 1,
}

BASE_DELTA3: dict[str, Any] = {
    "sn": "P231TEST00000001",
    "name": "DELTA 3",
    "product_name": "DELTA 3",
    "device_type": DEVICE_TYPE_DELTA3,
    "online": 1,
}

# A status frame with all three ports reported, which is what the device sends
# on every push: AC 1 essential at 40 %, AC 2 non-essential at 30 %, DC
# essential at 35 %, battery limits at their defaults.
REPORTED: dict[str, Any] = {
    "port_priority_ac1_limited": False,
    "port_priority_ac1_cutoff_soc": 40,
    "port_priority_ac2_limited": True,
    "port_priority_ac2_cutoff_soc": 30,
    "port_priority_dc_limited": False,
    "port_priority_dc_cutoff_soc": 35,
    "max_charge_soc_pct": 100,
    "min_discharge_soc_pct": 0,
}


def _entry(device: dict[str, Any], enhanced: bool = True) -> MockConfigEntry:
    """Build a config entry in the requested mode for one Delta 3."""
    if enhanced:
        data = {
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
    return MockConfigEntry(
        domain=DOMAIN, title="EcoFlow Energy", data=data, unique_id=unique_id
    )


def _coordinator(
    hass: HomeAssistant,
    device: dict[str, Any],
    data: dict[str, Any] | None = None,
    enhanced: bool = True,
) -> tuple[EcoFlowDeviceCoordinator, MockConfigEntry]:
    entry = _entry(device, enhanced)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    coordinator._enhanced_mode = enhanced
    coordinator._device_data = dict(data or {})
    coordinator.async_set_updated_data(dict(coordinator._device_data))
    coordinator.async_send_delta3_set = AsyncMock(return_value=True)
    return coordinator, entry


def _switch(coordinator: EcoFlowDeviceCoordinator, key: str) -> EcoFlowSwitch:
    defn = next(d for d in DELTA3_SWITCHES if d.key == key)
    entity = EcoFlowSwitch(coordinator, defn)
    entity.async_write_ha_state = MagicMock()
    entity.entity_id = f"switch.{key}"
    return entity


def _number(coordinator: EcoFlowDeviceCoordinator, key: str) -> EcoFlowNumber:
    defn = next(d for d in DELTA3_NUMBERS if d.key == key)
    entity = EcoFlowNumber(coordinator, defn)
    entity.async_write_ha_state = MagicMock()
    entity.entity_id = f"number.{key}"
    return entity


def _sent_item(coordinator: EcoFlowDeviceCoordinator) -> dict[str, Any]:
    """Return the port priority payload handed to the coordinator."""
    coordinator.async_send_delta3_set.assert_called_once()
    command = coordinator.async_send_delta3_set.call_args[0][0]
    return command["params"]["cfgPowerOutagesList"]


class TestSwitchCarriesTheReportedCutoff:
    """Flipping the flag must not move the threshold."""

    async def test_write_reuses_the_cutoff_the_device_reported(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _ = _coordinator(hass, DELTA3_MAX_PLUS, REPORTED)
        entity = _switch(coordinator, "port_priority_dc_switch")

        await entity.async_turn_on()

        item = _sent_item(coordinator)
        assert item["portType"] == 1
        assert item["limited"] is True
        assert item["cutoffSoc"] == 35  # not a default, not the AC 2 value

    async def test_each_port_reads_its_own_cutoff(
        self, hass: HomeAssistant
    ) -> None:
        """Three ports share one key prefix, so a stem mix-up is cheap to make
        and invisible afterwards - the write would succeed with the wrong
        threshold."""
        for key, port_type, cutoff in (
            ("port_priority_ac1_switch", 2, 40),
            ("port_priority_ac2_switch", 3, 30),
            ("port_priority_dc_switch", 1, 35),
        ):
            coordinator, _ = _coordinator(hass, DELTA3_MAX_PLUS, REPORTED)
            entity = _switch(coordinator, key)

            await entity.async_turn_off()

            item = _sent_item(coordinator)
            assert item["portType"] == port_type
            assert item["cutoffSoc"] == cutoff
            assert item["limited"] is False

    async def test_write_before_the_first_report_is_refused_as_not_ready(
        self, hass: HomeAssistant
    ) -> None:
        """Inventing a cutoff would overwrite one set in the app, so the write
        is refused - but as a temporary state, not as an unsupported control.
        """
        coordinator, _ = _coordinator(hass, DELTA3_MAX_PLUS, {})
        entity = _switch(coordinator, "port_priority_ac1_switch")

        with pytest.raises(HomeAssistantError) as err:
            await entity.async_turn_on()

        assert err.value.translation_key == "set_command_not_ready"
        coordinator.async_send_delta3_set.assert_not_called()

    async def test_a_refused_write_does_not_move_the_switch(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _ = _coordinator(hass, DELTA3_MAX_PLUS, {})
        entity = _switch(coordinator, "port_priority_ac1_switch")

        with pytest.raises(HomeAssistantError):
            await entity.async_turn_on()

        assert entity.is_on is None

    async def test_the_regular_switches_are_untouched(
        self, hass: HomeAssistant
    ) -> None:
        """The port priority branch sits in front of the shared builder."""
        coordinator, _ = _coordinator(hass, DELTA3_MAX_PLUS, REPORTED)
        entity = _switch(coordinator, "beeper_switch")

        await entity.async_turn_on()

        command = coordinator.async_send_delta3_set.call_args[0][0]
        assert "cfgPowerOutagesList" not in command["params"]


class TestNumberCarriesTheReportedFlag:
    """Moving the threshold must not re-home the port."""

    async def test_write_reuses_the_flag_the_device_reported(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _ = _coordinator(hass, DELTA3_MAX_PLUS, REPORTED)
        entity = _number(coordinator, "port_priority_ac2_soc")

        await entity.async_set_native_value(55.0)

        item = _sent_item(coordinator)
        assert item["portType"] == 3
        assert item["limited"] is True  # AC 2 was non-essential, still is
        assert item["cutoffSoc"] == 55

    async def test_an_essential_port_stays_essential(
        self, hass: HomeAssistant
    ) -> None:
        """False is the proto3 default and the common case, so a builder that
        assumed a value would most likely assume this one wrong."""
        coordinator, _ = _coordinator(hass, DELTA3_MAX_PLUS, REPORTED)
        entity = _number(coordinator, "port_priority_dc_soc")

        await entity.async_set_native_value(20.0)

        assert _sent_item(coordinator)["limited"] is False

    async def test_write_before_the_first_report_is_refused_as_not_ready(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _ = _coordinator(hass, DELTA3_MAX_PLUS, {})
        entity = _number(coordinator, "port_priority_dc_soc")

        with pytest.raises(HomeAssistantError) as err:
            await entity.async_set_native_value(20.0)

        assert err.value.translation_key == "set_command_not_ready"
        coordinator.async_send_delta3_set.assert_not_called()

    async def test_the_regular_numbers_are_untouched(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _ = _coordinator(hass, DELTA3_MAX_PLUS, REPORTED)
        entity = _number(coordinator, "max_charge_soc")

        await entity.async_set_native_value(80.0)

        command = coordinator.async_send_delta3_set.call_args[0][0]
        assert "cfgPowerOutagesList" not in command["params"]


class TestCutoffBoundsFollowTheDevice:
    """The slider range is derived from the battery limits, not fixed."""

    async def test_default_limits_give_the_widest_range(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _ = _coordinator(hass, DELTA3_MAX_PLUS, REPORTED)
        entity = _number(coordinator, "port_priority_ac1_soc")

        assert entity.native_min_value == 5
        assert entity.native_max_value == 95

    async def test_narrowed_limits_narrow_the_slider(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _ = _coordinator(
            hass,
            DELTA3_MAX_PLUS,
            {**REPORTED, "max_charge_soc_pct": 80, "min_discharge_soc_pct": 20},
        )
        entity = _number(coordinator, "port_priority_ac1_soc")

        assert entity.native_min_value == 25
        assert entity.native_max_value == 75

    async def test_the_range_follows_a_limit_change_without_a_reload(
        self, hass: HomeAssistant
    ) -> None:
        """Both limits are user-writable, so the range moves while HA runs."""
        coordinator, _ = _coordinator(hass, DELTA3_MAX_PLUS, REPORTED)
        entity = _number(coordinator, "port_priority_dc_soc")
        assert entity.native_max_value == 95

        coordinator.async_set_updated_data({**REPORTED, "max_charge_soc_pct": 70})

        assert entity.native_max_value == 65

    async def test_missing_limits_leave_the_slider_usable(
        self, hass: HomeAssistant
    ) -> None:
        """Before the first status frame there is nothing to derive from, and
        a slider pinned shut would look like a broken entity."""
        coordinator, _ = _coordinator(hass, DELTA3_MAX_PLUS, {})
        entity = _number(coordinator, "port_priority_ac2_soc")

        assert entity.native_min_value == 5
        assert entity.native_max_value == 95

    async def test_other_numbers_keep_their_declared_range(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _ = _coordinator(hass, DELTA3_MAX_PLUS, REPORTED)
        entity = _number(coordinator, "ac_charge_power_limit")

        assert entity.native_min_value == 200
        assert entity.native_max_value == 2400


class TestSwitchPlatformGating:
    """`async_setup_entry` gained serial and mode filtering for every device."""

    async def _setup(
        self, hass: HomeAssistant, device: dict[str, Any], enhanced: bool = True
    ) -> set[str]:
        coordinator, entry = _coordinator(hass, device, REPORTED, enhanced)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            device["sn"]: coordinator
        }
        created: list[Any] = []
        await switch_setup(hass, entry, created.extend)
        return {e._definition.key for e in created}

    async def test_max_plus_in_enhanced_mode_gets_them(
        self, hass: HomeAssistant
    ) -> None:
        keys = await self._setup(hass, DELTA3_MAX_PLUS)

        assert "port_priority_ac1_switch" in keys
        assert "port_priority_dc_switch" in keys

    async def test_standard_mode_does_not(self, hass: HomeAssistant) -> None:
        """With developer keys the read-back never arrives, so the switch could
        be flipped but never show where the device stands."""
        keys = await self._setup(hass, DELTA3_MAX_PLUS, enhanced=False)

        assert not {k for k in keys if k.startswith("port_priority_")}
        assert "beeper_switch" in keys

    async def test_a_base_delta3_does_not(self, hass: HomeAssistant) -> None:
        keys = await self._setup(hass, BASE_DELTA3)

        assert not {k for k in keys if k.startswith("port_priority_")}
        assert "ac_out_switch" in keys

    @pytest.mark.parametrize(
        ("device", "defs"),
        [(MOCK_DELTA_DEVICE, DELTA2MAX_SWITCHES), (MOCK_STREAM_DEVICE, STREAM_SWITCHES)],
    )
    async def test_other_device_types_lose_nothing(
        self, hass: HomeAssistant, device: dict[str, Any], defs: list[Any]
    ) -> None:
        """The two new filters run for every device type, not just Delta 3."""
        keys = await self._setup(hass, device)

        assert keys == {d.key for d in defs}
