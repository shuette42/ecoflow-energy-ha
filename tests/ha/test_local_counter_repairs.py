"""Repairs notice for a Local lifetime counter held back for more than a day."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.components.repairs import ConfirmRepairFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.ecoflow_energy import repairs
from custom_components.ecoflow_energy.const import (
    DOMAIN,
    LOCAL_COUNTER_HELD_NOTICE_S,
)
from custom_components.ecoflow_energy.coordinator.local_modbus import (
    EcoFlowLocalModbusCoordinator,
)
from custom_components.ecoflow_energy.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .test_local_modbus_coordinator import (
    DAY,
    DEVICE_DICT,
    SERIAL,
    SOLAR,
    StubClient,
    _coordinator,
    _frame,
    _held_clock,
    _local_entry,
    _set_up_local_entry,
    _solar,
)

BASE = DAY["solar_total_kwh"]
LOW = BASE - 5.0


def _issue_id(entry_id: str, key: str = SOLAR) -> str:
    return f"local_counter_held_{entry_id}_{key}"


def _issue(
    hass: HomeAssistant, entry_id: str, key: str = SOLAR
) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry_id, key))


async def test_a_counter_held_for_a_day_raises_a_fixable_notice(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The notice carries the id, the two values and the entry and key to fix."""
    clock = _held_clock(monkeypatch)
    start = clock["now"]
    entry = _local_entry()
    coordinator = _coordinator(
        hass, entry, [_frame(DAY)] + [_frame(DAY, solar_total_kwh=LOW)] * 2
    )
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is None

    clock["now"] = start + timedelta(seconds=LOCAL_COUNTER_HELD_NOTICE_S)
    await coordinator.async_refresh()

    issue = _issue(hass, entry.entry_id)
    assert issue is not None
    assert issue.is_fixable is True
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "local_counter_held"
    assert issue.translation_placeholders == {
        "counter": "Solar Lifetime Energy",
        "ha_value": f"{BASE:.3f}",
        "device_value": f"{LOW:.1f}",
    }
    assert issue.data == {"entry_id": entry.entry_id, "key": SOLAR}
    assert SERIAL not in json.dumps(
        [issue.issue_id, issue.data, issue.translation_placeholders]
    )


async def test_a_counter_held_for_less_than_a_day_or_never_raises_no_notice(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: neither a young hold nor a healthy counter makes a notice."""
    clock = _held_clock(monkeypatch)
    start = clock["now"]
    entry = _local_entry()
    coordinator = _coordinator(
        hass,
        entry,
        [_frame(DAY), _frame(DAY, solar_total_kwh=BASE + 1.0)]
        + [_frame(DAY, solar_total_kwh=LOW)] * 2
        + [_frame(DAY, solar_total_kwh=BASE + 2.0)],
    )
    await coordinator.async_refresh()
    # A day of a healthy, growing counter: nothing held, nothing raised.
    clock["now"] = start + timedelta(days=2)
    await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is None

    # A hold that began just now, then almost a day later: still young.
    await coordinator.async_refresh()
    clock["now"] += timedelta(seconds=LOCAL_COUNTER_HELD_NOTICE_S - 1)
    await coordinator.async_refresh()
    assert SOLAR in coordinator.held_counters()
    assert _issue(hass, entry.entry_id) is None


async def test_the_notice_goes_when_the_counter_recovers(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reading back above the bound clears the hold and with it the notice."""
    clock = _held_clock(monkeypatch)
    entry = _local_entry()
    coordinator = _coordinator(
        hass,
        entry,
        [_frame(DAY)]
        + [_frame(DAY, solar_total_kwh=LOW)] * 2
        + [_frame(DAY, solar_total_kwh=BASE + 1.0)],
    )
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    clock["now"] += timedelta(seconds=LOCAL_COUNTER_HELD_NOTICE_S)
    await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is not None

    await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is None


async def test_an_open_notice_is_written_once_until_its_wording_changes(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Polling a held counter does not rewrite an unchanged notice every time."""
    clock = _held_clock(monkeypatch)
    entry = _local_entry()
    coordinator = _coordinator(
        hass,
        entry,
        [_frame(DAY)] + [_frame(DAY, solar_total_kwh=LOW)] * 4,
    )
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    clock["now"] += timedelta(seconds=LOCAL_COUNTER_HELD_NOTICE_S)
    created: list[str] = []
    real = ir.async_create_issue

    def spy(*args: Any, **kwargs: Any) -> None:
        created.append(args[2])
        real(*args, **kwargs)

    monkeypatch.setattr(
        "custom_components.ecoflow_energy.coordinator.local_modbus.ir.async_create_issue",
        spy,
    )
    for _ in range(3):
        await coordinator.async_refresh()
    assert created == [_issue_id(entry.entry_id)]


async def _held_for_a_day(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, *later: Any
) -> tuple[Any, Any]:
    """An entry set up with its coordinator in ``hass.data`` and SOLAR held a day."""
    clock = _held_clock(monkeypatch)
    entry = _local_entry()
    script = [_frame(DAY), *[_frame(DAY, solar_total_kwh=LOW)] * 2, *later]
    assert await _set_up_local_entry(hass, entry, script)
    coordinator = hass.data[DOMAIN][entry.entry_id][SERIAL]
    await coordinator.async_refresh()
    clock["now"] += timedelta(seconds=LOCAL_COUNTER_HELD_NOTICE_S)
    await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is not None
    return entry, coordinator


async def test_submitting_the_notice_takes_the_device_value_and_clears_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirm lowers the floor: the next poll publishes what the device reports."""
    entry, coordinator = await _held_for_a_day(
        hass, monkeypatch, _frame(DAY, solar_total_kwh=LOW + 1.0)
    )
    issue = _issue(hass, entry.entry_id)
    assert issue is not None

    flow = await repairs.async_create_fix_flow(hass, issue.issue_id, issue.data)
    assert isinstance(flow, ConfirmRepairFlow)
    flow.hass = hass
    flow.handler = DOMAIN
    flow.issue_id = issue.issue_id
    shown = await flow.async_step_confirm(None)
    assert shown["type"] == "form"
    assert _solar(coordinator) == pytest.approx(BASE, abs=1e-3)

    done = await flow.async_step_confirm({})
    assert done["type"] == "create_entry"
    assert coordinator.held_counters() == {}

    await coordinator.async_refresh()
    assert _solar(coordinator) == pytest.approx(LOW + 1.0, abs=1e-3)
    assert _issue(hass, entry.entry_id) is None


@pytest.mark.parametrize(
    ("entry_id", "key"),
    [("gone", SOLAR), ("{live}", "soc_pct"), ("{live}", "no_such_key")],
    ids=["entry-gone", "key-not-held", "key-unknown"],
)
async def test_a_stale_notice_still_finishes_without_changing_anything(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, entry_id: str, key: str
) -> None:
    """An entry or key that is gone ends the flow quietly; the live hold is kept."""
    entry, coordinator = await _held_for_a_day(hass, monkeypatch)
    flow = await repairs.async_create_fix_flow(
        hass,
        "stale",
        {"entry_id": entry_id.replace("{live}", entry.entry_id), "key": key},
    )
    assert isinstance(flow, repairs.LocalCounterHeldRepairFlow)
    flow.hass = hass
    flow.handler = DOMAIN
    flow.issue_id = "stale"

    done = await flow.async_step_confirm({})

    assert done["type"] == "create_entry"
    assert SOLAR in coordinator.held_counters()
    assert _solar(coordinator) == pytest.approx(BASE, abs=1e-3)


async def test_unloading_the_entry_removes_its_notices(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = await _held_for_a_day(hass, monkeypatch)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert _issue(hass, entry.entry_id) is None


async def test_diagnostics_list_the_held_counters_without_the_serial(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _held_clock(monkeypatch)
    entry = _local_entry()
    assert await _set_up_local_entry(
        hass, entry, [_frame(DAY), _frame(DAY, solar_total_kwh=LOW)]
    )
    coordinator = hass.data[DOMAIN][entry.entry_id][SERIAL]

    (device,) = (await async_get_config_entry_diagnostics(hass, entry))["devices"]
    assert device["held_counters"] == {}

    await coordinator.async_refresh()
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    (device,) = diagnostics["devices"]
    held = device["held_counters"][SOLAR]
    assert datetime.fromisoformat(held["since"]) == clock["now"]
    assert held["published"] == pytest.approx(BASE, abs=1e-3)
    assert held["device_value"] == pytest.approx(LOW, abs=1e-3)
    assert SERIAL not in json.dumps(diagnostics)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_notice_wording_moves_only_with_the_held_value_not_every_poll(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Device readings a few thousandths apart do not rewrite an open notice."""
    clock = _held_clock(monkeypatch)
    entry = _local_entry()
    coordinator = _coordinator(
        hass,
        entry,
        [
            _frame(DAY),
            _frame(DAY, solar_total_kwh=LOW),
            _frame(DAY, solar_total_kwh=LOW + 0.004),
            _frame(DAY, solar_total_kwh=LOW + 0.008),
        ],
    )
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    clock["now"] += timedelta(seconds=LOCAL_COUNTER_HELD_NOTICE_S)
    created: list[str] = []
    real = ir.async_create_issue

    def spy(*args: Any, **kwargs: Any) -> None:
        created.append(args[2])
        real(*args, **kwargs)

    monkeypatch.setattr(
        "custom_components.ecoflow_energy.coordinator.local_modbus.ir.async_create_issue",
        spy,
    )
    seen: list[float] = []
    for _ in range(2):
        await coordinator.async_refresh()
        seen.append(coordinator.held_counters()[SOLAR]["device_value"])

    # Positive control: the device value did move between the polls.
    assert len(set(seen)) == 2
    assert created == [_issue_id(entry.entry_id)]


async def test_an_ignored_notice_survives_a_restart_and_comes_back_ignored(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ignore is kept on unload and by the first poll after a restart.

    A non-persistent issue is restored by Home Assistant only as an inactive
    stub that still carries ``dismissed_version``. Deleting that stub (on
    unload, or on the first poll for an id this run never raised) would bring
    the notice back as new 24 h later.
    """
    entry, _ = await _held_for_a_day(hass, monkeypatch)
    issue_id = _issue_id(entry.entry_id)
    ir.async_ignore_issue(hass, DOMAIN, issue_id, True)
    ignored = _issue(hass, entry.entry_id)
    assert ignored is not None
    assert ignored.dismissed_version is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    kept = _issue(hass, entry.entry_id)
    assert kept is not None
    assert kept.dismissed_version == ignored.dismissed_version

    # What ``IssueRegistry.async_load`` makes of a non-persistent issue.
    registry = ir.async_get(hass)
    registry.issues[(DOMAIN, issue_id)] = dataclasses.replace(
        kept,
        active=False,
        breaks_in_ha_version=None,
        data=None,
        is_fixable=None,
        issue_domain=None,
        learn_more_url=None,
        severity=None,
        translation_key=None,
        translation_placeholders=None,
    )

    # A new coordinator on the same hass, a healthy first poll: the stub stays.
    clock = _held_clock(monkeypatch)
    restarted = EcoFlowLocalModbusCoordinator(
        hass,
        entry,
        DEVICE_DICT,
        client=StubClient(
            [_frame(DAY), *[_frame(DAY, solar_total_kwh=LOW)] * 2],
        ),
    )
    await restarted.async_refresh()
    stub = _issue(hass, entry.entry_id)
    assert stub is not None
    assert stub.dismissed_version == ignored.dismissed_version

    # The counter drops again and is held a day: the notice is raised on the
    # same record, still ignored.
    await restarted.async_refresh()
    clock["now"] += timedelta(seconds=LOCAL_COUNTER_HELD_NOTICE_S)
    await restarted.async_refresh()
    again = _issue(hass, entry.entry_id)
    assert again is not None
    assert again.active is True
    assert again.dismissed_version == ignored.dismissed_version


async def test_a_notice_raised_this_run_goes_when_the_counter_recovers_even_if_ignored(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ignore answers one hold: once the hold is over the record goes."""
    entry, coordinator = await _held_for_a_day(
        hass, monkeypatch, _frame(DAY, solar_total_kwh=BASE + 1.0)
    )
    ir.async_ignore_issue(hass, DOMAIN, _issue_id(entry.entry_id), True)

    await coordinator.async_refresh()

    assert _issue(hass, entry.entry_id) is None


async def test_accepting_the_notice_shows_the_device_value_before_the_next_poll(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entity does not fall back to the restored high value while it waits."""
    entry, coordinator = await _held_for_a_day(hass, monkeypatch)
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{SERIAL}_{SOLAR}"
    )
    assert entity_id is not None
    shown = hass.states.get(entity_id)
    assert shown is not None
    assert float(shown.state) == pytest.approx(BASE, abs=0.01)

    assert coordinator.accept_device_counter(SOLAR) is True
    await hass.async_block_till_done()

    shown = hass.states.get(entity_id)
    assert shown is not None
    assert float(shown.state) == pytest.approx(LOW, abs=0.01)
    assert await hass.config_entries.async_unload(entry.entry_id)
