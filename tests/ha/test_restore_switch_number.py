"""Tests for switch and number state restore after a Home Assistant restart.

Enhanced Mode devices (e.g. Delta 3) send a full status frame only every
two minutes. Without restore, switches render as unknown (HA shows buttons
instead of a toggle) and numbers as empty fields until the first frame
arrives. These tests verify the restore placeholder, that live data always
wins, that invalid restored states are discarded, and that the restore
seed cooperates with the shared write gate and availability sentinel.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    EcoFlowNumberDef,
    EcoFlowSwitchDef,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.number import EcoFlowNumber
from custom_components.ecoflow_energy.switch import EcoFlowSwitch

from .conftest import MOCK_DELTA_DEVICE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_coordinator(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> EcoFlowDeviceCoordinator:
    """Create a coordinator without calling async_setup."""
    return EcoFlowDeviceCoordinator(hass, entry, MOCK_DELTA_DEVICE)


def _make_switch(coordinator: EcoFlowDeviceCoordinator) -> EcoFlowSwitch:
    defn = EcoFlowSwitchDef(key="ac_switch", name="AC Output", state_key="ac_enabled")
    return EcoFlowSwitch(coordinator, defn)


def _make_number(coordinator: EcoFlowDeviceCoordinator) -> EcoFlowNumber:
    defn = EcoFlowNumberDef(
        key="ac_charge_speed",
        name="AC Charge Speed",
        state_key="ac_charge_watts",
        unit="W",
        min_value=200,
        max_value=2400,
        step=100,
    )
    return EcoFlowNumber(coordinator, defn)


async def _add_switch(switch: EcoFlowSwitch, last_state: object) -> None:
    """Run async_added_to_hass with a mocked restore chain."""
    with (
        patch.object(CoordinatorEntity, "async_added_to_hass", new_callable=AsyncMock),
        patch.object(
            switch, "async_get_last_state", new_callable=AsyncMock, return_value=last_state
        ),
    ):
        await switch.async_added_to_hass()


async def _add_number(number: EcoFlowNumber, last_data: object) -> None:
    """Run async_added_to_hass with a mocked restore chain."""
    with (
        patch.object(CoordinatorEntity, "async_added_to_hass", new_callable=AsyncMock),
        patch.object(
            number,
            "async_get_last_number_data",
            new_callable=AsyncMock,
            return_value=last_data,
        ),
    ):
        await number.async_added_to_hass()


def _last_state(state: str) -> MagicMock:
    mock = MagicMock()
    mock.state = state
    return mock


def _last_number(native_value: float | None) -> MagicMock:
    mock = MagicMock()
    mock.native_value = native_value
    return mock


# ===========================================================================
# Switch restore
# ===========================================================================


class TestSwitchRestore:
    async def test_restored_on_state_shows_after_add(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """No live data yet: the restored 'on' state is shown as placeholder."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        switch = _make_switch(coordinator)

        await _add_switch(switch, _last_state("on"))

        assert switch.is_on is True
        assert switch._last_written_value is True

    async def test_restored_off_state_shows_after_add(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        switch = _make_switch(coordinator)

        await _add_switch(switch, _last_state("off"))

        assert switch.is_on is False
        assert switch._last_written_value is False

    async def test_live_data_replaces_restored_state(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """The first live frame always beats the restored placeholder."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        switch = _make_switch(coordinator)

        await _add_switch(switch, _last_state("on"))
        assert switch.is_on is True

        coordinator.async_set_updated_data({"ac_enabled": 0})
        assert switch.is_on is False

    async def test_live_data_at_add_time_skips_restore(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """A live value present at add time wins; the restore is skipped."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"ac_enabled": 0})
        switch = _make_switch(coordinator)

        await _add_switch(switch, _last_state("on"))

        assert switch._restored_is_on is None
        assert switch.is_on is False

    @pytest.mark.parametrize("state", ["unavailable", "unknown"])
    async def test_invalid_restored_state_is_discarded(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        state: str,
    ) -> None:
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        switch = _make_switch(coordinator)

        await _add_switch(switch, _last_state(state))

        assert switch._restored_is_on is None
        assert switch.is_on is None

    async def test_no_previous_state_leaves_switch_unknown(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        switch = _make_switch(coordinator)

        await _add_switch(switch, None)

        assert switch._restored_is_on is None
        assert switch.is_on is None

    async def test_no_double_write_on_identical_live_value(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Restore seeds the write gate: identical live data skips the write."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        switch = _make_switch(coordinator)

        await _add_switch(switch, _last_state("on"))

        coordinator.async_set_updated_data({"ac_enabled": 1})
        with patch.object(switch, "async_write_ha_state") as mock_write:
            switch._handle_coordinator_update()
            assert mock_write.call_count == 0

    async def test_availability_sentinel_seeded_via_super_chain(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """The write-gate mixin's availability seed still runs on add."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        switch = _make_switch(coordinator)
        assert switch._last_written_available is None

        await _add_switch(switch, _last_state("on"))

        assert switch._last_written_available is not None


# ===========================================================================
# Number restore
# ===========================================================================


class TestNumberRestore:
    async def test_restored_value_shows_after_add(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """No live data yet: the restored value is shown as placeholder."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        number = _make_number(coordinator)

        await _add_number(number, _last_number(800.0))

        assert number.native_value == 800.0
        assert number._last_written_value == 800.0

    async def test_live_data_replaces_restored_value(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """The first live frame always beats the restored placeholder."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        number = _make_number(coordinator)

        await _add_number(number, _last_number(800.0))
        assert number.native_value == 800.0

        coordinator.async_set_updated_data({"ac_charge_watts": 1200})
        assert number.native_value == 1200

    async def test_live_data_at_add_time_skips_restore(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """A live value present at add time wins; the restore is skipped."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        coordinator.async_set_updated_data({"ac_charge_watts": 1200})
        number = _make_number(coordinator)

        await _add_number(number, _last_number(800.0))

        assert number._restored_value is None
        assert number.native_value == 1200

    @pytest.mark.parametrize("restored", [100.0, 5000.0])
    async def test_out_of_range_restored_value_is_discarded(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        restored: float,
    ) -> None:
        """Values below min or above max are never restored."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        number = _make_number(coordinator)

        await _add_number(number, _last_number(restored))

        assert number._restored_value is None
        assert number.native_value is None

    async def test_none_restored_value_is_discarded(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """A previous unknown/unavailable state restores no native value."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        number = _make_number(coordinator)

        await _add_number(number, _last_number(None))

        assert number._restored_value is None
        assert number.native_value is None

    async def test_no_previous_data_leaves_number_unknown(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        number = _make_number(coordinator)

        await _add_number(number, None)

        assert number._restored_value is None
        assert number.native_value is None

    async def test_no_double_write_on_identical_live_value(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Restore seeds the write gate: identical live data skips the write."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        number = _make_number(coordinator)

        await _add_number(number, _last_number(800.0))

        coordinator.async_set_updated_data({"ac_charge_watts": 800})
        with patch.object(number, "async_write_ha_state") as mock_write:
            number._handle_coordinator_update()
            assert mock_write.call_count == 0

    async def test_availability_sentinel_seeded_via_super_chain(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """The write-gate mixin's availability seed still runs on add."""
        standard_config_entry.add_to_hass(hass)
        coordinator = _make_coordinator(hass, standard_config_entry)
        number = _make_number(coordinator)
        assert number._last_written_available is None

        await _add_number(number, _last_number(800.0))

        assert number._last_written_available is not None
