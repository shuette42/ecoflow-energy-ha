"""Entity behaviour of the Delta 3 LCD screen timeout select.

The device reports this setting as a number of seconds while the entity offers
labels, so the translation between the two is the delicate part and it lives
here. Two things it has to get right:

  * a value the device reports outside the app's six steps leaves the entity
    unknown rather than showing a neighbouring option that is not what is set
  * the optimistic write stores the value in the shape the device reports it,
    not the label, so the push that arrives two seconds later is the same kind
    of thing as what the write put there

The payload builder and the option map are covered in
tests/test_delta3_screen_timeout.py.
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
    DELTA3_SCREEN_TIMEOUT_KEY,
    DELTA3_SCREEN_TIMEOUT_STATE_KEY,
    DELTA3_SELECTS,
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_STREAM,
    DOMAIN,
    MODE_ENHANCED,
    MODE_STANDARD,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.delta3_commands import (
    SCREEN_TIMEOUT_PARAMS_KEY,
)
from custom_components.ecoflow_energy.select import (
    EcoFlowSelect,
    _get_select_defs,
    async_setup_entry as select_setup,
)

DELTA3_MAX_PLUS: dict[str, Any] = {
    "sn": "D3M1TEST00000001",
    "name": "Delta 3 Max Plus",
    "product_name": "DELTA 3 Max Plus",
    "device_type": DEVICE_TYPE_DELTA3,
    "online": 1,
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
    coordinator._device_data = dict(data or {})
    coordinator.async_set_updated_data(dict(coordinator._device_data))
    coordinator.async_send_delta3_set = AsyncMock(return_value=True)
    return coordinator, entry


def _select(coordinator: EcoFlowDeviceCoordinator) -> EcoFlowSelect:
    defn = next(d for d in DELTA3_SELECTS if d.key == DELTA3_SCREEN_TIMEOUT_KEY)
    entity = EcoFlowSelect(coordinator, defn)
    entity.async_write_ha_state = MagicMock()
    entity.entity_id = f"select.{DELTA3_SCREEN_TIMEOUT_KEY}"
    return entity


class TestReadBack:
    @pytest.mark.parametrize(
        ("seconds", "option"),
        [
            (10, "10_seconds"),
            (30, "30_seconds"),
            (60, "1_minute"),
            (300, "5_minutes"),
            (1800, "30_minutes"),
            (0, "never"),
        ],
    )
    async def test_each_reported_value_shows_its_label(
        self, hass: HomeAssistant, seconds: int, option: str
    ) -> None:
        coordinator, _ = _coordinator(
            hass, {DELTA3_SCREEN_TIMEOUT_STATE_KEY: seconds}
        )
        assert _select(coordinator).current_option == option

    async def test_zero_reads_never_rather_than_off(
        self, hass: HomeAssistant
    ) -> None:
        """The whole feature turns on this one value being read correctly.

        Zero is the app's "Never": the screen stays lit. Showing it as an
        off-state would tell a user the panel is dark while it is not.
        """
        coordinator, _ = _coordinator(hass, {DELTA3_SCREEN_TIMEOUT_STATE_KEY: 0})
        assert _select(coordinator).current_option == "never"

    async def test_foreign_value_leaves_the_entity_unknown(
        self, hass: HomeAssistant
    ) -> None:
        """Another client may write a value the app never offers."""
        coordinator, _ = _coordinator(hass, {DELTA3_SCREEN_TIMEOUT_STATE_KEY: 47})
        assert _select(coordinator).current_option is None

    async def test_missing_value_is_unknown(self, hass: HomeAssistant) -> None:
        coordinator, _ = _coordinator(hass, {})
        assert _select(coordinator).current_option is None

    async def test_a_bool_is_not_mistaken_for_a_timeout(
        self, hass: HomeAssistant
    ) -> None:
        """True would otherwise index the map at 1 and read as a real step."""
        coordinator, _ = _coordinator(hass, {DELTA3_SCREEN_TIMEOUT_STATE_KEY: True})
        assert _select(coordinator).current_option is None


class TestWrite:
    async def test_write_sends_the_seconds_behind_the_label(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _ = _coordinator(hass, {DELTA3_SCREEN_TIMEOUT_STATE_KEY: 300})
        entity = _select(coordinator)

        await entity.async_select_option("30_seconds")

        command = coordinator.async_send_delta3_set.call_args[0][0]
        assert command["params"] == {SCREEN_TIMEOUT_PARAMS_KEY: 30}

    async def test_never_writes_zero(self, hass: HomeAssistant) -> None:
        coordinator, _ = _coordinator(hass, {DELTA3_SCREEN_TIMEOUT_STATE_KEY: 10})
        entity = _select(coordinator)

        await entity.async_select_option("never")

        command = coordinator.async_send_delta3_set.call_args[0][0]
        assert command["params"] == {SCREEN_TIMEOUT_PARAMS_KEY: 0}

    async def test_optimistic_value_is_stored_as_seconds(
        self, hass: HomeAssistant
    ) -> None:
        """The coordinator keeps the device's shape, not the label.

        Storing the label would make the next read map a string as if it were
        a wire value, and the entity would go unknown until the device pushed.
        """
        coordinator, _ = _coordinator(hass, {DELTA3_SCREEN_TIMEOUT_STATE_KEY: 300})
        entity = _select(coordinator)

        await entity.async_select_option("1_minute")

        assert coordinator.data[DELTA3_SCREEN_TIMEOUT_STATE_KEY] == 60
        assert entity.current_option == "1_minute"

    async def test_a_failed_write_raises(self, hass: HomeAssistant) -> None:
        coordinator, _ = _coordinator(hass, {DELTA3_SCREEN_TIMEOUT_STATE_KEY: 10})
        coordinator.async_send_delta3_set = AsyncMock(return_value=False)
        entity = _select(coordinator)

        with pytest.raises(HomeAssistantError):
            await entity.async_select_option("30_seconds")

    async def test_a_failed_write_leaves_the_device_value_alone(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _ = _coordinator(hass, {DELTA3_SCREEN_TIMEOUT_STATE_KEY: 10})
        coordinator.async_send_delta3_set = AsyncMock(return_value=False)
        entity = _select(coordinator)

        with pytest.raises(HomeAssistantError):
            await entity.async_select_option("30_seconds")

        assert coordinator.data[DELTA3_SCREEN_TIMEOUT_STATE_KEY] == 10
        assert entity.current_option == "10_seconds"

    async def test_an_option_outside_the_list_sends_nothing(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _ = _coordinator(hass, {DELTA3_SCREEN_TIMEOUT_STATE_KEY: 10})
        entity = _select(coordinator)

        await entity.async_select_option("2_hours")

        coordinator.async_send_delta3_set.assert_not_called()


class TestOptimisticLock:
    """The five seconds after a write, where the two shapes could collide.

    A push arriving inside the window carries the value the device had before
    it processed the write. The entity has to keep showing what was asked for
    until the window closes, then follow the device again - and both sides of
    that have to speak wire values, or the label would be read as if it were
    one.
    """

    async def test_a_push_inside_the_window_does_not_win(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = 1000.0
        monkeypatch.setattr(
            "custom_components.ecoflow_energy.select.time.monotonic", lambda: clock
        )
        coordinator, _ = _coordinator(hass, {DELTA3_SCREEN_TIMEOUT_STATE_KEY: 300})
        entity = _select(coordinator)

        await entity.async_select_option("30_seconds")
        coordinator.async_set_updated_data({DELTA3_SCREEN_TIMEOUT_STATE_KEY: 300})

        clock += 2.0
        assert entity.current_option == "30_seconds"

    async def test_the_device_wins_once_the_window_closes(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write the device silently declined must not be shown forever."""
        clock = 1000.0
        monkeypatch.setattr(
            "custom_components.ecoflow_energy.select.time.monotonic", lambda: clock
        )
        coordinator, _ = _coordinator(hass, {DELTA3_SCREEN_TIMEOUT_STATE_KEY: 300})
        entity = _select(coordinator)

        await entity.async_select_option("30_seconds")
        clock += 6.0
        coordinator.async_set_updated_data({DELTA3_SCREEN_TIMEOUT_STATE_KEY: 300})

        assert entity.current_option == "5_minutes"


class TestPlatformGating:
    async def _setup(self, hass: HomeAssistant, enhanced: bool = True) -> set[str]:
        coordinator, entry = _coordinator(
            hass, {DELTA3_SCREEN_TIMEOUT_STATE_KEY: 10}, enhanced
        )
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            DELTA3_MAX_PLUS["sn"]: coordinator
        }
        created: list[Any] = []
        await select_setup(hass, entry, created.extend)
        return {e._definition.key for e in created}

    async def test_enhanced_mode_gets_the_select(self, hass: HomeAssistant) -> None:
        assert DELTA3_SCREEN_TIMEOUT_KEY in await self._setup(hass)

    async def test_standard_mode_does_not(self, hass: HomeAssistant) -> None:
        """The polled quota carries no screen field, so it could never read.

        This is the heating-rod failure mode with the modes swapped: an entity
        that exists, accepts a change, and shows nothing back.
        """
        assert DELTA3_SCREEN_TIMEOUT_KEY not in await self._setup(hass, enhanced=False)

    async def test_delta3_defs_are_the_delta3_list(self) -> None:
        assert _get_select_defs(DEVICE_TYPE_DELTA3) is DELTA3_SELECTS

    @pytest.mark.parametrize(
        "device_type",
        [
            DEVICE_TYPE_DELTA,
            DEVICE_TYPE_SMARTPLUG,
            DEVICE_TYPE_STREAM,
            "unknown",
        ],
    )
    def test_the_new_list_does_not_leak_to_other_devices(
        self, device_type: str
    ) -> None:
        """Field 18 belongs to the Delta 3 status frame and nowhere else."""
        assert _get_select_defs(device_type) == []
