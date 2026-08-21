"""Tests for the Stream (BK-series) number entities and their SET paths.

Two write paths are covered. Backup reserve is the one Issue #98 fixed: it
must build a protobuf frame on the verified ConfigWrite write path
(cmd_func=254, cmd_id=17) and hand it to the coordinator's proto SET sender.
cmd_id=18 is the device reply/ack id, not the write id.

The charge and discharge limits are the second. The device holds them
together with backup reserve as one grouped setting, so the two values a
write does not change travel with it and must come from live telemetry rather
than from a default. Both limits are Enhanced Mode only and confirmed on the
Stream AC Pro alone, so the entity set is pinned per serial prefix here: Home
Assistant keeps an entity in the registry after a later release stops creating
it, which makes a wrongly created write entity permanent for that owner.
"""

from __future__ import annotations

import asyncio
from typing import Any
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SECRET_KEY,
    CONF_USER_ID,
    DEVICE_TYPE_STREAM,
    DOMAIN,
    MODE_ENHANCED,
    MODE_STANDARD,
    STREAM_NUMBERS,
    STREAMAC5000_NUMBERS,
    filter_defs_for_serial,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.proto_encoding import encode_field_varint
from custom_components.ecoflow_energy.number import (
    EcoFlowNumber,
    _get_number_defs,
    async_setup_entry as number_setup,
)

from .conftest import MOCK_STREAM_DEVICE
from .test_stream_ac5000_entities import ES22_DEVICE

BK31_DEVICE: dict[str, Any] = {
    "sn": "BK31TEST00000001",
    "name": "Stream AC Pro",
    "product_name": "Stream AC Pro",
    "device_type": DEVICE_TYPE_STREAM,
    "online": 1,
}

_AC_PRO_NUMBER_KEYS = {
    "stream_charge_limit",
    "stream_discharge_limit",
    "led_brightness",
}


class TestStreamNumberDefs:
    def test_stream_has_backup_reserve(self):
        defs = _get_number_defs(DEVICE_TYPE_STREAM)
        keys = {d.key for d in defs}
        assert "backup_reserve" in keys

    def test_ac_pro_controls_are_bk31_only(self):
        bk31 = _get_number_defs(DEVICE_TYPE_STREAM, "BK31TEST00000001")
        other = _get_number_defs(DEVICE_TYPE_STREAM, "BK11TEST00000001")

        assert {definition.key for definition in bk31} == {
            "stream_charge_limit",
            "stream_discharge_limit",
            "led_brightness",
            "backup_reserve",
        }
        assert {definition.key for definition in other} == {"backup_reserve"}
        assert filter_defs_for_serial(other, "BK01TEST00000001") == []

    def test_an_unknown_serial_gets_no_write_entities(self):
        """Fail closed: a serial that cannot be tied to a confirmed model must
        not unlock a write. An empty serial is the case that matters, because
        it is what every caller that has no device to hand passes."""
        for serial in ("", "0000", "BK31"[::-1]):
            keys = {d.key for d in _get_number_defs(DEVICE_TYPE_STREAM, serial)}
            assert keys == {"backup_reserve"}, serial

    @pytest.mark.parametrize(
        ("key", "state_key", "minimum", "maximum", "step"),
        [
            ("stream_charge_limit", "max_charge_soc_pct", 3, 100, 1),
            ("stream_discharge_limit", "min_discharge_soc_pct", 0, 95, 1),
            ("led_brightness", "led_brightness", 0, 100, 5),
        ],
    )
    def test_ac_pro_number_ranges_match_the_documented_values(
        self,
        key: str,
        state_key: str,
        minimum: int,
        maximum: int,
        step: int,
    ) -> None:
        """documentation/entities/stream.md publishes these ranges, and a
        number carries no description, so the range is the only guard rail a
        user sees while dragging it."""
        definition = next(item for item in STREAM_NUMBERS if item.key == key)

        assert definition.state_key == state_key
        assert definition.min_value == minimum
        assert definition.max_value == maximum
        assert definition.step == step
        assert definition.unit == "%"
        assert definition.enhanced_only is True


class TestStreamNumberPlatformSetup:
    """AC Pro write entities are available in Enhanced Mode only."""

    @staticmethod
    def _entry(mode: str) -> MockConfigEntry:
        if mode == MODE_ENHANCED:
            return MockConfigEntry(
                domain=DOMAIN,
                title="EcoFlow Energy",
                data={
                    CONF_AUTH_METHOD: AUTH_METHOD_APP,
                    CONF_MODE: MODE_ENHANCED,
                    CONF_EMAIL: "test@example.com",
                    CONF_PASSWORD: "test_password",
                    CONF_USER_ID: "user123",
                    CONF_DEVICES: [BK31_DEVICE],
                },
                unique_id="test@example.com",
            )
        return MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_ACCESS_KEY: "test_ak",
                CONF_SECRET_KEY: "test_sk",
                CONF_MODE: MODE_STANDARD,
                CONF_DEVICES: [BK31_DEVICE],
            },
            unique_id="test_ak",
        )

    async def _setup_keys(self, hass: HomeAssistant, mode: str) -> set[str]:
        entry = self._entry(mode)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, BK31_DEVICE)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            BK31_DEVICE["sn"]: coordinator
        }
        created: list[Any] = []
        await number_setup(hass, entry, created.extend)
        return {
            entity._definition.key
            for entity in created
            if hasattr(entity, "_definition")
        }

    async def test_enhanced_mode_creates_ac_pro_numbers(
        self, hass: HomeAssistant
    ) -> None:
        assert _AC_PRO_NUMBER_KEYS <= await self._setup_keys(hass, MODE_ENHANCED)

    async def test_standard_mode_creates_no_write_numbers(
        self, hass: HomeAssistant
    ) -> None:
        keys = await self._setup_keys(hass, MODE_STANDARD)

        assert keys.isdisjoint(_AC_PRO_NUMBER_KEYS)
        assert "backup_reserve" not in keys


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


class TestStreamLedBrightnessSet:
    """The LED number writes field 384 and reads state from live field 994."""

    def _make_entity(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> tuple[EcoFlowNumber, EcoFlowDeviceCoordinator]:
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, MOCK_STREAM_DEVICE)
        coordinator._device_data = {"led_brightness": 70}
        coordinator.async_set_updated_data(dict(coordinator._device_data))
        definition = next(
            item for item in STREAM_NUMBERS if item.key == "led_brightness"
        )
        entity = EcoFlowNumber(coordinator, definition)
        entity.async_write_ha_state = MagicMock()
        return entity, coordinator

    async def test_set_builds_captured_field_384_frame(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        entity, coordinator = self._make_entity(hass, enhanced_config_entry)
        coordinator.async_send_proto_set_command = AsyncMock(return_value=True)

        await entity.async_set_native_value(10)

        from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
            decode_header_message,
        )

        payload = coordinator.async_send_proto_set_command.call_args.args[0]
        headers, _ = decode_header_message(payload)
        header = headers[0]
        assert int(header["cmd_func"]) == 254
        assert int(header["cmd_id"]) == 17
        assert header["from"] == "ios"
        assert bytes.fromhex(header["pdata"]) == b"\x80\x18\x0a"
        assert coordinator.data["led_brightness"] == 10

    async def test_failed_set_keeps_live_value(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        entity, coordinator = self._make_entity(hass, enhanced_config_entry)
        coordinator.async_send_proto_set_command = AsyncMock(return_value=False)

        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(10)

        assert coordinator.data["led_brightness"] == 70

    async def test_the_write_goes_out_under_the_device_config_lock(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Every Stream config write shares one lock, this one included.

        The lock is what keeps a grouped write from reading the device state
        while another write is in flight. A writer outside it is invisible to
        that queue, so the guard is that the frame is built and sent while the
        lock is held, not merely that a frame is sent.
        """
        entity, coordinator = self._make_entity(hass, enhanced_config_entry)
        held: list[bool] = []

        async def _record(*_args, **_kwargs):
            held.append(coordinator._device_config_lock.locked())
            return True

        coordinator.async_send_proto_set_command = AsyncMock(side_effect=_record)

        await entity.async_set_native_value(10)

        assert held == [True]

    async def test_a_value_the_device_cannot_take_is_reported_as_rejected(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """The builder's range guard must not surface as a raw ValueError.

        Home Assistant filters out-of-bounds service calls before the entity
        sees them, so this is unreachable from the slider today. It is guarded
        because the sibling limit controls translate the same failure, and an
        untranslated one would reach the user as an unhandled exception rather
        than as a rejected write.
        """
        entity, coordinator = self._make_entity(hass, enhanced_config_entry)
        coordinator.async_send_proto_set_command = AsyncMock(return_value=True)
        entity._definition = replace(entity._definition, max_value=120)

        with pytest.raises(HomeAssistantError) as err:
            await entity.async_set_native_value(120)

        assert not isinstance(err.value, ValueError)
        coordinator.async_send_proto_set_command.assert_not_called()
        assert coordinator.data["led_brightness"] == 70


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

    async def test_a_write_reaches_the_persistent_store_too(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The snapshot alone is not enough. A queued second write reads the
        store to decide what its companions should be, and the store is what
        survives the next coordinator refresh, so a value seeded only into
        `data` is restored to its old reading a moment later."""
        _, coordinator = self._make_entity(
            hass, enhanced_config_entry, "stream_charge_limit"
        )

        # Deliberately not through the entity: that one writes the store
        # itself as its optimistic update, so it would pass whether or not
        # the coordinator seeded anything. The waiter this seed exists for
        # calls the coordinator directly.
        assert await coordinator.async_set_stream_soc_limits(charge=90)

        assert coordinator._device_data["max_charge_soc_pct"] == 90
        assert coordinator._device_data["min_discharge_soc_pct"] == 20
        assert coordinator._device_data["backup_reserve_pct"] == 23

    @pytest.mark.parametrize("requested", [21, 19])
    async def test_a_discharge_write_leaves_backup_exactly_as_reported(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        requested: int,
    ) -> None:
        """Backup reserve is a companion here, not a derived value. Both
        directions send back the reading the device last gave, because any
        rule relating the two would be a claim about the device that no
        capture supports."""
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "stream_discharge_limit"
        )

        await entity.async_set_native_value(requested)

        _header, pdata = self._sent_pdata(coordinator)
        assert encode_field_varint(34, requested) in pdata
        assert encode_field_varint(102, 23) in pdata
        assert coordinator.data["min_discharge_soc_pct"] == requested
        assert coordinator.data["backup_reserve_pct"] == 23

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

    async def test_rejects_limits_that_cross(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Both entity ranges overlap, so a user can ask for a charge limit
        below the discharge limit the device is holding."""
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "stream_charge_limit"
        )

        with pytest.raises(HomeAssistantError) as err:
            await entity.async_set_native_value(19)

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
        assert encode_field_varint(102, 23) in pdata


class TestBackupReserveFloorFollowsTheDischargeLimit:
    """The reserve's lower bound is derived, not fixed at the declared 3.

    The device holds backup reserve at least three points above the discharge
    limit and carries it up whenever that limit rises (#264, measured on a
    BK31 in #98). A fixed floor offers values the device refuses: they look
    accepted for about half a minute, then live telemetry snaps the entity
    back to the value the device actually kept.

    Only the displayed bound derives. What the user picks is sent untouched,
    because a client that corrects a value before sending reads its own
    arithmetic back as if the device had confirmed it.
    """

    @staticmethod
    def _entity(
        hass: HomeAssistant,
        entry: MockConfigEntry,
        reported: dict[str, Any],
        device: dict[str, Any] | None = None,
        definitions: list[Any] | None = None,
        key: str = "backup_reserve",
    ) -> tuple[EcoFlowNumber, EcoFlowDeviceCoordinator]:
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, device or MOCK_STREAM_DEVICE
        )
        coordinator._device_data = dict(reported)
        coordinator.async_set_updated_data(dict(reported))
        definition = next(
            item for item in (definitions or STREAM_NUMBERS) if item.key == key
        )
        entity = EcoFlowNumber(coordinator, definition)
        entity.async_write_ha_state = MagicMock()
        return entity, coordinator

    async def test_a_reported_limit_lifts_the_floor(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        entity, _ = self._entity(
            hass,
            enhanced_config_entry,
            {"min_discharge_soc_pct": 20, "backup_reserve_pct": 23},
        )

        assert entity.native_min_value == 23
        assert entity.native_max_value == 95

    async def test_the_floor_follows_a_limit_change_without_a_reload(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The discharge limit is a control of its own, so it moves while HA
        runs. Home Assistant snapshots min and max on a state write, and the
        reserve itself does not move with it, so the new bound only reaches
        the frontend if the entity writes its state anyway."""
        entity, coordinator = self._entity(
            hass,
            enhanced_config_entry,
            {"min_discharge_soc_pct": 20, "backup_reserve_pct": 23},
        )
        entity._handle_coordinator_update()
        entity.async_write_ha_state.reset_mock()

        coordinator.async_set_updated_data(
            {"min_discharge_soc_pct": 40, "backup_reserve_pct": 23}
        )
        entity._handle_coordinator_update()

        assert entity.native_min_value == 43
        assert entity.async_write_ha_state.called

    async def test_an_unchanged_limit_does_not_force_a_write(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Zero-noise: a push that moves neither the value nor the bound must
        stay out of the recorder."""
        entity, coordinator = self._entity(
            hass,
            enhanced_config_entry,
            {"min_discharge_soc_pct": 20, "backup_reserve_pct": 23},
        )
        entity._handle_coordinator_update()
        entity.async_write_ha_state.reset_mock()

        coordinator.async_set_updated_data(dict(coordinator._device_data))
        entity._handle_coordinator_update()

        assert not entity.async_write_ha_state.called

    @pytest.mark.parametrize("reported", [{}, {"min_discharge_soc_pct": None}])
    async def test_without_a_usable_limit_the_declared_floor_stands(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        reported: dict[str, Any],
    ) -> None:
        """Enhanced Mode can take two minutes to deliver the first full frame.
        A control pinned shut for that window reads as broken, while one that
        is briefly too permissive costs at most a value the device corrects."""
        entity, _ = self._entity(
            hass, enhanced_config_entry, {**reported, "backup_reserve_pct": 23}
        )

        assert entity.native_min_value == 3
        assert entity.native_max_value == 95

    async def test_a_value_below_the_floor_is_sent_exactly_as_asked(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The regression this whole change must not reintroduce.

        A client-side correction was removed once already, because the value
        it invented came back through the read-back looking like a device
        confirmation. The bound is what moved here, never the write.
        """
        entity, coordinator = self._entity(
            hass,
            enhanced_config_entry,
            {"min_discharge_soc_pct": 20, "backup_reserve_pct": 23},
        )
        coordinator.async_send_proto_set_command = AsyncMock(return_value=True)

        await entity.async_set_native_value(5.0)

        from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
            decode_header_message,
        )

        payload = coordinator.async_send_proto_set_command.call_args.args[0]
        headers, _ = decode_header_message(payload)
        pdata = bytes.fromhex(headers[0]["pdata"])
        assert encode_field_varint(102, 5) in pdata
        assert encode_field_varint(102, 23) not in pdata
        assert coordinator.data["backup_reserve_pct"] == 5.0

    async def test_a_stream_without_the_confirmed_prefix_keeps_its_range(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Backup reserve exists on every BK serial, the measurement does not.

        The three-point rule was read off a BK31. Another model reporting the
        same telemetry key is not evidence that it holds there, and a floor
        invented for it would refuse values that model may well accept.
        """
        entity, _ = self._entity(
            hass,
            enhanced_config_entry,
            {"min_discharge_soc_pct": 20, "backup_reserve_pct": 23},
            device={**MOCK_STREAM_DEVICE, "sn": "BK11TEST00000001"},
        )

        assert entity.native_min_value == 3

    async def test_the_stream_ac_5000_reserve_keeps_its_declared_range(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """A different device with the same entity key and the same telemetry
        key, and no capture tying the two together."""
        entity, _ = self._entity(
            hass,
            enhanced_config_entry,
            {"min_discharge_soc_pct": 20, "backup_reserve_pct": 25},
            device=ES22_DEVICE,
            definitions=STREAMAC5000_NUMBERS,
        )

        assert entity.native_min_value == 0
        assert entity.native_max_value == 100

    async def test_an_in_flight_float_still_holds_the_floor(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """Home Assistant hands `number.set_value` a float, and the optimistic
        write stores exactly that until the device echoes the change back. A
        strict integer check on the limit would collapse the floor to the
        declared 3 for those seconds, and it would do it in the moment right
        after the write that raised the limit."""
        entity, _ = self._entity(
            hass,
            enhanced_config_entry,
            {"min_discharge_soc_pct": 40.0, "backup_reserve_pct": 23},
        )

        assert entity.native_min_value == 43

    async def test_raising_the_limit_lifts_the_floor_before_the_echo(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The whole chain, because the float is not hypothetical: the limit
        write seeds an int and the optimistic update then overwrites the same
        key with the float the service call carried."""
        entity, coordinator = self._entity(
            hass,
            enhanced_config_entry,
            {
                "max_charge_soc_pct": 95,
                "min_discharge_soc_pct": 20,
                "backup_reserve_pct": 23,
            },
        )
        coordinator.async_send_proto_set_command = AsyncMock(return_value=True)
        limit = EcoFlowNumber(
            coordinator,
            next(
                item
                for item in STREAM_NUMBERS
                if item.key == "stream_discharge_limit"
            ),
        )
        limit.async_write_ha_state = MagicMock()
        assert entity.native_min_value == 23

        await limit.async_set_native_value(40.0)

        assert isinstance(coordinator.data["min_discharge_soc_pct"], float)
        assert entity.native_min_value == 43

    async def test_another_number_on_the_same_device_keeps_its_range(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
    ) -> None:
        """The derivation belongs to backup reserve alone. The discharge limit
        is the sharpest test of that: same list, same hardware, same gate, and
        it reads the very key the floor derives from."""
        entity, _ = self._entity(
            hass,
            enhanced_config_entry,
            {"min_discharge_soc_pct": 20, "backup_reserve_pct": 23},
            key="stream_discharge_limit",
        )

        assert entity.native_min_value == 0
        assert entity.native_max_value == 95
