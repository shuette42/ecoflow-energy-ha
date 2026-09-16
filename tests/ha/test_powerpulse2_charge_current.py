"""The coordinator side of the PowerPulse 2 standalone charge-current control
(issue #7, 2026-09-14 recording, PLAN-140).

Coordinator level only: every test calls
`EcoFlowDeviceCoordinator.async_set_powerpulse_charge_current` directly, the
own-channel mirror image of `test_powerpulse2_max_current.py`'s sibling-route
tests. Wiring and the small helpers below are the ones
`test_powerpulse2_charge_action.py` already built for the same
one-or-two-coordinator setup - imported rather than duplicated.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.coordinator.core import WallboxActionPending
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)
from tests.ha.test_powerpulse2_charge_action import (
    POWEROCEAN_DEVICE,
    TOPIC,
    _apply_status,
    _mqtt,
    _wire_entry,
)


def _heartbeat_frame_with_charge_current(
    plug_status: int, charge_current_da: int
) -> bytes:
    """A synthetic, unmasked `2/33` heartbeat carrying field 1 (status) and
    field 17 (charge current, deci-amps) - the only read-back path a charge
    current write confirms on (`powerpulse_proto.py`, no `2/34` ParamReport
    equivalent exists for this field)."""
    pdata = encode_field_varint(1, plug_status) + encode_field_varint(
        17, charge_current_da
    )
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 2)
        + encode_field_varint(9, 33)
    )
    return encode_field_bytes(1, header)


def _apply_frame(wallbox: EcoFlowDeviceCoordinator, raw: bytes) -> dict[str, Any]:
    """Parse one raw frame through the wallbox's own ingest path and apply it,
    exactly the way a real MQTT message would arrive."""
    parsed = wallbox._parse_message(TOPIC, raw)
    assert parsed is not None
    wallbox._apply_data(parsed)
    return parsed


async def test_set_charge_current_publishes_on_own_channel_confirms_on_heartbeat(
    hass: HomeAssistant,
) -> None:
    """No PowerOcean in the entry (PLAN-140): published through the
    wallbox's own MQTT client on module 2 with its own serial, confirmed on
    its own heartbeat's field 17.

    Mutation probe: dropping the `record.expected_value is not None` branch
    in `_resolve_wallbox_action` leaves this pending forever, since a
    heartbeat with no charging session carries no `ev_charge_status` change
    that would otherwise resolve it.
    """
    _entry_obj, _oceans, wallbox = _wire_entry(hass, [])
    _apply_status(wallbox, 3)  # charging

    task = asyncio.create_task(wallbox.async_set_powerpulse_charge_current(6))
    await asyncio.sleep(0.05)

    assert _mqtt(wallbox).send_proto_set.call_count == 1
    published = _mqtt(wallbox).send_proto_set.call_args.args[0]
    headers, _ = decode_header_message(published)
    header = headers[0]
    assert header["cmd_func"] == 2
    assert header["cmd_id"] == 81
    assert header["dest"] == 2
    assert header["device_sn"] == wallbox.device_sn
    assert bytes.fromhex(header["pdata"]) == bytes.fromhex("283c3802")

    _apply_frame(wallbox, _heartbeat_frame_with_charge_current(3, 60))  # 6.0 A
    await task
    assert wallbox._wallbox_action_pending is None
    assert any(
        entry["type"] == "powerpulse_charge_current" for entry in wallbox._event_log
    )


async def test_a_frame_without_the_key_does_not_confirm(hass: HomeAssistant) -> None:
    """A frame parsed without `ev_charge_current_a` at all must not resolve
    the wait - the wallbox's status-only heartbeat used here as the stand-in
    carries only `ev_charge_status`.

    Mutation probe: replacing the `record.state_key not in parsed: return`
    guard in `_resolve_wallbox_action` with
    `parsed.get(state_key, expected_value)` makes this test fail, since any
    frame at all - including this one - would then confirm the pending
    write before the wallbox reported anything.
    """
    _entry_obj, _oceans, wallbox = _wire_entry(hass, [])
    _apply_status(wallbox, 3)  # charging

    task = asyncio.create_task(wallbox.async_set_powerpulse_charge_current(6))
    await asyncio.sleep(0.05)
    assert _mqtt(wallbox).send_proto_set.call_count == 1

    _apply_status(wallbox, 3)  # a frame with no ev_charge_current_a key at all
    pending = wallbox._wallbox_action_pending
    assert pending is not None and not pending.future.done()
    assert not task.done()

    _apply_frame(wallbox, _heartbeat_frame_with_charge_current(3, 60))  # confirms
    await task
    assert wallbox._wallbox_action_pending is None


async def test_a_frame_with_the_old_value_does_not_confirm(
    hass: HomeAssistant,
) -> None:
    """A frame still reporting the old charge current must not resolve the
    wait, and a value the store holds but no frame carried must not confirm
    it either (the frame-not-store rule, PLAN-147 review M1): the store is
    seeded directly with the requested value, then a frame without the key
    is applied, and the wait must still be pending.

    Mutation probe: widening the tolerance from `< 0.05` to `< 1.0` in
    `_resolve_wallbox_action` makes this test fail, since 5.5/6.5 A then
    fall inside it and confirm the write to 6 A immediately.
    """
    _entry_obj, _oceans, wallbox = _wire_entry(hass, [])
    _apply_status(wallbox, 3)  # charging

    task = asyncio.create_task(wallbox.async_set_powerpulse_charge_current(6))
    await asyncio.sleep(0.05)
    assert _mqtt(wallbox).send_proto_set.call_count == 1

    _apply_frame(wallbox, _heartbeat_frame_with_charge_current(3, 55))  # 5.5 A stale
    pending = wallbox._wallbox_action_pending
    assert pending is not None and not pending.future.done()
    assert not task.done()

    _apply_frame(wallbox, _heartbeat_frame_with_charge_current(3, 65))  # 6.5 A stale
    pending = wallbox._wallbox_action_pending
    assert pending is not None and not pending.future.done()
    assert not task.done()

    # The store now holds the requested value, put there directly rather
    # than by a frame; a frame without the key must not confirm from it.
    wallbox.set_device_value("ev_charge_current_a", 6.0)
    _apply_status(wallbox, 3)
    assert pending is not None and not pending.future.done()
    assert not task.done()

    _apply_frame(wallbox, _heartbeat_frame_with_charge_current(3, 60))  # confirms
    await task
    assert wallbox._wallbox_action_pending is None


async def test_sibling_route_refuses_without_publishing(hass: HomeAssistant) -> None:
    """The mirror image of `async_set_powerpulse_max_current`'s own-route
    refusal: with a PowerOcean in the entry, this write's route is
    unevidenced and is refused before anything is sent.

    Mutation probe: swapping `route == "sibling"` for `route == "own"` in
    `async_set_powerpulse_charge_current` makes this test fail, since the
    write would then be sent through the sibling instead of refused.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _apply_status(wallbox, 3)

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_charge_current(6)

    assert (
        excinfo.value.translation_key == "powerpulse_charge_current_needs_own_channel"
    )
    assert _mqtt(oceans[0]).send_proto_set.call_count == 0
    assert _mqtt(wallbox).send_proto_set.call_count == 0


async def test_publish_not_delivered_clears_the_record(hass: HomeAssistant) -> None:
    """A publish that reports failure clears the pending record, so a second
    call is not refused as still in progress.

    Mutation probe: removing `self._clear_wallbox_action(record)` from the
    `except BaseException` block in `async_set_powerpulse_charge_current`
    leaves the record behind, so the second call below would be refused as
    `powerpulse_action_in_progress` instead of publishing again.
    """
    _entry_obj, _oceans, wallbox = _wire_entry(hass, [])
    _mqtt(wallbox).send_proto_set.return_value = False
    _apply_status(wallbox, 3)

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_charge_current(6)

    assert excinfo.value.translation_key == "powerpulse_action_not_delivered"
    assert _mqtt(wallbox).send_proto_set.call_count == 1
    assert wallbox._wallbox_action_pending is None

    _mqtt(wallbox).send_proto_set.return_value = True
    task = asyncio.create_task(wallbox.async_set_powerpulse_charge_current(6))
    await asyncio.sleep(0.05)
    assert _mqtt(wallbox).send_proto_set.call_count == 2
    _apply_frame(wallbox, _heartbeat_frame_with_charge_current(3, 60))
    await task
    assert wallbox._wallbox_action_pending is None


async def test_timeout_raises_and_names_the_last_reported_value(
    hass: HomeAssistant,
) -> None:
    """A wallbox that stops reporting fails loudly, naming what it last
    reported, and the record is cleared so the next call is not refused as
    still in progress.

    Mutation probe: removing the `finally: self._clear_wallbox_action(record)`
    call makes the second call below fail with `powerpulse_action_in_progress`
    instead of publishing again.
    """
    _entry_obj, _oceans, wallbox = _wire_entry(hass, [])
    _apply_status(wallbox, 3)
    _apply_frame(wallbox, _heartbeat_frame_with_charge_current(3, 160))  # last: 16.0 A

    with (
        patch(
            "custom_components.ecoflow_energy.coordinator.set_commands."
            "POWERPULSE2_CHARGE_CURRENT_WINDOW_S",
            0.05,
        ),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await wallbox.async_set_powerpulse_charge_current(6)

    assert excinfo.value.translation_key == "powerpulse_charge_current_not_confirmed"
    assert excinfo.value.translation_placeholders == {"reported": "16 A"}
    assert wallbox._wallbox_action_pending is None

    task = asyncio.create_task(wallbox.async_set_powerpulse_charge_current(6))
    await asyncio.sleep(0.05)
    assert _mqtt(wallbox).send_proto_set.call_count == 2
    _apply_frame(wallbox, _heartbeat_frame_with_charge_current(3, 60))
    await task


@pytest.mark.parametrize(
    ("scenario", "current_a", "expected_key"),
    [
        ("range_low", 5, "powerpulse_charge_current_range"),
        ("range_high", 17, "powerpulse_charge_current_range"),
        ("own_channel_offline", 6, "powerpulse_own_channel_offline"),
        ("action_in_progress", 6, "powerpulse_action_in_progress"),
        ("two_poweroceans", 6, "powerpulse_charge_current_route_ambiguous"),
        ("shutdown", 6, "powerpulse_action_not_delivered"),
    ],
)
async def test_refusals(
    hass: HomeAssistant, scenario: str, current_a: int, expected_key: str
) -> None:
    """Every precondition failure refuses without publishing anything.

    Mutation probe: swapping the `type(current_a) is not int or not (low <=
    current_a <= high)` guard for `current_a < low` alone makes `range_high`
    (17) fall through to a publish attempt instead of the range error.

    `two_poweroceans` covers the `route is None` branch of
    `async_set_powerpulse_charge_current` (review finding 5): with the
    coordinator table intact but two PowerOceans in the entry, the route is
    ambiguous the same way it is for the sibling-only max-current write, but
    the message must not claim a "missing sibling" - this own-only write
    wants no PowerOcean in the loop at all.
    """
    if scenario == "two_poweroceans":
        second = {**POWEROCEAN_DEVICE, "sn": "HJ31TEST00000002"}
        ocean_devices: list[dict[str, Any]] = [POWEROCEAN_DEVICE, second]
    else:
        ocean_devices = []

    _entry_obj, oceans, wallbox = _wire_entry(hass, ocean_devices)
    _apply_status(wallbox, 3)

    if scenario == "own_channel_offline":
        _mqtt(wallbox).is_connected.return_value = False
    if scenario == "action_in_progress":
        wallbox._wallbox_action_pending = WallboxActionPending(
            action="start", issued_at=0.0, future=hass.loop.create_future()
        )
    if scenario == "shutdown":
        wallbox._shutdown = True

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_charge_current(current_a)

    assert excinfo.value.translation_key == expected_key
    assert _mqtt(wallbox).send_proto_set.call_count == 0
    for ocean in oceans:
        assert _mqtt(ocean).send_proto_set.call_count == 0
