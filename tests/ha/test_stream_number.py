"""Tests for Stream AC Pro number entities - backup reserve SET via WSS proto.

Covers the SET path that Issue #98 fixed: the Stream backup-reserve number
must build a protobuf frame on the verified ConfigWrite write path
(cmd_func=254, cmd_id=17) and hand it to the coordinator's proto SET sender.
cmd_id=18 is the device reply/ack id, not the write id.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    DEVICE_TYPE_STREAM,
    STREAM_NUMBERS,
    filter_defs_for_serial,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.proto_encoding import encode_field_varint
from custom_components.ecoflow_energy.number import (
    EcoFlowNumber,
    _get_number_defs,
)

from .conftest import MOCK_STREAM_DEVICE


class TestStreamNumberDefs:
    def test_stream_returns_stream_numbers(self):
        defs = _get_number_defs(DEVICE_TYPE_STREAM)
        assert defs is STREAM_NUMBERS

    def test_stream_has_backup_reserve(self):
        defs = _get_number_defs(DEVICE_TYPE_STREAM)
        keys = {d.key for d in defs}
        assert "backup_reserve" in keys

    def test_soc_limit_controls_are_bk31_only(self):
        bk31 = _get_number_defs(DEVICE_TYPE_STREAM, "BK31TEST00000001")
        other = _get_number_defs(DEVICE_TYPE_STREAM, "BK11TEST00000001")

        assert {definition.key for definition in bk31} == {
            "stream_charge_limit",
            "stream_discharge_limit",
            "backup_reserve",
        }
        assert {definition.key for definition in other} == {"backup_reserve"}
        assert filter_defs_for_serial(other, "BK01TEST00000001") == []


class TestStreamBackupReserveSet:
    """The number entity routes a value through build_stream_backup_reserve_payload
    and the coordinator's proto SET sender, with the #98-verified cmd_id=17."""

    def _make_entity(
        self, hass, entry,
    ) -> tuple[EcoFlowNumber, EcoFlowDeviceCoordinator]:
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_STREAM_DEVICE,
        )
        coordinator._device_data = {"backup_reserve_pct": 20}
        coordinator.async_set_updated_data(dict(coordinator._device_data))
        defn = next(d for d in STREAM_NUMBERS if d.key == "backup_reserve")
        entity = EcoFlowNumber(coordinator, defn)
        entity.async_write_ha_state = MagicMock()
        return entity, coordinator

    async def test_set_builds_cmd_id_17_payload(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """A write on the Stream backup_reserve number sends a proto SET whose
        header carries cmd_id=17 and field 102 = requested value (#98)."""
        entity, coordinator = self._make_entity(hass, enhanced_config_entry)
        coordinator.async_send_proto_set_command = AsyncMock(return_value=True)

        await entity.async_set_native_value(50.0)

        coordinator.async_send_proto_set_command.assert_called_once()
        payload = coordinator.async_send_proto_set_command.call_args[0][0]
        assert isinstance(payload, bytes)

        # Decode the frame at field level (robust against byte-offset drift):
        # the outer envelope must carry cmd_func=254 / cmd_id=17 (ConfigWrite
        # SET), and the inner pdata field 102 must equal the requested value.
        from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
            decode_header_message,
        )

        headers, _ = decode_header_message(payload)
        assert headers, "expected a decodable header frame"
        header = headers[0]
        assert int(header["cmd_func"]) == 254
        assert int(header["cmd_id"]) == 17
        # Regression guard for #98: the reply id 18 must never be used as the SET.
        assert int(header["cmd_id"]) != 18
        pdata = bytes.fromhex(header["pdata"])
        # field 102, wire-type 0 (varint): tag = (102 << 3) | 0 = 816 -> b"\xb0\x06"
        assert b"\xb0\x06\x32" in pdata  # field 102 = 0x32 = 50

    async def test_set_uses_device_sn(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        entity, coordinator = self._make_entity(hass, enhanced_config_entry)
        coordinator.async_send_proto_set_command = AsyncMock(return_value=True)

        await entity.async_set_native_value(40.0)

        payload = coordinator.async_send_proto_set_command.call_args[0][0]
        assert coordinator.device_sn.encode("ascii") in payload

    async def test_failed_set_no_optimistic_update(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        entity, coordinator = self._make_entity(hass, enhanced_config_entry)
        coordinator.async_send_proto_set_command = AsyncMock(return_value=False)

        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(60.0)

        # SET failed -> original value retained, no optimistic override
        assert coordinator.data["backup_reserve_pct"] == 20


class TestStreamSocLimitSet:
    """Charge/discharge numbers preserve the captured grouped configuration."""

    def _make_entity(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        key: str,
        data: dict[str, int] | None = None,
    ) -> tuple[EcoFlowNumber, EcoFlowDeviceCoordinator]:
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, MOCK_STREAM_DEVICE)
        values = data or {
            "max_charge_soc_pct": 95,
            "min_discharge_soc_pct": 20,
            "backup_reserve_pct": 23,
        }
        coordinator._device_data = dict(values)
        coordinator.async_set_updated_data(dict(values))
        definition = next(item for item in STREAM_NUMBERS if item.key == key)
        entity = EcoFlowNumber(coordinator, definition)
        entity.async_write_ha_state = MagicMock()
        coordinator.async_send_proto_set_command = AsyncMock(return_value=True)
        return entity, coordinator

    @staticmethod
    def _sent_pdata(coordinator: EcoFlowDeviceCoordinator) -> tuple[dict, bytes]:
        from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
            decode_header_message,
        )

        payload = coordinator.async_send_proto_set_command.call_args.args[0]
        headers, _ = decode_header_message(payload)
        return headers[0], bytes.fromhex(headers[0]["pdata"])

    async def test_charge_limit_preserves_discharge_and_backup(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "stream_charge_limit"
        )

        await entity.async_set_native_value(90)

        header, pdata = self._sent_pdata(coordinator)
        assert header["from"] == "ios"
        assert encode_field_varint(33, 90) in pdata
        assert encode_field_varint(34, 20) in pdata
        assert encode_field_varint(102, 23) in pdata
        assert coordinator.data["max_charge_soc_pct"] == 90

    async def test_raising_discharge_moves_backup_to_three_points_above(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "stream_discharge_limit"
        )

        await entity.async_set_native_value(21)

        _header, pdata = self._sent_pdata(coordinator)
        assert encode_field_varint(34, 21) in pdata
        assert encode_field_varint(102, 24) in pdata
        assert coordinator.data["min_discharge_soc_pct"] == 21
        assert coordinator.data["backup_reserve_pct"] == 24

    async def test_lowering_discharge_does_not_lower_backup(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        entity, coordinator = self._make_entity(
            hass,
            enhanced_config_entry,
            "stream_discharge_limit",
            {
                "max_charge_soc_pct": 95,
                "min_discharge_soc_pct": 21,
                "backup_reserve_pct": 24,
            },
        )

        await entity.async_set_native_value(20)

        _header, pdata = self._sent_pdata(coordinator)
        assert encode_field_varint(34, 20) in pdata
        assert encode_field_varint(102, 24) in pdata

    async def test_missing_companion_is_not_guessed(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        entity, coordinator = self._make_entity(
            hass,
            enhanced_config_entry,
            "stream_charge_limit",
            {"max_charge_soc_pct": 95, "min_discharge_soc_pct": 20},
        )

        with pytest.raises(HomeAssistantError) as err:
            await entity.async_set_native_value(90)

        assert err.value.translation_key == "set_command_not_ready"
        coordinator.async_send_proto_set_command.assert_not_called()

    async def test_rejects_a_charge_limit_below_backup(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "stream_charge_limit"
        )

        with pytest.raises(HomeAssistantError) as err:
            await entity.async_set_native_value(22)

        assert err.value.translation_key == "set_value_rejected"
        coordinator.async_send_proto_set_command.assert_not_called()

    async def test_concurrent_limits_preserve_both_changes(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        charge_entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "stream_charge_limit"
        )
        discharge_definition = next(
            item
            for item in STREAM_NUMBERS
            if item.key == "stream_discharge_limit"
        )
        discharge_entity = EcoFlowNumber(coordinator, discharge_definition)
        discharge_entity.async_write_ha_state = MagicMock()

        first_started = asyncio.Event()
        release_first = asyncio.Event()
        payloads: list[bytes] = []

        async def send(payload: bytes, label: str) -> bool:
            assert label == "stream_soc_limits"
            payloads.append(payload)
            if len(payloads) == 1:
                first_started.set()
                await release_first.wait()
            return True

        coordinator.async_send_proto_set_command = AsyncMock(side_effect=send)
        charge_task = asyncio.create_task(charge_entity.async_set_native_value(90))
        await first_started.wait()
        discharge_task = asyncio.create_task(
            discharge_entity.async_set_native_value(21)
        )
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(charge_task, discharge_task)

        from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
            decode_header_message,
        )

        headers, _ = decode_header_message(payloads[1])
        pdata = bytes.fromhex(headers[0]["pdata"])
        assert encode_field_varint(33, 90) in pdata
        assert encode_field_varint(34, 21) in pdata
        assert encode_field_varint(102, 24) in pdata
