"""Repairs notice for a Local inverter that has not answered for 30 minutes."""

from __future__ import annotations

import json
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.ecoflow_energy.const import (
    DOMAIN,
    LOCAL_MODBUS_FAILURES_UNAVAILABLE,
)
from custom_components.ecoflow_energy.coordinator import local_modbus
from custom_components.ecoflow_energy.coordinator.local_modbus import (
    EcoFlowLocalModbusCoordinator,
)
from custom_components.ecoflow_energy.ecoflow.const import device_log_tag
from custom_components.ecoflow_energy.ecoflow.modbus_local import ModbusTimeoutError

from .test_local_modbus_coordinator import (
    DAY,
    SERIAL,
    _coordinator,
    _frame,
    _local_entry,
    _set_up_local_entry,
)

N = LOCAL_MODBUS_FAILURES_UNAVAILABLE
TIMEOUT = ModbusTimeoutError("no answer")
# A fixed positive start: the monotonic clock of a fresh CI container may read
# a few seconds (`test-time-mocking.md`).
T0 = 1000.0
# The boundary is spelled out, not read from the constant: a test that adds the
# constant to the start time passes whatever the constant is set to.
THIRTY_MINUTES = 30 * 60
# Silent polls to script after the device has turned unavailable.
MORE = 12


def _clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Replace the coordinator's monotonic clock with one the test moves."""
    clock = {"now": T0}
    monkeypatch.setattr(local_modbus, "_monotonic", lambda: clock["now"])
    return clock


def _issue(hass: HomeAssistant, entry_id: str) -> ir.IssueEntry | None:
    tag = device_log_tag(SERIAL)
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"local_device_not_answering_{entry_id}_{tag}"
    )


def _spy_creates(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    created: list[str] = []
    real = ir.async_create_issue

    def spy(*args: Any, **kwargs: Any) -> None:
        created.append(args[2])
        real(*args, **kwargs)

    monkeypatch.setattr(
        "custom_components.ecoflow_energy.coordinator.local_modbus.ir.async_create_issue",
        spy,
    )
    return created


async def _turn_unavailable(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, *tail: Any
) -> tuple[Any, EcoFlowLocalModbusCoordinator, dict[str, float]]:
    """One answered poll, then N failures: the device turns unavailable at T0.

    ``tail`` is appended to the script after MORE further silent polls.
    """
    clock = _clock(monkeypatch)
    entry = _local_entry()
    coordinator = _coordinator(
        hass, entry, [_frame(DAY), *[TIMEOUT] * (N + MORE), *tail]
    )
    await coordinator.async_refresh()
    for _ in range(N):
        await coordinator.async_refresh()
    assert coordinator.device_available is False
    assert _issue(hass, entry.entry_id) is None
    return entry, coordinator, clock


async def test_no_notice_one_second_before_thirty_minutes(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: polls keep failing, the clock stops one second short."""
    entry, coordinator, clock = await _turn_unavailable(hass, monkeypatch)

    clock["now"] = T0 + THIRTY_MINUTES - 1
    for _ in range(3):
        await coordinator.async_refresh()

    assert coordinator.device_available is False
    assert _issue(hass, entry.entry_id) is None


async def test_the_notice_appears_at_thirty_minutes_and_carries_no_serial(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rendered result: the registry entry, its text key and its placeholder."""
    entry, coordinator, clock = await _turn_unavailable(hass, monkeypatch)

    clock["now"] = T0 + THIRTY_MINUTES
    await coordinator.async_refresh()

    issue = _issue(hass, entry.entry_id)
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "local_device_not_answering"
    assert issue.translation_placeholders == {"device": device_log_tag(SERIAL)}
    assert issue.active is True
    assert SERIAL not in json.dumps(
        [issue.issue_id, issue.data, issue.translation_placeholders]
    )


async def test_one_notice_per_outage_not_one_per_failed_poll(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Many failed polls past the threshold write the registry once."""
    entry, coordinator, clock = await _turn_unavailable(hass, monkeypatch)
    created = _spy_creates(monkeypatch)

    clock["now"] = T0 + THIRTY_MINUTES
    for _ in range(6):
        await coordinator.async_refresh()
        clock["now"] += 2

    issue = _issue(hass, entry.entry_id)
    assert issue is not None
    assert created == [issue.issue_id]


async def test_the_first_answered_poll_deletes_the_notice(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, coordinator, clock = await _turn_unavailable(hass, monkeypatch, _frame(DAY))
    clock["now"] = T0 + THIRTY_MINUTES
    await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is not None
    # Burn the remaining silent polls so the next one is the answered frame.
    for _ in range(MORE - 1):
        await coordinator.async_refresh()

    await coordinator.async_refresh()

    assert coordinator.device_available is True
    assert _issue(hass, entry.entry_id) is None


async def test_a_second_outage_raises_the_notice_again(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, coordinator, clock = await _turn_unavailable(
        hass,
        monkeypatch,
        _frame(DAY),
        *[TIMEOUT] * (N + 2),
    )
    created = _spy_creates(monkeypatch)
    clock["now"] = T0 + THIRTY_MINUTES
    for _ in range(MORE):
        await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is not None
    assert len(created) == 1

    # The answered poll ends outage one; N failures begin outage two.
    await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is None
    second_start = clock["now"]
    for _ in range(N):
        await coordinator.async_refresh()
    assert coordinator.device_available is False
    assert _issue(hass, entry.entry_id) is None

    # The second outage is timed from its own start, not from the first.
    clock["now"] = second_start + THIRTY_MINUTES - 1
    await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is None
    clock["now"] = second_start + THIRTY_MINUTES
    await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is not None
    assert len(created) == 2


async def test_an_outage_that_ends_before_thirty_minutes_raises_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Twenty minutes silent, an answer, twenty minutes more: no notice.

    Together the two gaps exceed 30 minutes, so an outage clock that carried
    over the recovery would raise the notice at the end of the second gap.
    """
    created = _spy_creates(monkeypatch)
    entry, coordinator, clock = await _turn_unavailable(
        hass, monkeypatch, _frame(DAY), *[TIMEOUT] * (N + MORE)
    )
    clock["now"] = T0 + 1200
    for _ in range(MORE):
        await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert coordinator.device_available is True

    for _ in range(N):
        await coordinator.async_refresh()
    assert coordinator.device_available is False
    clock["now"] = T0 + 1200 + 1200
    await coordinator.async_refresh()

    assert _issue(hass, entry.entry_id) is None
    assert created == []


async def test_unloading_the_entry_removes_the_open_notice(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _clock(monkeypatch)
    entry = _local_entry()
    assert await _set_up_local_entry(
        hass, entry, [_frame(DAY), *[TIMEOUT] * (N + MORE)]
    )
    coordinator = hass.data[DOMAIN][entry.entry_id][SERIAL]
    for _ in range(N):
        await coordinator.async_refresh()
    clock["now"] = T0 + THIRTY_MINUTES
    await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert _issue(hass, entry.entry_id) is None
