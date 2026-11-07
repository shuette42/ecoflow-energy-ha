"""Smart Plug: Current reads 0 A while the relay is off (#682).

The plug sends only the fields that changed, so a frame without `current_a`
is not a zero. On the reporter's plug the last reading (0.308 A) stood for
good after the switch-off. The frames below follow his diagnostics download.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator

from .conftest import MOCK_SMARTPLUG_DEVICE


def _plug(
    hass: HomeAssistant, entry: MockConfigEntry
) -> tuple[EcoFlowDeviceCoordinator, dict]:
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, MOCK_SMARTPLUG_DEVICE)
    return coordinator, coordinator._device_data


async def test_off_state_in_the_full_status_zeroes_the_stale_current(
    hass: HomeAssistant, standard_config_entry: MockConfigEntry
) -> None:
    """Full status 0.279 A with the relay off, then a 0 W frame without current."""
    coordinator, data = _plug(hass, standard_config_entry)
    coordinator._apply_data({"current_a": 0.279, "power_w": 1.0, "switch_state": 0})
    assert data["current_a"] == 0.0
    coordinator._apply_data({"power_w": 0.0})
    assert data["current_a"] == 0.0


async def test_switching_off_under_load_zeroes_the_current(
    hass: HomeAssistant, standard_config_entry: MockConfigEntry
) -> None:
    """1.274 A under load, then a frame carrying only switch_state 0."""
    coordinator, data = _plug(hass, standard_config_entry)
    coordinator._apply_data({"current_a": 1.274, "power_w": 288.0, "switch_state": 1})
    assert data["current_a"] == 1.274
    coordinator._apply_data({"switch_state": 0})
    assert data["current_a"] == 0.0


async def test_switching_on_restores_the_last_real_current(
    hass: HomeAssistant, standard_config_entry: MockConfigEntry
) -> None:
    """The plug reports 0.308 A once and never again: the entity must not stay 0 A."""
    coordinator, data = _plug(hass, standard_config_entry)
    coordinator._apply_data({"current_a": 0.308, "power_w": 3.0, "switch_state": 0})
    assert data["current_a"] == 0.0
    coordinator._apply_data({"switch_state": 1, "power_w": 70.0})
    assert data["current_a"] == 0.308


async def test_unknown_switch_state_leaves_the_current_alone(
    hass: HomeAssistant, standard_config_entry: MockConfigEntry
) -> None:
    """Never seen switch_state: a frame without it changes nothing about current_a."""
    coordinator, data = _plug(hass, standard_config_entry)
    coordinator._apply_data({"current_a": 0.279, "power_w": 1.0})
    coordinator._apply_data({"power_w": 0.0})
    assert data["current_a"] == 0.279
    assert "switch_state" not in data


async def test_a_real_current_in_the_switch_on_frame_is_kept(
    hass: HomeAssistant, standard_config_entry: MockConfigEntry
) -> None:
    """The plug's own reading wins over the remembered one on the same frame."""
    coordinator, data = _plug(hass, standard_config_entry)
    coordinator._apply_data({"current_a": 0.308, "switch_state": 0})
    coordinator._apply_data({"switch_state": 1, "current_a": 1.274})
    assert data["current_a"] == 1.274


async def test_switch_on_with_nothing_remembered_adds_no_current(
    hass: HomeAssistant, standard_config_entry: MockConfigEntry
) -> None:
    """A first frame with switch_state 1 and no current has no value to restore."""
    coordinator, data = _plug(hass, standard_config_entry)
    coordinator._apply_data({"switch_state": 1, "power_w": 70.0})
    assert "current_a" not in data


async def test_poll_with_the_relay_off_publishes_zero_current(
    hass: HomeAssistant, standard_config_entry: MockConfigEntry
) -> None:
    """Standard Mode: 0.28 A in the quota with switchSta 0 reads 0 A."""
    coordinator, _ = _plug(hass, standard_config_entry)
    coordinator._http_client = MagicMock()
    coordinator._http_client.get_quota_all = AsyncMock(
        return_value={"2_1.current": 280, "2_1.watts": 10, "2_1.switchSta": 0}
    )
    data = await coordinator._async_update_data()
    assert data["current_a"] == 0.0


async def test_poll_after_switch_on_restores_the_last_current(
    hass: HomeAssistant, standard_config_entry: MockConfigEntry
) -> None:
    """Standard Mode: relay on again, the quota lacks current: 0.28 A returns."""
    coordinator, _ = _plug(hass, standard_config_entry)
    coordinator._http_client = MagicMock()
    coordinator._http_client.get_quota_all = AsyncMock(
        side_effect=[
            {"2_1.current": 280, "2_1.switchSta": 0},
            {"2_1.watts": 700, "2_1.switchSta": 1},
        ]
    )
    off = await coordinator._async_update_data()
    assert off["current_a"] == 0.0
    on = await coordinator._async_update_data()
    assert on["current_a"] == 0.28
