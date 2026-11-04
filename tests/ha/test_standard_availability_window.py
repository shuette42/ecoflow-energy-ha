"""Availability of a push-fed device in Standard Mode when its quota poll fails.

A Stream Micro (BK02, #674) pushes over MQTT in bursts while its quota poll
brings no data. Three failed polls used to mark it unavailable even with a
frame seconds old, so the unit sat unavailable most of the time while its data
kept arriving. The rule: a failed poll takes a device offline only once its
last own MQTT frame is older than the availability window. A device with no
frame in the window (a Micro after sunset, #437) still goes unavailable and
its snapshot is wiped, as before.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    STANDARD_MQTT_AVAILABILITY_WINDOW_S,
)
from custom_components.ecoflow_energy.coordinator import (
    EcoFlowDeviceCoordinator,
)

from .conftest import MOCK_DELTA_DEVICE, MOCK_STREAM_DEVICE

# Ages relative to the window: well inside, just inside, beyond.
_FRESH_S = 30.0
_LATE_BUT_INSIDE_S = STANDARD_MQTT_AVAILABILITY_WINDOW_S - 100.0
# Exactly on the boundary: a frame as old as the window still counts.
_AT_WINDOW_S = STANDARD_MQTT_AVAILABILITY_WINDOW_S
_BEYOND_S = STANDARD_MQTT_AVAILABILITY_WINDOW_S + 100.0
# Fixed clock for the age helper (test-time-mocking: never subtract from the
# real monotonic clock, a fresh CI runner can be younger than the window).
_NOW = 100_000.0


@pytest.fixture
def clock() -> Iterator[None]:
    """Pin the clock the MQTT age is read from, and only that one."""
    with patch(
        "custom_components.ecoflow_energy.coordinator.availability.time"
    ) as mock_time:
        mock_time.monotonic.return_value = _NOW
        yield


async def _coordinator(
    hass: HomeAssistant, entry: MockConfigEntry, device: dict[str, Any]
) -> EcoFlowDeviceCoordinator:
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    await coordinator.async_setup()
    return coordinator


def _event_types(coordinator: EcoFlowDeviceCoordinator) -> list[str]:
    return [event["type"] for event in coordinator._event_log]


@pytest.mark.usefixtures("mock_iot_api", "mock_mqtt_client", "clock")
class TestStandardAvailabilityWindow:
    """A recent own MQTT frame keeps a device available through failed polls."""

    def test_window_is_thirty_minutes(self) -> None:
        """The window decided for #674: twice the reported 15 minute interval."""
        assert STANDARD_MQTT_AVAILABILITY_WINDOW_S == 1800.0

    @pytest.mark.parametrize(
        "device", [MOCK_DELTA_DEVICE, MOCK_STREAM_DEVICE], ids=["delta", "stream"]
    )
    @pytest.mark.parametrize("age", [_FRESH_S, _LATE_BUT_INSIDE_S, _AT_WINDOW_S])
    async def test_empty_answer_with_recent_mqtt_stays_available(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_http_client,
        device: dict[str, Any],
        age: float,
    ) -> None:
        """The #674 case: empty quota, frames still arriving."""
        coordinator = await _coordinator(hass, standard_config_entry, device)
        mock_http_client.get_quota_all = AsyncMock(return_value={})
        mock_http_client.last_error_code = None
        coordinator._last_mqtt_ts = _NOW - age

        for _ in range(5):
            await coordinator._async_update_data()

        assert coordinator._consecutive_http_failures == 5
        assert coordinator.device_available is True

    @pytest.mark.parametrize("error_code", ["8519", "network"])
    async def test_failed_poll_with_recent_mqtt_stays_available(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_http_client,
        error_code: str,
    ) -> None:
        """Every failure that counts toward availability is covered, not only `{}`."""
        coordinator = await _coordinator(hass, standard_config_entry, MOCK_DELTA_DEVICE)
        mock_http_client.get_quota_all = AsyncMock(return_value=None)
        mock_http_client.last_error_code = error_code
        coordinator._last_mqtt_ts = _NOW - _FRESH_S

        for _ in range(4):
            await coordinator._async_update_data()

        assert coordinator._consecutive_http_failures == 4
        assert coordinator.device_available is True

    @pytest.mark.parametrize("age", [_BEYOND_S, None], ids=["old_frame", "no_frame"])
    async def test_empty_answer_without_recent_mqtt_goes_unavailable(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_http_client,
        age: float | None,
    ) -> None:
        """#437 kept: a Micro after sunset, silent past the window, goes offline.

        The snapshot is wiped as before, so diagnostics stop showing the last
        values as current.
        """
        coordinator = await _coordinator(hass, standard_config_entry, MOCK_DELTA_DEVICE)
        mock_http_client.get_quota_all = AsyncMock(return_value={"pd.soc": 75})
        await coordinator._async_update_data()
        assert coordinator._snapshot.key_count > 0

        coordinator._last_mqtt_ts = 0.0 if age is None else _NOW - age
        mock_http_client.get_quota_all = AsyncMock(return_value={})
        mock_http_client.last_error_code = None
        for _ in range(3):
            await coordinator._async_update_data()

        assert coordinator.device_available is False
        assert coordinator._snapshot.data == {}
        assert coordinator._snapshot.key_count == 0

    async def test_mqtt_frame_resets_the_failure_count(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_http_client,
    ) -> None:
        """Two failures, a frame, two failures: the streak restarted at the frame.

        Both pairs run with the frame already beyond the window, so only the
        reset keeps the count below three.
        """
        coordinator = await _coordinator(hass, standard_config_entry, MOCK_DELTA_DEVICE)
        mock_http_client.get_quota_all = AsyncMock(return_value={})
        mock_http_client.last_error_code = None
        coordinator._last_mqtt_ts = _NOW - _BEYOND_S
        for _ in range(2):
            await coordinator._async_update_data()

        coordinator._apply_data({"soc": 80})
        coordinator._last_mqtt_ts = _NOW - _BEYOND_S
        for _ in range(2):
            await coordinator._async_update_data()

        assert coordinator._consecutive_http_failures == 2
        assert coordinator.device_available is True

    async def test_empty_answer_is_logged_apart_from_a_failure(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_http_client,
    ) -> None:
        """`http_empty` for `{}`, `http_fail` for no answer; both still count.

        The diagnostics download is how a reporter's next file confirms the
        empty-answer hypothesis for #674.
        """
        coordinator = await _coordinator(hass, standard_config_entry, MOCK_DELTA_DEVICE)
        coordinator._event_log.clear()

        mock_http_client.get_quota_all = AsyncMock(return_value={})
        mock_http_client.last_error_code = None
        await coordinator._async_update_data()
        assert _event_types(coordinator) == ["http_empty"]

        mock_http_client.get_quota_all = AsyncMock(return_value=None)
        mock_http_client.last_error_code = "8519"
        await coordinator._async_update_data()
        assert _event_types(coordinator) == ["http_empty", "http_fail"]
        assert coordinator._consecutive_http_failures == 2
