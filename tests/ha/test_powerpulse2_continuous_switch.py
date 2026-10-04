"""The PowerPulse 2 Continuous charging switch (PLAN-172).

`Wallbox Continuous Charging` is a switch on the wallbox device. Like the
settings numbers and the phase select it exists only on the sibling route
(exactly one PowerOcean in the entry) and only once the wallbox's settings
report has carried the switch bits, and it applies nothing on its own: the
coordinator write returns when the wallbox reports the new bits, and that
report is what moves the display. The coordinator write itself is covered by
`test_powerpulse2_settings_writes.py`; this file goes through the switch
platform.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ecoflow_energy.const import DOMAIN
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.switch import async_setup_entry as switch_setup
from tests.ha.test_powerpulse2_charge_action import (
    POWEROCEAN_DEVICE,
    POWERPULSE2_SN,
    _connected_mqtt,
    _mqtt,
    _set_descriptor,
    _wire_entry,
)

from .conftest import add_entities_collector

BITS_KEY = "ev_settings_switch_bits"
SWITCH_ID = f"{POWERPULSE2_SN}_ev_continuous_charging"


def _report_bits(wallbox: EcoFlowDeviceCoordinator, bits: int) -> None:
    """Put the settings switch bits into the store and publish them."""
    wallbox.set_device_value(BITS_KEY, bits)
    wallbox.async_set_updated_data(dict(wallbox._device_data))


def _continuous_switch(entities: list[Any]) -> list[Any]:
    """The Continuous charging switch among everything setup collected.

    A PowerOcean sibling in the same entry contributes its own switches to
    the same collector, so the filter names the unique id.
    """
    return [e for e in entities if e.unique_id == SWITCH_ID]


@pytest.mark.parametrize("ocean_count", [0, 1, 2])
async def test_switch_exists_only_with_one_powerocean_and_after_the_first_report(
    hass: HomeAssistant, ocean_count: int
) -> None:
    """With one PowerOcean the switch is created by the first settings report,
    not at setup; with none or two it is never created, report or not.

    Mutation probes: removing the route filter from the switch setup creates
    it on the zero- and two-ocean cases; flipping `accessory` off in const.py
    creates it at setup, before any report.
    """
    second = {**POWEROCEAN_DEVICE, "sn": "HJ31TEST00000002"}
    ocean_devices = [POWEROCEAN_DEVICE, second][:ocean_count]
    entry, _oceans, wallbox = _wire_entry(hass, ocean_devices)
    _set_descriptor(wallbox)

    entities: list[Any] = []
    await switch_setup(hass, entry, add_entities_collector(entities))
    assert _continuous_switch(entities) == []

    _report_bits(wallbox, 2)

    found = _continuous_switch(entities)
    if ocean_count == 1:
        assert len(found) == 1
        assert found[0].translation_key == "ev_continuous_charging"
    else:
        assert found == []


async def test_is_on_reads_the_continuous_bit_and_is_unknown_without_the_bits(
    hass: HomeAssistant,
) -> None:
    """Bit 0x10 decides, whatever else is set; absent bits read unknown.

    18 is 0x12 (Continuous plus another bit) and 2 is the other bit alone, so a
    "non-zero means on" reading, a wrong mask and a swapped polarity each fail
    a different assertion; 16 is the bare control. A frame that lacks the key
    reads None, never off.

    Mutation probes: dropping the PowerPulse branch from `is_on` in switch.py
    (the generic `!= 0` reading calls 2 on), masking 0x01 or 0x02 instead of
    `POWERPULSE2_SWITCH_BIT_CONTINUOUS`, and returning False for a missing key.
    """
    entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _report_bits(wallbox, 18)

    entities: list[Any] = []
    await switch_setup(hass, entry, add_entities_collector(entities))
    switch = _continuous_switch(entities)[0]

    assert switch.is_on is True

    _report_bits(wallbox, 2)
    assert switch.is_on is False

    _report_bits(wallbox, 16)
    assert switch.is_on is True

    wallbox.async_set_updated_data({"ev_custom_current_a": 12.0})
    assert switch.is_on is None


@pytest.mark.parametrize(
    ("bits", "call", "expected_arg"),
    [(2, "async_turn_on", True), (16, "async_turn_off", False)],
)
async def test_turning_the_switch_calls_the_coordinator_and_shows_no_optimistic_state(
    hass: HomeAssistant, bits: int, call: str, expected_arg: bool
) -> None:
    """A turn reaches only the Continuous charging write, with the requested
    state, and neither writes state nor changes the display: the wallbox's own
    report does that.

    Mutation probes: passing `not turn_on` to the coordinator, removing the
    PowerPulse branch from `_send_command` (the call then falls into the
    generic command path and is refused), and calling `_apply_optimistic`
    after the write.
    """
    entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _report_bits(wallbox, bits)

    entities: list[Any] = []
    await switch_setup(hass, entry, add_entities_collector(entities))
    switch = _continuous_switch(entities)[0]
    switch.hass = hass
    switch.entity_id = "switch.test_wallbox_continuous_charging"
    shown_before = switch.is_on

    write = AsyncMock()
    generic_set = AsyncMock()
    state_write = MagicMock()
    with (
        patch.object(wallbox, "async_set_powerpulse_continuous_charging", write),
        patch.object(wallbox, "async_send_set_command", generic_set),
        patch.object(switch, "async_write_ha_state", state_write),
    ):
        await getattr(switch, call)()

    write.assert_awaited_once_with(expected_arg)
    generic_set.assert_not_called()
    state_write.assert_not_called()
    assert switch.is_on is shown_before


async def test_switch_follows_the_sibling_powerocean_connection(
    hass: HomeAssistant,
) -> None:
    """The write goes through the sibling PowerOcean's MQTT connection, so a
    disconnected sibling makes the switch unavailable and a reconnect brings
    it back; the connected control keeps the assertion from reading a switch
    that is simply never available.

    Mutation probe: dropping the `is_connected()` condition from
    `EcoFlowSwitch.available` leaves the switch available with the sibling
    offline, where every toggle would be refused.
    """
    entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _report_bits(wallbox, 2)

    entities: list[Any] = []
    await switch_setup(hass, entry, add_entities_collector(entities))
    switch = _continuous_switch(entities)[0]
    assert switch.available is True

    _mqtt(oceans[0]).is_connected.return_value = False
    assert switch.available is False

    _mqtt(oceans[0]).is_connected.return_value = True
    assert switch.available is True


async def test_switch_is_unavailable_once_a_second_powerocean_joins_the_entry(
    hass: HomeAssistant,
) -> None:
    """The setup gate runs once; a second PowerOcean that joins afterwards
    leaves no single sibling to carry the write, so the switch goes
    unavailable even though both PowerOceans are connected.

    Mutation probe: resolving "the sibling" as the first PowerOcean of the
    entry instead of `powerocean_sibling()` keeps the switch available.
    """
    entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _report_bits(wallbox, 2)

    entities: list[Any] = []
    await switch_setup(hass, entry, add_entities_collector(entities))
    switch = _continuous_switch(entities)[0]
    assert switch.available is True

    second_ocean = EcoFlowDeviceCoordinator(
        hass, entry, {**POWEROCEAN_DEVICE, "sn": "HJ31TEST00000002"}
    )
    second_ocean._mqtt_client = _connected_mqtt()
    coordinators: dict[str, EcoFlowDeviceCoordinator] = hass.data[DOMAIN][
        entry.entry_id
    ]
    coordinators[second_ocean.device_sn] = second_ocean

    assert switch.available is False


async def test_switch_is_unavailable_while_the_wallbox_itself_is_unavailable(
    hass: HomeAssistant,
) -> None:
    """The wallbox's own availability still gates the switch with a connected
    sibling, as before the sibling rule was added.

    Mutation probe: dropping `device_available` from `EcoFlowSwitch.available`
    leaves the switch available with the wallbox unreachable.
    """
    entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _report_bits(wallbox, 2)

    entities: list[Any] = []
    await switch_setup(hass, entry, add_entities_collector(entities))
    switch = _continuous_switch(entities)[0]
    assert switch.available is True

    wallbox._device_available = False
    assert switch.available is False

    wallbox._device_available = True
    assert switch.available is True
