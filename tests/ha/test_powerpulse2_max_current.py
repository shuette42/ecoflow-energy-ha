"""The coordinator side of the PowerPulse 2 maximum-current control (PLAN-146).

Coordinator level only: no number platform exists yet (Phase 1c), so every
test calls `EcoFlowDeviceCoordinator.async_set_powerpulse_max_current`
directly. Wiring, device dicts and the small helpers below are the ones
`test_powerpulse2_charge_action.py` already built for the same two-coordinator
setup (a PowerOcean and a PowerPulse 2, `C376`, each with its own mocked MQTT
client) - imported rather than duplicated.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
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
    _set_descriptor,
    _wire_entry,
)

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "powerpulse"
    / "c376_param_set_echo_20260824.json"
)


def _fixture_frame_hex(index: int) -> bytes:
    """One raw frame from the real `2/34` ParamReport recording (PLAN-146).

    `frames[0]` decodes to `{"ev_max_current_a": 11.0, ...}` through the
    production parser - verified once against
    `parse_powerpulse_message`, not re-derived here.
    """
    data = json.loads(_FIXTURE_PATH.read_text())
    return bytes.fromhex(data["frames"][index]["hex"])


def _heartbeat_frame_with_max_current(plug_status: int, max_current_da: int) -> bytes:
    """A synthetic, unmasked `2/33` heartbeat carrying field 1 (status) and
    field 18 (maximum current, deci-amps) - the second read-back path a
    maximum-current write also confirms on, per `powerpulse_proto.py`.
    """
    pdata = encode_field_varint(1, plug_status) + encode_field_varint(
        18, max_current_da
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


async def test_set_max_current_publishes_once_and_confirms_on_param_report(
    hass: HomeAssistant,
) -> None:
    """Publishes EDevParamSet (241/102) once through the sibling and confirms
    on the wallbox's own ParamReport (2/34) echoing the new value.

    Mutation probe: dropping the `record.expected_value is not None` branch
    in `_resolve_wallbox_action` leaves this pending forever, since the
    ParamReport frame carries no `ev_charge_status` at all.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    ocean = oceans[0]
    _set_descriptor(wallbox)
    _apply_status(wallbox, 1)  # available

    task = asyncio.create_task(wallbox.async_set_powerpulse_max_current(11))
    await asyncio.sleep(0.05)

    assert _mqtt(ocean).send_proto_set.call_count == 1
    assert _mqtt(wallbox).send_proto_set.call_count == 0
    published = _mqtt(ocean).send_proto_set.call_args.args[0]
    headers, _ = decode_header_message(published)
    header = headers[0]
    assert header["cmd_func"] == 241
    assert header["cmd_id"] == 102
    assert header["pdata"].endswith("2202186e")

    _apply_frame(wallbox, _fixture_frame_hex(0))  # ev_max_current_a -> 11.0
    await task
    assert wallbox._wallbox_action_pending is None
    assert any(
        entry["type"] == "powerpulse_max_current" for entry in wallbox._event_log
    )


async def test_confirms_on_heartbeat_too(hass: HomeAssistant) -> None:
    """The heartbeat's own field 18 confirms a write just as well as the
    ParamReport - `_resolve_wallbox_action` reads whatever `state_key` names,
    not a specific message type.

    Mutation probe: hardcoding `state_key` to `"ev_charge_status"` in the
    `async_set_powerpulse_max_current` record (instead of
    `"ev_max_current_a"`) leaves this pending forever, since a heartbeat with
    no charging session in progress reports no `ev_charge_status` change that
    resolves it.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_status(wallbox, 1)

    task = asyncio.create_task(wallbox.async_set_powerpulse_max_current(11))
    await asyncio.sleep(0.05)
    assert _mqtt(oceans[0]).send_proto_set.call_count == 1

    _apply_frame(wallbox, _heartbeat_frame_with_max_current(1, 110))  # 11.0 A
    await task
    assert wallbox._wallbox_action_pending is None


async def test_a_frame_without_the_key_does_not_confirm(hass: HomeAssistant) -> None:
    """A frame parsed without `ev_max_current_a` at all must not resolve the
    wait - the wallbox sends a `241/44` descriptor report about once a
    second that carries no maximum-current field, and the heartbeat used
    here as the stand-in carries only `ev_charge_status`.

    Mutation probe: replacing the `record.state_key not in parsed: return`
    guard in `_resolve_wallbox_action` with
    `parsed.get(state_key, expected_value)` makes this test fail, since any
    frame at all - including this one - would then confirm the pending
    write within about a second, before the wallbox has reported anything.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_status(wallbox, 1)  # available

    task = asyncio.create_task(wallbox.async_set_powerpulse_max_current(11))
    await asyncio.sleep(0.05)
    assert _mqtt(oceans[0]).send_proto_set.call_count == 1

    _apply_status(wallbox, 1)  # a frame with no ev_max_current_a key at all
    pending = wallbox._wallbox_action_pending
    assert pending is not None and not pending.future.done()
    assert not task.done()

    _apply_frame(
        wallbox, _heartbeat_frame_with_max_current(1, 110)
    )  # 11.0 A - confirms
    await task
    assert wallbox._wallbox_action_pending is None


async def test_a_frame_with_the_old_value_does_not_confirm(
    hass: HomeAssistant,
) -> None:
    """A frame still reporting the old maximum current must not resolve the
    wait; only a frame reporting the requested value does. Uses stale values
    half an amp on both sides of the requested 11 A, not just a wide
    16-vs-11 gap, so a widened tolerance is caught at the distance it would
    actually be wrong at (a coarser tolerance still correctly rejects an
    adjacent whole-amp value like 10 or 12; 0.5 A is where it starts
    confirming a write it should not).

    Mutation probe: widening the tolerance from `< 0.05` to `< 1.0` makes
    this test fail, since 10.5/11.5 A then fall inside it and confirm the
    write to 11 A immediately - a 16-vs-11 A gap alone would not have
    caught this, since 5 A stays outside even a 1.0 A tolerance.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_status(wallbox, 1)

    task = asyncio.create_task(wallbox.async_set_powerpulse_max_current(11))
    await asyncio.sleep(0.05)
    assert _mqtt(oceans[0]).send_proto_set.call_count == 1

    _apply_frame(wallbox, _heartbeat_frame_with_max_current(1, 105))  # 10.5 A - stale
    pending = wallbox._wallbox_action_pending
    assert pending is not None and not pending.future.done()
    assert not task.done()

    _apply_frame(wallbox, _heartbeat_frame_with_max_current(1, 115))  # 11.5 A - stale
    pending = wallbox._wallbox_action_pending
    assert pending is not None and not pending.future.done()
    assert not task.done()

    # The store now holds the requested value, put there directly rather than
    # by a frame; a frame without the key must not confirm from it (the
    # frame-not-store rule, PLAN-147 review M1: a store-fallback mutation in
    # `_resolve_wallbox_action` passes every other assertion in this file).
    wallbox.set_device_value("ev_max_current_a", 11.0)
    _apply_status(wallbox, 1)
    assert pending is not None and not pending.future.done()
    assert not task.done()

    _apply_frame(
        wallbox, _heartbeat_frame_with_max_current(1, 110)
    )  # 11.0 A - confirms
    await task
    assert wallbox._wallbox_action_pending is None


async def test_publish_not_delivered_clears_the_record(hass: HomeAssistant) -> None:
    """A publish that reports failure clears the pending record, mirroring
    `test_sibling_publish_not_delivered_clears_the_record` in
    `test_powerpulse2_charge_action.py` for the start/stop path - the
    max-current method has its own, separate `except BaseException` clause
    with the same obligation.

    Mutation probe: removing `self._clear_wallbox_action(record)` from the
    `except BaseException` block in `async_set_powerpulse_max_current`
    (set_commands.py) leaves the record behind, so the second call below
    would be refused as `powerpulse_action_in_progress` instead of
    publishing again.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _mqtt(oceans[0]).send_proto_set.return_value = False
    _set_descriptor(wallbox)
    _apply_status(wallbox, 1)

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_max_current(11)

    assert excinfo.value.translation_key == "powerpulse_action_not_delivered"
    assert _mqtt(oceans[0]).send_proto_set.call_count == 1
    assert wallbox._wallbox_action_pending is None

    # A second call publishes again - not refused as "in progress".
    _mqtt(oceans[0]).send_proto_set.return_value = True
    task = asyncio.create_task(wallbox.async_set_powerpulse_max_current(11))
    await asyncio.sleep(0.05)
    assert _mqtt(oceans[0]).send_proto_set.call_count == 2
    _apply_frame(wallbox, _fixture_frame_hex(0))  # ev_max_current_a -> 11.0
    await task
    assert wallbox._wallbox_action_pending is None


async def test_timeout_raises_and_names_the_last_reported_value(
    hass: HomeAssistant,
) -> None:
    """A wallbox that stops reporting fails loudly, naming what it last
    reported, and the record is cleared so the next press is not refused as
    still in progress.

    Mutation probe: removing the `finally: self._clear_wallbox_action(record)`
    call makes the second press below fail with `powerpulse_action_in_progress`
    instead of publishing again.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_status(wallbox, 1)
    _apply_frame(wallbox, _heartbeat_frame_with_max_current(1, 160))  # last: 16.0 A

    with (
        patch(
            "custom_components.ecoflow_energy.coordinator.set_commands."
            "POWERPULSE2_MAX_CURRENT_WINDOW_S",
            0.05,
        ),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await wallbox.async_set_powerpulse_max_current(11)

    assert excinfo.value.translation_key == "powerpulse_max_current_not_confirmed"
    assert excinfo.value.translation_placeholders == {"reported": "16 A"}
    assert wallbox._wallbox_action_pending is None

    # Not refused as in progress - the record was cleared.
    task = asyncio.create_task(wallbox.async_set_powerpulse_max_current(11))
    await asyncio.sleep(0.05)
    assert _mqtt(oceans[0]).send_proto_set.call_count == 2
    _apply_frame(wallbox, _heartbeat_frame_with_max_current(1, 110))
    await task


@pytest.mark.parametrize(
    ("scenario", "max_current_a", "expected_key"),
    [
        ("range_low", 5, "powerpulse_max_current_range"),
        ("range_high", 17, "powerpulse_max_current_range"),
        ("no_powerocean", 11, "powerpulse_max_current_needs_powerocean"),
        ("two_poweroceans", 11, "powerpulse_sibling_missing"),
        ("sibling_offline", 11, "powerpulse_sibling_offline"),
        ("descriptor_missing", 11, "powerpulse_descriptor_missing"),
        ("action_in_progress", 11, "powerpulse_action_in_progress"),
    ],
)
async def test_refusals(
    hass: HomeAssistant, scenario: str, max_current_a: int, expected_key: str
) -> None:
    """Every precondition failure refuses without publishing anything.

    `range_low` and `range_high` wire zero PowerOceans on purpose: with one
    wired, `build_powerpulse_param_set_current_payload` enforces the same
    6-16 bound and its `ValueError` is caught into the same
    `powerpulse_max_current_range` key, so a weakened top-level guard would
    still show the right key for the wrong reason. With no PowerOcean, the
    route is `"own"` and the builder is never reached, so the top-level guard
    is the only thing that can produce that key at all.

    Mutation probe: swapping the `type(max_current_a) is not int or not (low
    <= max_current_a <= high)` guard for `max_current_a < low` alone makes
    `range_high` (17) fall through to `powerpulse_max_current_needs_powerocean`
    instead.
    """
    if scenario in ("no_powerocean", "range_low", "range_high"):
        ocean_devices: list[dict[str, Any]] = []
    elif scenario == "two_poweroceans":
        second = {**POWEROCEAN_DEVICE, "sn": "HJ31TEST00000002"}
        ocean_devices = [POWEROCEAN_DEVICE, second]
    else:
        ocean_devices = [POWEROCEAN_DEVICE]

    _entry_obj, oceans, wallbox = _wire_entry(hass, ocean_devices)
    if scenario != "descriptor_missing":
        _set_descriptor(wallbox)
    _apply_status(wallbox, 1)

    if scenario == "sibling_offline":
        _mqtt(oceans[0]).is_connected.return_value = False
    if scenario == "action_in_progress":
        wallbox._wallbox_action_pending = WallboxActionPending(
            action="start", issued_at=0.0, future=hass.loop.create_future()
        )

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_max_current(max_current_a)

    assert excinfo.value.translation_key == expected_key
    for ocean in oceans:
        assert _mqtt(ocean).send_proto_set.call_count == 0
    assert _mqtt(wallbox).send_proto_set.call_count == 0


async def test_a_pending_max_current_write_refuses_a_start_press(
    hass: HomeAssistant,
) -> None:
    """The reverse direction of `test_refusals[action_in_progress]`: a
    pending max-current write refuses a start/stop press too, through the
    same `_wallbox_action_pending is not None` check in
    `async_set_powerpulse_charge_action` (set_commands.py:1100).

    Mutation probe: removing that guard from
    `async_set_powerpulse_charge_action` makes this test fail - the start
    press would publish instead of being refused.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_status(wallbox, 6)  # finishing - a valid state to start from

    wallbox._wallbox_action_pending = WallboxActionPending(
        action="max_current",
        issued_at=0.0,
        future=hass.loop.create_future(),
        state_key="ev_max_current_a",
        expected_value=11.0,
    )

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_charge_action("start")

    assert excinfo.value.translation_key == "powerpulse_action_in_progress"
    assert _mqtt(oceans[0]).send_proto_set.call_count == 0
