"""Entity behaviour of the four Delta 3 idle shutdowns.

Same shape as the screen timeout, four times, with one difference that matters:
these switch power off. The device shutdown powers the whole unit down. So the
tests here care less about the mechanism, which the screen timeout already
covers, and more about the two ways a user could end up with an output dead:
a value read as the wrong step, or a write landing on the wrong setting.

The payload builder and the option map are covered in
tests/test_delta3_idle_shutdown.py.
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
    DELTA3_IDLE_SHUTDOWNS,
    DELTA3_SELECTS,
    DEVICE_TYPE_DELTA3,
    DOMAIN,
    MODE_ENHANCED,
    MODE_STANDARD,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.select import (
    EcoFlowSelect,
    async_setup_entry as select_setup,
)

DELTA3_MAX_PLUS: dict[str, Any] = {
    "sn": "D3M1TEST00000001",
    "name": "Delta 3 Max Plus",
    "product_name": "DELTA 3 Max Plus",
    "device_type": DEVICE_TYPE_DELTA3,
    "online": 1,
}

# entity key -> (state key, params key)
SETTINGS: dict[str, tuple[str, str]] = {
    "device_idle_shutdown": ("dev_standby_time_min", "cfgDevStandbyTime"),
    "ac1_idle_shutdown": ("ac_standby_time_min", "cfgAcStandbyTime"),
    "ac2_idle_shutdown": ("ac2_standby_time_min", "cfgAc2StandbyTime"),
    "dc_idle_shutdown": ("dc_standby_time_min", "cfgDcStandbyTime"),
}

# What the maintainer's own unit reported on 2026-08-05: three at Never, the
# 12 V group at two hours.
REPORTED: dict[str, Any] = {
    "dev_standby_time_min": 0,
    "ac_standby_time_min": 0,
    "ac2_standby_time_min": 0,
    "dc_standby_time_min": 120,
}


def _entry(enhanced: bool = True) -> MockConfigEntry:
    if enhanced:
        data = {
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [DELTA3_MAX_PLUS],
        }
        unique_id = "test@example.com"
    else:
        data = {
            CONF_AUTH_METHOD: AUTH_METHOD_DEVELOPER,
            CONF_ACCESS_KEY: "test_ak",
            CONF_SECRET_KEY: "test_sk",
            CONF_MODE: MODE_STANDARD,
            CONF_DEVICES: [DELTA3_MAX_PLUS],
        }
        unique_id = "test_ak"
    return MockConfigEntry(
        domain=DOMAIN, title="EcoFlow Energy", data=data, unique_id=unique_id
    )


def _coordinator(
    hass: HomeAssistant,
    data: dict[str, Any] | None = None,
    enhanced: bool = True,
) -> tuple[EcoFlowDeviceCoordinator, MockConfigEntry]:
    entry = _entry(enhanced)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, DELTA3_MAX_PLUS)
    coordinator._enhanced_mode = enhanced
    coordinator._device_data = dict(data if data is not None else REPORTED)
    coordinator.async_set_updated_data(dict(coordinator._device_data))
    coordinator.async_send_delta3_set = AsyncMock(return_value=True)
    return coordinator, entry


def _select(coordinator: EcoFlowDeviceCoordinator, key: str) -> EcoFlowSelect:
    defn = next(d for d in DELTA3_SELECTS if d.key == key)
    entity = EcoFlowSelect(coordinator, defn)
    entity.async_write_ha_state = MagicMock()
    entity.entity_id = f"select.{key}"
    return entity


class TestReadBack:
    @pytest.mark.parametrize(
        ("minutes", "option"),
        [
            (30, "30_minutes"),
            (60, "1_hour"),
            (120, "2_hours"),
            (240, "4_hours"),
            (360, "6_hours"),
            (720, "12_hours"),
            (1440, "24_hours"),
            (0, "never"),
        ],
    )
    @pytest.mark.parametrize("key", sorted(SETTINGS))
    async def test_each_value_shows_its_label(
        self, hass: HomeAssistant, key: str, minutes: int, option: str
    ) -> None:
        state_key, _ = SETTINGS[key]
        coordinator, _ = _coordinator(hass, {state_key: minutes})
        assert _select(coordinator, key).current_option == option

    @pytest.mark.parametrize("key", sorted(SETTINGS))
    async def test_zero_reads_never(self, hass: HomeAssistant, key: str) -> None:
        """Nothing switches off. Reading this as a span would be the worst
        possible error on the device shutdown."""
        state_key, _ = SETTINGS[key]
        coordinator, _ = _coordinator(hass, {state_key: 0})
        assert _select(coordinator, key).current_option == "never"

    async def test_the_maintainers_own_reading(self, hass: HomeAssistant) -> None:
        """The four values captured on 2026-08-05, against what the app showed."""
        coordinator, _ = _coordinator(hass)

        assert _select(coordinator, "device_idle_shutdown").current_option == "never"
        assert _select(coordinator, "ac1_idle_shutdown").current_option == "never"
        assert _select(coordinator, "ac2_idle_shutdown").current_option == "never"
        assert _select(coordinator, "dc_idle_shutdown").current_option == "2_hours"

    @pytest.mark.parametrize("key", sorted(SETTINGS))
    async def test_a_foreign_value_leaves_it_unknown(
        self, hass: HomeAssistant, key: str
    ) -> None:
        state_key, _ = SETTINGS[key]
        coordinator, _ = _coordinator(hass, {state_key: 47})
        assert _select(coordinator, key).current_option is None

    async def test_each_entity_reads_its_own_field(
        self, hass: HomeAssistant
    ) -> None:
        """Four distinct values at once, none crossed."""
        coordinator, _ = _coordinator(
            hass,
            {
                "dev_standby_time_min": 30,
                "ac_standby_time_min": 60,
                "ac2_standby_time_min": 240,
                "dc_standby_time_min": 1440,
            },
        )

        assert _select(coordinator, "device_idle_shutdown").current_option == "30_minutes"
        assert _select(coordinator, "ac1_idle_shutdown").current_option == "1_hour"
        assert _select(coordinator, "ac2_idle_shutdown").current_option == "4_hours"
        assert _select(coordinator, "dc_idle_shutdown").current_option == "24_hours"


class TestWrite:
    @pytest.mark.parametrize("key", sorted(SETTINGS))
    async def test_write_sends_minutes_on_the_right_params_key(
        self, hass: HomeAssistant, key: str
    ) -> None:
        _, params_key = SETTINGS[key]
        coordinator, _ = _coordinator(hass)

        await _select(coordinator, key).async_select_option("4_hours")

        command = coordinator.async_send_delta3_set.call_args[0][0]
        assert command["params"] == {params_key: 240}

    @pytest.mark.parametrize("key", sorted(SETTINGS))
    async def test_never_writes_zero(self, hass: HomeAssistant, key: str) -> None:
        _, params_key = SETTINGS[key]
        coordinator, _ = _coordinator(hass)

        await _select(coordinator, key).async_select_option("never")

        command = coordinator.async_send_delta3_set.call_args[0][0]
        assert command["params"] == {params_key: 0}

    async def test_a_failed_write_raises_and_keeps_the_device_value(
        self, hass: HomeAssistant
    ) -> None:
        """A silently dropped write must not leave the UI claiming success.

        On the device shutdown that would tell someone the unit powers down
        after four hours when it is actually set never to."""
        coordinator, _ = _coordinator(hass)
        coordinator.async_send_delta3_set = AsyncMock(return_value=False)
        entity = _select(coordinator, "device_idle_shutdown")

        with pytest.raises(HomeAssistantError):
            await entity.async_select_option("4_hours")

        assert coordinator.data["dev_standby_time_min"] == 0
        assert entity.current_option == "never"

    @pytest.mark.parametrize("key", sorted(SETTINGS))
    async def test_an_option_outside_the_list_sends_nothing(
        self, hass: HomeAssistant, key: str
    ) -> None:
        coordinator, _ = _coordinator(hass)

        await _select(coordinator, key).async_select_option("5_minutes")

        coordinator.async_send_delta3_set.assert_not_called()

    async def test_writing_one_does_not_disturb_the_others(
        self, hass: HomeAssistant
    ) -> None:
        """One frame carries one setting; the rest keep what the device reported."""
        coordinator, _ = _coordinator(hass)

        await _select(coordinator, "ac1_idle_shutdown").async_select_option("6_hours")

        assert coordinator.data["ac_standby_time_min"] == 360
        assert coordinator.data["dev_standby_time_min"] == 0
        assert coordinator.data["ac2_standby_time_min"] == 0
        assert coordinator.data["dc_standby_time_min"] == 120


class TestPlatformGating:
    async def _setup(self, hass: HomeAssistant, enhanced: bool = True) -> set[str]:
        coordinator, entry = _coordinator(hass, enhanced=enhanced)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            DELTA3_MAX_PLUS["sn"]: coordinator
        }
        created: list[Any] = []
        await select_setup(hass, entry, created.extend)
        return {e._definition.key for e in created}

    async def test_enhanced_mode_gets_all_four(self, hass: HomeAssistant) -> None:
        keys = await self._setup(hass)
        assert set(SETTINGS) <= keys

    async def test_the_screen_timeout_is_still_there(
        self, hass: HomeAssistant
    ) -> None:
        assert "screen_timeout" in await self._setup(hass)

    async def test_standard_mode_gets_none_of_them(
        self, hass: HomeAssistant
    ) -> None:
        """No standby field is in the polled quota, so with developer keys the
        entity would accept a change and never show one."""
        keys = await self._setup(hass, enhanced=False)
        assert not (set(SETTINGS) & keys)

    def test_every_listed_setting_has_a_definition(self) -> None:
        defined = {d.key for d in DELTA3_SELECTS}
        assert {key for key, _, _, _ in DELTA3_IDLE_SHUTDOWNS} <= defined
