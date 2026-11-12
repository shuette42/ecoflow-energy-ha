"""The STREAM AC 5000 "Scheduled Tasks" sensor (#681).

The state counts the enabled tasks and the `tasks` attribute lists every one.
The list is the parser's `scheduled_tasks` key; the sensor adds no reading of
its own, so these tests drive the coordinator the way a frame does.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ecoflow_energy.sensor import EcoFlowTaskListSensor

from .test_stream_ac5000_entities import _setup_with_coordinator

ON: dict[str, Any] = {
    "slot": 1,
    "kind": "charge",
    "enabled": True,
    "start": "07:00",
    "end": "08:00",
    "source": "grid",
    "power_w": 2500,
    "soc_target": 95,
}
OFF: dict[str, Any] = {
    "slot": 2,
    "kind": "charge",
    "enabled": False,
    "start": "08:00",
    "end": "10:00",
    "source": "solar_only",
    "power_w": None,
    "soc_target": 95,
}


async def _sensor(hass: HomeAssistant, reported: dict[str, Any] | None = None):
    coordinator, created = await _setup_with_coordinator(hass, reported)
    sensor = next(
        entity
        for entity in created
        if getattr(entity, "_definition", None) is not None
        and entity._definition.key == "scheduled_tasks"
    )
    assert isinstance(sensor, EcoFlowTaskListSensor)
    return coordinator, sensor


class TestTaskListSensor:
    @pytest.mark.parametrize(
        ("tasks", "enabled"),
        [([ON, OFF], 1), ([ON, {**OFF, "enabled": True}], 2), ([OFF], 0), ([], 0)],
    )
    async def test_the_state_counts_the_enabled_tasks(
        self, hass: HomeAssistant, tasks: list[dict[str, Any]], enabled: int
    ) -> None:
        _coordinator, sensor = await _sensor(hass, {"scheduled_tasks": tasks})
        assert sensor.native_value == enabled

    async def test_the_state_is_unknown_while_the_list_is_unknown(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, sensor = await _sensor(hass)
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {}

        coordinator.async_set_updated_data({"scheduled_tasks": [ON]})
        assert sensor.native_value == 1

        coordinator.async_set_updated_data({"scheduled_tasks": None})
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {}

    async def test_the_tasks_attribute_is_the_list(self, hass: HomeAssistant) -> None:
        _coordinator, sensor = await _sensor(hass, {"scheduled_tasks": [ON, OFF]})
        assert sensor.extra_state_attributes == {"tasks": [ON, OFF]}

    async def test_a_frame_without_the_key_keeps_the_stored_list(
        self, hass: HomeAssistant
    ) -> None:
        """A push that does not repeat the task container must not blank it."""
        coordinator, sensor = await _sensor(hass)
        coordinator._apply_data({"scheduled_tasks": [ON, OFF]})
        assert sensor.native_value == 1

        coordinator._apply_data({"soc_pct": 55.0})
        assert sensor.native_value == 1
        assert sensor.extra_state_attributes == {"tasks": [ON, OFF]}
