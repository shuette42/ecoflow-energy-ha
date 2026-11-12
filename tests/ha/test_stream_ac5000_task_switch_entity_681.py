"""The STREAM AC 5000 per-task on/off switch entity (#681).

One switch per (number, kind) in `scheduled_tasks`, added by a coordinator
listener when the list first carries it. The frames are the reporter's toggle
on #681: task 1 is the grid charge task (the kind's settable task), task 2 a
Solar only charge task the app switched off and on again. The report 0.126 s
after the ON write still listed task 2 off: every first report after a write
is stale.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntityPlatform,
)

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_DEVELOPER,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_MODE,
    CONF_SECRET_KEY,
    DEVICE_TYPE_STREAM,
    DOMAIN,
    MODE_STANDARD,
)
from custom_components.ecoflow_energy.coordinator import (
    DeviceValueNotReported,
    EcoFlowDeviceCoordinator,
)
from custom_components.ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import (
    parse_stream_ac5000_message,
)
from custom_components.ecoflow_energy.switch import EcoFlowStreamTaskSwitch
from custom_components.ecoflow_energy.switch import (
    async_setup_entry as switch_setup,
)

from .test_stream_ac5000_entities import ES22_DEVICE, _entry

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "stream_ac5000"
    / "es22_681_task_toggle_masked.json"
)
FRAMES = {f["role"]: f for f in json.loads(FIXTURE.read_text())["frames"]}
SWITCH_CLOCK = "custom_components.ecoflow_energy.switch.time.monotonic"
SN = ES22_DEVICE["sn"]
TASK2_ID = f"{SN}_scheduled_task_2_charge_enabled"


def _report(role: str) -> dict[str, Any]:
    parsed = parse_stream_ac5000_message(
        bytes.fromhex(FRAMES[f"get_reply_{role}"]["hex"])
    )
    assert parsed is not None
    return parsed


def _without_task2(role: str) -> dict[str, Any]:
    report = _report(role)
    report["scheduled_tasks"] = [
        task for task in report["scheduled_tasks"] if task["slot"] != 2
    ]
    return report


async def _platform(
    hass: HomeAssistant,
    role: str | None = None,
    entry: MockConfigEntry | None = None,
    device: dict[str, Any] = ES22_DEVICE,
) -> tuple[EcoFlowDeviceCoordinator, list[Any]]:
    """Set the switch platform up with its entities really sitting in `hass`."""
    entry = entry or _entry(device)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    if role is not None:
        coordinator._apply_data(_report(role))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {device["sn"]: coordinator}
    platform = MockEntityPlatform(hass, domain="switch", platform_name=DOMAIN)
    platform.config_entry = entry
    created: list[Any] = []

    def _add(new_entities: Any, update_before_add: bool = False) -> None:
        batch = list(new_entities)
        created.extend(batch)
        hass.async_create_task(platform.async_add_entities(batch))

    await switch_setup(hass, entry, _add)
    await hass.async_block_till_done()
    return coordinator, created


def _task_switches(created: list[Any]) -> list[EcoFlowStreamTaskSwitch]:
    return [e for e in created if isinstance(e, EcoFlowStreamTaskSwitch)]


def _one(created: list[Any], slot: int, kind: str = "charge") -> Any:
    (switch,) = [
        e for e in _task_switches(created) if (e._slot, e._kind) == (slot, kind)
    ]
    return switch


def _state(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def _apply(hass: HomeAssistant, coordinator: Any, data: dict[str, Any]) -> None:
    coordinator._apply_data(data)
    await hass.async_block_till_done()


class TestAddPath:
    async def test_the_listener_adds_a_task_once(self, hass: HomeAssistant) -> None:
        coordinator, created = await _platform(hass)
        assert _task_switches(created) == []

        await _apply(hass, coordinator, _without_task2("task2_off"))
        assert [(e._slot, e._kind) for e in _task_switches(created)] == [(1, "charge")]

        for role in ("task2_off", "task2_on_again", "task2_on_stale", "task2_off"):
            await _apply(hass, coordinator, _report(role))
        keys = [(e._slot, e._kind) for e in _task_switches(created)]
        assert sorted(keys) == [(1, "charge"), (2, "charge")]
        switch = _one(created, 2)
        assert switch.unique_id == TASK2_ID
        assert switch.translation_key == "scheduled_charge_task"
        assert switch._attr_translation_placeholders == {"slot": "2"}
        assert switch.entity_category is None
        assert switch.entity_registry_enabled_default is True

    async def test_the_kind_picks_the_name(self, hass: HomeAssistant) -> None:
        """Both keys are the ones `test_entity_translations` pins."""
        coordinator, _created = await _platform(hass)
        names = {
            kind: EcoFlowStreamTaskSwitch(coordinator, 3, kind).translation_key
            for kind in ("charge", "discharge")
        }
        assert names == {
            "charge": "scheduled_charge_task",
            "discharge": "scheduled_discharge_task",
        }

    async def test_tasks_listed_at_setup_get_their_switch_at_once(
        self, hass: HomeAssistant
    ) -> None:
        _coordinator, created = await _platform(hass, "task2_off")
        assert sorted(str(e.unique_id) for e in _task_switches(created)) == [
            f"{SN}_scheduled_task_1_charge_enabled",
            TASK2_ID,
        ]


class TestTaskGoneAndBack:
    async def test_a_task_that_leaves_goes_unavailable_and_comes_back(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, created = await _platform(hass, "task2_off")
        switch = _one(created, 2)
        entity_id = switch.entity_id
        assert _state(hass, entity_id).state == "off"

        await _apply(hass, coordinator, _without_task2("task2_on_again"))
        assert _state(hass, entity_id).state == "unavailable"
        registry = er.async_get(hass)
        assert registry.async_get_entity_id("switch", DOMAIN, TASK2_ID) == entity_id

        await _apply(hass, coordinator, _report("task2_on_again"))
        assert _state(hass, entity_id).state == "on"
        assert _one(created, 2) is switch

    async def test_unavailable_while_the_list_is_unknown(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, created = await _platform(hass, "task2_off")
        switch = _one(created, 2)
        await _apply(hass, coordinator, {"scheduled_tasks": None})
        assert _state(hass, switch.entity_id).state == "unavailable"


class TestIdentity:
    async def test_a_window_edit_keeps_the_entity(self, hass: HomeAssistant) -> None:
        coordinator, created = await _platform(hass, "task2_off")
        switch = _one(created, 2)
        assert _state(hass, switch.entity_id).attributes["start"] == "08:00"
        # Seed the write gate with an unchanged report, so the edit below is
        # written only if the gate sees the attributes, not just the state.
        await _apply(hass, coordinator, _report("task2_off"))

        edited = _report("task2_off")
        edited["scheduled_tasks"][1] = {
            **edited["scheduled_tasks"][1],
            "start": "09:00",
            "end": "11:00",
            "power_w": 1200,
        }
        await _apply(hass, coordinator, edited)

        assert _one(created, 2) is switch
        assert switch.unique_id == TASK2_ID
        attributes = _state(hass, switch.entity_id).attributes
        assert (attributes["start"], attributes["end"]) == ("09:00", "11:00")
        assert attributes["source"] == "solar_only"


class TestState:
    async def test_another_task_follows_its_list_entry(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, created = await _platform(hass, "task2_off")
        switch = _one(created, 2)
        assert switch.is_on is False
        await _apply(hass, coordinator, _report("task2_on_again"))
        assert switch.is_on is True

    async def test_the_settable_task_keeps_the_held_value_past_a_stale_report(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, created = await _platform(hass, "task2_on_stale")
        switch = _one(created, 1)
        assert switch.is_on is True
        coordinator.async_send_proto_set_command = AsyncMock(return_value=True)

        assert await coordinator.async_set_stream_ac5000_task_enabled(
            1, "charge", False
        )
        # Every recorded report lists task 1 on: to this write it is stale.
        await _apply(hass, coordinator, _report("task2_off"))
        assert coordinator.data["scheduled_tasks"][0]["enabled"] is True
        assert switch.is_on is False
        assert _state(hass, switch.entity_id).state == "off"


class TestPress:
    async def test_a_press_sends_number_kind_and_state(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, created = await _platform(hass, "task2_off")
        switch = _one(created, 2)
        with patch.object(
            coordinator,
            "async_set_stream_ac5000_task_enabled",
            AsyncMock(return_value=True),
        ) as write:
            await switch.async_turn_on()
            write.assert_awaited_once_with(2, "charge", True)
            await switch.async_turn_off()
            write.assert_awaited_with(2, "charge", False)

    @pytest.mark.parametrize(
        ("outcome", "list_known", "translation_key"),
        [
            (DeviceValueNotReported("scheduled_tasks"), False, "set_command_not_ready"),
            (
                DeviceValueNotReported("scheduled task 2 charge"),
                True,
                "set_command_gone",
            ),
            (ValueError("repeat not supported"), True, "set_value_rejected"),
            (False, True, "set_command_failed"),
        ],
    )
    async def test_each_refusal_maps_to_its_error(
        self,
        hass: HomeAssistant,
        outcome: Any,
        list_known: bool,
        translation_key: str,
    ) -> None:
        coordinator, created = await _platform(hass, "task2_off")
        switch = _one(created, 2)
        if not list_known:
            coordinator.data["scheduled_tasks"] = None
        mock = (
            AsyncMock(return_value=outcome)
            if isinstance(outcome, bool)
            else AsyncMock(side_effect=outcome)
        )
        with (
            patch.object(coordinator, "async_set_stream_ac5000_task_enabled", mock),
            pytest.raises(HomeAssistantError) as raised,
        ):
            await switch.async_turn_on()
        assert raised.value.translation_key == translation_key
        assert switch._optimistic_lock_until == 0.0


class TestLock:
    async def test_the_stale_report_after_an_on_press_keeps_the_switch_on(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, created = await _platform(hass, "task2_off")
        switch = _one(created, 2)
        with patch.object(
            coordinator,
            "async_set_stream_ac5000_task_enabled",
            AsyncMock(return_value=True),
        ):
            await switch.async_turn_on()
        pressed = time.monotonic()

        # The report 0.126 s after the ON write still lists task 2 off.
        await _apply(hass, coordinator, _report("task2_off"))
        assert switch.is_on is True
        assert _state(hass, switch.entity_id).state == "on"

        with patch(SWITCH_CLOCK, return_value=pressed + 6.0):
            await _apply(hass, coordinator, _report("task2_off"))
            assert switch.is_on is False
            assert _state(hass, switch.entity_id).state == "off"


def _standard_entry(device: dict[str, Any]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_DEVELOPER,
            CONF_ACCESS_KEY: "test_ak",
            CONF_SECRET_KEY: "test_sk",
            CONF_MODE: MODE_STANDARD,
            CONF_DEVICES: [device],
        },
        unique_id="test_ak",
    )


class TestWhoGetsIt:
    async def test_es21_gets_the_switch(self, hass: HomeAssistant) -> None:
        device = {**ES22_DEVICE, "sn": "ES21TEST00000001"}
        coordinator, created = await _platform(hass, device=device)
        await _apply(hass, coordinator, _report("task2_off"))
        assert len(_task_switches(created)) == 2

    @pytest.mark.parametrize(
        "device",
        [
            {**ES22_DEVICE, "sn": "ES99TEST00000001"},
            {
                **ES22_DEVICE,
                "sn": "BK11TEST00000001",
                "device_type": DEVICE_TYPE_STREAM,
            },
        ],
    )
    async def test_other_devices_get_none(
        self, hass: HomeAssistant, device: dict[str, Any]
    ) -> None:
        coordinator, created = await _platform(hass, device=device)
        await _apply(hass, coordinator, _report("task2_off"))
        assert _task_switches(created) == []

    async def test_standard_mode_gets_none(self, hass: HomeAssistant) -> None:
        coordinator, created = await _platform(hass, entry=_standard_entry(ES22_DEVICE))
        assert coordinator.enhanced_mode is False
        await _apply(hass, coordinator, _report("task2_off"))
        assert _task_switches(created) == []
