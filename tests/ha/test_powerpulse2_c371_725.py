"""PowerPulse 2 `C371` controls behind one PowerOcean (#725).

The `C371` row in `POWERPULSE_CONTROLS` admits the three current numbers up
to 32 A, the charging mode and start/stop through a PowerOcean, and nothing
on its own topic and no settings or display control. `C376` keeps 16 A and
every control on both routes. The `C371` read-back frames come from the
reporter's recording (`c371_param_writes_20261113.json`).
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
from pytest_homeassistant_custom_component.common import MockEntityPlatform

from custom_components.ecoflow_energy.button import async_setup_entry as button_setup
from custom_components.ecoflow_energy.const import DOMAIN
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.parsers.stream_proto import (
    _iter_fields,
    _read_varint,
)
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)
from custom_components.ecoflow_energy.number import async_setup_entry as number_setup
from custom_components.ecoflow_energy.select import async_setup_entry as select_setup
from custom_components.ecoflow_energy.switch import async_setup_entry as switch_setup

from . import test_powerpulse2_charge_action as charge_action
from . import test_powerpulse2_controls as controls
from .conftest import add_entities_collector
from .test_powerpulse2_max_current import _heartbeat_frame_with_max_current

C371_SN = "C371TEST00000001"
C376_SN = "C376TEST00000001"
FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "powerpulse"
    / "c371_param_writes_20261113.json"
)
SETTINGS_KEYS = {
    "ev_continuous_charging",
    "ev_battery_discharge_block",
    "ev_plug_and_play",
    "ev_screen_enabled",
    "ev_led_enabled",
    "ev_phase_setting",
    "ev_screen_brightness_pct",
    "ev_led_brightness_pct",
}
CORE_KEYS = {
    "ev_max_current_a",
    "ev_custom_current_a",
    "ev_solar_min_current_a",
    "ev_charge_mode",
}


def _fixture_frame(device: str, index: int) -> bytes:
    frames = json.loads(FIXTURE.read_text())["frames"]
    (frame,) = [f for f in frames if f["device"] == device and f["index"] == index]
    return bytes.fromhex(frame["hex"])


def _report_everything(wallbox: EcoFlowDeviceCoordinator, descriptor_sn: str) -> None:
    """Report the descriptor and every read-back key a control waits for."""
    wallbox.set_device_value("ev_charger_sn", descriptor_sn)
    wallbox.set_device_value("ev_charger_dev_addr", 215)
    for key, value in {
        "ev_charge_status": "available",
        "ev_max_current_a": 16.0,
        "ev_charge_current_a": 6.0,
        "ev_charge_mode": "solar",
        "ev_solar_min_current_a": 6.0,
        "ev_custom_current_a": 6.0,
        "ev_phase_setting": 0,
        "ev_settings_switch_bits": 0,
        "ev_led_enabled": True,
        "ev_screen_enabled": True,
        "ev_led_brightness_pct": 50,
        "ev_screen_brightness_pct": 25,
    }.items():
        wallbox.set_device_value(key, value)


async def _wallbox_entities(
    hass: HomeAssistant, serial: str, oceans: int
) -> tuple[EcoFlowDeviceCoordinator, dict[str, Any]]:
    """Set up all four platforms; return the wallbox and its entities by key."""
    with patch.dict(controls.POWERPULSE2_DEVICE, {"sn": serial}):
        entry, _, wallbox = controls._wire_entry(
            hass, [controls.POWEROCEAN_DEVICE] * oceans
        )
    _report_everything(wallbox, serial)
    by_key: dict[str, Any] = {}
    for setup in (button_setup, number_setup, select_setup, switch_setup):
        entities: list[Any] = []
        await setup(hass, entry, add_entities_collector(entities))
        for entity in entities:
            if entity.unique_id.startswith(serial):
                by_key[entity.unique_id[len(serial) + 1 :]] = entity
    return wallbox, by_key


async def test_c371_behind_one_powerocean_gets_only_the_proven_controls(
    hass: HomeAssistant,
) -> None:
    """Current numbers, mode select and start/stop; no settings or display."""
    _, c371 = await _wallbox_entities(hass, C371_SN, oceans=1)
    assert set(c371) >= CORE_KEYS
    assert sum(1 for k in c371 if "start" in k or "stop" in k) == 2
    assert not SETTINGS_KEYS & set(c371)


async def test_c376_behind_one_powerocean_keeps_every_control(
    hass: HomeAssistant,
) -> None:
    _, c376 = await _wallbox_entities(hass, C376_SN, oceans=1)
    assert set(c376) >= CORE_KEYS | SETTINGS_KEYS


def _publish_state(wallbox: EcoFlowDeviceCoordinator) -> None:
    """Hand the accumulated device data to the entities, as a push does."""
    wallbox.async_set_updated_data(dict(wallbox._device_data))


@pytest.mark.parametrize(
    ("serial", "maximum", "solar_min"),
    [(C371_SN, 32.0, 16.0), (C376_SN, 16.0, 16.0)],
)
async def test_the_current_numbers_end_where_the_app_ends_them(
    hass: HomeAssistant, serial: str, maximum: float, solar_min: float
) -> None:
    """Maximum and Custom at the row ceiling, Solar Minimum at 16 A (#725).

    No Maximum Current is reported here, so the Custom current keeps the row
    ceiling instead of a guessed one.
    """
    wallbox, entities = await _wallbox_entities(hass, serial, oceans=1)
    wallbox._device_data.pop("ev_max_current_a")
    _publish_state(wallbox)
    expected = {
        "ev_max_current_a": maximum,
        "ev_custom_current_a": maximum,
        "ev_solar_min_current_a": solar_min,
    }
    for key, ceiling in expected.items():
        assert entities[key].native_min_value == 6.0
        assert entities[key].native_max_value == ceiling, key


async def test_c371_custom_current_ends_at_the_reported_maximum(
    hass: HomeAssistant,
) -> None:
    """A reported Maximum of 20 A narrows only the Custom current (#725)."""
    wallbox, entities = await _wallbox_entities(hass, C371_SN, oceans=1)
    custom = entities["ev_custom_current_a"]
    platform = MockEntityPlatform(hass, domain="number", platform_name=DOMAIN)
    await platform.async_add_entities([custom])
    _publish_state(wallbox)  # settles the value (6 A) under the 16 A maximum
    await hass.async_block_till_done()
    settled = hass.states.get(custom.entity_id)
    assert settled is not None
    assert settled.attributes["max"] == 16.0
    wallbox.set_device_value("ev_max_current_a", 20.0)
    _publish_state(wallbox)
    await hass.async_block_till_done()
    assert custom.native_max_value == 20.0
    # The value did not move, so only the bounds check publishes the new max.
    state = hass.states.get(custom.entity_id)
    assert state is not None
    assert state.attributes["max"] == 20.0
    assert entities["ev_max_current_a"].native_max_value == 32.0
    assert entities["ev_solar_min_current_a"].native_max_value == 16.0


@pytest.mark.parametrize(("reported", "ceiling"), [(6.0, 6.0), (16.5, 16.0)])
async def test_c371_custom_current_bound_is_whole_amps_and_never_below_6(
    hass: HomeAssistant, reported: float, ceiling: float
) -> None:
    """A maximum at the floor collapses the range; 16.5 A rounds down."""
    wallbox, entities = await _wallbox_entities(hass, C371_SN, oceans=1)
    wallbox.set_device_value("ev_max_current_a", reported)
    _publish_state(wallbox)
    custom = entities["ev_custom_current_a"]
    assert (custom.native_min_value, custom.native_max_value) == (6.0, ceiling)


async def test_c371_without_a_powerocean_has_no_route_and_no_controls(
    hass: HomeAssistant,
) -> None:
    wallbox, entities = await _wallbox_entities(hass, C371_SN, oceans=0)
    assert wallbox.charge_action_route() is None
    assert entities == {}


async def test_c376_without_a_powerocean_keeps_its_own_route(
    hass: HomeAssistant,
) -> None:
    wallbox, entities = await _wallbox_entities(hass, C376_SN, oceans=0)
    assert wallbox.charge_action_route() == "own"
    assert "ev_charge_current_a" in entities
    assert sum(1 for k in entities if "start" in k or "stop" in k) == 2


def _wire_writer(
    hass: HomeAssistant, serial: str
) -> tuple[EcoFlowDeviceCoordinator, EcoFlowDeviceCoordinator]:
    with patch.dict(charge_action.POWERPULSE2_DEVICE, {"sn": serial}):
        _, oceans, wallbox = charge_action._wire_entry(
            hass, [charge_action.POWEROCEAN_DEVICE]
        )
    wallbox.set_device_value("ev_charger_dev_addr", 215)
    wallbox.set_device_value("ev_charger_sn", serial)
    charge_action._apply_status(wallbox, 1)
    return oceans[0], wallbox


def _published_max_current_da(ocean: EcoFlowDeviceCoordinator) -> int:
    published = charge_action._mqtt(ocean).send_proto_set.call_args.args[0]
    headers, _ = decode_header_message(published)
    assert (headers[0]["cmd_func"], headers[0]["cmd_id"]) == (241, 102)
    pdata = bytes.fromhex(headers[0]["pdata"])
    (pile,) = [v for f, _w, v in _iter_fields(pdata) if f == 4]
    (raw,) = [v for f, _w, v in _iter_fields(pile) if f == 3]
    return int(_read_varint(memoryview(raw), 0)[0])


def _apply(wallbox: EcoFlowDeviceCoordinator, raw: bytes) -> None:
    parsed = wallbox._parse_message(charge_action.TOPIC, raw)
    assert parsed is not None
    wallbox._apply_data(parsed)


async def test_c371_max_current_20_is_published_and_confirmed(
    hass: HomeAssistant,
) -> None:
    ocean, wallbox = _wire_writer(hass, C371_SN)
    task = asyncio.create_task(wallbox.async_set_powerpulse_max_current(20))
    await asyncio.sleep(0.05)
    assert charge_action._mqtt(ocean).send_proto_set.call_count == 1
    assert _published_max_current_da(ocean) == 200
    _apply(wallbox, _heartbeat_frame_with_max_current(1, 200))
    await task
    assert wallbox._pending_write is None


async def test_c371_max_current_32_is_confirmed_by_the_real_param_report(
    hass: HomeAssistant,
) -> None:
    """Frame #87 is the C371's own 2/34 report of 320 after the app's write."""
    ocean, wallbox = _wire_writer(hass, C371_SN)
    task = asyncio.create_task(wallbox.async_set_powerpulse_max_current(32))
    await asyncio.sleep(0.05)
    assert _published_max_current_da(ocean) == 320
    _apply(wallbox, _fixture_frame("C371", 87))
    await task
    assert wallbox.device_data["ev_max_current_a"] == 32.0


async def test_c376_max_current_20_is_refused_before_anything_is_published(
    hass: HomeAssistant,
) -> None:
    ocean, wallbox = _wire_writer(hass, C376_SN)
    with pytest.raises(HomeAssistantError) as err:
        await wallbox.async_set_powerpulse_max_current(20)
    assert err.value.translation_key == "powerpulse_max_current_range"
    assert err.value.translation_placeholders == {"min": "6", "max": "16"}
    assert charge_action._mqtt(ocean).send_proto_set.call_count == 0


async def test_c371_solar_min_current_17_is_refused_before_anything_is_published(
    hass: HomeAssistant,
) -> None:
    ocean, wallbox = _wire_writer(hass, C371_SN)
    _report_everything(wallbox, C371_SN)
    with pytest.raises(HomeAssistantError) as err:
        await wallbox.async_set_powerpulse_solar_min_current(17)
    assert err.value.translation_key == "powerpulse_solar_min_current_range"
    assert err.value.translation_placeholders == {"min": "6", "max": "16"}
    assert charge_action._mqtt(ocean).send_proto_set.call_count == 0


async def test_c371_solar_min_current_16_is_published(hass: HomeAssistant) -> None:
    ocean, wallbox = _wire_writer(hass, C371_SN)
    _report_everything(wallbox, C371_SN)
    task = asyncio.create_task(wallbox.async_set_powerpulse_solar_min_current(16))
    await asyncio.sleep(0.05)
    assert charge_action._mqtt(ocean).send_proto_set.call_count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_c371_custom_current_above_the_reported_maximum_is_refused(
    hass: HomeAssistant,
) -> None:
    ocean, wallbox = _wire_writer(hass, C371_SN)
    _report_everything(wallbox, C371_SN)
    wallbox.set_device_value("ev_max_current_a", 20.0)
    with pytest.raises(HomeAssistantError) as err:
        await wallbox.async_set_powerpulse_custom_current(21)
    assert err.value.translation_key == "powerpulse_custom_current_above_maximum"
    assert charge_action._mqtt(ocean).send_proto_set.call_count == 0


_WITHHELD_WRITES = [
    lambda w: w.async_set_powerpulse_phase_setting("auto"),
    lambda w: w.async_set_powerpulse_switch_bit(1, True, "ev_continuous_charging"),
    lambda w: w.async_set_powerpulse_display("ev_led_enabled", 1),
]


@pytest.mark.parametrize("write", _WITHHELD_WRITES, ids=["phase", "bit", "display"])
async def test_c371_refuses_settings_and_display_writes(
    hass: HomeAssistant, write: Any
) -> None:
    """No entity calls these on a C371; the setter refuses on its own too."""
    ocean, wallbox = _wire_writer(hass, C371_SN)
    _report_everything(wallbox, C371_SN)
    with pytest.raises(HomeAssistantError) as err:
        await write(wallbox)
    assert err.value.translation_key == "powerpulse_setting_not_supported"
    assert charge_action._mqtt(ocean).send_proto_set.call_count == 0


async def test_c376_still_writes_the_phase_setting(hass: HomeAssistant) -> None:
    ocean, wallbox = _wire_writer(hass, C376_SN)
    _report_everything(wallbox, C376_SN)
    task = asyncio.create_task(wallbox.async_set_powerpulse_phase_setting("auto"))
    await asyncio.sleep(0.05)
    assert charge_action._mqtt(ocean).send_proto_set.call_count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_the_current_numbers_are_every_powerpulse_number_in_amps() -> None:
    from custom_components.ecoflow_energy.number import _POWERPULSE_CURRENT_KEYS

    assert {
        "ev_max_current_a",
        "ev_charge_current_a",
        "ev_solar_min_current_a",
        "ev_custom_current_a",
    } == _POWERPULSE_CURRENT_KEYS
