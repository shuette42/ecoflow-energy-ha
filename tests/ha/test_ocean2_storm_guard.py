"""Ocean 2 Storm Guard: coordinator write, read-back and switch (PLAN-192, #145).

The payload builder and the action acknowledgement parser are unit-tested in
`tests/test_ocean2_commands.py`; the read-back key in
`tests/test_ocean2_proto_parser.py`. These tests prove the wiring around them:
what the write publishes, that only the next full snapshot confirms it (the
acknowledgement does not), what a missing read-back or a refusal puts back, how
the write queues behind a Backup Reserve write, and the switch that sits on it.

Time is the hand-driven loop clock of the reserve tests, so "35 s later" is
`timers.advance(35)`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_OCEAN2,
    DOMAIN,
    MODE_ENHANCED,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.coordinator.set_commands import (
    DeviceValueNotReported,
)
from custom_components.ecoflow_energy.ecoflow.ocean2_commands import (
    ACTION_STORM,
    build_ocean2_storm_guard_payload,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)
from custom_components.ecoflow_energy.switch import (
    EcoFlowSwitch,
    _get_switch_defs,
)
from custom_components.ecoflow_energy.switch import (
    async_setup_entry as switch_setup,
)

from .conftest import add_entities_collector
from .test_ocean2_backup_reserve_coordinator import (
    SNAPSHOT_5_80,
    _integration_warnings,
    _settle,
    _Timers,
    _write_held_in_publish,
)

_COMMANDS_TIME = "custom_components.ecoflow_energy.ecoflow.ocean2_commands.time"
SERIAL = "RE11TEST00000001"
SET_REPLY_TOPIC = f"/app/user123/{SERIAL}/thing/property/set_reply"


@pytest.fixture
def timers(hass: HomeAssistant) -> Iterator[_Timers]:
    clock = _Timers()
    with patch.object(hass.loop, "call_later", side_effect=clock.call_later):
        yield clock


def _device(serial: str) -> dict[str, Any]:
    return {
        "sn": serial,
        "name": "",
        "product_name": "",
        "device_type": DEVICE_TYPE_OCEAN2,
        "online": 1,
    }


def _coordinator(
    hass: HomeAssistant, serial: str = SERIAL
) -> tuple[EcoFlowDeviceCoordinator, MagicMock, MockConfigEntry]:
    device = _device(serial)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [device],
        },
        unique_id="test@example.com",
    )
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    mqtt = MagicMock()
    mqtt.is_connected.return_value = True
    mqtt.send_proto_set.return_value = True
    mqtt.send_get_all.return_value = True
    coordinator._mqtt_client = mqtt
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {serial: coordinator}
    return coordinator, mqtt, entry


def _action_ack(action_id: int, cmd_ok: bool) -> bytes:
    """A 254/37 acknowledgement; a refusal is a payload without `cmd_ok`."""
    pdata = encode_field_varint(1, action_id)
    if cmd_ok:
        pdata += encode_field_varint(2, 1)
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 254)
        + encode_field_varint(9, 37)
        + encode_field_varint(12, 1)
    )
    return encode_field_bytes(1, header)


async def _deliver(hass: HomeAssistant, coordinator: Any, payload: bytes) -> None:
    """Deliver a set_reply the way the MQTT thread does, then let the loop run."""
    coordinator._on_mqtt_message(SET_REPLY_TOPIC, payload)
    await asyncio.sleep(0)
    await _settle(hass)


class TestStormGuardWrite:
    async def test_on_publishes_the_builder_frame_seeds_and_arms_the_readback(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        coordinator, mqtt, _ = _coordinator(hass)
        coordinator._apply_data({"storm_guard_enabled": False})

        # The builder stamps a millisecond sequence number when none is given;
        # a fixed clock makes the two frames comparable byte for byte.
        with patch(_COMMANDS_TIME) as clock:
            clock.time.return_value = 1_700_000_000.0
            ok = await coordinator.async_set_ocean2_storm_guard(True)
            expected = build_ocean2_storm_guard_payload(True, SERIAL)

        assert ok is True
        mqtt.send_proto_set.assert_called_once()
        assert mqtt.send_proto_set.call_args[0][0] == expected
        assert mqtt.send_get_all.call_count == 0  # no snapshot needed first
        assert coordinator.data["storm_guard_enabled"] is True
        assert timers.pending() == [8.0, 30.0, 35.0]

    async def test_an_ack_then_a_confirming_snapshot_keeps_the_value(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        coordinator, mqtt, _ = _coordinator(hass)
        coordinator._apply_data({"storm_guard_enabled": False})
        await coordinator.async_set_ocean2_storm_guard(True)
        await _deliver(hass, coordinator, _action_ack(ACTION_STORM, True))

        timers.advance(8)
        await _settle(hass)
        assert mqtt.send_get_all.call_count == 1  # the +8 s request
        coordinator._apply_data({"storm_guard_enabled": True})  # the snapshot
        timers.advance(28)
        await _settle(hass)

        assert coordinator.data["storm_guard_enabled"] is True
        assert not any(e["type"] == "readback_missing" for e in coordinator.event_log)
        assert mqtt.send_get_all.call_count == 1  # no retry once confirmed
        assert coordinator._ocean2_writes == {}

    async def test_an_ack_without_a_snapshot_reverts_and_logs_the_event(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        """The acknowledgement is not the state: only a snapshot confirms.

        The acknowledgement comes after the +8 s request here, so nothing but
        the acknowledgement itself could mark the write as confirmed.
        """
        coordinator, mqtt, _ = _coordinator(hass)
        coordinator._apply_data({"storm_guard_enabled": False})
        await coordinator.async_set_ocean2_storm_guard(True)
        timers.advance(9)
        await _settle(hass)
        await _deliver(hass, coordinator, _action_ack(ACTION_STORM, True))
        assert coordinator.data["storm_guard_enabled"] is True

        timers.advance(27)
        await _settle(hass)

        assert coordinator.data["storm_guard_enabled"] is False
        assert mqtt.send_get_all.call_count == 2  # +8 s request, +30 s retry
        missing = [e for e in coordinator.event_log if e["type"] == "readback_missing"]
        assert [e["detail"] for e in missing] == ["action=17"]
        assert coordinator._ocean2_writes == {}

    async def test_a_reply_for_another_action_is_not_ours(
        self,
        hass: HomeAssistant,
        timers: _Timers,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        coordinator, _, _ = _coordinator(hass)
        coordinator._apply_data({"storm_guard_enabled": False})
        await coordinator.async_set_ocean2_storm_guard(True)

        with caplog.at_level(logging.DEBUG):
            await _deliver(hass, coordinator, _action_ack(99, False))

        assert coordinator.data["storm_guard_enabled"] is True
        assert not any(e["type"] == "set_rejected" for e in coordinator.event_log)
        assert not _integration_warnings(caplog)

        # Control: the same refusal for our own action id takes the write back.
        await _deliver(hass, coordinator, _action_ack(ACTION_STORM, False))

        assert coordinator.data["storm_guard_enabled"] is False
        rejected = [e for e in coordinator.event_log if e["type"] == "set_rejected"]
        assert [e["detail"] for e in rejected] == ["action=17"]

    async def test_nothing_is_sent_unreported_or_on_a_model_without_a_write(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        coordinator, mqtt, _ = _coordinator(hass)
        with pytest.raises(DeviceValueNotReported):
            await coordinator.async_set_ocean2_storm_guard(True)
        mqtt.send_proto_set.assert_not_called()

        other, other_mqtt, _ = _coordinator(hass, serial="RE17TEST00000001")
        other._apply_data({"storm_guard_enabled": False})
        with pytest.raises(ValueError):
            await other.async_set_ocean2_storm_guard(True)
        other_mqtt.send_proto_set.assert_not_called()

    async def test_the_write_queues_behind_a_reserve_write_and_both_confirm_alone(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        coordinator, mqtt, _ = _coordinator(hass)
        coordinator._apply_data({**SNAPSHOT_5_80, "storm_guard_enabled": False})
        task, release = await _write_held_in_publish(
            hass, coordinator, mqtt, 23, SNAPSHOT_5_80
        )
        held = mqtt.send_proto_set
        published: list[Any] = []

        def _count(*args: Any, **kwargs: Any) -> bool:
            published.append(args)
            return bool(held(*args, **kwargs))

        mqtt.send_proto_set = _count
        storm = hass.async_create_task(coordinator.async_set_ocean2_storm_guard(True))
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            assert not storm.done()
            assert published == []  # the reserve still holds the lock
        finally:
            release.set()
        assert await task is True
        assert await storm is True

        assert len(published) == 1
        assert coordinator.data["backup_reserve_pct"] == 23
        assert coordinator.data["storm_guard_enabled"] is True
        # One read-back each: the storm write did not replace the reserve's.
        assert set(coordinator._ocean2_writes) == {
            "backup_reserve_pct",
            "storm_guard_enabled",
        }
        assert timers.pending() == [8.0, 8.0, 30.0, 30.0, 35.0, 35.0]


class TestStormGuardSwitch:
    async def test_the_switch_waits_for_the_report_and_is_a_config_entity(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _, entry = _coordinator(hass)
        created: list[Any] = []

        await switch_setup(hass, entry, add_entities_collector(created))
        assert created == []

        coordinator._apply_data({"storm_guard_enabled": False})
        coordinator.async_update_listeners()
        await hass.async_block_till_done()

        assert [e._definition.key for e in created] == ["storm_guard"]
        switch = created[0]
        assert isinstance(switch, EcoFlowSwitch)
        assert switch.unique_id == f"{SERIAL}_storm_guard"
        assert switch.entity_category is EntityCategory.CONFIG
        assert switch.entity_registry_enabled_default is True
        # The model gate sits in front of the accessory gate.
        assert _get_switch_defs(DEVICE_TYPE_OCEAN2, "RE17TEST00000001") == []

    async def test_turning_on_writes_true_and_a_rollback_lifts_the_lock(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _, entry = _coordinator(hass)
        coordinator._apply_data({"storm_guard_enabled": False})
        created: list[Any] = []
        await switch_setup(hass, entry, add_entities_collector(created))
        switch = created[0]
        switch.entity_id = "switch.ocean_2_storm_guard"
        switch.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        write = AsyncMock(return_value=True)

        with patch.object(coordinator, "async_set_ocean2_storm_guard", write):
            await switch.async_turn_on()

        write.assert_awaited_once_with(True)
        assert switch.is_on is True  # optimistic

        # The read-back did not come: the coordinator puts the old value back
        # and advances the generation, which must lift the 5 s lock.
        coordinator.data["storm_guard_enabled"] = False
        coordinator._write_rollback_generation += 1
        switch._handle_coordinator_update()

        assert switch.is_on is False
