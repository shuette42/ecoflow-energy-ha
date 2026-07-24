"""Tests for Stream AC Pro number entities — backup reserve SET via WSS proto.

Covers the SET path that Issue #98 fixed: the Stream backup-reserve number
must build a protobuf frame on the verified ConfigWrite write path
(cmd_func=254, cmd_id=17) and hand it to the coordinator's proto SET sender.
cmd_id=18 is the device reply/ack id, not the write id.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    DEVICE_TYPE_STREAM,
    STREAM_NUMBERS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
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

        await entity.async_set_native_value(60.0)

        # SET failed -> original value retained, no optimistic override
        assert coordinator.data["backup_reserve_pct"] == 20
