"""Per-report freshness of the PowerGlow heating rod sensors (#700).

The heating rod sensors write on every applied report that carried their key,
so `last_reported` advances while the value stays the same, and no recorder row
is made (a write with equal state only fires `EVENT_STATE_REPORTED`). Every
other sensor keeps the write gate. These tests run the real sensor platform
against a real `hass.states` and count the captured events, never clock deltas
(`tests/ha` has no freezer).

Frames come from `tests/fixtures/powerglow/hf33_power_source_frames.json`
(`rod_off_212_8`, `serial_only`) and the pair stream frame of
`tests/fixtures/powerocean/parallel_energy_stream_masked.json`. The one value
that is not on a wire (a changed state of charge) is the real frame's parsed
dict with one number moved, because the control needs a change the fixture does
not hold.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import EVENT_STATE_CHANGED, EVENT_STATE_REPORTED
from homeassistant.core import (
    Event,
    EventStateReportedData,
    HomeAssistant,
    State,
    callback,
)
from pytest_homeassistant_custom_component.common import (
    MockEntityPlatform,
    async_capture_events,
)

from custom_components.ecoflow_energy.const import DOMAIN
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

from .test_powerglow_gating import HEATING_ROD_KEYS, POWEROCEAN_DEVICE, _entry

FIXTURES = Path(__file__).parent.parent / "fixtures"
TOPIC = f"/app/user123/{POWEROCEAN_DEVICE['sn']}/thing/property"

ROD_POWER = "heating_rod_power_w"
SHARES = (
    "heating_rod_power_from_pv_w",
    "heating_rod_power_from_battery_w",
    "heating_rod_power_from_grid_w",
)
SOC = "soc_pct"

ROD_AT_ZERO = {
    "heating_rod_power_w": 0.0,
    "heating_rod_water_temp_c": 58.0,
    "heating_rod_target_power_w": 3500.0,
    "heating_rod_target_temp_c": 60.0,
}


def _powerglow_frame(name: str) -> bytes:
    frames = json.loads(
        (FIXTURES / "powerglow" / "hf33_power_source_frames.json").read_text()
    )["frames"]
    return bytes.fromhex(next(f for f in frames if f["name"] == name)["hex"])


def _pair_stream_frame() -> bytes:
    """The pair's first property push: EMS stream rows, no heating rod."""
    frames = json.loads(
        (FIXTURES / "powerocean" / "parallel_energy_stream_masked.json").read_text()
    )["frames"]
    frame = next(
        f
        for f in frames
        if f["source"].startswith("andy-j32e-pair") and f["frame_index"] == 1
    )
    return bytes.fromhex(frame["hex"])


async def _live(
    hass: HomeAssistant, device_data: dict[str, Any]
) -> tuple[EcoFlowDeviceCoordinator, dict[str, str]]:
    """Set the sensor platform up with entities that really sit in `hass`.

    Returns the coordinator and a key -> entity_id map of what was created.
    One real stream frame is applied first: the first message of a device flips
    it available, which writes every entity once (a changed event of its own),
    and that write must not be mistaken for the report under test.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN_DEVICE)
    for key, value in device_data.items():
        coordinator.set_device_value(key, value)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        POWEROCEAN_DEVICE["sn"]: coordinator
    }

    platform = MockEntityPlatform(hass, domain="sensor", platform_name=DOMAIN)
    platform.config_entry = entry
    created: list[Any] = []

    def _add(new_entities: Any, update_before_add: bool = False) -> None:
        batch = list(new_entities)
        created.extend(batch)
        hass.async_create_task(platform.async_add_entities(batch))

    await sensor_setup(hass, entry, _add)
    await hass.async_block_till_done()
    ids = {
        entity._definition.key: entity.entity_id
        for entity in created
        if hasattr(entity, "_definition") and entity.entity_id
    }
    _apply(coordinator, _pair_stream_frame())
    await hass.async_block_till_done()
    return coordinator, ids


def _apply(coordinator: EcoFlowDeviceCoordinator, frame: bytes) -> dict[str, Any]:
    parsed = coordinator._parse_message(TOPIC, frame)
    assert parsed
    coordinator._apply_data(parsed)
    return parsed


def _capture_reported(hass: HomeAssistant) -> list[Event[Any]]:
    """Collect `state_reported` events (the bus insists on an event filter)."""
    events: list[Event[Any]] = []

    @callback
    def _collect(event: Event[EventStateReportedData]) -> None:
        events.append(event)

    @callback
    def _accept_all(data: EventStateReportedData) -> bool:
        return True

    hass.bus.async_listen(EVENT_STATE_REPORTED, _collect, event_filter=_accept_all)
    return events


def _state(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


def _count(events: list[Event[Any]], entity_id: str) -> int:
    return sum(1 for event in events if event.data["entity_id"] == entity_id)


async def test_the_same_rod_report_twice_is_reported_again_not_changed(
    hass: HomeAssistant,
) -> None:
    """The second 212/8 at 0 W moves `last_reported` only: one reported event,
    no changed event (so no recorder row), `last_changed` untouched."""
    coordinator, ids = await _live(hass, ROD_AT_ZERO)
    entity_id = ids[ROD_POWER]
    frame = _powerglow_frame("rod_off_212_8")
    _apply(coordinator, frame)
    await hass.async_block_till_done()
    changed_before = _state(hass, entity_id).last_changed
    reported = _capture_reported(hass)
    changed = async_capture_events(hass, EVENT_STATE_CHANGED)

    _apply(coordinator, frame)
    await hass.async_block_till_done()

    assert _state(hass, entity_id).state == "0"
    assert _count(reported, entity_id) == 1
    assert _count(changed, entity_id) == 0
    assert _state(hass, entity_id).last_changed == changed_before


async def test_an_unrelated_powerocean_report_leaves_the_rod_sensors_alone(
    hass: HomeAssistant,
) -> None:
    """A stream frame carries no rod key, so none of the four rod sensors
    writes anything."""
    coordinator, ids = await _live(hass, ROD_AT_ZERO)
    assert set(ids) >= HEATING_ROD_KEYS
    reported = _capture_reported(hass)
    changed = async_capture_events(hass, EVENT_STATE_CHANGED)

    parsed = _apply(coordinator, _pair_stream_frame())
    await hass.async_block_till_done()

    assert SOC in parsed
    assert not parsed.keys() & HEATING_ROD_KEYS
    for key in HEATING_ROD_KEYS:
        assert _count(reported, ids[key]) == 0, key
        assert _count(changed, ids[key]) == 0, key


async def test_a_notify_without_an_apply_is_not_a_report(
    hass: HomeAssistant,
) -> None:
    """Availability and SET paths call `async_update_listeners`: the key set of
    the last apply must not replay as a fresh report."""
    coordinator, ids = await _live(hass, ROD_AT_ZERO)
    reported = _capture_reported(hass)
    _apply(coordinator, _powerglow_frame("rod_off_212_8"))
    await hass.async_block_till_done()
    assert _count(reported, ids[ROD_POWER]) == 1  # the apply itself is a report
    reported.clear()

    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert coordinator.last_applied_keys == frozenset()
    assert _count(reported, ids[ROD_POWER]) == 0


async def test_an_unflagged_sensor_stays_behind_the_gate_on_a_repeat(
    hass: HomeAssistant,
) -> None:
    """Negative control: the same stream frame twice writes nothing the
    second time for a sensor without `write_on_report`."""
    coordinator, ids = await _live(hass, {})
    frame = _pair_stream_frame()
    reported = _capture_reported(hass)
    changed = async_capture_events(hass, EVENT_STATE_CHANGED)

    _apply(coordinator, frame)
    await hass.async_block_till_done()

    assert _state(hass, ids[SOC]).state == "97"
    assert _count(reported, ids[SOC]) == 0
    assert _count(changed, ids[SOC]) == 0


async def test_an_unflagged_sensor_still_writes_when_its_value_changes(
    hass: HomeAssistant,
) -> None:
    """Positive control for the gate test: a changed value does produce a
    changed event, so the zero above is the gate and not a dead listener."""
    coordinator, ids = await _live(hass, {})
    parsed = coordinator._parse_message(TOPIC, _pair_stream_frame())
    assert parsed
    changed = async_capture_events(hass, EVENT_STATE_CHANGED)

    coordinator._apply_data({**parsed, SOC: parsed[SOC] - 1.0})
    await hass.async_block_till_done()

    assert _state(hass, ids[SOC]).state == "96"
    assert _count(changed, ids[SOC]) == 1


async def test_a_bare_source_report_counts_as_a_report_for_the_three_shares(
    hass: HomeAssistant,
) -> None:
    """Rod known at 0 W and the shares already at 0 W: the bare 212/33 puts
    0 W on each share, which is a same-value write, so each share entity gets
    exactly one reported event and no changed event."""
    seed = {**ROD_AT_ZERO, **dict.fromkeys(SHARES, 0.0)}
    coordinator, ids = await _live(hass, seed)
    assert set(SHARES) <= set(ids)
    reported = _capture_reported(hass)
    changed = async_capture_events(hass, EVENT_STATE_CHANGED)

    parsed = _apply(coordinator, _powerglow_frame("serial_only"))
    await hass.async_block_till_done()

    assert all(parsed[key] == 0.0 for key in SHARES)
    for key in SHARES:
        assert _count(reported, ids[key]) == 1, key
        assert _count(changed, ids[key]) == 0, key


async def test_an_apply_that_raises_leaves_no_report_behind(
    hass: HomeAssistant,
) -> None:
    """An apply that fails before its notify never reported anything, so the
    next availability or SET notify must not write the rod sensors as if it
    had: the key set brackets the notify only."""
    seed = {**ROD_AT_ZERO, **dict.fromkeys(SHARES, 0.0)}
    coordinator, ids = await _live(hass, seed)
    rod_keys = (ROD_POWER, *SHARES)
    assert set(rod_keys) <= set(ids)
    parsed = coordinator._parse_message(TOPIC, _powerglow_frame("rod_off_212_8"))
    assert parsed is not None
    assert set(rod_keys) <= set(parsed)

    with (
        patch.object(
            coordinator, "_integrate_energy", side_effect=RuntimeError("boom")
        ),
        pytest.raises(RuntimeError),
    ):
        coordinator._apply_data(parsed)
    reported = _capture_reported(hass)

    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert coordinator.last_applied_keys == frozenset()
    for key in rod_keys:
        assert _count(reported, ids[key]) == 0, key
