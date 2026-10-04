"""The PowerPulse 2 settings entities: two numbers and one select (PLAN-172).

`Wallbox Solar Minimum Current` and `Wallbox Custom Charging Current` are
numbers, `Wallbox Phase Setting` is a select. All three exist only on the
sibling route and only once the wallbox's settings report has been decoded, and
none of them applies a value on its own: the coordinator write returns when the
wallbox reports the new value, and that report is what moves the display. The
coordinator writes themselves are covered by `test_powerpulse2_settings_writes.py`;
this file goes through the number and select platforms.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ecoflow_energy.const import (
    POWERPULSE2_PHASE_SETTING_OPTIONS,
    POWERPULSE2_SELECTS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.number import async_setup_entry as number_setup
from custom_components.ecoflow_energy.select import EcoFlowSelect
from custom_components.ecoflow_energy.select import async_setup_entry as select_setup
from tests.ha.test_powerpulse2_charge_action import (
    POWEROCEAN_DEVICE,
    POWERPULSE2_SN,
    _set_descriptor,
    _wire_entry,
)

from .conftest import add_entities_collector

SOLAR_MIN_KEY = "ev_solar_min_current_a"
CUSTOM_KEY = "ev_custom_current_a"
PHASE_KEY = "ev_phase_setting"
SETTINGS_KEYS = (SOLAR_MIN_KEY, CUSTOM_KEY, PHASE_KEY)

WRITE_METHODS = {
    SOLAR_MIN_KEY: "async_set_powerpulse_solar_min_current",
    CUSTOM_KEY: "async_set_powerpulse_custom_current",
}
ALL_NUMBER_WRITES = (
    *WRITE_METHODS.values(),
    "async_set_powerpulse_max_current",
    "async_set_powerpulse_charge_current",
)


def _report_settings(
    wallbox: EcoFlowDeviceCoordinator,
    *,
    solar_min: float = 6.0,
    custom: float = 12.0,
    phase: int = 0,
) -> None:
    """Put a decoded settings report into the store and publish it."""
    wallbox.set_device_value(SOLAR_MIN_KEY, solar_min)
    wallbox.set_device_value(CUSTOM_KEY, custom)
    wallbox.set_device_value(PHASE_KEY, phase)
    wallbox.async_set_updated_data(dict(wallbox._device_data))


def _settings_entities(entities: list[Any]) -> dict[str, Any]:
    """The three settings entities among everything setup collected, by key.

    A PowerOcean sibling in the same entry contributes its own numbers and
    selects to the same collector, and the wallbox has other numbers of its
    own, so the filter names the three unique ids.
    """
    wanted = {f"{POWERPULSE2_SN}_{key}": key for key in SETTINGS_KEYS}
    return {wanted[e.unique_id]: e for e in entities if e.unique_id in wanted}


async def _setup_platforms(hass: HomeAssistant, entry: Any) -> list[Any]:
    entities: list[Any] = []
    await number_setup(hass, entry, add_entities_collector(entities))
    await select_setup(hass, entry, add_entities_collector(entities))
    return entities


async def test_entities_appear_with_the_first_settings_report_on_one_sibling(
    hass: HomeAssistant,
) -> None:
    """Accessory entities: none before the report, all three after it.

    Mutation probes: dropping the accessory gate from the select (so it is
    created at setup) fails the first assertion; flipping the solar number's
    `accessory` flag in const.py does the same for that number.
    """
    entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)

    entities = await _setup_platforms(hass, entry)
    assert _settings_entities(entities) == {}

    _report_settings(wallbox)

    found = _settings_entities(entities)
    assert set(found) == set(SETTINGS_KEYS)
    assert found[SOLAR_MIN_KEY].native_unit_of_measurement == "A"
    assert (
        found[SOLAR_MIN_KEY].native_min_value,
        found[SOLAR_MIN_KEY].native_max_value,
    ) == (6, 16)
    assert (found[CUSTOM_KEY].native_min_value, found[CUSTOM_KEY].native_max_value) == (
        6,
        16,
    )
    assert found[PHASE_KEY].options == list(POWERPULSE2_PHASE_SETTING_OPTIONS)


@pytest.mark.parametrize("ocean_count", [0, 2])
async def test_no_settings_entity_without_exactly_one_powerocean(
    hass: HomeAssistant, ocean_count: int
) -> None:
    """The write has no evidenced route on the wallbox's own channel, so zero or
    two-or-more PowerOceans in the entry gets none of the three, whatever the
    wallbox has reported.

    Mutation probe: removing the route filter from the number setup in
    number.py creates both numbers on the zero-ocean case.
    """
    second = {**POWEROCEAN_DEVICE, "sn": "HJ31TEST00000002"}
    ocean_devices = [POWEROCEAN_DEVICE, second][:ocean_count]
    entry, _oceans, wallbox = _wire_entry(hass, ocean_devices)
    _set_descriptor(wallbox)
    _report_settings(wallbox)

    entities = await _setup_platforms(hass, entry)
    _report_settings(wallbox, phase=1)

    assert _settings_entities(entities) == {}


@pytest.mark.parametrize(
    ("key", "reported", "other_reported", "written"),
    [
        (SOLAR_MIN_KEY, 7.0, 13.0, 9),
        (CUSTOM_KEY, 13.0, 7.0, 10),
    ],
)
async def test_current_number_shows_the_reported_value_and_writes_through_its_method(
    hass: HomeAssistant,
    key: str,
    reported: float,
    other_reported: float,
    written: int,
) -> None:
    """Each number reads its own key, and a write reaches only its own
    coordinator method as a whole number, with no optimistic apply.

    The two keys carry different reported values and Home Assistant hands the
    write a float, so crossed keys, a float reaching the coordinator and an
    optimistic apply each fail a distinct assertion.

    Mutation probes: swapping the two `elif` targets in number.py, passing
    `value` instead of `int(value)`.
    """
    entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    if key == SOLAR_MIN_KEY:
        _report_settings(wallbox, solar_min=reported, custom=other_reported)
    else:
        _report_settings(wallbox, custom=reported, solar_min=other_reported)

    entities = await _setup_platforms(hass, entry)
    number_entity = _settings_entities(entities)[key]
    assert number_entity.native_value == int(reported)

    mocks = {name: AsyncMock() for name in ALL_NUMBER_WRITES}
    with (
        patch.object(
            wallbox, WRITE_METHODS[SOLAR_MIN_KEY], mocks[WRITE_METHODS[SOLAR_MIN_KEY]]
        ),
        patch.object(
            wallbox, WRITE_METHODS[CUSTOM_KEY], mocks[WRITE_METHODS[CUSTOM_KEY]]
        ),
        patch.object(
            wallbox,
            "async_set_powerpulse_max_current",
            mocks["async_set_powerpulse_max_current"],
        ),
        patch.object(
            wallbox,
            "async_set_powerpulse_charge_current",
            mocks["async_set_powerpulse_charge_current"],
        ),
    ):
        await number_entity.async_set_native_value(float(written))

    expected = mocks[WRITE_METHODS[key]]
    expected.assert_awaited_once()
    assert expected.await_args is not None
    assert expected.await_args.args == (written,)
    assert type(expected.await_args.args[0]) is int
    for name, mock in mocks.items():
        if name != WRITE_METHODS[key]:
            mock.assert_not_called()
    # No optimistic apply: the display holds the reported value until the
    # wallbox's own report changes it.
    assert number_entity.native_value == int(reported)


async def test_phase_setting_select_maps_the_wire_value_and_writes_the_option(
    hass: HomeAssistant,
) -> None:
    """0/1/2 read as auto/one_phase/three_phases, an absent or unknown value
    reads as unknown (never as Auto), and a selection reaches only the phase
    write with the option string, applying nothing itself.

    Mutation probes: returning the first option for an absent value in
    select.py, swapping the 1 and 2 entries of `POWERPULSE2_PHASE_SETTING_WIRE`
    in const.py, and removing the `ev_phase_setting` branch from
    `async_select_option` (the write then goes to the charging-mode method).
    """
    entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)

    # A frame that carries other keys but not the phase setting must read
    # unknown. The coordinator needs a payload at all, otherwise the select
    # returns None before it ever looks at the key.
    wallbox.async_set_updated_data({CUSTOM_KEY: 12.0})
    definition = next(d for d in POWERPULSE2_SELECTS if d.key == PHASE_KEY)
    unreported = EcoFlowSelect(wallbox, definition)
    assert unreported.current_option is None

    _report_settings(wallbox, phase=0)
    entities = await _setup_platforms(hass, entry)
    select_entity = _settings_entities(entities)[PHASE_KEY]
    # Control: a reported 0 is Auto, so the None above is absence, not a
    # mapping that cannot produce a value.
    assert select_entity.current_option == "auto"

    for wire, option in ((1, "one_phase"), (2, "three_phases"), (0, "auto")):
        _report_settings(wallbox, phase=wire)
        assert select_entity.current_option == option

    _report_settings(wallbox, phase=7)
    assert select_entity.current_option is None

    _report_settings(wallbox, phase=1)
    set_phase = AsyncMock()
    set_mode = AsyncMock()
    with (
        patch.object(wallbox, "async_set_powerpulse_phase_setting", set_phase),
        patch.object(wallbox, "async_set_powerpulse_charge_mode", set_mode),
    ):
        await select_entity.async_select_option("three_phases")

    set_phase.assert_awaited_once_with("three_phases")
    set_mode.assert_not_called()
    assert select_entity.current_option == "one_phase"
