"""The coordinator side of the PowerPulse 2 settings writes (PLAN-172, #480-#482).

Coordinator level only: the number, select and switch platforms come later, so
every test calls the `async_set_powerpulse_*` methods directly. Wiring and
helpers are the two-coordinator setup (a PowerOcean and a PowerPulse 2, `C376`,
each with its own mocked MQTT client) `test_powerpulse2_charge_action.py`
built - imported rather than duplicated. Settings reports are the real
`241/44` frames of the 2026-10-04 afternoon recording, applied through the
wallbox's own ingest path; the synthetic ones (Smart mode, a bit pattern no
frame on file carries) are parsed dicts handed to `_apply_data`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.parsers.stream_proto import _iter_fields
from custom_components.ecoflow_energy.ecoflow.proto import ecocharge_pb2
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)
from tests.ha.test_powerpulse2_charge_action import (
    POWEROCEAN_DEVICE,
    TOPIC,
    _apply_status,
    _mqtt,
    _set_descriptor,
    _wire_entry,
)

_SET_COMMANDS = "custom_components.ecoflow_energy.coordinator.set_commands"

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "powerpulse"
    / "c376_settings_480_afternoon_20261004.json"
)

# Frames of that recording, by index, with what their settings block reports
# (switchBits / work mode / Solar minimum, A). Decoded once through the
# production parser; the assertions below do not re-derive them.
_REPORT_CONTINUOUS_ON = 1  # 18 / 2 (Solar) / 6.0
_REPORT_CONTINUOUS_OFF = 4  # 2 / 2 (Solar) / 7.0
_REPORT_PHASE_AUTO = 1  # also ev_phase_setting 0

_FIELD_NAMES = {
    1: "switch_bits",
    2: "work_mode",
    4: "solar_current_min",
    5: "phase_specified",
    6: "user_current_set",
}


def _frame_hex(index: int) -> bytes:
    data = json.loads(_FIXTURE_PATH.read_text())
    return bytes.fromhex(data["frames"][index]["hex"])


def _apply_frame(wallbox: EcoFlowDeviceCoordinator, index: int) -> dict[str, Any]:
    """Apply one real recorded frame the way a real MQTT message arrives."""
    parsed = wallbox._parse_message(TOPIC, _frame_hex(index))
    assert parsed is not None and "ev_settings_switch_bits" in parsed
    wallbox._apply_data(parsed)
    return parsed


def _apply_report(wallbox: EcoFlowDeviceCoordinator, **settings: Any) -> None:
    """Apply a synthetic settings report (a parsed dict, no recorded frame)."""
    wallbox._apply_data(dict(settings))


def _published_fields(call_args: Any) -> dict[int, int]:
    """The integer fields of the published `EDevPileParamSet`, read through the
    protobuf bindings and not through the builder that wrote them: exactly the
    fields carrying presence."""
    payload = call_args.args[0]
    pdata = bytes.fromhex(decode_header_message(payload)[0][0]["pdata"])
    fields: dict[int, int] = {}
    for field_num, _wire, raw in _iter_fields(pdata):
        if field_num != 4:
            continue
        message = ecocharge_pb2.EDevPileParamSet()
        message.ParseFromString(raw)
        for number, name in _FIELD_NAMES.items():
            if message.HasField(name):
                fields[number] = getattr(message, name)
    return fields


async def _start(
    coro: Coroutine[Any, Any, None],
) -> asyncio.Task[None]:
    """Run a write until it has published and is waiting for its read-back."""
    task = asyncio.create_task(coro)
    await asyncio.sleep(0.05)
    return task


async def test_continuous_rebuilds_the_byte_from_the_latest_report(
    hass: HomeAssistant,
) -> None:
    """OFF from a report with switchBits 18 publishes {1:2, 2:2, 4:60}; ON from
    the next report (switchBits 2, Solar minimum 7 A) publishes {1:18, 2:2,
    4:70}. The bits, the mode and the Solar minimum all come from the report
    frame: the store is made to disagree on purpose before the first write.

    Mutation probes: sending `{1: 16, ...}` instead of `bits | 0x10` makes the
    ON half fail (18 is not 16); reading the mode and Solar minimum from
    `_device_data` instead of the report snapshot makes the OFF half fail (the
    store says mode 3 and 9 A).
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    send = _mqtt(oceans[0]).send_proto_set

    _apply_frame(wallbox, _REPORT_CONTINUOUS_ON)
    wallbox.set_device_value("ev_settings_work_mode", 3)
    wallbox.set_device_value("ev_solar_min_current_a", 9.0)

    task = await _start(wallbox.async_set_powerpulse_continuous_charging(False))
    assert send.call_count == 1
    assert _published_fields(send.call_args) == {1: 2, 2: 2, 4: 60}
    _apply_frame(wallbox, _REPORT_CONTINUOUS_OFF)  # switchBits 2 confirms
    await task
    assert wallbox._wallbox_action_pending is None

    task = await _start(wallbox.async_set_powerpulse_continuous_charging(True))
    assert send.call_count == 2
    assert _published_fields(send.call_args) == {1: 18, 2: 2, 4: 70}
    _apply_frame(wallbox, _REPORT_CONTINUOUS_ON)  # switchBits 18 confirms
    await task
    assert wallbox._wallbox_action_pending is None


async def test_continuous_refuses_a_report_older_than_the_max_age(
    hass: HomeAssistant,
) -> None:
    """A report 11 s old refuses and publishes nothing; one 9 s old, the same
    report, publishes. The clock is the one `set_commands` reads, set relative
    to the report's own stamp, so the 10 s limit is bracketed on both sides.

    Mutation probes: dropping the age comparison in the Continuous plan makes
    the stale half fail (no refusal); changing `POWERPULSE2_SETTINGS_MAX_AGE_S`
    from 10 to 100 makes it fail the same way.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    send = _mqtt(oceans[0]).send_proto_set
    _apply_frame(wallbox, _REPORT_CONTINUOUS_ON)
    assert wallbox._settings_report is not None
    stamped = wallbox._settings_report[0]

    with (
        patch(f"{_SET_COMMANDS}.time", SimpleNamespace(monotonic=lambda: stamped + 11)),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await wallbox.async_set_powerpulse_continuous_charging(False)
    assert excinfo.value.translation_key == "powerpulse_continuous_report_stale"
    assert send.call_count == 0
    assert wallbox._wallbox_action_pending is None

    with patch(f"{_SET_COMMANDS}.time", SimpleNamespace(monotonic=lambda: stamped + 9)):
        task = await _start(wallbox.async_set_powerpulse_continuous_charging(False))
        assert send.call_count == 1
    _apply_frame(wallbox, _REPORT_CONTINUOUS_OFF)
    await task


async def test_a_bit_write_confirms_on_the_frame_and_only_on_its_bit(
    hass: HomeAssistant,
) -> None:
    """The store already holds the requested bits; a frame without the key must
    not confirm, nor a frame whose Continuous bit is still set. A frame with
    the bit cleared confirms even though its other bits (3 against the
    requested 2) differ: only the masked bit is the write's business.

    Mutation probes: reading the value from the store (`self._device_data.get(
    record.state_key)` in place of `parsed[record.state_key]` in
    `_resolve_wallbox_action`) makes the frame whose bit 0x10 is still set
    confirm, because the store holds the requested 2 on purpose; comparing
    the whole byte instead of the masked bit makes the final frame (3) fail
    to confirm. Dropping the `state_key not in parsed` return fails it too,
    but through a KeyError on the keyless frame, not through a confirmation.
    """
    _entry_obj, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _apply_frame(wallbox, _REPORT_CONTINUOUS_ON)
    wallbox.set_device_value("ev_settings_switch_bits", 2)

    task = await _start(wallbox.async_set_powerpulse_continuous_charging(False))
    pending = wallbox._wallbox_action_pending
    assert pending is not None and not pending.future.done()

    _apply_status(wallbox, 1)  # a frame with no settings key at all
    assert not pending.future.done()
    _apply_frame(wallbox, _REPORT_CONTINUOUS_ON)  # bit 0x10 still set
    assert not pending.future.done()
    assert not task.done()

    _apply_report(wallbox, ev_settings_switch_bits=3)  # bit 0x10 cleared, bit 0x01 set
    await task
    assert wallbox._wallbox_action_pending is None


async def test_current_and_phase_writes_send_their_own_field_alone(
    hass: HomeAssistant,
) -> None:
    """Solar minimum 7 A is {4:70}, Custom current 10 A is {6:100} and phase
    Auto is {5:0}, each alone: no mode, no switch bits, and the value 0 on the
    wire. Each confirms on its own read-back key.

    Mutation probe: adding the reported mode to the Solar minimum write
    (`{2: 2, 4: ...}`) makes the first assertion fail.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set

    task = await _start(wallbox.async_set_powerpulse_solar_min_current(7))
    assert _published_fields(send.call_args) == {4: 70}
    _apply_frame(wallbox, _REPORT_CONTINUOUS_OFF)  # reports 7.0 A
    await task

    task = await _start(wallbox.async_set_powerpulse_custom_current(10))
    assert _published_fields(send.call_args) == {6: 100}
    _apply_report(wallbox, ev_settings_switch_bits=18, ev_custom_current_a=10.0)
    await task

    task = await _start(wallbox.async_set_powerpulse_phase_setting("auto"))
    assert _published_fields(send.call_args) == {5: 0}
    _apply_frame(wallbox, _REPORT_PHASE_AUTO)  # reports phase 0
    await task
    assert send.call_count == 3
    assert wallbox._wallbox_action_pending is None


_WRITES: list[tuple[str, Callable[[EcoFlowDeviceCoordinator], Any], str]] = [
    (
        "solar_min",
        lambda w: w.async_set_powerpulse_solar_min_current(7),
        "powerpulse_solar_min_current_needs_powerocean",
    ),
    (
        "custom",
        lambda w: w.async_set_powerpulse_custom_current(10),
        "powerpulse_custom_current_needs_powerocean",
    ),
    (
        "phase",
        lambda w: w.async_set_powerpulse_phase_setting("auto"),
        "powerpulse_phase_setting_needs_powerocean",
    ),
    (
        "continuous",
        lambda w: w.async_set_powerpulse_continuous_charging(True),
        "powerpulse_continuous_needs_powerocean",
    ),
]


@pytest.mark.parametrize(
    ("write_id", "write", "needs_key"), _WRITES, ids=[w[0] for w in _WRITES]
)
@pytest.mark.parametrize(
    "oceans_wired", [0, 2], ids=["no_powerocean", "two_poweroceans"]
)
async def test_without_exactly_one_powerocean_every_write_refuses(
    hass: HomeAssistant,
    write_id: str,
    write: Callable[[EcoFlowDeviceCoordinator], Any],
    needs_key: str,
    oceans_wired: int,
) -> None:
    """No PowerOcean refuses with the write's own `needs_powerocean` key, two
    refuse with `powerpulse_sibling_missing`; none publishes anything, on any
    client. A fresh settings report is applied first so the Continuous write
    is refused for its route and not for a missing report.

    Mutation probe: dropping the `route == "own"` refusal in
    `_async_write_powerpulse_settings` makes the no-PowerOcean cases fail
    (they reach the sibling lookup and refuse with another key).
    """
    devices = [POWEROCEAN_DEVICE, {**POWEROCEAN_DEVICE, "sn": "HJ31TEST00000002"}]
    _entry_obj, oceans, wallbox = _wire_entry(hass, devices[:oceans_wired])
    _set_descriptor(wallbox)
    _apply_frame(wallbox, _REPORT_CONTINUOUS_ON)

    with pytest.raises(HomeAssistantError) as excinfo:
        await write(wallbox)

    expected = needs_key if oceans_wired == 0 else "powerpulse_sibling_missing"
    assert excinfo.value.translation_key == expected
    for ocean in oceans:
        assert _mqtt(ocean).send_proto_set.call_count == 0
    assert _mqtt(wallbox).send_proto_set.call_count == 0
    assert wallbox._wallbox_action_pending is None


async def test_continuous_refuses_a_smart_mode_report_and_follows_other_modes(
    hass: HomeAssistant,
) -> None:
    """A report in Smart mode (4) refuses: a bare mode write to Smart is
    unobserved. The control: a report in Custom mode (3) publishes with mode 3
    in the write, so the mode is the reported one and not a constant.

    Mutation probe: removing the `mode == 4` refusal makes the Smart half fail
    (the write goes out with `{2: 4}` instead of refusing).
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set

    _apply_report(
        wallbox,
        ev_settings_switch_bits=2,
        ev_settings_work_mode=4,
        ev_solar_min_current_a=6.0,
    )
    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_continuous_charging(True)
    assert excinfo.value.translation_key == "powerpulse_continuous_smart_mode"
    assert send.call_count == 0
    assert wallbox._wallbox_action_pending is None

    _apply_report(
        wallbox,
        ev_settings_switch_bits=2,
        ev_settings_work_mode=3,
        ev_solar_min_current_a=6.0,
    )
    task = await _start(wallbox.async_set_powerpulse_continuous_charging(True))
    assert _published_fields(send.call_args) == {1: 18, 2: 3, 4: 60}
    _apply_report(wallbox, ev_settings_switch_bits=18)
    await task


_REPORT_MISSING = "powerpulse_continuous_report_missing"
_REPORT_UNUSABLE = "powerpulse_continuous_report_unusable"


async def test_continuous_without_any_settings_report_refuses_as_missing(
    hass: HomeAssistant,
) -> None:
    """A wallbox that has delivered no settings report leaves nothing to rebuild
    the switch byte from: the write refuses with `report_missing`, publishes
    nothing and leaves no pending record.

    Mutation probe: letting the plan run on `_settings_report is None` (the
    refusal dropped) raises a TypeError on the unpacking instead of the
    refusal, so the `HomeAssistantError` expectation fails.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    assert wallbox._settings_report is None

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_continuous_charging(True)

    assert excinfo.value.translation_key == _REPORT_MISSING
    assert send.call_count == 0
    assert wallbox._wallbox_action_pending is None


@pytest.mark.parametrize(
    "report",
    [
        {"ev_settings_switch_bits": 2, "ev_solar_min_current_a": 6.0},
        {"ev_settings_switch_bits": 2, "ev_settings_work_mode": 2},
        {
            "ev_settings_switch_bits": 2.0,
            "ev_settings_work_mode": 2,
            "ev_solar_min_current_a": 6.0,
        },
    ],
    ids=["no_mode", "no_solar_min", "bits_not_an_int"],
)
async def test_continuous_refuses_a_report_missing_a_field_it_needs(
    hass: HomeAssistant, report: dict[str, Any]
) -> None:
    """A report that carries the switch bits but not the mode, or not the Solar
    minimum, or carries the bits as a float, refuses with `report_missing`. The
    store holds a valid mode and Solar minimum on purpose: the write rebuilds
    from the report snapshot, so the store cannot fill the gap.

    Mutation probe: dropping the `type(mode) is not int` clause makes the
    `no_mode` case reach the range check and refuse with `report_unusable`
    instead, so the key assertion fails.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    wallbox.set_device_value("ev_settings_work_mode", 2)
    wallbox.set_device_value("ev_solar_min_current_a", 6.0)
    _apply_report(wallbox, **report)

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_continuous_charging(True)

    assert excinfo.value.translation_key == _REPORT_MISSING
    assert send.call_count == 0
    assert wallbox._wallbox_action_pending is None


@pytest.mark.parametrize(
    ("report", "field", "value"),
    [
        (
            {
                "ev_settings_switch_bits": 2,
                "ev_settings_work_mode": 2,
                "ev_solar_min_current_a": 17.0,
            },
            "Solar minimum current",
            "17.0 A",
        ),
        (
            {
                "ev_settings_switch_bits": 2,
                "ev_settings_work_mode": 0,
                "ev_solar_min_current_a": 6.0,
            },
            "charging mode",
            "0",
        ),
        (
            {
                "ev_settings_switch_bits": 256,
                "ev_settings_work_mode": 2,
                "ev_solar_min_current_a": 6.0,
            },
            "switch bits",
            "256",
        ),
    ],
    ids=["solar_min_17", "mode_0", "bits_256"],
)
async def test_continuous_refuses_an_out_of_range_report_as_unusable(
    hass: HomeAssistant, report: dict[str, Any], field: str, value: str
) -> None:
    """A report that exists but carries a value the builder cannot send (a
    Solar minimum above the 16 A range, mode 0, a switch byte above 255)
    refuses with `report_unusable`, naming the field and the value: waiting for
    the next report cannot help, so it must not read as `report_missing`.

    Mutation probe: raising `report_missing` from the out-of-range branch
    again (the pre-fix behaviour) fails every case on the key.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _apply_report(wallbox, **report)

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_continuous_charging(True)

    assert excinfo.value.translation_key == _REPORT_UNUSABLE
    assert excinfo.value.translation_placeholders == {"field": field, "value": value}
    assert send.call_count == 0
    assert wallbox._wallbox_action_pending is None


async def test_a_handed_over_frame_is_not_a_settings_report(
    hass: HomeAssistant,
) -> None:
    """A frame handed over from a sibling (`own_connection=False`) that carries
    settings keys leaves the settings report untouched: it is no report this
    wallbox received. The control, the same dict on the wallbox's own
    connection, replaces the snapshot, so the assertion cannot hold merely
    because nothing ever records a report.

    Mutation probe: dropping the `if own_connection:` guard around
    `_record_settings_report` in `_apply_data` makes the hand-over replace the
    snapshot and the identity assertion fail.
    """
    _entry_obj, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _apply_frame(wallbox, _REPORT_CONTINUOUS_ON)
    before = wallbox._settings_report
    assert before is not None
    handed = {
        "ev_settings_switch_bits": 2,
        "ev_settings_work_mode": 1,
        "ev_solar_min_current_a": 9.0,
    }

    wallbox._apply_data(dict(handed), own_connection=False)
    assert wallbox._settings_report is before

    wallbox._apply_data(dict(handed), own_connection=True)
    assert wallbox._settings_report is not before
    assert wallbox._settings_report is not None
    assert wallbox._settings_report[1] == handed
