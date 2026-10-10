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

from custom_components.ecoflow_energy.const import POWERPULSE2_SWITCH_BIT_CONTINUOUS
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


def _set_continuous(
    wallbox: EcoFlowDeviceCoordinator, enabled: bool
) -> Coroutine[Any, Any, None]:
    """The Continuous charging write, called the way its switch calls it."""
    return wallbox.async_set_powerpulse_switch_bit(
        POWERPULSE2_SWITCH_BIT_CONTINUOUS, enabled, "ev_continuous_charging"
    )


async def test_continuous_writes_field_one_alone_from_the_latest_report(
    hass: HomeAssistant,
) -> None:
    """OFF from a report with switchBits 18 publishes exactly {1:2}; ON from
    the next report (switchBits 2) publishes exactly {1:18}. The published
    message is read back through the protobuf bindings, which track presence
    per field, so equality with a one-entry dict also means fields 2 (mode)
    and 4 (Solar minimum) carry no presence at all. The bits come from the
    report frame: the store is made to disagree on purpose before each write.

    Mutation probes: adding field 2 back to the write fails the OFF half (the
    published dict has two entries); sending `{1: 16}` instead of `bits | 0x10`
    fails the ON half (18 is not 16); reading the bits from `_device_data`
    instead of the report snapshot fails the OFF half (the store says 3, and a
    write built on it publishes `{1: 3}`).
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    send = _mqtt(oceans[0]).send_proto_set

    _apply_frame(wallbox, _REPORT_CONTINUOUS_ON)
    wallbox.set_device_value("ev_settings_switch_bits", 3)  # store: bit 0x10 clear

    task = await _start(_set_continuous(wallbox, False))
    assert send.call_count == 1
    assert _published_fields(send.call_args) == {1: 2}
    _apply_frame(wallbox, _REPORT_CONTINUOUS_OFF)  # switchBits 2 confirms
    await task
    assert wallbox._wallbox_action_pending is None

    wallbox.set_device_value("ev_settings_switch_bits", 0)  # store: all bits clear
    task = await _start(_set_continuous(wallbox, True))
    assert send.call_count == 2
    assert _published_fields(send.call_args) == {1: 18}
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
        await _set_continuous(wallbox, False)
    assert excinfo.value.translation_key == "powerpulse_continuous_report_stale"
    assert send.call_count == 0
    assert wallbox._wallbox_action_pending is None

    with patch(f"{_SET_COMMANDS}.time", SimpleNamespace(monotonic=lambda: stamped + 9)):
        task = await _start(_set_continuous(wallbox, False))
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

    task = await _start(_set_continuous(wallbox, False))
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
        lambda w: _set_continuous(w, True),
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


async def test_continuous_in_smart_mode_writes_field_one_alone(
    hass: HomeAssistant,
) -> None:
    """A report in Smart mode (4) no longer refuses: the write is {1:18} and
    nothing else, so no mode (not 4, not any other) goes on the wire. The
    control, a report in Custom mode (3), publishes the same {1:18}: the
    reported mode plays no part in what is sent.

    Mutation probes: restoring a Smart-mode refusal fails the first half (it
    raises instead of publishing); adding the reported mode back to the write
    fails the test on the first half (`{1: 18, 2: 4}` against `{1: 18}`).
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set

    _apply_report(
        wallbox,
        ev_settings_switch_bits=2,
        ev_settings_work_mode="smart",
        ev_solar_min_current_a=6.0,
    )
    task = await _start(_set_continuous(wallbox, True))
    assert send.call_count == 1
    assert _published_fields(send.call_args) == {1: 18}
    _apply_report(wallbox, ev_settings_switch_bits=18)
    await task
    assert wallbox._wallbox_action_pending is None

    _apply_report(
        wallbox,
        ev_settings_switch_bits=2,
        ev_settings_work_mode="custom",
        ev_solar_min_current_a=6.0,
    )
    task = await _start(_set_continuous(wallbox, True))
    assert send.call_count == 2
    assert _published_fields(send.call_args) == {1: 18}
    _apply_report(wallbox, ev_settings_switch_bits=18)
    await task
    assert wallbox._wallbox_action_pending is None


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
        await _set_continuous(wallbox, True)

    assert excinfo.value.translation_key == _REPORT_MISSING
    assert send.call_count == 0
    assert wallbox._wallbox_action_pending is None


@pytest.mark.parametrize(
    "report",
    [
        {"ev_settings_switch_bits": 2.0},
        {"ev_settings_switch_bits": None},
    ],
    ids=["bits_a_float", "bits_none"],
)
async def test_continuous_refuses_a_report_whose_bits_are_not_an_int(
    hass: HomeAssistant, report: dict[str, Any]
) -> None:
    """A report that carries the switch bits key but not as an int (a float, or
    None) refuses with `report_missing`, publishes nothing and leaves no
    pending record. The store holds valid bits on purpose: the write is built
    from the report snapshot, so the store cannot fill the gap.

    Mutation probes: loosening `type(bits) is not int` to an `isinstance` check
    that accepts the float makes `bits_a_float` publish instead of refusing;
    dropping the clause altogether makes both cases fail on a TypeError from
    the range comparison rather than the refusal.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    wallbox.set_device_value("ev_settings_switch_bits", 2)
    _apply_report(wallbox, **report)

    with pytest.raises(HomeAssistantError) as excinfo:
        await _set_continuous(wallbox, True)

    assert excinfo.value.translation_key == _REPORT_MISSING
    assert send.call_count == 0
    assert wallbox._wallbox_action_pending is None


@pytest.mark.parametrize(
    "report",
    [
        {"ev_settings_switch_bits": 2},
        {"ev_settings_switch_bits": 2, "ev_settings_work_mode": "solar"},
        {"ev_settings_switch_bits": 2, "ev_solar_min_current_a": 6.0},
    ],
    ids=["bits_only", "no_solar_min", "no_mode"],
)
async def test_continuous_writes_from_a_report_that_carries_only_the_bits(
    hass: HomeAssistant, report: dict[str, Any]
) -> None:
    """A report with the switch bits but without the mode, without the Solar
    minimum, or without both, now writes: the byte is all the write reads. The
    store holds no mode and no Solar minimum either, so nothing else can have
    stood in for the missing keys.

    Mutation probe: restoring the old requirement that the report carry the
    mode and the Solar minimum (a `report_missing` refusal when either is
    absent) makes every case raise instead of publishing.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    assert "ev_settings_work_mode" not in wallbox._device_data
    assert "ev_solar_min_current_a" not in wallbox._device_data
    _apply_report(wallbox, **report)

    task = await _start(_set_continuous(wallbox, True))
    assert send.call_count == 1
    assert _published_fields(send.call_args) == {1: 18}
    _apply_report(wallbox, ev_settings_switch_bits=18)
    await task
    assert wallbox._wallbox_action_pending is None


@pytest.mark.parametrize(
    ("bits", "value"),
    [(256, "256"), (-1, "-1")],
    ids=["bits_256", "bits_negative"],
)
async def test_continuous_refuses_bits_outside_a_byte_as_unusable(
    hass: HomeAssistant, bits: int, value: str
) -> None:
    """A report whose switch bits are not a byte (above 255, or negative)
    refuses with `report_unusable`, naming the field and the value: waiting for
    the next report cannot help, so it must not read as `report_missing`.

    Mutation probes: raising `report_missing` from the range branch fails both
    cases on the key; narrowing the range check to `bits <= 255` fails
    `bits_negative` (the refusal no longer carries the `report_unusable` key).
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    _apply_report(wallbox, ev_settings_switch_bits=bits)

    with pytest.raises(HomeAssistantError) as excinfo:
        await _set_continuous(wallbox, True)

    assert excinfo.value.translation_key == _REPORT_UNUSABLE
    assert excinfo.value.translation_placeholders == {
        "field": "switch bits",
        "value": value,
    }
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
        "ev_settings_work_mode": "fast",
        "ev_solar_min_current_a": 9.0,
    }

    wallbox._apply_data(dict(handed), own_connection=False)
    assert wallbox._settings_report is before

    wallbox._apply_data(dict(handed), own_connection=True)
    assert wallbox._settings_report is not before
    assert wallbox._settings_report is not None
    assert wallbox._settings_report[1] == handed


async def test_custom_current_above_the_reported_maximum_is_refused_at_once(
    hass: HomeAssistant,
) -> None:
    """15 A over a reported 14 A refuses before anything is published, naming
    both values; 14 A (equal) and 13 A (below) are written. This is the
    2026-10-05 observation on a C376: the wallbox never stored 15 A over 14 A.

    Mutation probes: dropping the comparison lets 15 A publish (first block
    fails); `>` changed to `>=` refuses the equal case (second block fails).
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    wallbox._device_data["ev_max_current_a"] = 14.0

    with pytest.raises(HomeAssistantError) as excinfo:
        await wallbox.async_set_powerpulse_custom_current(15)
    assert excinfo.value.translation_key == "powerpulse_custom_current_above_maximum"
    assert excinfo.value.translation_placeholders == {
        "requested": "15",
        "maximum": "14",
    }
    assert send.call_count == 0
    assert wallbox._wallbox_action_pending is None

    for allowed in (14, 13):
        task = await _start(wallbox.async_set_powerpulse_custom_current(allowed))
        assert _published_fields(send.call_args) == {6: allowed * 10}
        _apply_report(
            wallbox, ev_settings_switch_bits=18, ev_custom_current_a=float(allowed)
        )
        await task
    assert send.call_count == 2


async def test_custom_current_is_not_refused_while_the_maximum_is_unreported(
    hass: HomeAssistant,
) -> None:
    """No `ev_max_current_a` in the store: the write goes out as asked. The
    maximum is never guessed, so a wallbox that has not reported it yet is not
    blocked.

    Mutation probe: treating a missing maximum as 0 refuses the write.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    assert "ev_max_current_a" not in wallbox._device_data

    task = await _start(wallbox.async_set_powerpulse_custom_current(16))
    assert _published_fields(send.call_args) == {6: 160}
    _apply_report(wallbox, ev_settings_switch_bits=18, ev_custom_current_a=16.0)
    await task


async def test_solar_minimum_has_no_maximum_check(hass: HomeAssistant) -> None:
    """16 A Solar minimum over a reported 14 A maximum is written: the wallbox
    kept exactly this on 2026-10-05 and on 2026-10-04 (16 A over 15 A).

    Mutation probe: applying the Custom check to the Solar minimum write makes
    this refuse.
    """
    _entry_obj, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    _set_descriptor(wallbox)
    send = _mqtt(oceans[0]).send_proto_set
    wallbox._device_data["ev_max_current_a"] = 14.0

    task = await _start(wallbox.async_set_powerpulse_solar_min_current(16))
    assert _published_fields(send.call_args) == {4: 160}
    _apply_report(wallbox, ev_settings_switch_bits=18, ev_solar_min_current_a=16.0)
    await task


def test_the_above_maximum_message_exists_in_both_languages() -> None:
    """The refusal's translation key has an English and a German text carrying
    both placeholders, so the message names the numbers in either language."""
    base = Path("custom_components/ecoflow_energy")
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        message = json.loads((base / name).read_text(encoding="utf-8"))["exceptions"][
            "powerpulse_custom_current_above_maximum"
        ]["message"]
        assert "{requested}" in message
        assert "{maximum}" in message
