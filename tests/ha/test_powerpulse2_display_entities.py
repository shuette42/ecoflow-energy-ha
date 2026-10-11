"""The PowerPulse 2 display entities: Screen and LED Indicator switches and
their two brightness numbers (PLAN-188, issue #495).

The write itself is pinned byte for byte in `test_powerpulse2_display_writes.py`.
These tests cover what the platforms add on top: when the four entities exist,
what the switches show, and which coordinator call a turn or a set value
reaches. None of them applies state of its own; the wallbox's next settings
report does that.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.number import NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.number import async_setup_entry as number_setup
from custom_components.ecoflow_energy.switch import async_setup_entry as switch_setup
from tests.ha.test_powerpulse2_charge_action import (
    POWEROCEAN_DEVICE,
    POWERPULSE2_SN,
    _set_descriptor,
    _wire_entry,
)

from .conftest import add_entities_collector

SWITCH_KEYS = ("ev_screen_enabled", "ev_led_enabled")
NUMBER_KEYS = ("ev_screen_brightness_pct", "ev_led_brightness_pct")


def _report(
    wallbox: EcoFlowDeviceCoordinator,
    *,
    led: bool = True,
    screen: bool = True,
    led_pct: int = 50,
    screen_pct: int = 25,
) -> None:
    """Publish a settings report that carries the four display values."""
    wallbox.set_device_value("ev_led_enabled", led)
    wallbox.set_device_value("ev_screen_enabled", screen)
    wallbox.set_device_value("ev_led_brightness_pct", led_pct)
    wallbox.set_device_value("ev_screen_brightness_pct", screen_pct)
    wallbox.async_set_updated_data(dict(wallbox._device_data))


async def _display_entities(
    hass: HomeAssistant, ocean_devices: list[dict[str, Any]], *, report: bool
) -> tuple[EcoFlowDeviceCoordinator, dict[str, Any], list[Any]]:
    """Set the switch and number platforms up for one wallbox entry.

    Returns the wallbox, the wallbox's display entities by key, and the
    collector the platforms keep adding to (a later report can add more).
    """
    entry, _oceans, wallbox = _wire_entry(hass, ocean_devices)
    _set_descriptor(wallbox)
    if report:
        _report(wallbox)
    entities: list[Any] = []
    await switch_setup(hass, entry, add_entities_collector(entities))
    await number_setup(hass, entry, add_entities_collector(entities))
    return wallbox, _by_key(entities), entities


def _by_key(entities: list[Any]) -> dict[str, Any]:
    """The wallbox's four display entities among everything collected."""
    wanted = {f"{POWERPULSE2_SN}_{key}": key for key in (*SWITCH_KEYS, *NUMBER_KEYS)}
    return {wanted[e.unique_id]: e for e in entities if e.unique_id in wanted}


@pytest.mark.parametrize("ocean_count", [0, 1, 2])
async def test_the_four_display_entities_need_exactly_one_powerocean_and_a_report(
    hass: HomeAssistant, ocean_count: int
) -> None:
    """With one PowerOcean the four entities are created by the report that
    carries the display values, not at setup; with none or two they never are,
    report or not.

    Mutation probes: dropping `powerpulse_route="sibling"` from the two
    brightness numbers creates them on the zero-ocean case; flipping a
    definition's `accessory` flag creates it before the report; dropping
    `slider=True` from a brightness number fails the mode assertion.
    """
    second = {**POWEROCEAN_DEVICE, "sn": "HJ31TEST00000002"}
    ocean_devices = [POWEROCEAN_DEVICE, second][:ocean_count]
    wallbox, found, entities = await _display_entities(
        hass, ocean_devices, report=False
    )
    assert found == {}

    _report(wallbox)
    found = _by_key(entities)

    if ocean_count == 1:
        assert set(found) == {*SWITCH_KEYS, *NUMBER_KEYS}
        assert [found[k].native_unit_of_measurement for k in NUMBER_KEYS] == ["%", "%"]
        assert [
            (found[k].native_min_value, found[k].native_max_value, found[k].native_step)
            for k in NUMBER_KEYS
        ] == [(25, 100, 25)] * 2
        assert [found[k].mode for k in NUMBER_KEYS] == [NumberMode.SLIDER] * 2
    else:
        assert found == {}


async def test_the_switches_show_the_reported_value_and_unknown_without_it(
    hass: HomeAssistant,
) -> None:
    """Each switch follows its own bool and never invents an off.

    A reading that is not a bool (the int 1) is no on state either: the parser
    only ever publishes bools, so anything else is shown as unknown.

    Mutation probes: reading `ev_led_enabled` for the screen switch fails the
    two mixed cases; returning False for a missing key fails the final
    assertion; returning `bool(reported)` for any present value fails the int
    case.
    """
    wallbox, found, _entities = await _display_entities(
        hass, [POWEROCEAN_DEVICE], report=True
    )
    screen, led = found["ev_screen_enabled"], found["ev_led_enabled"]

    _report(wallbox, led=True, screen=False)
    assert (screen.is_on, led.is_on) == (False, True)
    _report(wallbox, led=False, screen=True)
    assert (screen.is_on, led.is_on) == (True, False)

    wallbox.set_device_value("ev_screen_enabled", 1)
    wallbox.set_device_value("ev_led_enabled", 1)
    wallbox.async_set_updated_data(dict(wallbox._device_data))
    assert (screen.is_on, led.is_on) == (None, None)

    wallbox._device_data.pop("ev_led_enabled")
    wallbox._device_data.pop("ev_screen_enabled")
    wallbox.async_set_updated_data(dict(wallbox._device_data))
    assert (screen.is_on, led.is_on) == (None, None)


async def test_the_brightness_numbers_show_their_own_reported_value(
    hass: HomeAssistant,
) -> None:
    """Each slider follows its own key: with distinct values (screen 25, LED 50),
    then the two swapped (screen 50, LED 25), each shows the value reported for
    it. With both keys gone they show unknown, not a remembered value.

    Mutation probe: pointing the Screen number's `state_key` at
    `ev_led_brightness_pct` shows 50 for the screen and fails the first
    assertion.
    """
    wallbox, found, _entities = await _display_entities(
        hass, [POWEROCEAN_DEVICE], report=True
    )
    screen, led = found["ev_screen_brightness_pct"], found["ev_led_brightness_pct"]

    _report(wallbox, led_pct=50, screen_pct=25)
    assert (screen.native_value, led.native_value) == (25, 50)
    _report(wallbox, led_pct=25, screen_pct=50)
    assert (screen.native_value, led.native_value) == (50, 25)

    wallbox._device_data.pop("ev_led_brightness_pct")
    wallbox._device_data.pop("ev_screen_brightness_pct")
    wallbox.async_set_updated_data(dict(wallbox._device_data))
    assert (screen.native_value, led.native_value) == (None, None)


@pytest.mark.parametrize(
    ("key", "call", "expected"),
    [
        ("ev_screen_enabled", "async_turn_off", 0),
        ("ev_led_enabled", "async_turn_on", 1),
    ],
)
async def test_a_turn_reaches_the_display_write_with_its_own_key_and_value(
    hass: HomeAssistant, key: str, call: str, expected: int
) -> None:
    """A turn calls the display setter with the switch's own key and 0 or 1,
    and neither the bit write nor the generic SET path. The displayed state
    stays what the wallbox last reported.

    Mutation probes: passing `int(not turn_on)` fails the awaited-with
    assertion; handing the write to `async_set_powerpulse_switch_bit` fails the
    display-awaited and bit-not-awaited assertions.
    """
    wallbox, found, _entities = await _display_entities(
        hass, [POWEROCEAN_DEVICE], report=True
    )
    target = found[key]
    target.hass = hass
    target.entity_id = f"switch.test_wallbox_{key}"
    shown_before = target.is_on

    display = AsyncMock()
    bit = AsyncMock()
    with (
        patch.object(wallbox, "async_set_powerpulse_display", display),
        patch.object(wallbox, "async_set_powerpulse_switch_bit", bit),
    ):
        await getattr(target, call)()

    display.assert_awaited_once_with(key, expected)
    bit.assert_not_awaited()
    assert target.is_on is shown_before


@pytest.mark.parametrize("key", NUMBER_KEYS)
async def test_a_brightness_set_reaches_the_display_write_and_never_the_amps_guard(
    hass: HomeAssistant, key: str
) -> None:
    """Setting 50 calls the display setter with the key and the int 50, and no
    current setter. A value off the steps (37.5) is not turned into an amps
    message: the real setter refuses it with the brightness text.

    Mutation probes: routing the brightness keys to the maximum-current setter
    fails the first block; running the "whole amps only" guard before the
    brightness branch fails the second with `set_value_rejected`.
    """
    wallbox, found, _entities = await _display_entities(
        hass, [POWEROCEAN_DEVICE], report=True
    )
    target = found[key]
    target.hass = hass
    target.entity_id = f"number.test_wallbox_{key}"

    display = AsyncMock()
    max_current = AsyncMock()
    with (
        patch.object(wallbox, "async_set_powerpulse_display", display),
        patch.object(wallbox, "async_set_powerpulse_max_current", max_current),
    ):
        await target.async_set_native_value(50.0)

    display.assert_awaited_once_with(key, 50)
    assert display.await_args is not None
    assert type(display.await_args.args[1]) is int
    max_current.assert_not_awaited()

    with pytest.raises(HomeAssistantError) as raised:
        await target.async_set_native_value(37.5)
    assert raised.value.translation_key == "powerpulse_display_brightness_invalid"
    assert raised.value.translation_placeholders == {
        "allowed": "25, 50, 75, 100",
        "value": "37.5",
    }
