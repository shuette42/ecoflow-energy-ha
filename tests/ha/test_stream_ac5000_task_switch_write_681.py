"""Tests for the STREAM AC 5000 task on/off write in the coordinator (#681).

The frames are @sciurius' toggle on #681: task 1 is a grid charge task
07:00-08:00 (the kind's settable task), task 2 a Solar only charge task
08:00-10:00 that the app switched off at 12:29:04.092 and on again at
12:31:00.897. The get-all reply 0.126 s after the ON write still showed task 2
off: every first report after a write is stale.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.coordinator.set_commands import (
    STREAM_AC5000_TASK_ENABLED_HOLD_S,
    DeviceValueNotReported,
)
from custom_components.ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import (
    parse_stream_ac5000_message,
)
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)
from custom_components.ecoflow_energy.ecoflow.stream_ac5000_commands import (
    TASK_UPDATE,
    build_task_payload,
)

from .test_stream_ac5000_entities import ES22_DEVICE

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "stream_ac5000"
    / "es22_681_task_toggle_masked.json"
)
FRAMES = {f["role"]: f for f in json.loads(FIXTURE.read_text())["frames"]}
# The recording masks the serial as 16 X and the entries carry it, so the
# coordinator writes under the same serial to compare byte for byte.
MASKED_DEVICE: dict[str, Any] = {**ES22_DEVICE, "sn": "X" * 16}
HOLD_KEY = "scheduled_charge_enabled"
CLOCK = "custom_components.ecoflow_energy.coordinator.state_apply.time.monotonic"


def _report(role: str) -> dict[str, Any]:
    parsed = parse_stream_ac5000_message(
        bytes.fromhex(FRAMES[f"get_reply_{role}"]["hex"])
    )
    assert parsed is not None
    return parsed


def _coordinator(
    hass: HomeAssistant, entry: MockConfigEntry, role: str
) -> EcoFlowDeviceCoordinator:
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, MASKED_DEVICE)
    coordinator._apply_data(_report(role))
    coordinator.async_send_proto_set_command = AsyncMock(return_value=True)
    return coordinator


def _fields(data: bytes) -> list[tuple[int, int | bytes, bytes]]:
    """Top-level (field number, value, raw bytes incl. tag) of a message."""

    def varint(i: int) -> tuple[int, int]:
        value = shift = 0
        while True:
            byte = data[i]
            i += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if byte < 0x80:
                return value, i

    out: list[tuple[int, int | bytes, bytes]] = []
    i = 0
    while i < len(data):
        start = i
        key, i = varint(i)
        if key & 7 == 0:
            value, i = varint(i)
            out.append((key >> 3, value, data[start:i]))
        else:
            assert key & 7 == 2, "only varint and length-delimited fields here"
            length, i = varint(i)
            out.append((key >> 3, data[i : i + length], data[start : i + length]))
            i += length
    return out


def _task_entries(frame: bytes) -> list[bytes]:
    """Each `39.1` task entry of a config write, as raw bytes incl. its tag."""
    headers, _ = decode_header_message(frame)
    assert headers
    pdata = bytes.fromhex(headers[0]["pdata"])
    (container,) = [value for number, value, _raw in _fields(pdata) if number == 39]
    assert isinstance(container, bytes)
    return [raw for number, _value, raw in _fields(container) if number == 1]


def _sent_entries(coordinator: EcoFlowDeviceCoordinator) -> list[list[bytes]]:
    sender = coordinator.async_send_proto_set_command
    assert isinstance(sender, AsyncMock)
    return [_task_entries(call[0][0]) for call in sender.call_args_list]


def _task_one(power_w: int, enabled: bool) -> bytes:
    """Task 1 as the power write updates it: grid, 07:00-08:00, limit 95 %."""
    (entry,) = _task_entries(
        build_task_payload(
            "charge",
            420,
            480,
            power_w,
            "X" * 16,
            operation=TASK_UPDATE,
            enabled=enabled,
            charge_soc_target=95,
            task_slot=1,
        )
    )
    return entry


class TestSwitchWriteEqualsTheAppWrite:
    async def test_off_sends_task_two_of_the_app_write(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry, "task2_on_stale")

        assert await coordinator.async_set_stream_ac5000_task_enabled(
            2, "charge", False
        )

        _task_one_app, task_two_app = _task_entries(
            bytes.fromhex(FRAMES["set_task2_off"]["hex"])
        )
        assert _sent_entries(coordinator) == [[task_two_app]]
        # Task 2 is not the kind's settable task: nothing to seed or hold.
        assert coordinator.data[HOLD_KEY] is True
        assert coordinator._schedule_armed_latch == {}

    async def test_on_sends_task_two_of_the_app_write(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry, "task2_off")

        assert await coordinator.async_set_stream_ac5000_task_enabled(2, "charge", True)

        _task_one_app, task_two_app = _task_entries(
            bytes.fromhex(FRAMES["set_task2_on"]["hex"])
        )
        assert _sent_entries(coordinator) == [[task_two_app]]


class TestHoldAgainstTheStaleReport:
    async def test_power_write_after_the_stale_report_echoes_the_switch(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The automation on #681: switch the task, then set its power."""
        coordinator = _coordinator(hass, enhanced_config_entry, "task2_on_stale")
        before = time.monotonic()
        assert await coordinator.async_set_stream_ac5000_task_enabled(
            1, "charge", False
        )
        after = time.monotonic()
        (written, until) = coordinator._schedule_armed_latch[HOLD_KEY]
        assert written is False
        assert before + STREAM_AC5000_TASK_ENABLED_HOLD_S <= until
        assert until <= after + STREAM_AC5000_TASK_ENABLED_HOLD_S

        # Every recorded report lists task 1 on: to this write it is stale.
        with patch(CLOCK, return_value=until - 0.001):
            coordinator._apply_data(_report("task2_off"))
        assert coordinator.data[HOLD_KEY] is False

        assert await coordinator.async_set_stream_ac5000_task_power("charge", 1800)

        switch_entry, power_entry = (calls[0] for calls in _sent_entries(coordinator))
        assert switch_entry == _task_one(2500, enabled=False)
        assert power_entry == _task_one(1800, enabled=False)

    async def test_the_hold_ends_after_its_window(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry, "task2_on_stale")
        assert await coordinator.async_set_stream_ac5000_task_enabled(
            1, "charge", False
        )
        (_written, until) = coordinator._schedule_armed_latch[HOLD_KEY]

        with patch(CLOCK, return_value=until):
            coordinator._apply_data(_report("task2_on_again"))

        assert coordinator.data[HOLD_KEY] is True
        assert coordinator._schedule_armed_latch == {}

    async def test_an_agreeing_report_ends_the_hold_at_once(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry, "task2_on_stale")
        assert await coordinator.async_set_stream_ac5000_task_enabled(
            1, "charge", False
        )
        (_written, until) = coordinator._schedule_armed_latch[HOLD_KEY]
        confirming = _report("task2_off")
        confirming[HOLD_KEY] = False

        with patch(CLOCK, return_value=until - 1.0):
            coordinator._apply_data(confirming)
            coordinator._apply_data(_report("task2_on_again"))

        assert coordinator.data[HOLD_KEY] is True


def _edit_echo(coordinator: EcoFlowDeviceCoordinator, **values: Any) -> None:
    echo = coordinator.data["scheduled_task_echo"]
    coordinator.data["scheduled_task_echo"] = {**echo, 2: {**echo[2], **values}}


class TestRefusals:
    @pytest.mark.parametrize("key", ["scheduled_tasks", "scheduled_task_echo"])
    async def test_no_decoded_list(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry, key: str
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry, "task2_off")
        coordinator.data[key] = None

        # Named as the list, so the switch can tell "not read yet" from a
        # task that is gone.
        with pytest.raises(DeviceValueNotReported, match="^scheduled_tasks$"):
            await coordinator.async_set_stream_ac5000_task_enabled(2, "charge", True)
        assert _sent_entries(coordinator) == []

    @pytest.mark.parametrize(("slot", "kind"), [(3, "charge"), (2, "discharge")])
    async def test_no_task_with_this_number_and_kind(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        slot: int,
        kind: str,
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry, "task2_off")

        with pytest.raises(DeviceValueNotReported):
            await coordinator.async_set_stream_ac5000_task_enabled(slot, kind, True)
        assert _sent_entries(coordinator) == []

    @pytest.mark.parametrize(
        "edit",
        [{"time_mode": 2}, {"time_mode": None}, {"charge_source": 2}],
        ids=["weekly", "repeat_unknown", "source_2"],
    )
    async def test_unsupported_repeat_or_source(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        edit: dict[str, Any],
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry, "task2_off")
        _edit_echo(coordinator, **edit)

        with pytest.raises(ValueError):
            await coordinator.async_set_stream_ac5000_task_enabled(2, "charge", True)
        assert _sent_entries(coordinator) == []

    async def test_a_number_listed_twice(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry, "task2_off")
        tasks = coordinator.data["scheduled_tasks"]
        coordinator.data["scheduled_tasks"] = [
            *tasks,
            {**tasks[1], "kind": "discharge", "source": None},
        ]

        with pytest.raises(ValueError):
            await coordinator.async_set_stream_ac5000_task_enabled(2, "charge", True)
        assert _sent_entries(coordinator) == []

    async def test_during_unload(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry, "task2_off")
        coordinator._shutdown = True

        assert not await coordinator.async_set_stream_ac5000_task_enabled(
            2, "charge", True
        )
        assert _sent_entries(coordinator) == []


class TestFailedSend:
    async def test_seeds_and_holds_nothing(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry, "task2_on_stale")
        coordinator.async_send_proto_set_command = AsyncMock(return_value=False)

        assert not await coordinator.async_set_stream_ac5000_task_enabled(
            1, "charge", False
        )

        assert coordinator.data[HOLD_KEY] is True
        assert coordinator.device_data[HOLD_KEY] is True
        assert coordinator._schedule_armed_latch == {}


class TestRemovalDropsTheHold:
    async def test_a_removed_task_takes_its_hold_with_it(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """A later task of the kind must not inherit the removed task's hold."""
        coordinator = _coordinator(hass, enhanced_config_entry, "task2_on_stale")
        assert await coordinator.async_set_stream_ac5000_task_enabled(
            1, "charge", False
        )

        assert await coordinator._remove_stream_ac5000_task("charge", 1)

        assert coordinator._schedule_armed_latch == {}
