"""The PowerPulse 2 charging-mode control (PLAN-147): coordinator and select.

The coordinator side calls `async_set_powerpulse_charge_mode` directly and
confirms on real heartbeat frames from the owner's mode capture (#7,
2026-09-13). The platform side sets the select up the way Home Assistant
does and checks the route gate, the availability rule and that a selection
applies nothing on its own. Wiring, device dicts and the small helpers are
the ones `test_powerpulse2_charge_action.py` built for the same
two-coordinator setup (a PowerOcean and a PowerPulse 2, `C376`, each with
its own mocked MQTT client).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ecoflow_energy.const import POWERPULSE2_CHARGE_MODE_OPTIONS
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.coordinator.core import WallboxActionPending
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)
from custom_components.ecoflow_energy.select import async_setup_entry as select_setup
from tests.ha.test_powerpulse2_charge_action import (
    POWEROCEAN_DEVICE,
    POWERPULSE2_SN,
    TOPIC,
    _apply_status,
    _mqtt,
    _set_descriptor,
    _wire_entry,
)

from .conftest import add_entities_collector

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "powerpulse"
    / "c376_charging_mode_echo_20260913.json"
)


def _fixture_frame(ts_prefix: str) -> bytes:
    """One raw `2/33` heartbeat from the owner's mode capture, by timestamp.

    `2026-09-13T22:49:14` decodes to `ev_charge_mode == "solar"` and
    `2026-09-13T22:50:12.810` to `"smart"` through the production parser -
    verified in `tests/test_powerpulse_proto.py`, not re-derived here.
    """
    data = json.loads(_FIXTURE_PATH.read_text())
    matches = [f for f in data["frames"] if f["ts_iso"].startswith(ts_prefix)]
    assert len(matches) == 1, ts_prefix
    return bytes.fromhex(matches[0]["hex"])


def _heartbeat_frame_with_mode(plug_status: int, work_mode: int) -> bytes:
    """A synthetic, unmasked `2/33` heartbeat carrying field 1 (status) and
    the linkage record (field 63) with sub-field 4 = work_mode - the shape
    every heartbeat on file carries."""
    linkage = (
        encode_field_varint(1, 1)
        + encode_field_varint(2, 1)
        + encode_field_bytes(3, b"X" * 16)
        + encode_field_varint(4, work_mode)
        + encode_field_varint(5, 1)
    )
    pdata = encode_field_varint(1, plug_status) + encode_field_bytes(63, linkage)
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


def _mode_entities(entities: list[Any]) -> list[Any]:
    """Setup's collected entities, filtered down to the wallbox's own select.

    A PowerOcean sibling in the same entry contributes its own work-mode
    select to the same collector.
    """
    return [e for e in entities if e.unique_id == f"{POWERPULSE2_SN}_ev_charge_mode"]


async def test_set_mode_publishes_once_and_confirms_on_a_real_heartbeat(
    hass: HomeAssistant,
) -> None:
    """Publishes EDevParamSet (241/102) once through the sibling, with the
    mode as the only nested field, and confirms on the owner's own heartbeat
    frame that carried mode 2 ten seconds after his Solar write.

    Mutation probe: dropping the `isinstance(record.expected_value, str)`
    branch in `_resolve_wallbox_action` leaves this pending forever, because
    `abs("solar" - "solar")` never runs and nothing else resolves the future.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    ocean = oceans[0]
    _set_descriptor(wallbox)
    _apply_frame(wallbox, _heartbeat_frame_with_mode(1, 3))  # custom before

    task = asyncio.create_task(wallbox.async_set_powerpulse_charge_mode("solar"))
    await asyncio.sleep(0.05)

    assert _mqtt(ocean).send_proto_set.call_count == 1
    assert _mqtt(wallbox).send_proto_set.call_count == 0
    published = _mqtt(ocean).send_proto_set.call_args.args[0]
    headers, _ = decode_header_message(published)
    header = headers[0]
    assert header["cmd_func"] == 241
    assert header["cmd_id"] == 102
    # field 4 of the pdata is EDevPileParamSet {2: 2}, two bytes, nothing else
    assert header["pdata"].endswith("22021002")

    _apply_frame(wallbox, _fixture_frame("2026-09-13T22:49:14"))
    await task
    assert wallbox._wallbox_action_pending is None
    assert wallbox._device_data["ev_charge_mode"] == "solar"
    assert any(
        entry["type"] == "powerpulse_charge_mode" for entry in wallbox._event_log
    )


async def test_a_heartbeat_with_the_old_mode_does_not_confirm(
    hass: HomeAssistant,
) -> None:
    """A heartbeat that still reports the old mode, and one without the
    linkage record at all, leave the write pending; the one with the new
    mode resolves it."""
    _entry_obj, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_frame(wallbox, _heartbeat_frame_with_mode(1, 2))  # solar before

    task = asyncio.create_task(wallbox.async_set_powerpulse_charge_mode("custom"))
    await asyncio.sleep(0.05)
    record = wallbox._wallbox_action_pending
    assert record is not None

    _apply_frame(wallbox, _heartbeat_frame_with_mode(1, 2))  # still solar
    assert not record.future.done()
    _apply_status(wallbox, 1)  # a frame without the key
    assert not record.future.done()

    _apply_frame(wallbox, _heartbeat_frame_with_mode(1, 3))
    await task
    assert wallbox._wallbox_action_pending is None


async def test_a_stale_value_in_the_store_does_not_confirm(
    hass: HomeAssistant,
) -> None:
    """The check runs on the arriving frame, never on the store: a wallbox
    already reporting `fast` does not confirm a write to `fast` until a
    frame carrying it arrives after the write.

    The frame WITHOUT the key is the part that tests this. A confirming
    frame carrying the store's own value satisfies both a frame-only and a
    store-fallback implementation; a keyless frame while the store already
    holds the expected value is where the two differ. Mutation probe:
    `value = parsed.get(key, self._device_data.get(key))` in
    `_resolve_wallbox_action` confirms on the keyless frame and fails here.
    """
    _entry_obj, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_frame(wallbox, _heartbeat_frame_with_mode(1, 1))  # fast already

    task = asyncio.create_task(wallbox.async_set_powerpulse_charge_mode("fast"))
    await asyncio.sleep(0.05)
    record = wallbox._wallbox_action_pending
    assert record is not None
    assert not record.future.done()

    _apply_status(wallbox, 1)  # a frame without the key, store still `fast`
    assert not record.future.done()

    _apply_frame(wallbox, _heartbeat_frame_with_mode(1, 1))
    await task


async def test_timeout_raises_and_names_the_last_reported_mode(
    hass: HomeAssistant,
) -> None:
    """A wallbox that does not report the new mode fails loudly, naming the
    mode it last reported, and clears the record so the next write is not
    refused as still in progress.

    Mutation probe: removing the `finally: self._clear_wallbox_action(record)`
    makes the second write below fail with `powerpulse_action_in_progress`.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_frame(wallbox, _fixture_frame("2026-09-13T22:50:12.810"))  # smart

    with (
        patch(
            "custom_components.ecoflow_energy.coordinator.set_commands."
            "POWERPULSE2_CHARGE_MODE_WINDOW_S",
            0.05,
        ),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await wallbox.async_set_powerpulse_charge_mode("solar")

    assert excinfo.value.translation_key == "powerpulse_charge_mode_not_confirmed"
    assert excinfo.value.translation_placeholders == {"reported": "smart"}
    assert wallbox._wallbox_action_pending is None

    task = asyncio.create_task(wallbox.async_set_powerpulse_charge_mode("solar"))
    await asyncio.sleep(0.05)
    assert _mqtt(oceans[0]).send_proto_set.call_count == 2
    _apply_frame(wallbox, _fixture_frame("2026-09-13T22:51:13.907"))
    await task


@pytest.mark.parametrize(
    ("scenario", "option", "expected_key"),
    [
        ("smart_in_app", "smart", "powerpulse_charge_mode_smart_in_app"),
        ("unknown_option", "eco", "powerpulse_charge_mode_unknown"),
        ("no_powerocean", "solar", "powerpulse_charge_mode_needs_powerocean"),
        ("two_poweroceans", "solar", "powerpulse_sibling_missing"),
        ("sibling_offline", "solar", "powerpulse_sibling_offline"),
        ("descriptor_missing", "solar", "powerpulse_descriptor_missing"),
        ("action_in_progress", "solar", "powerpulse_action_in_progress"),
    ],
)
async def test_refusals(
    hass: HomeAssistant, scenario: str, option: str, expected_key: str
) -> None:
    """Every precondition failure refuses without publishing anything.

    `smart_in_app` and `unknown_option` run with a pending record installed
    on purpose: both must be refused for their own reason before the
    in-progress check, so a weakened order would show
    `powerpulse_action_in_progress` for them instead.

    Mutation probe: moving the `option == "smart"` check below the
    `_wallbox_action_pending` check makes `smart_in_app` fail with the
    in-progress key.
    """
    if scenario == "no_powerocean":
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
    if scenario in ("action_in_progress", "smart_in_app", "unknown_option"):
        wallbox._wallbox_action_pending = WallboxActionPending(
            action="start", issued_at=0.0, future=hass.loop.create_future()
        )

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_charge_mode(option)

    assert excinfo.value.translation_key == expected_key
    for ocean in oceans:
        assert _mqtt(ocean).send_proto_set.call_count == 0
    assert _mqtt(wallbox).send_proto_set.call_count == 0


async def test_a_pending_mode_write_refuses_a_current_write_and_a_press(
    hass: HomeAssistant,
) -> None:
    """One pending record for every wallbox write: a mode write in flight
    refuses a maximum-current write and a start press through the same
    check, and neither publishes."""
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_status(wallbox, 6)  # finishing - a valid state to start from

    wallbox._wallbox_action_pending = WallboxActionPending(
        action="charge_mode",
        issued_at=0.0,
        future=hass.loop.create_future(),
        state_key="ev_charge_mode",
        expected_value="solar",
    )

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_max_current(11)
    assert excinfo.value.translation_key == "powerpulse_action_in_progress"
    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_charge_action("start")
    assert excinfo.value.translation_key == "powerpulse_action_in_progress"
    assert _mqtt(oceans[0]).send_proto_set.call_count == 0

    # And a mode frame does not resolve a pending current write.
    wallbox._wallbox_action_pending = WallboxActionPending(
        action="max_current",
        issued_at=0.0,
        future=hass.loop.create_future(),
        state_key="ev_max_current_a",
        expected_value=11.0,
    )
    _apply_frame(wallbox, _heartbeat_frame_with_mode(1, 2))
    assert not wallbox._wallbox_action_pending.future.done()


class TestSelectPlatform:
    async def test_select_created_with_one_sibling_and_reads_the_real_frame(
        self, hass: HomeAssistant
    ) -> None:
        """Created at setup on the sibling route with the four options, and
        shows the mode a real heartbeat carries once it arrives through the
        normal ingest path."""
        entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _set_descriptor(wallbox)

        entities: list[Any] = []
        await select_setup(hass, entry, add_entities_collector(entities))
        matches = _mode_entities(entities)
        assert len(matches) == 1
        select_entity = matches[0]
        assert select_entity.options == list(POWERPULSE2_CHARGE_MODE_OPTIONS)
        assert select_entity.current_option is None

        _apply_frame(wallbox, _fixture_frame("2026-09-13T22:50:12.810"))
        assert select_entity.current_option == "smart"

    @pytest.mark.parametrize("ocean_count", [0, 2])
    async def test_no_select_without_exactly_one_powerocean(
        self, hass: HomeAssistant, ocean_count: int
    ) -> None:
        second = {**POWEROCEAN_DEVICE, "sn": "HJ31TEST00000002"}
        ocean_devices = [POWEROCEAN_DEVICE, second][:ocean_count]
        entry, _oceans, wallbox = _wire_entry(hass, ocean_devices)
        _set_descriptor(wallbox)
        _apply_frame(wallbox, _heartbeat_frame_with_mode(1, 2))

        entities: list[Any] = []
        await select_setup(hass, entry, add_entities_collector(entities))

        assert _mode_entities(entities) == []

    async def test_select_option_calls_the_coordinator_without_optimistic_apply(
        self, hass: HomeAssistant
    ) -> None:
        entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _set_descriptor(wallbox)
        _apply_frame(wallbox, _heartbeat_frame_with_mode(1, 2))

        entities: list[Any] = []
        await select_setup(hass, entry, add_entities_collector(entities))
        select_entity = _mode_entities(entities)[0]
        assert select_entity.current_option == "solar"

        mock_set = AsyncMock()
        with patch.object(wallbox, "async_set_powerpulse_charge_mode", mock_set):
            await select_entity.async_select_option("custom")

        mock_set.assert_awaited_once_with("custom")
        # No optimistic apply: the store still holds the mode from before the
        # write until the wallbox's own heartbeat changes it.
        assert select_entity.current_option == "solar"
        assert _mqtt(wallbox).send_proto_set.call_count == 0

    async def test_select_available_follows_the_sibling_connection(
        self, hass: HomeAssistant
    ) -> None:
        entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _set_descriptor(wallbox)
        _apply_frame(wallbox, _heartbeat_frame_with_mode(1, 2))

        entities: list[Any] = []
        await select_setup(hass, entry, add_entities_collector(entities))
        select_entity = _mode_entities(entities)[0]
        assert select_entity.available

        _mqtt(oceans[0]).is_connected.return_value = False
        assert not select_entity.available
