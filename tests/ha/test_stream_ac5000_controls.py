"""Write-path tests for the STREAM AC 5000 (ES22) controls.

Every control here builds a `cmd_func=254 / cmd_id=38` config write and hands
it to the coordinator's proto SET sender. The frames themselves are checked
against the app's own bytes in `tests/test_stream_ac5000_commands.py`; these
tests cover what the entity does with them, which is where the device-specific
awkwardness lives: the SoC limits share one field, and a power setpoint is
really a rewrite of a scheduled task.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    STREAMAC5000_NUMBERS,
    STREAMAC5000_SELECTS,
    STREAMAC5000_SWITCHES,
)
from custom_components.ecoflow_energy.ecoflow.stream_ac5000_commands import TASK_REMOVE
from custom_components.ecoflow_energy.switch import EcoFlowSwitch
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
    encode_varint,
)
from custom_components.ecoflow_energy.number import EcoFlowNumber
from custom_components.ecoflow_energy.select import EcoFlowSelect

from custom_components.ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import (
    parse_stream_ac5000_message,
)

from .test_stream_ac5000_entities import ES22_DEVICE

TASK_FRAMES = (
    Path(__file__).parent.parent
    / "fixtures"
    / "stream_ac5000"
    / "es22_task_frames_masked.json"
)

# A task the device already reports: discharge, 00:00-23:00, 600 W.
REPORTED: dict[str, Any] = {
    "max_charge_soc_pct": 90,
    "min_discharge_soc_pct": 15,
    "work_mode": "custom",
    "scheduled_discharge_power_w": 600,
    "scheduled_discharge_start_min": 0,
    "scheduled_discharge_end_min": 1380,
    "scheduled_discharge_enabled": True,
}


def _coordinator(
    hass: HomeAssistant, entry: MockConfigEntry, data: dict[str, Any] | None = None
) -> EcoFlowDeviceCoordinator:
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, ES22_DEVICE)
    coordinator._device_data = dict(REPORTED if data is None else data)
    coordinator.async_set_updated_data(dict(coordinator._device_data))
    coordinator.async_send_proto_set_command = AsyncMock(return_value=True)
    return coordinator


def _number(coordinator: EcoFlowDeviceCoordinator, key: str) -> EcoFlowNumber:
    defn = next(d for d in STREAMAC5000_NUMBERS if d.key == key)
    entity = EcoFlowNumber(coordinator, defn)
    entity.async_write_ha_state = MagicMock()
    return entity


def _select(coordinator: EcoFlowDeviceCoordinator, key: str) -> EcoFlowSelect:
    defn = next(d for d in STREAMAC5000_SELECTS if d.key == key)
    entity = EcoFlowSelect(coordinator, defn)
    entity.async_write_ha_state = MagicMock()
    return entity


def _sent(coordinator: EcoFlowDeviceCoordinator) -> tuple[dict, bytes]:
    """Return the header and pdata of the single frame that was sent."""
    coordinator.async_send_proto_set_command.assert_called_once()
    payload = coordinator.async_send_proto_set_command.call_args[0][0]
    assert isinstance(payload, bytes)
    headers, _ = decode_header_message(payload)
    assert headers
    return headers[0], bytes.fromhex(headers[0]["pdata"])


def _field_10(pdata: bytes) -> dict[int, int]:
    """Return config field 10 of a write payload as {subfield: value}."""
    for number, value in _walk(pdata):
        if number == 10 and isinstance(value, bytes):
            return {sub: val for sub, val in _walk(value) if isinstance(val, int)}
    raise AssertionError("no config field 10 in this payload")


def _walk(buf: bytes):
    """Yield (field number, value) for one level of a protobuf message."""
    offset = 0
    while offset < len(buf):
        key, offset = _varint(buf, offset)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, offset = _varint(buf, offset)
        elif wire == 2:
            length, offset = _varint(buf, offset)
            value, offset = buf[offset : offset + length], offset + length
        else:  # pragma: no cover - not present in config writes
            raise AssertionError(f"unexpected wire type {wire}")
        yield number, value


def _varint(buf: bytes, offset: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, offset


def _pdata_of(call) -> bytes:
    """Return the pdata of one recorded send, for multi-frame writes."""
    headers, _ = decode_header_message(call[0][0])
    assert headers
    return bytes.fromhex(headers[0]["pdata"])


def _last_pdata(coordinator: EcoFlowDeviceCoordinator) -> bytes:
    """The task itself, when a removal of the other kind went out first."""
    return _pdata_of(coordinator.async_send_proto_set_command.call_args_list[-1])


def _config_field(pdata: bytes) -> int:
    """pdata always opens with field 1 naming the config field being written."""
    assert pdata[0] == 0x08
    return pdata[1]


def _switch(coordinator: EcoFlowDeviceCoordinator, key: str) -> EcoFlowSwitch:
    defn = next(d for d in STREAMAC5000_SWITCHES if d.key == key)
    entity = EcoFlowSwitch(coordinator, defn)
    entity.async_write_ha_state = MagicMock()
    return entity


def _suspending_sender(coordinator: EcoFlowDeviceCoordinator) -> list[bytes]:
    """Replace the sender with one that yields, and collect the pdata it sent.

    An `AsyncMock` completes without ever handing control back to the event
    loop, so two concurrent writes could not interleave against one and a race
    test would pass whether or not the sequence is serialised.
    """
    sent: list[bytes] = []

    async def send(payload: bytes, label: str) -> bool:
        headers, _ = decode_header_message(payload)
        sent.append(bytes.fromhex(headers[0]["pdata"]))
        await asyncio.sleep(0)
        return True

    coordinator.async_send_proto_set_command = send
    return sent


def _task_op_and_kind(pdata: bytes) -> tuple[int, int]:
    """Read `39.1.1` operation and `39.1.2` task type out of a task write."""
    for operation in (1, 2, 3):
        for kind in (1, 2):
            if bytes([0x08, operation, 0x10, kind]) in pdata:
                return operation, kind
    raise AssertionError("not a scheduled-task frame")


class TestWorkModeSelect:
    async def test_writes_a_config_field_25_frame(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry)
        entity = _select(coordinator, "work_mode")

        await entity.async_select_option("self_powered")

        header, pdata = _sent(coordinator)
        assert int(header["cmd_func"]) == 254
        assert int(header["cmd_id"]) == 38
        assert _config_field(pdata) == 25
        assert pdata[-1] == 0  # self_powered
        assert coordinator.data["work_mode"] == "self_powered"

    async def test_failed_write_raises_and_keeps_the_device_value(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry)
        coordinator.async_send_proto_set_command = AsyncMock(return_value=False)
        entity = _select(coordinator, "work_mode")

        with pytest.raises(HomeAssistantError):
            await entity.async_select_option("self_powered")

        assert coordinator.data["work_mode"] == "custom"


class TestSocLimitNumbers:
    async def test_charge_limit_carries_the_current_discharge_limit(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Config field 29 holds both, so the untouched one travels along."""
        coordinator = _coordinator(hass, enhanced_config_entry)
        entity = _number(coordinator, "max_charge_soc_pct")

        await entity.async_set_native_value(85)

        header, pdata = _sent(coordinator)
        assert int(header["cmd_id"]) == 38
        assert _config_field(pdata) == 29
        # field 29 = {1: charge, 2: discharge}
        assert pdata.endswith(bytes([0x08, 85, 0x10, 15]))

    async def test_discharge_limit_carries_the_current_charge_limit(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry)
        entity = _number(coordinator, "min_discharge_soc_pct")

        await entity.async_set_native_value(20)

        _header, pdata = _sent(coordinator)
        assert pdata.endswith(bytes([0x08, 90, 0x10, 20]))

    async def test_refuses_when_the_counterpart_is_unknown(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Guessing it would change a limit the user never touched.

        Reported as not-ready rather than unsupported: it clears with the next
        status frame, and "unsupported" sends the user looking for a device
        limitation that does not exist.
        """
        coordinator = _coordinator(hass, enhanced_config_entry, data={})
        entity = _number(coordinator, "max_charge_soc_pct")

        with pytest.raises(HomeAssistantError) as err:
            await entity.async_set_native_value(85)

        assert err.value.translation_key == "set_command_not_ready"
        coordinator.async_send_proto_set_command.assert_not_called()

    async def test_a_limit_the_device_would_reject_is_not_sent(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The device refuses a pair where the limits cross.

        Rejected rather than not-ready: both values are known and the pair is
        the problem, so retrying later would not help.
        """
        coordinator = _coordinator(hass, enhanced_config_entry)
        entity = _number(coordinator, "max_charge_soc_pct")

        with pytest.raises(HomeAssistantError) as err:
            await entity.async_set_native_value(10)

        assert err.value.translation_key == "set_value_rejected"
        coordinator.async_send_proto_set_command.assert_not_called()

    @pytest.mark.parametrize(
        ("key", "value", "counterpart", "expected"),
        [
            ("max_charge_soc_pct", 85.0, "min_discharge_soc_pct",
             bytes([0x08, 85, 0x10, 15])),
            ("min_discharge_soc_pct", 20.0, "max_charge_soc_pct",
             bytes([0x08, 90, 0x10, 20])),
        ],
    )
    async def test_a_float_counterpart_still_writes(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        key: str,
        value: float,
        counterpart: str,
        expected: bytes,
    ) -> None:
        """The counterpart is a float for several seconds after any write.

        `number.set_value` hands Home Assistant a float and the optimistic
        write stores exactly that until the device echoes the change, so a
        strict int check would refuse every consecutive write in that window.
        """
        data = dict(REPORTED)
        data[counterpart] = float(data[counterpart])
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        entity = _number(coordinator, key)

        await entity.async_set_native_value(value)

        _header, pdata = _sent(coordinator)
        assert pdata.endswith(expected)

    async def test_reserve_level_refuses_before_the_flag_is_reported(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Field 30 carries the on/off flag with the level, so both are needed."""
        coordinator = _coordinator(hass, enhanced_config_entry, data={})
        entity = _number(coordinator, "backup_reserve")

        with pytest.raises(HomeAssistantError) as err:
            await entity.async_set_native_value(40)

        assert err.value.translation_key == "set_command_not_ready"
        coordinator.async_send_proto_set_command.assert_not_called()

    async def test_a_float_reserve_still_toggles_the_switch(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Same problem one platform over: the level feeds the switch."""
        from custom_components.ecoflow_energy.const import STREAMAC5000_SWITCHES
        from custom_components.ecoflow_energy.switch import EcoFlowSwitch

        data = dict(REPORTED, backup_reserve_enabled=True, backup_reserve_pct=30.0)
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        defn = next(d for d in STREAMAC5000_SWITCHES if d.key == "backup_reserve_switch")
        entity = EcoFlowSwitch(coordinator, defn)
        entity.async_write_ha_state = MagicMock()

        await entity.async_turn_off()

        _header, pdata = _sent(coordinator)
        assert pdata == bytes([0x08, 30, 0xF2, 0x01, 0x04, 0x08, 0, 0x10, 30])

    async def test_switch_refuses_before_the_level_is_reported(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The level travels with the flag, so it cannot be guessed."""
        from custom_components.ecoflow_energy.const import STREAMAC5000_SWITCHES
        from custom_components.ecoflow_energy.switch import EcoFlowSwitch

        coordinator = _coordinator(hass, enhanced_config_entry, data={})
        defn = next(d for d in STREAMAC5000_SWITCHES if d.key == "backup_reserve_switch")
        entity = EcoFlowSwitch(coordinator, defn)
        entity.async_write_ha_state = MagicMock()

        with pytest.raises(HomeAssistantError) as err:
            await entity.async_turn_off()

        assert err.value.translation_key == "set_command_not_ready"
        coordinator.async_send_proto_set_command.assert_not_called()


class TestPowerSetpoints:
    async def test_discharge_power_rewrites_the_reported_task(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry)
        entity = _number(coordinator, "scheduled_discharge_power_w")

        await entity.async_set_native_value(300)

        header, pdata = _sent(coordinator)
        assert int(header["cmd_id"]) == 38
        assert _config_field(pdata) == 39
        # operation 2 (update), type 2 (discharge)
        assert bytes([0x08, 2, 0x10, 2]) in pdata
        # always a whole-day window
        assert encode_field_bytes(7, encode_varint((1439 << 16) | 0)) in pdata
        # field 9 = {1: 300}
        assert encode_field_bytes(9, encode_field_varint(1, 300)) in pdata

    async def test_a_device_without_a_task_gets_one_for_the_whole_day(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """A setpoint means nothing without a task, so one is added."""
        coordinator = _coordinator(
            hass, enhanced_config_entry, data={"work_mode": "custom"}
        )
        entity = _number(coordinator, "scheduled_discharge_power_w")

        await entity.async_set_native_value(500)

        _header, pdata = _sent(coordinator)
        # operation 1 (add), not 2 (update)
        assert bytes([0x08, 1, 0x10, 2]) in pdata
        # 00:00 to 23:59
        assert encode_field_bytes(7, encode_varint((1439 << 16) | 0)) in pdata

    async def test_a_cleared_task_still_builds_an_enabled_one(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The state a deleted task leaves behind: keys present but None."""
        cleared = {
            "work_mode": "custom",
            "scheduled_discharge_power_w": None,
            "scheduled_discharge_enabled": None,
            "scheduled_discharge_start_min": None,
            "scheduled_discharge_end_min": None,
            "scheduled_charge_soc_target": None,
        }
        coordinator = _coordinator(hass, enhanced_config_entry, data=cleared)
        entity = _number(coordinator, "scheduled_discharge_power_w")

        await entity.async_set_native_value(500)

        _header, pdata = _sent(coordinator)
        # enabled = 1, its inverse = 0
        assert bytes.fromhex("18012000") in pdata
        assert bytes([0x08, 1, 0x10, 2]) in pdata
        assert encode_field_bytes(7, encode_varint((1439 << 16) | 0)) in pdata

    async def test_a_power_write_enables_the_task(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        data = dict(REPORTED, scheduled_discharge_enabled=False)
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        entity = _number(coordinator, "scheduled_discharge_power_w")

        await entity.async_set_native_value(300)

        _header, pdata = _sent(coordinator)
        assert bytes.fromhex("18012000") in pdata

    async def test_a_power_write_covers_the_whole_day(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Whatever window the device reports, the setpoint applies now."""
        data = dict(
            REPORTED,
            scheduled_discharge_start_min=540,
            scheduled_discharge_end_min=630,
        )
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        entity = _number(coordinator, "scheduled_discharge_power_w")

        await entity.async_set_native_value(300)

        _header, pdata = _sent(coordinator)
        assert encode_field_bytes(7, encode_varint((1439 << 16) | 0)) in pdata
        assert encode_field_bytes(7, encode_varint((630 << 16) | 540)) not in pdata

    async def test_the_charge_target_soc_is_carried_over(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """It decides what charging does, so a power change must not reset it."""
        data = dict(REPORTED, scheduled_charge_soc_target=80)
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        entity = _number(coordinator, "scheduled_charge_power_w")

        await entity.async_set_native_value(600)

        pdata = _last_pdata(coordinator)
        assert encode_field_varint(2, 80) in pdata
        assert encode_field_varint(2, 100) not in pdata

    async def test_charge_power_is_per_device(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """A charge task names the device it applies to, a discharge one does not."""
        coordinator = _coordinator(hass, enhanced_config_entry)
        entity = _number(coordinator, "scheduled_charge_power_w")

        await entity.async_set_native_value(600)

        pdata = _last_pdata(coordinator)
        assert bytes([0x08, 1, 0x10, 1]) in pdata  # add, charge
        assert ES22_DEVICE["sn"].encode() in pdata

    async def test_zero_is_writable(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """A 0 W discharge task parks the battery; no task at all does not."""
        coordinator = _coordinator(hass, enhanced_config_entry)
        entity = _number(coordinator, "scheduled_discharge_power_w")

        await entity.async_set_native_value(0)

        _header, pdata = _sent(coordinator)
        assert bytes.fromhex("4a020800") in pdata

    async def test_the_other_kind_is_removed_first(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Two whole-day tasks overlap and the device then acts on neither."""
        data = dict(REPORTED, scheduled_charge_power_w=600)
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        entity = _number(coordinator, "scheduled_discharge_power_w")

        await entity.async_set_native_value(300)

        sent = coordinator.async_send_proto_set_command.call_args_list
        assert len(sent) == 2
        remove = _pdata_of(sent[0])
        # operation 3 (remove), type 1 (charge), and it goes out first
        assert bytes([0x08, 3, 0x10, 1]) in remove
        write = _pdata_of(sent[1])
        assert bytes([0x08, 2, 0x10, 2]) in write

    async def test_a_parked_charge_task_from_a_real_frame_is_removed_first(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The whole chain from the 2026-08-08 incident, on the real frame.

        An optimiser parked this device with a 0 W charge task. A task at 0 W
        omits its watts, so the readback carried no power for it, so every
        later discharge write skipped the removal and landed on top of it. The
        device sat at its 200 W base output for 39 minutes while Home Assistant
        reported success on every write.

        Driven from the captured frame rather than from a hand-written state,
        because the defect was in what the frame decodes to. Seeding the state
        directly would pass either way.
        """
        frames = json.loads(TASK_FRAMES.read_text(encoding="utf-8"))["frames"]
        parsed = parse_stream_ac5000_message(bytes.fromhex(frames[1]["hex"]))
        assert parsed is not None
        coordinator = _coordinator(
            hass, enhanced_config_entry, data=dict(parsed, work_mode="custom")
        )
        entity = _number(coordinator, "scheduled_discharge_power_w")

        await entity.async_set_native_value(300)

        sent = coordinator.async_send_proto_set_command.call_args_list
        assert len(sent) == 2, "the parked charge task was not removed"
        # operation 3 (remove) naming task 1, the parked charge task
        assert bytes([0x08, 3, 0x10, 1]) in _pdata_of(sent[0])

    async def test_the_removal_names_the_number_the_device_reported(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The app numbers tasks its own way, so the derived number can miss.

        On 2026-08-08 it removed the charge task numbered 1 and added its
        discharge task at 1 in the same frame.
        """
        data = dict(
            REPORTED, scheduled_charge_power_w=600, scheduled_charge_task_slot=2
        )
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        entity = _number(coordinator, "scheduled_discharge_power_w")

        await entity.async_set_native_value(300)

        sent = coordinator.async_send_proto_set_command.call_args_list
        # operation 3 (remove) naming 2, the number reported, not the kind's 1
        assert bytes([0x08, 3, 0x10, 2]) in _pdata_of(sent[0])

    async def test_the_update_names_the_number_the_device_reported(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Same argument as the removal, on the frame that replaces the task."""
        data = dict(REPORTED, scheduled_discharge_task_slot=1)
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        entity = _number(coordinator, "scheduled_discharge_power_w")

        await entity.async_set_native_value(300)

        # operation 2 (update) naming 1, the number reported, not the kind's 2
        assert bytes([0x08, 2, 0x10, 1]) in _last_pdata(coordinator)

    async def test_an_add_is_never_given_a_number_to_reuse(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """A number is only ever known when this kind's power was reported.

        That is exactly when the operation is an update, so an add always falls
        back to the number derived from the kind.
        """
        coordinator = _coordinator(hass, enhanced_config_entry, data={"work_mode": "custom"})
        entity = _number(coordinator, "scheduled_charge_power_w")

        await entity.async_set_native_value(300)

        # operation 1 (add), type 1 (charge)
        assert bytes([0x08, 1, 0x10, 1]) in _last_pdata(coordinator)

    async def test_nothing_is_removed_when_there_is_no_other_task(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry)
        entity = _number(coordinator, "scheduled_discharge_power_w")

        await entity.async_set_native_value(300)

        assert len(coordinator.async_send_proto_set_command.call_args_list) == 1

    async def test_the_removed_task_stops_being_reported(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The device never retracts a deleted task, so this has to.

        It stops mentioning the task instead, and the parser's clear-everything
        branch needs an empty task list, which the replacement task prevents.
        """
        data = dict(
            REPORTED,
            scheduled_charge_power_w=600,
            scheduled_charge_enabled=True,
            scheduled_charge_start_min=0,
            scheduled_charge_end_min=1439,
            scheduled_charge_soc_target=80,
            scheduled_charge_task_slot=1,
        )
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        entity = _number(coordinator, "scheduled_discharge_power_w")

        await entity.async_set_native_value(300)

        for key in (
            "scheduled_charge_power_w",
            "scheduled_charge_enabled",
            "scheduled_charge_start_min",
            "scheduled_charge_end_min",
            "scheduled_charge_task_slot",
        ):
            assert coordinator.data[key] is None
            assert coordinator.device_data[key] is None
        # The app's charge limit. Clearing it too would reset a task set to
        # stop at 80% into charging to 100% on the next charge write.
        assert coordinator.data["scheduled_charge_soc_target"] == 80
        assert coordinator.data["scheduled_discharge_power_w"] == 300

    async def test_a_removed_task_is_not_removed_twice(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        data = dict(REPORTED, scheduled_charge_power_w=600)
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        entity = _number(coordinator, "scheduled_discharge_power_w")

        await entity.async_set_native_value(300)
        coordinator.async_send_proto_set_command.reset_mock()
        await entity.async_set_native_value(400)

        assert len(coordinator.async_send_proto_set_command.call_args_list) == 1

    async def test_a_failed_removal_does_not_write_the_new_task(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Writing anyway would leave exactly the overlapping pair."""
        data = dict(REPORTED, scheduled_charge_power_w=600)
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        coordinator.async_send_proto_set_command.return_value = False
        entity = _number(coordinator, "scheduled_discharge_power_w")

        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(300)

        assert len(coordinator.async_send_proto_set_command.call_args_list) == 1
        # The task may well still be there, so it stays reported.
        assert coordinator.data["scheduled_charge_power_w"] == 600

    async def test_warns_when_the_mode_cannot_act_on_it(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The write succeeds and the device ignores it, which is worth saying."""
        data = dict(REPORTED, work_mode="self_powered")
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        entity = _number(coordinator, "scheduled_discharge_power_w")

        await entity.async_set_native_value(300)

        coordinator.async_send_proto_set_command.assert_called_once()
        assert "custom mode" in caplog.text

    async def test_no_warning_in_custom_mode(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry)
        entity = _number(coordinator, "scheduled_discharge_power_w")

        await entity.async_set_native_value(300)

        assert "custom mode" not in caplog.text

    async def test_failed_write_raises_and_keeps_the_device_value(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry)
        coordinator.async_send_proto_set_command = AsyncMock(return_value=False)
        entity = _number(coordinator, "scheduled_discharge_power_w")

        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(300)

        assert coordinator.data["scheduled_discharge_power_w"] == 600


class TestConcurrentWrites:
    """Two controls writing one config field at the same moment.

    Home Assistant runs service calls to different entities as concurrent
    tasks, and every write on this device reads what the device currently
    reports before it can build its frame. Interleaved, each one acts on the
    state from before the other's write.
    """

    @pytest.mark.parametrize("first", ["charge", "discharge"])
    async def test_two_power_writes_never_leave_two_tasks(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry, first: str
    ) -> None:
        """The pair the remove-then-write sequence exists to prevent.

        Interleaved, the discharge write removes the charge task while the
        charge write is deciding it has no sibling to remove, and both then
        write. The device reports overlapping time periods and acts on
        neither, while both entities report success.

        Both starting orders are covered because only one of them is harmful,
        and nothing decides which one Home Assistant runs.
        """
        data = {
            "work_mode": "custom",
            "scheduled_charge_power_w": 600,
            "scheduled_charge_enabled": True,
            "scheduled_charge_start_min": 0,
            "scheduled_charge_end_min": 1439,
        }
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        sent = _suspending_sender(coordinator)

        writes = [
            _number(coordinator, "scheduled_charge_power_w").async_set_native_value(700),
            _number(coordinator, "scheduled_discharge_power_w").async_set_native_value(300),
        ]
        if first == "discharge":
            writes.reverse()
        await asyncio.gather(*writes)

        held = {1}  # the charge task the device starts with
        for pdata in sent:
            operation, kind = _task_op_and_kind(pdata)
            if operation == TASK_REMOVE:
                held.discard(kind)
            else:
                held.add(kind)
            assert len(held) <= 1, "the device was left holding both tasks"
        assert len(held) == 1

    async def test_two_soc_limit_writes_keep_both_changes(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Field 29 holds both limits, so the loser's change is reverted.

        Interleaved, each write carries the other's value from before it was
        changed, and whichever frame lands second undoes the first.
        """
        coordinator = _coordinator(hass, enhanced_config_entry)
        sent = _suspending_sender(coordinator)

        await asyncio.gather(
            _number(coordinator, "max_charge_soc_pct").async_set_native_value(85),
            _number(coordinator, "min_discharge_soc_pct").async_set_native_value(20),
        )

        # field 29 = {1: charge, 2: discharge}
        assert sent[-1].endswith(bytes([0x08, 85, 0x10, 20]))

    async def test_the_reserve_switch_and_its_level_keep_both_changes(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Field 30's two halves sit on two platforms.

        A per-platform lock could not cover this one, which is why the
        serialisation belongs to the coordinator.
        """
        data = dict(REPORTED, backup_reserve_enabled=False, backup_reserve_pct=30)
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        sent = _suspending_sender(coordinator)

        await asyncio.gather(
            _number(coordinator, "backup_reserve").async_set_native_value(60),
            _switch(coordinator, "backup_reserve_switch").async_turn_on(),
        )

        # field 30 = {1: on/off, 2: level}
        assert sent[-1].endswith(bytes([0x08, 1, 0x10, 60]))


class TestBackupSocketSwitch:
    """The one config write on this device that reads nothing before sending.

    It goes through the coordinator like every other config write here, so the
    lock can serialise it against a write that does read first. That routing is
    what these two cover: the frame has to arrive at the wire unchanged by it.
    """

    async def test_turning_it_on_writes_config_field_19(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry)
        entity = _switch(coordinator, "backup_socket_switch")

        await entity.async_turn_on()

        header, pdata = _sent(coordinator)
        assert int(header["cmd_func"]) == 254
        assert int(header["cmd_id"]) == 38
        assert _config_field(pdata) == 19
        assert pdata[-1] == 1

    async def test_a_failed_write_raises(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry)
        coordinator.async_send_proto_set_command = AsyncMock(return_value=False)
        entity = _switch(coordinator, "backup_socket_switch")

        with pytest.raises(HomeAssistantError):
            await entity.async_turn_on()


class TestGridOutputPower:
    """The grid-tied output setpoint, config field 10.

    Its bound is not the model rating but a ceiling the device reports on
    `f10.6`, and its write carries two companion values that belong to the
    unit rather than to the caller. Both are places a plausible-looking
    shortcut would be silently wrong on somebody else's hardware.
    """

    REPORTED_WITH_FIELD_10 = {
        **REPORTED,
        "max_grid_output_power_w": 2000,
        "_grid_output_field_4": 21,
        "_grid_output_field_5": 800,
        "_grid_output_ceiling_w": 2500,
    }

    async def test_the_write_reproduces_the_recorded_app_frame(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Not a hand-written expectation: this is the payload the EcoFlow
        app put on the wire for the same change on a live ES21 (#231).
        """
        recorded = json.loads(
            (
                Path(__file__).parents[1]
                / "fixtures/stream_ac5000/es21_write_frames_masked.json"
            ).read_text()
        )["frames"][1]
        expected = bytes.fromhex(
            decode_header_message(bytes.fromhex(recorded["hex"]))[0][0]["pdata"]
        )
        coordinator = _coordinator(
            hass, enhanced_config_entry, self.REPORTED_WITH_FIELD_10
        )
        entity = _number(coordinator, "max_grid_output_power_w")

        await entity.async_set_native_value(1000)

        _, pdata = _sent(coordinator)
        assert pdata == expected

    async def test_a_unit_reporting_other_companions_sends_those(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The values are read per write, not captured once at setup.

        An ES22 reported 5 and 600 where the recorded ES21 has 21 and 800, so
        a build that reused the recorded pair would be wrong on the other
        model and no assertion on the setpoint alone would catch it.
        """
        coordinator = _coordinator(
            hass,
            enhanced_config_entry,
            {
                **self.REPORTED_WITH_FIELD_10,
                "_grid_output_field_4": 5,
                "_grid_output_field_5": 600,
            },
        )
        entity = _number(coordinator, "max_grid_output_power_w")

        await entity.async_set_native_value(1000)

        _, pdata = _sent(coordinator)
        assert _field_10(pdata) == {1: 1000, 4: 5, 5: 600}

    async def test_nothing_is_sent_before_the_device_reported_the_companions(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """A guessed companion would put another unit's numbers on the wire."""
        coordinator = _coordinator(
            hass, enhanced_config_entry, {"max_grid_output_power_w": 2000}
        )
        entity = _number(coordinator, "max_grid_output_power_w")

        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(1000)

        coordinator.async_send_proto_set_command.assert_not_called()

    async def test_the_reported_ceiling_narrows_the_slider(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """An ES22 was seen at a 600 W ceiling on a unit rated 2500."""
        coordinator = _coordinator(
            hass,
            enhanced_config_entry,
            {**self.REPORTED_WITH_FIELD_10, "_grid_output_ceiling_w": 600},
        )
        entity = _number(coordinator, "max_grid_output_power_w")

        assert entity.native_max_value == 600
        assert entity.native_min_value == 0

    async def test_the_ceiling_never_widens_past_the_rating(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(
            hass,
            enhanced_config_entry,
            {**self.REPORTED_WITH_FIELD_10, "_grid_output_ceiling_w": 9999},
        )
        entity = _number(coordinator, "max_grid_output_power_w")

        assert entity.native_max_value == 2500

    async def test_the_declared_range_holds_until_a_ceiling_arrives(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The first minutes of a connection must not leave a dead slider."""
        coordinator = _coordinator(
            hass, enhanced_config_entry, {"max_grid_output_power_w": 2000}
        )
        entity = _number(coordinator, "max_grid_output_power_w")

        assert entity.native_max_value == 2500

    async def test_a_nonsense_ceiling_does_not_collapse_the_control(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(
            hass,
            enhanced_config_entry,
            {**self.REPORTED_WITH_FIELD_10, "_grid_output_ceiling_w": 0},
        )
        entity = _number(coordinator, "max_grid_output_power_w")

        assert entity.native_max_value == 2500


class TestGridInputPower:
    """Max Grid Input Power, config field 10 one subfield over.

    It shares its config field with the grid-tied output setpoint and is
    written unlike it: watts alone, with none of the companion values the
    output write carries. Getting that wrong writes the other setting.
    """

    GRID_INPUT_FRAMES = (
        Path(__file__).parent.parent
        / "fixtures"
        / "stream_ac5000"
        / "es22_grid_input_write_masked.json"
    )

    def _recorded_pdata(self, index: int) -> bytes:
        frame = json.loads(self.GRID_INPUT_FRAMES.read_text())["frames"][index]
        header = decode_header_message(bytes.fromhex(frame["hex"]))[0][0]
        return bytes.fromhex(header["pdata"])

    async def test_the_write_reproduces_the_recorded_app_frame(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The payload the EcoFlow app put on the wire for the same change on
        a live ES22, which the device then acknowledged (#284).
        """
        coordinator = _coordinator(
            hass, enhanced_config_entry, {"max_grid_input_power_w": 2500}
        )
        entity = _number(coordinator, "max_grid_input_power_w")

        await entity.async_set_native_value(1200)

        _, pdata = _sent(coordinator)
        assert pdata == self._recorded_pdata(0)

    async def test_it_sends_the_setpoint_and_nothing_else(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """A build that copied the output write would send subfield 1 and two
        companion values, changing the grid-tied output at the same time.
        """
        coordinator = _coordinator(
            hass,
            enhanced_config_entry,
            {
                "max_grid_input_power_w": 2500,
                "max_grid_output_power_w": 800,
                "_grid_output_field_4": 5,
                "_grid_output_field_5": 800,
            },
        )
        entity = _number(coordinator, "max_grid_input_power_w")

        await entity.async_set_native_value(2200)

        _, pdata = _sent(coordinator)
        assert _field_10(pdata) == {2: 2200}

    async def test_it_writes_without_waiting_for_a_reported_value(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Nothing is read from the device first, so nothing can hold it up.

        The output setpoint refuses until its companions arrive; this one has
        none, and refusing here would be a limitation nobody asked for.
        """
        coordinator = _coordinator(hass, enhanced_config_entry, {})
        entity = _number(coordinator, "max_grid_input_power_w")

        await entity.async_set_native_value(2600)

        _, pdata = _sent(coordinator)
        assert _field_10(pdata) == {2: 2600}

    async def test_the_written_value_is_shown_before_the_device_answers(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(
            hass, enhanced_config_entry, {"max_grid_input_power_w": 2500}
        )
        entity = _number(coordinator, "max_grid_input_power_w")

        await entity.async_set_native_value(1200)

        assert coordinator.data["max_grid_input_power_w"] == 1200

    async def test_a_failed_write_keeps_the_device_value(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(
            hass, enhanced_config_entry, {"max_grid_input_power_w": 2500}
        )
        coordinator.async_send_proto_set_command = AsyncMock(return_value=False)
        entity = _number(coordinator, "max_grid_input_power_w")

        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(1200)

        assert coordinator.data["max_grid_input_power_w"] == 2500

    async def test_the_range_admits_every_value_the_device_took(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """2600 W is on file as accepted on a unit rated 2500 (#284), so a
        slider stopping at the rating would refuse a write the hardware took.
        """
        coordinator = _coordinator(
            hass, enhanced_config_entry, {"max_grid_input_power_w": 2500}
        )
        entity = _number(coordinator, "max_grid_input_power_w")

        recorded = {1200, 2200, 2600, 2500}
        assert entity.native_min_value <= min(recorded)
        assert entity.native_max_value >= max(recorded)
        # A range that admits them and a step that does not would still leave
        # the recorded values unreachable from the slider.
        assert all(value % entity.native_step == 0 for value in recorded)

    async def test_the_write_goes_out_under_the_device_config_lock(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """It names config field 10, which the output setpoint also writes.

        The output write reads two values off the device and sends them back
        with its setpoint. A write outside the lock could land between that
        read and that send, so the guard is that this frame goes out while
        the lock is held rather than merely that it goes out.
        """
        coordinator = _coordinator(
            hass, enhanced_config_entry, {"max_grid_input_power_w": 2500}
        )
        held: list[bool] = []

        async def _record(*_args, **_kwargs):
            held.append(coordinator._device_config_lock.locked())
            return True

        coordinator.async_send_proto_set_command = AsyncMock(side_effect=_record)
        entity = _number(coordinator, "max_grid_input_power_w")

        await entity.async_set_native_value(1200)

        assert held == [True]

    async def test_the_output_ceiling_does_not_bound_this_one(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The reporter's own unit shows why they cannot share a bound: its
        output ceiling sits at 800 W while it charges at 2500.
        """
        coordinator = _coordinator(
            hass,
            enhanced_config_entry,
            {"max_grid_input_power_w": 2500, "_grid_output_ceiling_w": 800},
        )
        entity = _number(coordinator, "max_grid_input_power_w")

        assert entity.native_max_value == 2600
