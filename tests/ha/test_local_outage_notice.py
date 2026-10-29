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
from custom_components.ecoflow_energy.ecoflow.modbus_local import (
    ModbusExceptionResponse,
    ModbusTimeoutError,
)

from .test_local_modbus_coordinator import (
    DAY,
    SERIAL,
    _coordinator,
    _frame,
    _frame_of_another_device,
    _local_entry,
    _set_up_local_entry,
)

N = LOCAL_MODBUS_FAILURES_UNAVAILABLE
TIMEOUT = ModbusTimeoutError("no answer")
REFUSAL = ModbusExceptionResponse(2, 0x0870)
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


def _issue_id(entry_id: str) -> str:
    return f"local_device_not_answering_{entry_id}_{device_log_tag(SERIAL)}"


def _issue(hass: HomeAssistant, entry_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry_id))


def _seed_notice(hass: HomeAssistant, entry_id: str) -> None:
    """A notice an earlier run raised, which this run did not."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="local_device_not_answering",
        translation_placeholders={"device": device_log_tag(SERIAL)},
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
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    *tail: Any,
    silent: int = MORE,
    error: Exception = TIMEOUT,
) -> tuple[Any, EcoFlowLocalModbusCoordinator, dict[str, float]]:
    """One answered poll, then N failures ten seconds apart.

    The device turns unavailable on the fifth failure; ``clock["started"]`` is
    the time of that failure, the moment the outage clock must start at (a
    clock that started at the first failure would be 40 s early). ``silent``
    further failures and then ``tail`` follow in the script.
    """
    clock = _clock(monkeypatch)
    entry = _local_entry()
    coordinator = _coordinator(
        hass, entry, [_frame(DAY), *[error] * (N + silent), *tail]
    )
    await coordinator.async_refresh()
    for failure in range(N):
        clock["now"] = T0 + 10.0 * failure
        await coordinator.async_refresh()
    clock["started"] = clock["now"]
    assert clock["started"] == T0 + 10.0 * (N - 1)
    assert coordinator.device_available is False
    assert _issue(hass, entry.entry_id) is None
    return entry, coordinator, clock


async def test_no_notice_one_second_before_thirty_minutes(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: polls keep failing, the clock stops one second short."""
    entry, coordinator, clock = await _turn_unavailable(hass, monkeypatch)

    clock["now"] = clock["started"] + THIRTY_MINUTES - 1
    for _ in range(3):
        await coordinator.async_refresh()

    assert coordinator.device_available is False
    assert _issue(hass, entry.entry_id) is None


async def test_the_notice_appears_at_thirty_minutes_and_carries_no_serial(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rendered result: the registry entry, its text key and its placeholder."""
    entry, coordinator, clock = await _turn_unavailable(hass, monkeypatch)

    clock["now"] = clock["started"] + THIRTY_MINUTES
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

    clock["now"] = clock["started"] + THIRTY_MINUTES
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
    clock["now"] = clock["started"] + THIRTY_MINUTES
    await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is not None
    # Burn the remaining silent polls so the next one is the answered frame.
    for _ in range(MORE - 1):
        await coordinator.async_refresh()

    await coordinator.async_refresh()

    assert coordinator.device_available is True
    assert _issue(hass, entry.entry_id) is None


async def test_recovery_deletes_an_ignored_notice(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ignore holds for the ongoing outage and goes with it."""
    entry, coordinator, clock = await _turn_unavailable(hass, monkeypatch, _frame(DAY))
    clock["now"] = clock["started"] + THIRTY_MINUTES
    await coordinator.async_refresh()
    ir.async_ignore_issue(hass, DOMAIN, _issue_id(entry.entry_id), True)
    ignored = _issue(hass, entry.entry_id)
    assert ignored is not None
    assert ignored.dismissed_version is not None
    # Still silent: an Ignored notice is neither re-raised nor deleted.
    for _ in range(MORE - 1):
        await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is not None

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
    clock["now"] = clock["started"] + THIRTY_MINUTES
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
    first_start = clock["started"]
    clock["now"] = first_start + 1200
    for _ in range(MORE):
        await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert coordinator.device_available is True

    for _ in range(N):
        await coordinator.async_refresh()
    assert coordinator.device_available is False
    clock["now"] = first_start + 1200 + 1200
    await coordinator.async_refresh()

    assert _issue(hass, entry.entry_id) is None
    assert created == []


async def test_a_serial_mismatch_for_more_than_thirty_minutes_raises_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device that answers with another serial has answered: no restart advice."""
    created = _spy_creates(monkeypatch)
    entry, coordinator, clock = await _turn_unavailable(
        hass, monkeypatch, *[_frame_of_another_device(DAY)] * 8, silent=0
    )

    clock["now"] = clock["started"] + 2 * THIRTY_MINUTES
    for _ in range(4):
        await coordinator.async_refresh()

    assert coordinator.device_available is False
    assert coordinator.last_error is not None
    assert "serial mismatch" in coordinator.last_error
    assert _issue(hass, entry.entry_id) is None
    assert created == []


async def test_a_refused_read_for_more_than_thirty_minutes_raises_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception answer is an answer, too."""
    created = _spy_creates(monkeypatch)
    entry, coordinator, clock = await _turn_unavailable(
        hass, monkeypatch, silent=6, error=REFUSAL
    )

    clock["now"] = clock["started"] + 2 * THIRTY_MINUTES
    for _ in range(4):
        await coordinator.async_refresh()

    assert coordinator.device_available is False
    assert coordinator.last_error is not None
    assert "ModbusExceptionResponse" in coordinator.last_error
    assert _issue(hass, entry.entry_id) is None
    assert created == []


async def test_silence_after_a_mismatch_starts_the_clock_at_the_first_silent_poll(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, coordinator, clock = await _turn_unavailable(
        hass,
        monkeypatch,
        _frame_of_another_device(DAY),
        *[TIMEOUT] * 6,
        silent=0,
    )
    started = clock["started"]

    clock["now"] = started + 100
    await coordinator.async_refresh()  # the mismatch: clock off
    clock["now"] = started + 900
    await coordinator.async_refresh()  # first silent poll: clock starts here
    silent_since = clock["now"]

    # Past 30 minutes from the first failure, short of 30 from the new start.
    clock["now"] = started + THIRTY_MINUTES + 60
    await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is None
    clock["now"] = silent_since + THIRTY_MINUTES - 1
    await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is None

    clock["now"] = silent_since + THIRTY_MINUTES
    await coordinator.async_refresh()
    assert _issue(hass, entry.entry_id) is not None


async def test_an_ignored_notice_survives_the_unload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reload while the device is silent keeps the notice, Ignore and all."""
    clock = _clock(monkeypatch)
    entry = _local_entry()
    assert await _set_up_local_entry(
        hass, entry, [_frame(DAY), *[TIMEOUT] * (N + MORE)]
    )
    coordinator = hass.data[DOMAIN][entry.entry_id][SERIAL]
    for failure in range(N):
        clock["now"] = T0 + 10.0 * failure
        await coordinator.async_refresh()
    clock["now"] += THIRTY_MINUTES
    await coordinator.async_refresh()
    ir.async_ignore_issue(hass, DOMAIN, _issue_id(entry.entry_id), True)
    ignored = _issue(hass, entry.entry_id)
    assert ignored is not None
    assert ignored.dismissed_version is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    kept = _issue(hass, entry.entry_id)
    assert kept is not None
    assert kept.dismissed_version == ignored.dismissed_version


async def test_a_notice_of_an_earlier_run_goes_with_the_first_answered_poll(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reload left it behind; the new run did not raise it and still clears it."""
    _clock(monkeypatch)
    entry = _local_entry()
    coordinator = _coordinator(hass, entry, [_frame(DAY)])
    _seed_notice(hass, entry.entry_id)
    assert _issue(hass, entry.entry_id) is not None

    await coordinator.async_refresh()

    assert coordinator.last_poll_ok is True
    assert _issue(hass, entry.entry_id) is None


async def test_a_notice_of_an_earlier_run_stays_while_the_new_run_keeps_failing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control to the one above: no answer, no deletion."""
    _clock(monkeypatch)
    entry = _local_entry()
    coordinator = _coordinator(hass, entry, [TIMEOUT] * 3 + [_frame(DAY)])
    _seed_notice(hass, entry.entry_id)

    for _ in range(3):
        await coordinator.async_refresh()
        assert coordinator.last_poll_ok is False
        assert _issue(hass, entry.entry_id) is not None

    await coordinator.async_refresh()
    assert coordinator.last_poll_ok is True
    assert _issue(hass, entry.entry_id) is None
