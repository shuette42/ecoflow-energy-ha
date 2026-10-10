"""The PowerPulse 2 Block Battery Discharge and Plug-and-Play switches (PLAN-187).

Two more bits of the settings switch byte that Continuous charging already
uses (0x01 Battery discharge block, ON = discharge blocked; 0x02 Plug and Play,
ON = enabled). All three go through one coordinator write that rebuilds the
byte from the latest settings report, so these tests pin what is new: the bit
each switch writes, that the other bits of the byte stay as reported, that
writes across the three bits queue behind each other, that each shared refusal
and the not-confirmed message hold for the new switches, and that each switch
reads and writes only its own bit through the switch platform.

Helpers are the ones the Continuous charging tests built, imported rather than
duplicated. Report bits in the cases below are the values of the reporter's
write tests (18 -> 19 -> 18 for 0x01, 16 -> 18 -> 16 for 0x02).
"""

from __future__ import annotations

from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ecoflow_energy.const import POWERPULSE2_SWITCHES
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.switch import async_setup_entry as switch_setup
from tests.ha.test_powerpulse2_charge_action import (
    POWEROCEAN_DEVICE,
    POWERPULSE2_SN,
    _mqtt,
    _set_descriptor,
    _wire_entry,
)
from tests.ha.test_powerpulse2_continuous_switch import _report_bits
from tests.ha.test_powerpulse2_settings_writes import (
    _SET_COMMANDS,
    _apply_report,
    _published_fields,
    _set_continuous,
    _start,
)

from .conftest import add_entities_collector

# (entity key, bit): the literals the definitions must agree with.
DISCHARGE_BLOCK = ("ev_battery_discharge_block", 0x01)
PLUG_AND_PLAY = ("ev_plug_and_play", 0x02)
SWITCH_KEYS = (
    "ev_continuous_charging",
    DISCHARGE_BLOCK[0],
    PLUG_AND_PLAY[0],
)
_BOTH = pytest.mark.parametrize(
    "switch",
    [DISCHARGE_BLOCK, PLUG_AND_PLAY],
    ids=["battery_discharge_block", "plug_and_play"],
)


def _write(
    wallbox: EcoFlowDeviceCoordinator, switch: tuple[str, int], enabled: bool
) -> Coroutine[Any, Any, None]:
    """The shared write, called the way the switch platform calls it."""
    key, mask = switch
    return wallbox.async_set_powerpulse_switch_bit(mask, enabled, key)


def test_every_definition_carries_its_own_bit_on_the_shared_state_key() -> None:
    """The switch decodes and writes the bit its definition names, so the three
    definitions must stand for the three distinct bits of the one byte, in the
    order the platform creates them.

    Mutation probe: giving two definitions the same mask fails the sorted
    comparison.
    """
    assert [d.key for d in POWERPULSE2_SWITCHES] == list(SWITCH_KEYS)
    assert sorted(d.bit_mask or 0 for d in POWERPULSE2_SWITCHES) == [0x01, 0x02, 0x10]
    for definition in POWERPULSE2_SWITCHES:
        assert definition.state_key == "ev_settings_switch_bits"


@pytest.mark.parametrize(
    ("switch", "report_bits", "enabled", "written"),
    [
        (DISCHARGE_BLOCK, 18, True, 19),
        (DISCHARGE_BLOCK, 19, False, 18),
        (DISCHARGE_BLOCK, 16, True, 17),
        (PLUG_AND_PLAY, 16, True, 18),
        (PLUG_AND_PLAY, 18, False, 16),
        (PLUG_AND_PLAY, 19, False, 17),
    ],
)
async def test_each_new_switch_writes_field_one_with_only_its_bit_changed(
    hass: HomeAssistant,
    switch: tuple[str, int],
    report_bits: int,
    enabled: bool,
    written: int,
) -> None:
    """The write is `{1: bits}` built from the report: the requested bit set or
    cleared, every other bit as reported, nothing else on the wire. The store is
    made to disagree with the report before each write, so a write built on it
    publishes another byte.

    Mutation probes: swapping the set and clear branches in the shared plan
    fails all six; reading `_device_data` instead of the report snapshot fails
    all six.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _apply_report(wallbox, ev_settings_switch_bits=report_bits)
    wallbox.set_device_value("ev_settings_switch_bits", 0xFF ^ report_bits)

    task = await _start(_write(wallbox, switch, enabled))
    assert send.call_count == 1
    assert _published_fields(send.call_args) == {1: written}
    _apply_report(wallbox, ev_settings_switch_bits=written)
    await task
    assert wallbox._wallbox_action_pending is None


async def test_writes_across_the_three_bits_queue_and_each_builds_on_the_last(
    hass: HomeAssistant,
) -> None:
    """While Battery discharge block waits for its read-back, a Plug and Play
    write is refused with the earlier-command message and publishes nothing.
    The report that confirms the first write also carries 0x04, a bit the
    integration never wrote (17 | 0x04 = 21, as if it was changed in the app in
    between). Plug and Play is then built from that newer report (21 -> 23) and
    Continuous charging off from the one that shows both (23 -> 7): a write that
    builds on the last byte it wrote, and not on the latest report, publishes
    19 and 3 and loses the app's change.

    Mutation probes: building every write after the first on a cached
    last-written byte (the reviewer's probe_last_written.py) publishes 19 for
    the second write and fails; rebuilding from a first report kept aside
    publishes 18; dropping the pending check in
    `_async_write_powerpulse_settings` lets the second call through while the
    first waits.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _apply_report(wallbox, ev_settings_switch_bits=16)

    first = await _start(_write(wallbox, DISCHARGE_BLOCK, True))
    assert _published_fields(send.call_args) == {1: 17}
    pending = wallbox._wallbox_action_pending
    assert pending is not None and pending.action == "battery_discharge_block"

    with pytest.raises(HomeAssistantError) as excinfo:
        await _write(wallbox, PLUG_AND_PLAY, True)
    assert excinfo.value.translation_key == "powerpulse_action_in_progress"
    assert send.call_count == 1
    assert wallbox._wallbox_action_pending is pending

    _apply_report(wallbox, ev_settings_switch_bits=17 | 0x04)
    await first
    second = await _start(_write(wallbox, PLUG_AND_PLAY, True))
    assert _published_fields(send.call_args) == {1: 23}
    _apply_report(wallbox, ev_settings_switch_bits=23)
    await second
    third = await _start(_set_continuous(wallbox, False))
    assert _published_fields(send.call_args) == {1: 7}
    _apply_report(wallbox, ev_settings_switch_bits=7)
    await third

    assert send.call_count == 3
    assert wallbox._wallbox_action_pending is None


@_BOTH
async def test_new_switches_refuse_without_a_settings_report(
    hass: HomeAssistant, switch: tuple[str, int]
) -> None:
    """No report at all leaves nothing to rebuild the byte from: `report_missing`,
    nothing published, no pending record.

    Mutation probe: letting the shared plan run on a missing report raises a
    TypeError instead of the refusal.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    assert wallbox._settings_report is None

    with pytest.raises(HomeAssistantError) as excinfo:
        await _write(wallbox, switch, True)

    assert excinfo.value.translation_key == "powerpulse_continuous_report_missing"
    assert _mqtt(oceans[0]).send_proto_set.call_count == 0
    assert wallbox._wallbox_action_pending is None


@_BOTH
async def test_new_switches_refuse_a_stale_report_and_accept_a_fresh_one(
    hass: HomeAssistant, switch: tuple[str, int]
) -> None:
    """A report 11 s old refuses with `report_stale`; the same report 9 s old
    publishes. The clock is the one `set_commands` reads, so the 10 s limit is
    bracketed on both sides.

    Mutation probe: dropping the age comparison from the shared plan fails the
    stale half (no refusal).
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _apply_report(wallbox, ev_settings_switch_bits=16)
    assert wallbox._settings_report is not None
    stamped = wallbox._settings_report[0]

    with (
        patch(f"{_SET_COMMANDS}.time", SimpleNamespace(monotonic=lambda: stamped + 11)),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await _write(wallbox, switch, True)
    assert excinfo.value.translation_key == "powerpulse_continuous_report_stale"
    assert send.call_count == 0
    assert wallbox._wallbox_action_pending is None

    with patch(f"{_SET_COMMANDS}.time", SimpleNamespace(monotonic=lambda: stamped + 9)):
        task = await _start(_write(wallbox, switch, True))
        assert send.call_count == 1
    pending = wallbox._wallbox_action_pending
    assert pending is not None
    _apply_report(wallbox, ev_settings_switch_bits=int(pending.expected_value))
    await task


@_BOTH
@pytest.mark.parametrize("bits", [256, -1], ids=["bits_256", "bits_negative"])
async def test_new_switches_refuse_bits_outside_a_byte_as_unusable(
    hass: HomeAssistant, switch: tuple[str, int], bits: int
) -> None:
    """A report whose switch bits are not a byte refuses with `report_unusable`,
    naming the field and the value, and publishes nothing.

    Mutation probe: narrowing the shared range check to `bits <= 255` fails
    `bits_negative`.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _apply_report(wallbox, ev_settings_switch_bits=bits)

    with pytest.raises(HomeAssistantError) as excinfo:
        await _write(wallbox, switch, True)

    assert excinfo.value.translation_key == "powerpulse_continuous_report_unusable"
    assert excinfo.value.translation_placeholders == {
        "field": "switch bits",
        "value": str(bits),
    }
    assert _mqtt(oceans[0]).send_proto_set.call_count == 0
    assert wallbox._wallbox_action_pending is None


@pytest.mark.parametrize(
    ("switch", "report_bits", "written"),
    [(DISCHARGE_BLOCK, 18, 19), (PLUG_AND_PLAY, 16, 18)],
    ids=["battery_discharge_block", "plug_and_play"],
)
async def test_a_new_switch_that_is_not_confirmed_names_its_own_bit(
    hass: HomeAssistant,
    switch: tuple[str, int],
    report_bits: int,
    written: int,
) -> None:
    """The write publishes, the wallbox keeps reporting the old byte, and the
    call fails with the not-confirmed message naming the last reported state of
    the switch's own bit: off. Both starting reports carry 0x10, so a message
    that described the Continuous bit would say "on".

    Mutation probe: hard-wiring the Continuous mask into the `describe_reported`
    lambda in `async_set_powerpulse_switch_bit` (the reviewer's
    probe_describe_mask.py) makes both cases report "on".
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _apply_report(wallbox, ev_settings_switch_bits=report_bits)

    with patch(f"{_SET_COMMANDS}.POWERPULSE2_SETTINGS_WINDOW_S", 0.1):
        task = await _start(_write(wallbox, switch, True))
        assert _published_fields(send.call_args) == {1: written}
        _apply_report(wallbox, ev_settings_switch_bits=report_bits)
        with pytest.raises(HomeAssistantError) as excinfo:
            await task

    assert excinfo.value.translation_key == "powerpulse_continuous_not_confirmed"
    assert excinfo.value.translation_placeholders == {"reported": "off"}
    assert wallbox._wallbox_action_pending is None


async def _setup_switches(
    hass: HomeAssistant, bits: int
) -> tuple[EcoFlowDeviceCoordinator, dict[str, Any], list[Any]]:
    """The wallbox with its three settings switches, created from a report.

    Returns the wallbox, the switches by entity key and the PowerOcean
    coordinators whose MQTT client carries the writes.
    """
    entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    _report_bits(wallbox, bits)
    entities: list[Any] = []
    await switch_setup(hass, entry, add_entities_collector(entities))
    by_id = {e.unique_id: e for e in entities}
    switches = {key: by_id[f"{POWERPULSE2_SN}_{key}"] for key in SWITCH_KEYS}
    return wallbox, switches, oceans


@pytest.mark.parametrize(
    ("bits", "expected"),
    [
        (0, (False, False, False)),
        (1, (False, True, False)),
        (2, (False, False, True)),
        (16, (True, False, False)),
        (18, (True, False, True)),
        (19, (True, True, True)),
    ],
)
async def test_each_switch_reads_only_its_own_bit_and_none_without_the_bits(
    hass: HomeAssistant, bits: int, expected: tuple[bool, bool, bool]
) -> None:
    """Continuous (0x10), Battery discharge block (0x01) and Plug and Play
    (0x02) are created together by the first report and each follows its own
    bit; ON for the discharge block means blocked. A frame without the bits
    reads None on all three, never off.

    Mutation probes: giving the discharge-block definition mask 0x02 fails the
    cases with exactly one of the two bits set; returning False for a missing
    key fails the final assertion.
    """
    wallbox, switches, _oceans = await _setup_switches(hass, 19)

    _report_bits(wallbox, bits)
    assert tuple(switches[key].is_on for key in SWITCH_KEYS) == expected

    wallbox.async_set_updated_data({"ev_custom_current_a": 12.0})
    assert [switches[key].is_on for key in SWITCH_KEYS] == [None, None, None]


@pytest.mark.parametrize(
    ("switch", "bits", "call", "requested"),
    [
        (DISCHARGE_BLOCK, 16, "async_turn_on", True),
        (DISCHARGE_BLOCK, 17, "async_turn_off", False),
        (PLUG_AND_PLAY, 16, "async_turn_on", True),
        (PLUG_AND_PLAY, 18, "async_turn_off", False),
    ],
)
async def test_a_new_switch_reaches_the_shared_write_with_its_own_bit_and_key(
    hass: HomeAssistant,
    switch: tuple[str, int],
    bits: int,
    call: str,
    requested: bool,
) -> None:
    """A turn reaches the one coordinator write with the definition's bit, the
    requested state and the switch's own key; it neither writes state nor moves
    the display. The wallbox's next report does that.

    Mutation probes: handing the write another definition's bit or key fails
    the awaited-with assertion; passing `not turn_on` fails the argument;
    calling `_apply_optimistic` after the write fails the unchanged-display
    assertion.
    """
    key, mask = switch
    wallbox, switches, _oceans = await _setup_switches(hass, bits)
    target = switches[key]
    target.hass = hass
    target.entity_id = f"switch.test_wallbox_{key}"
    shown_before = target.is_on
    assert shown_before is not requested

    write = AsyncMock()
    generic_set = AsyncMock()
    state_write = MagicMock()
    with (
        patch.object(wallbox, "async_set_powerpulse_switch_bit", write),
        patch.object(wallbox, "async_send_set_command", generic_set),
        patch.object(target, "async_write_ha_state", state_write),
    ):
        await getattr(target, call)()

    write.assert_awaited_once_with(mask, requested, key)
    generic_set.assert_not_called()
    state_write.assert_not_called()
    assert target.is_on is shown_before

    _report_bits(wallbox, bits | mask if requested else bits & ~mask)
    assert target.is_on is requested


@pytest.mark.parametrize(
    ("key", "published"),
    [("ev_battery_discharge_block", 17), ("ev_plug_and_play", 18)],
)
async def test_a_new_switch_turned_on_runs_the_real_write_and_the_report_turns_it_on(
    hass: HomeAssistant, key: str, published: int
) -> None:
    """Switch, dispatch, real coordinator write, published payload, report and
    display in one run, with nothing patched but the MQTT client: from 16 the
    discharge block publishes {1: 17} and Plug and Play {1: 18}, the display
    stays off until the wallbox reports the new byte, and then reads on.

    Mutation probes: a wrong mask on the definition or the Continuous bit
    hard-wired in `_send_command` publishes another byte; an optimistic
    `_apply_optimistic` after the write turns the display on before the report.
    """
    wallbox, switches, oceans = await _setup_switches(hass, 16)
    send = _mqtt(oceans[0]).send_proto_set
    _apply_report(wallbox, ev_settings_switch_bits=16)
    target = switches[key]
    target.hass = hass
    target.entity_id = f"switch.test_wallbox_{key}"
    assert target.is_on is False

    task = await _start(target.async_turn_on())
    assert send.call_count == 1
    assert _published_fields(send.call_args) == {1: published}
    assert target.is_on is False

    _apply_report(wallbox, ev_settings_switch_bits=published)
    _report_bits(wallbox, published)
    await task
    assert target.is_on is True
    assert wallbox._wallbox_action_pending is None
