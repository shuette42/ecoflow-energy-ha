"""Wallbox schedule entities and the shared task-list state write gate (#731)."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockEntityPlatform

from custom_components.ecoflow_energy.const import (
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_STREAM_AC5000,
    DOMAIN,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.parsers.powerocean_proto import (
    EV_TASK_LIST_KEY,
)
from custom_components.ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import (
    TASK_LIST_KEY,
)
from custom_components.ecoflow_energy.sensor import EcoFlowTaskListSensor
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

from .test_stream_ac5000_entities import _entry

TASK: dict[str, Any] = {
    "task": 1,
    "enabled": True,
    "current_a": 10,
    "window": "07:00-08:00",
    "repeat": "weekly",
    "weekdays": ["mon", "thu"],
}
STREAM_TASK: dict[str, Any] = {
    "slot": 1,
    "kind": "charge",
    "enabled": True,
    "start": "09:00",
    "end": "10:00",
    "source": "grid",
    "power_w": 2500,
    "soc_target": 95,
}


async def _setup(
    hass: HomeAssistant,
    reported: dict[str, Any],
    *,
    stream: bool = False,
) -> tuple[EcoFlowDeviceCoordinator, list[Any]]:
    device = {
        "sn": "ES22" if stream else "HJ31",
        "name": "STREAM AC 5000" if stream else "PowerOcean",
        "product_name": "STREAM AC 5000" if stream else "PowerOcean",
        "device_type": DEVICE_TYPE_STREAM_AC5000 if stream else DEVICE_TYPE_POWEROCEAN,
        "online": 1,
    }
    entry = _entry(device)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    if reported:
        coordinator._apply_data(reported)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {device["sn"]: coordinator}
    platform = MockEntityPlatform(hass, domain="sensor", platform_name=DOMAIN)
    platform.config_entry = entry
    created: list[Any] = []

    def _add(new_entities: Any, update_before_add: bool = False) -> None:
        batch = list(new_entities)
        created.extend(batch)
        hass.async_create_task(platform.async_add_entities(batch))

    await sensor_setup(hass, entry, _add)
    await hass.async_block_till_done()
    return coordinator, created


def _schedule(created: list[Any], key: str = EV_TASK_LIST_KEY) -> EcoFlowTaskListSensor:
    matches = [
        entity
        for entity in created
        if getattr(getattr(entity, "_definition", None), "key", None) == key
    ]
    assert len(matches) == 1
    sensor = matches[0]
    assert isinstance(sensor, EcoFlowTaskListSensor)
    return sensor


@pytest.mark.parametrize(
    "tasks",
    [[TASK], [TASK, {**TASK, "task": 2, "enabled": False}]],
)
async def test_wallbox_schedule_counts_enabled_tasks_and_lists_every_task(
    hass: HomeAssistant, tasks: list[dict[str, Any]]
) -> None:
    _coordinator, created = await _setup(hass, {EV_TASK_LIST_KEY: tasks})
    sensor = _schedule(created)
    assert sensor.unique_id == "HJ31_ev_scheduled_tasks"
    assert sensor.translation_key == EV_TASK_LIST_KEY
    assert sensor._definition.name == "Wallbox Charging Schedule"
    assert sensor.entity_registry_enabled_default is True
    assert sensor.native_value == 1
    state = hass.states.get(sensor.entity_id)
    assert state is not None
    assert state.state == "1"
    assert state.attributes["tasks"] == tasks


async def test_a_window_edit_with_the_same_count_updates_the_attribute(
    hass: HomeAssistant,
) -> None:
    coordinator, created = await _setup(hass, {EV_TASK_LIST_KEY: [TASK]})
    sensor = _schedule(created)
    coordinator._apply_data({EV_TASK_LIST_KEY: [dict(TASK)]})
    await hass.async_block_till_done()
    before = hass.states.get(sensor.entity_id)
    assert before is not None
    assert before.state == "1"
    assert before.attributes["tasks"] == [TASK]

    edited = {**TASK, "window": "07:30-09:00"}
    coordinator._apply_data({EV_TASK_LIST_KEY: [edited]})
    await hass.async_block_till_done()

    after = hass.states.get(sensor.entity_id)
    assert after is not None
    assert after.state == "1"
    assert after.attributes["tasks"] == [edited]


async def test_no_wallbox_schedule_entity_until_the_key_is_reported(
    hass: HomeAssistant,
) -> None:
    coordinator, created = await _setup(hass, {"ev_charge_power_w": 1200})
    assert not any(
        getattr(getattr(entity, "_definition", None), "key", None) == EV_TASK_LIST_KEY
        for entity in created
    )

    coordinator._apply_data({EV_TASK_LIST_KEY: [TASK]})
    await hass.async_block_till_done()
    sensor = _schedule(created)
    state = hass.states.get(sensor.entity_id)
    assert state is not None
    assert state.state == "1"
    assert state.attributes["tasks"] == [TASK]


async def test_stream_schedule_still_reads_its_own_list(hass: HomeAssistant) -> None:
    _coordinator, created = await _setup(
        hass,
        {TASK_LIST_KEY: [STREAM_TASK], EV_TASK_LIST_KEY: [TASK, TASK]},
        stream=True,
    )
    sensor = _schedule(created, TASK_LIST_KEY)
    assert sensor.native_value == 1
    state = hass.states.get(sensor.entity_id)
    assert state is not None
    assert state.state == "1"
    assert state.attributes["tasks"] == [STREAM_TASK]
