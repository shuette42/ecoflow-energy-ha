"""The coordinator side of the PowerPulse 2 start/stop action (ADR-009, PLAN-136).

Coordinator level only: no button platform exists yet (Phase 3b), so every
test calls `EcoFlowDeviceCoordinator.async_set_powerpulse_charge_action`
directly. An Enhanced-mode entry holds two devices, a PowerOcean and a
PowerPulse 2 (`C376`), each with its own coordinator and its own mocked MQTT
client - the command is published through the PowerOcean's client and
confirmed on the wallbox's own heartbeat, never the other way round.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_POWERPULSE2,
    DOMAIN,
    MODE_ENHANCED,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.energy_stream import (
    build_powerpulse_charge_action_payload,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

POWEROCEAN_SN = "HJ31TEST00000001"
POWERPULSE2_SN = "C376TEST00000007"
TOPIC = f"/app/device/property/{POWERPULSE2_SN}"
DEV_ADDR = 215
DEV_SN = "X" * 16

POWEROCEAN_DEVICE: dict[str, Any] = {
    "sn": POWEROCEAN_SN,
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}

POWERPULSE2_DEVICE: dict[str, Any] = {
    "sn": POWERPULSE2_SN,
    "name": "PowerPulse 2",
    "product_name": "",
    "device_type": DEVICE_TYPE_POWERPULSE2,
    "online": 1,
}


def _entry(hass: HomeAssistant, devices: list[dict[str, Any]]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: devices,
        },
        unique_id="test@example.com",
    )
    entry.add_to_hass(hass)
    return entry


def _connected_mqtt() -> MagicMock:
    mock_mqtt = MagicMock()
    mock_mqtt.is_connected.return_value = True
    mock_mqtt.send_proto_set.return_value = True
    return mock_mqtt


def _mqtt(coordinator: EcoFlowDeviceCoordinator) -> MagicMock:
    """The MagicMock installed by `_wire_entry`, typed as what it is."""
    return cast(MagicMock, coordinator._mqtt_client)


def _wire_entry(
    hass: HomeAssistant, ocean_devices: list[dict[str, Any]]
) -> tuple[MockConfigEntry, list[EcoFlowDeviceCoordinator], EcoFlowDeviceCoordinator]:
    """Build one entry with N PowerOcean devices plus one wallbox.

    Returns the entry, the list of PowerOcean coordinators (each with its own
    connected MQTT mock), and the wallbox coordinator (with its own,
    separately mocked MQTT client, so a mutation that publishes through the
    wrong client is visible as a call on the wrong mock).
    """
    devices = [*ocean_devices, POWERPULSE2_DEVICE]
    entry = _entry(hass, devices)
    oceans = []
    for device in ocean_devices:
        coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
        coordinator._mqtt_client = _connected_mqtt()
        oceans.append(coordinator)
    wallbox = EcoFlowDeviceCoordinator(hass, entry, POWERPULSE2_DEVICE)
    wallbox._mqtt_client = _connected_mqtt()
    coordinators = {c.device_sn: c for c in [*oceans, wallbox]}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinators
    return entry, oceans, wallbox


def _heartbeat_frame(plug_status: int) -> bytes:
    """A synthetic, unmasked `2/33` PowerPulse 2 heartbeat carrying one field.

    `pdata` (field 1 of the frame's header) is a single varint field 1 =
    plug_status - matches `_HEARTBEAT_FIELD_MAP[1] == ("_plug_status_raw", ...)`
    in `powerpulse_proto.py`. `cmd_func=2`, `cmd_id=33`.
    """
    pdata = encode_field_varint(1, plug_status)
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 2)
        + encode_field_varint(9, 33)
    )
    return encode_field_bytes(1, header)


def _apply_status(wallbox: EcoFlowDeviceCoordinator, plug_status: int) -> None:
    parsed = wallbox._parse_message(TOPIC, _heartbeat_frame(plug_status))
    assert parsed is not None and "ev_charge_status" in parsed
    wallbox._apply_data(parsed)


def _set_descriptor(wallbox: EcoFlowDeviceCoordinator) -> None:
    wallbox.set_device_value("ev_charger_dev_addr", DEV_ADDR)
    wallbox.set_device_value("ev_charger_sn", DEV_SN)


async def test_stop_while_charging_publishes_once_and_confirms_on_finishing(
    hass: HomeAssistant,
) -> None:
    """Stop publishes exactly once through the PowerOcean, never the wallbox.

    Also the mutation check for `POWERPULSE2_CHARGE_ACTION_CONFIRMED["stop"]`
    widened to include "charging" (mutation a): a repeated "charging" frame
    after the publish must not resolve the wait, only "finishing" may.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    ocean = oceans[0]
    _set_descriptor(wallbox)
    _apply_status(wallbox, 3)  # charging

    with patch(
        "custom_components.ecoflow_energy.ecoflow.energy_stream.time.time",
        return_value=1_700_000_000.0,
    ):
        expected_seq = int(1_700_000_000.0 * 1000) & 0x7FFFFFFF
        expected = build_powerpulse_charge_action_payload(
            "stop", DEV_ADDR, DEV_SN, seq=expected_seq
        )
        task = asyncio.create_task(wallbox.async_set_powerpulse_charge_action("stop"))
        await asyncio.sleep(0.05)

    assert _mqtt(ocean).send_proto_set.call_count == 1
    assert _mqtt(wallbox).send_proto_set.call_count == 0
    published = _mqtt(ocean).send_proto_set.call_args.args[0]
    assert published == expected

    # A repeated "charging" report must not confirm a stop.
    _apply_status(wallbox, 3)
    await asyncio.sleep(0)
    assert not task.done()

    # "finishing" does confirm it.
    _apply_status(wallbox, 6)
    await task
    assert wallbox._wallbox_action_pending is None


async def test_start_while_finishing_confirms_only_on_charging(
    hass: HomeAssistant,
) -> None:
    """Start resolves on "charging"; a "finishing" frame first must not resolve it."""
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_status(wallbox, 6)  # finishing

    task = asyncio.create_task(wallbox.async_set_powerpulse_charge_action("start"))
    await asyncio.sleep(0.05)
    assert _mqtt(oceans[0]).send_proto_set.call_count == 1

    _apply_status(wallbox, 6)  # finishing again - must not confirm a start
    # The future is checked directly: `wait_for` needs more than one loop
    # iteration to wake after `set_result`, so `task.done()` after one
    # `sleep(0)` is false either way and proves nothing.
    pending = wallbox._wallbox_action_pending
    assert pending is not None and not pending.future.done()
    assert not task.done()

    _apply_status(wallbox, 3)  # charging - confirms
    await task
    assert wallbox._wallbox_action_pending is None


async def test_stop_while_charging_is_not_confirmed_by_another_charging_frame(
    hass: HomeAssistant,
) -> None:
    """Stop resolves on "finishing" or "available"; a "charging" frame must not.

    Mutation control: widening the stop confirmation set to include
    "charging" stays green in every other test here, because the confirming
    frame always arrives first. This test feeds the wrong frame first.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_status(wallbox, 3)  # charging

    task = asyncio.create_task(wallbox.async_set_powerpulse_charge_action("stop"))
    await asyncio.sleep(0.05)
    assert _mqtt(oceans[0]).send_proto_set.call_count == 1

    _apply_status(wallbox, 3)  # still charging - must not confirm a stop
    pending = wallbox._wallbox_action_pending
    assert pending is not None and not pending.future.done()
    assert not task.done()

    _apply_status(wallbox, 1)  # available - confirms
    await task
    assert wallbox._wallbox_action_pending is None


async def test_a_cancelled_press_clears_the_record(hass: HomeAssistant) -> None:
    """A press cancelled while waiting (automation timeout, HA stop) must not
    leave the pending record behind, or every later press would be refused
    as in progress until the entry reloaded (review finding of 2026-09-11).
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_status(wallbox, 3)  # charging

    task = asyncio.create_task(wallbox.async_set_powerpulse_charge_action("stop"))
    await asyncio.sleep(0.05)
    assert _mqtt(oceans[0]).send_proto_set.call_count == 1
    assert wallbox._wallbox_action_pending is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wallbox._wallbox_action_pending is None

    # The next press goes through, and is not refused as in progress.
    task = asyncio.create_task(wallbox.async_set_powerpulse_charge_action("stop"))
    await asyncio.sleep(0.05)
    assert _mqtt(oceans[0]).send_proto_set.call_count == 2
    _apply_status(wallbox, 6)  # finishing - confirms
    await task


async def test_shutdown_while_pending_cancels_the_wait(hass: HomeAssistant) -> None:
    """Tearing the wallbox coordinator down while a press waits cancels the
    wait and clears the record; nothing is published again."""
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_status(wallbox, 3)  # charging

    task = asyncio.create_task(wallbox.async_set_powerpulse_charge_action("stop"))
    await asyncio.sleep(0.05)
    assert _mqtt(oceans[0]).send_proto_set.call_count == 1

    await wallbox.async_shutdown()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wallbox._wallbox_action_pending is None
    assert _mqtt(oceans[0]).send_proto_set.call_count == 1

    # A press on the torn-down coordinator raises rather than reporting
    # success for a command that was never sent.
    with pytest.raises(HomeAssistantError) as err:
        await wallbox.async_set_powerpulse_charge_action("stop")
    assert err.value.translation_key == "powerpulse_action_not_delivered"
    assert _mqtt(oceans[0]).send_proto_set.call_count == 1


async def test_no_confirming_frame_times_out(hass: HomeAssistant) -> None:
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_status(wallbox, 3)  # charging

    with (
        patch(
            "custom_components.ecoflow_energy.coordinator.set_commands."
            "POWERPULSE2_CHARGE_ACTION_WINDOW_S",
            {"start": 0.2, "stop": 0.2},
        ),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await wallbox.async_set_powerpulse_charge_action("stop")

    assert excinfo.value.translation_key == "powerpulse_action_not_confirmed"
    assert _mqtt(oceans[0]).send_proto_set.call_count == 1
    assert wallbox._wallbox_action_pending is None


@pytest.mark.parametrize(
    ("action", "status", "plug_status"),
    [("start", 3, 3), ("stop", 6, 6)],
    ids=["start-while-charging", "stop-while-finishing"],
)
async def test_action_state_precondition_refuses(
    hass: HomeAssistant, action: str, status: int, plug_status: int
) -> None:
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_status(wallbox, plug_status)

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_charge_action(action)

    assert excinfo.value.translation_key == "powerpulse_action_state"
    assert _mqtt(oceans[0]).send_proto_set.call_count == 0


async def test_descriptor_missing_refuses(hass: HomeAssistant) -> None:
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _apply_status(wallbox, 3)  # charging, but no descriptor set

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_charge_action("stop")

    assert excinfo.value.translation_key == "powerpulse_descriptor_missing"
    assert _mqtt(oceans[0]).send_proto_set.call_count == 0


async def test_no_powerocean_in_entry_refuses(hass: HomeAssistant) -> None:
    _entry_obj, _oceans, wallbox = _wire_entry(hass, [])
    _set_descriptor(wallbox)
    _apply_status(wallbox, 3)

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_charge_action("stop")

    assert excinfo.value.translation_key == "powerpulse_sibling_missing"
    assert _mqtt(wallbox).send_proto_set.call_count == 0


async def test_two_poweroceans_in_entry_refuses(hass: HomeAssistant) -> None:
    second_ocean = {**POWEROCEAN_DEVICE, "sn": "HJ31TEST00000002"}
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE, second_ocean])
    _set_descriptor(wallbox)
    _apply_status(wallbox, 3)

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_charge_action("stop")

    assert excinfo.value.translation_key == "powerpulse_sibling_missing"
    for ocean in oceans:
        assert _mqtt(ocean).send_proto_set.call_count == 0


async def test_sibling_offline_refuses(hass: HomeAssistant) -> None:
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _mqtt(oceans[0]).is_connected.return_value = False
    _set_descriptor(wallbox)
    _apply_status(wallbox, 3)

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_charge_action("stop")

    assert excinfo.value.translation_key == "powerpulse_sibling_offline"
    assert _mqtt(oceans[0]).send_proto_set.call_count == 0


async def test_second_press_while_pending_refuses(hass: HomeAssistant) -> None:
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_status(wallbox, 3)  # charging

    first = asyncio.create_task(wallbox.async_set_powerpulse_charge_action("stop"))
    await asyncio.sleep(0.05)
    assert _mqtt(oceans[0]).send_proto_set.call_count == 1

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_charge_action("stop")
    assert excinfo.value.translation_key == "powerpulse_action_in_progress"
    assert _mqtt(oceans[0]).send_proto_set.call_count == 1

    _apply_status(wallbox, 6)  # finishing - resolve and clean up the first task
    await first


async def test_sibling_publish_not_delivered_clears_the_record(
    hass: HomeAssistant,
) -> None:
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _mqtt(oceans[0]).send_proto_set.return_value = False
    _set_descriptor(wallbox)
    _apply_status(wallbox, 3)

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_charge_action("stop")

    assert excinfo.value.translation_key == "powerpulse_action_not_delivered"
    assert _mqtt(oceans[0]).send_proto_set.call_count == 1
    assert wallbox._wallbox_action_pending is None


async def test_confirming_frame_during_the_publish_is_not_missed(
    hass: HomeAssistant,
) -> None:
    """Mutation (c): the pending record must be set BEFORE the publish.

    `sibling.async_send_proto_set_command` is replaced with a stand-in that
    applies the confirming "finishing" frame to the wallbox coordinator
    itself before returning True - simulating the device replying between
    the publish call and its awaited result. If the record were set only
    after the publish returns, this frame would arrive to find no pending
    record and be lost, and the subsequent wait would time out instead of
    resolving immediately.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_status(wallbox, 3)  # charging

    async def _publish_and_race(payload: bytes, label: str) -> bool:
        _apply_status(wallbox, 6)  # finishing, arrives "during" the publish
        return True

    oceans[0].async_send_proto_set_command = AsyncMock(side_effect=_publish_and_race)

    await wallbox.async_set_powerpulse_charge_action("stop")

    assert wallbox._wallbox_action_pending is None
