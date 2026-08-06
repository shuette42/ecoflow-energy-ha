"""Write-path tests for the STREAM AC 5000 (ES22) controls.

Every control here builds a `cmd_func=254 / cmd_id=38` config write and hands
it to the coordinator's proto SET sender. The frames themselves are checked
against the app's own bytes in `tests/test_stream_ac5000_commands.py`; these
tests cover what the entity does with them, which is where the device-specific
awkwardness lives: the SoC limits share one field, and a power setpoint is
really a rewrite of a scheduled task.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    STREAMAC5000_NUMBERS,
    STREAMAC5000_SELECTS,
)
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

from .test_stream_ac5000_entities import ES22_DEVICE

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
        entity = _number(coordinator, "max_discharging_power")

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
        entity = _number(coordinator, "max_discharging_power")

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
        entity = _number(coordinator, "max_discharging_power")

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
        entity = _number(coordinator, "max_discharging_power")

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
        entity = _number(coordinator, "max_discharging_power")

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
        entity = _number(coordinator, "max_grid_charging_power")

        await entity.async_set_native_value(600)

        pdata = _last_pdata(coordinator)
        assert encode_field_varint(2, 80) in pdata
        assert encode_field_varint(2, 100) not in pdata

    async def test_charge_power_is_per_device(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """A charge task names the device it applies to, a discharge one does not."""
        coordinator = _coordinator(hass, enhanced_config_entry)
        entity = _number(coordinator, "max_grid_charging_power")

        await entity.async_set_native_value(600)

        pdata = _last_pdata(coordinator)
        assert bytes([0x08, 1, 0x10, 1]) in pdata  # add, charge
        assert ES22_DEVICE["sn"].encode() in pdata

    async def test_zero_is_writable(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """A 0 W discharge task parks the battery; no task at all does not."""
        coordinator = _coordinator(hass, enhanced_config_entry)
        entity = _number(coordinator, "max_discharging_power")

        await entity.async_set_native_value(0)

        _header, pdata = _sent(coordinator)
        assert bytes.fromhex("4a020800") in pdata

    async def test_the_other_kind_is_removed_first(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Two whole-day tasks overlap and the device then acts on neither."""
        data = dict(REPORTED, scheduled_charge_power_w=600)
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        entity = _number(coordinator, "max_discharging_power")

        await entity.async_set_native_value(300)

        sent = coordinator.async_send_proto_set_command.call_args_list
        assert len(sent) == 2
        remove = _pdata_of(sent[0])
        # operation 3 (remove), type 1 (charge), and it goes out first
        assert bytes([0x08, 3, 0x10, 1]) in remove
        write = _pdata_of(sent[1])
        assert bytes([0x08, 2, 0x10, 2]) in write

    async def test_nothing_is_removed_when_there_is_no_other_task(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry)
        entity = _number(coordinator, "max_discharging_power")

        await entity.async_set_native_value(300)

        assert len(coordinator.async_send_proto_set_command.call_args_list) == 1

    async def test_a_failed_removal_does_not_write_the_new_task(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Writing anyway would leave exactly the overlapping pair."""
        data = dict(REPORTED, scheduled_charge_power_w=600)
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        coordinator.async_send_proto_set_command.return_value = False
        entity = _number(coordinator, "max_discharging_power")

        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(300)

        assert len(coordinator.async_send_proto_set_command.call_args_list) == 1

    async def test_warns_when_the_mode_cannot_act_on_it(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The write succeeds and the device ignores it, which is worth saying."""
        data = dict(REPORTED, work_mode="self_powered")
        coordinator = _coordinator(hass, enhanced_config_entry, data=data)
        entity = _number(coordinator, "max_discharging_power")

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
        entity = _number(coordinator, "max_discharging_power")

        await entity.async_set_native_value(300)

        assert "custom mode" not in caplog.text

    async def test_failed_write_raises_and_keeps_the_device_value(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        coordinator = _coordinator(hass, enhanced_config_entry)
        coordinator.async_send_proto_set_command = AsyncMock(return_value=False)
        entity = _number(coordinator, "max_discharging_power")

        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(300)

        assert coordinator.data["scheduled_discharge_power_w"] == 600
