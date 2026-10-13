"""The coordinator side of the PowerGlow heating rod writes (PLAN-190, issue #7).

Coordinator level only: every test calls
`EcoFlowDeviceCoordinator.async_set_powerglow_value` or
`async_set_powerglow_operating_mode` directly on a PowerOcean coordinator with
a mocked MQTT client (the platforms are covered in `test_powerglow_controls.py`).
A write is confirmed on the arriving frame (`_apply_data` ->
`_resolve_pending_write`), never on the stored value, and only when the frame
names the rod the write was addressed to, so the tests feed the confirming
value through `_report` exactly like a `212/8` report would.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.energy_stream import (
    build_powerglow_param_set_payload,
)
from custom_components.ecoflow_energy.ecoflow.parsers.powerocean_proto import (
    remap_heating_rod_keys,
)
from custom_components.ecoflow_energy.ecoflow.parsers.stream_proto import _iter_fields
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)
from tests.ha.test_powerpulse2_charge_action import (
    POWEROCEAN_DEVICE,
    _connected_mqtt,
    _entry,
    _mqtt,
)

ROD_SN = "HF32TEST00000001"  # prefix HF32: ceiling 6000 W
OTHER_ROD_SN = "HF32TEST00000002"  # a second rod on the same PowerOcean
ROD_SN_DEFAULT_CEILING = "HF31TEST00000001"  # any other prefix: ceiling 3500 W
WINDOW = (
    "custom_components.ecoflow_energy.coordinator.set_commands.POWERGLOW_WRITE_WINDOW_S"
)


def _ocean(
    hass: HomeAssistant, rod_sn: str | None = ROD_SN, **seed: Any
) -> EcoFlowDeviceCoordinator:
    """A PowerOcean coordinator with a connected MQTT mock and a known rod."""
    entry = _entry(hass, [POWEROCEAN_DEVICE])
    ocean = EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN_DEVICE)
    ocean._mqtt_client = _connected_mqtt()
    if rod_sn is not None:
        ocean.set_device_value("heating_rod_sn", rod_sn)
    for key, value in seed.items():
        ocean.set_device_value(key, value)
    return ocean


def _report(ocean: EcoFlowDeviceCoordinator, **frame: Any) -> None:
    """Apply one `212/8` report of the rod the coordinator knows (it always
    carries the rod's serial, which is what ties a confirmation to its write)."""
    ocean._apply_data({"heating_rod_sn": ROD_SN, **frame})


def _pdata(payload: bytes) -> str:
    headers, _ = decode_header_message(payload)
    return str(headers[0]["pdata"])


def _published_pdata(ocean: EcoFlowDeviceCoordinator, call: int = 0) -> str:
    """The `pdata` hex of the n-th frame sent through the mocked client."""
    payload = _mqtt(ocean).send_proto_set.call_args_list[call].args[0]
    headers, _ = decode_header_message(payload)
    assert headers[0]["cmd_func"] == 212
    assert headers[0]["cmd_id"] == 99
    return str(headers[0]["pdata"])


def _expected_pdata(ocean: EcoFlowDeviceCoordinator, **setting: Any) -> str:
    return _pdata(build_powerglow_param_set_payload(ROD_SN, ocean.device_sn, **setting))


async def test_target_power_write_publishes_builder_bytes_and_confirms(
    hass: HomeAssistant,
) -> None:
    """The write sends exactly the builder's `pdata` and returns once a frame
    carrying the value is applied. 6000 W is the HF32 ceiling, so the boundary
    value is accepted.

    Mutation probe: dropping the `expected_value is not None` branch in
    `_resolve_pending_write` (or a wrong `state_key`) leaves the task waiting
    until the window ends, so `await task` raises instead of returning.
    """
    ocean = _ocean(hass)

    task = asyncio.create_task(ocean.async_set_powerglow_value("target_power", 6000))
    await asyncio.sleep(0.05)

    assert _mqtt(ocean).send_proto_set.call_count == 1
    assert _published_pdata(ocean) == _expected_pdata(ocean, target_power_w=6000)
    assert not task.done()

    _report(ocean, heating_rod_target_power_w=6000.0)
    await task
    assert ocean._pending_write is None
    assert any(e["type"] == "powerglow_target_power" for e in ocean._event_log)


async def test_unconfirmed_write_raises_not_confirmed_and_clears_the_record(
    hass: HomeAssistant,
) -> None:
    """A frame with another value does not confirm; the window ends in a
    translated error and the record is gone, so the next write is allowed.

    Mutation probe: confirming on any frame that carries the key (ignoring the
    value) makes the 1500.0 frame resolve the write and the test fail.
    """
    ocean = _ocean(hass)

    with patch(WINDOW, 0.1):
        task = asyncio.create_task(ocean.async_set_powerglow_value("target_temp", 60))
        await asyncio.sleep(0.02)
        _report(ocean, heating_rod_target_temp_c=55.0)
        with pytest.raises(HomeAssistantError) as err:
            await task

    assert err.value.translation_key == "powerglow_not_confirmed"
    assert ocean._pending_write is None
    assert any(
        e["type"] == "powerglow_target_temp_unconfirmed" for e in ocean._event_log
    )


@pytest.mark.parametrize(
    ("rod_sn", "setting", "value", "key", "bounds"),
    [
        (ROD_SN, "target_power", 6001, "powerglow_target_power_range", ("0", "6000")),
        (
            ROD_SN_DEFAULT_CEILING,
            "target_power",
            3501,
            "powerglow_target_power_range",
            ("0", "3500"),
        ),
        (ROD_SN, "target_power", -1, "powerglow_target_power_range", ("0", "6000")),
        (ROD_SN, "target_temp", 9, "powerglow_target_temp_range", ("10", "80")),
        (ROD_SN, "target_temp", 81, "powerglow_target_temp_range", ("10", "80")),
    ],
)
async def test_value_out_of_range_is_refused_before_anything_is_sent(
    hass: HomeAssistant,
    rod_sn: str,
    setting: str,
    value: int,
    key: str,
    bounds: tuple[str, str],
) -> None:
    """The ceiling follows the rod's serial prefix and the refusal names the
    limits; nothing is published and no record is left behind.

    Mutation probe: reading the ceiling from a fixed 9000 (or 3500) instead of
    `powerglow_max_power_w(sn)` turns the first or second case green on the
    wrong side.
    """
    ocean = _ocean(hass, rod_sn)

    with pytest.raises(HomeAssistantError) as err:
        await ocean.async_set_powerglow_value(setting, value)  # type: ignore[arg-type]

    assert err.value.translation_key == key
    assert err.value.translation_placeholders == {"min": bounds[0], "max": bounds[1]}
    assert _mqtt(ocean).send_proto_set.call_count == 0
    assert ocean._pending_write is None


@pytest.mark.parametrize("rod_sn", [None, ROD_SN[:15]])
async def test_missing_or_short_rod_serial_is_not_reported(
    hass: HomeAssistant, rod_sn: str | None
) -> None:
    """Without a reported 16-character rod serial nothing can be addressed.

    Mutation probe: dropping the `isinstance(rod_sn, str)` check makes the
    missing-serial case raise a TypeError instead of the translated error. The
    short serial is refused either way (a removed length check is caught later
    by the builder's ValueError, mapped to the same key), so that case pins the
    outcome, not the guard.
    """
    ocean = _ocean(hass, rod_sn)

    with pytest.raises(HomeAssistantError) as err:
        await ocean.async_set_powerglow_value("target_power", 1000)

    assert err.value.translation_key == "powerglow_not_reported"
    assert _mqtt(ocean).send_proto_set.call_count == 0


async def test_second_write_while_one_is_pending_is_refused(
    hass: HomeAssistant,
) -> None:
    """One write at a time: the second call is refused with the in-progress
    error while the first waits for its confirmation.

    Mutation probe: removing the `_pending_write is not None` guard sends a
    second frame, so `send_proto_set.call_count` is 2 and no error is raised.
    """
    ocean = _ocean(hass)
    first = asyncio.create_task(ocean.async_set_powerglow_value("target_power", 1000))
    await asyncio.sleep(0.05)

    with pytest.raises(HomeAssistantError) as err:
        await ocean.async_set_powerglow_value("target_temp", 50)

    assert err.value.translation_key == "powerglow_write_in_progress"
    assert _mqtt(ocean).send_proto_set.call_count == 1
    _report(ocean, heating_rod_target_power_w=1000.0)
    await first


async def test_solar_from_off_writes_mode_then_run_state_each_confirmed(
    hass: HomeAssistant,
) -> None:
    """Off + Manual -> Solar: the mode is written and confirmed first, the run
    switch only after, and the task ends when the second is confirmed.

    Mutation probe: swapping the two writes in `async_set_powerglow_operating_mode`
    makes the first published `pdata` the run switch and the order assertion
    fail.
    """
    ocean = _ocean(hass, heating_rod_mode=1, heating_rod_run_state=0)

    task = asyncio.create_task(ocean.async_set_powerglow_operating_mode("solar"))
    await asyncio.sleep(0.05)
    assert _mqtt(ocean).send_proto_set.call_count == 1
    assert _published_pdata(ocean, 0) == _expected_pdata(ocean, mode=0)

    _report(ocean, heating_rod_mode=0)
    await asyncio.sleep(0.05)
    assert _mqtt(ocean).send_proto_set.call_count == 2
    assert _published_pdata(ocean, 1) == _expected_pdata(ocean, run_on=True)
    assert not task.done()

    # A running rod reports a non-zero run state that need not be 1 (the parser
    # reads every non-zero value as on), so the run step is confirmed on the
    # derived option the select shows, here with run state 2.
    _report(
        ocean,
        heating_rod_run_state=2,
        heating_rod_mode=0,
        heating_rod_operating_mode="solar",
    )
    await task
    assert ocean._pending_write is None


async def test_off_is_one_write_of_the_run_switch(hass: HomeAssistant) -> None:
    """Off sends a single `run_on=False` and confirms on the derived option
    "off" (the select's own key), not on a frame that only repeats the mode.

    Mutation probes: making the off branch write `run_on=True` changes the
    published `pdata` (the field-2 value) and the equality below fails;
    confirming Off on `heating_rod_run_state` instead of the derived key
    resolves the write on the keyless run-state frame and `not task.done()`
    fails.
    """
    ocean = _ocean(hass, heating_rod_mode=0, heating_rod_run_state=1)

    task = asyncio.create_task(ocean.async_set_powerglow_operating_mode("off"))
    await asyncio.sleep(0.05)
    assert _mqtt(ocean).send_proto_set.call_count == 1
    assert _published_pdata(ocean) == _expected_pdata(ocean, run_on=False)

    _report(ocean, heating_rod_run_state=1, heating_rod_operating_mode="solar")
    await asyncio.sleep(0.05)
    assert not task.done()  # still running: not a confirmation of off
    # The raw run state alone is not the select's own key; the derived option is.
    _report(ocean, heating_rod_run_state=0)
    await asyncio.sleep(0.05)
    assert not task.done()

    _report(ocean, heating_rod_run_state=0, heating_rod_operating_mode="off")
    await task
    assert _mqtt(ocean).send_proto_set.call_count == 1
    assert ocean._pending_write is None


async def test_mode_already_set_and_rod_on_sends_nothing(hass: HomeAssistant) -> None:
    """Manual on a rod that is already in Manual and running is a no-op.

    Mutation probe: dropping the `!= target_mode` guard writes the mode again,
    so the call count is 1 (and the task would hang until the window ends).
    """
    ocean = _ocean(hass, heating_rod_mode=1, heating_rod_run_state=1)

    await ocean.async_set_powerglow_operating_mode("manual")

    assert _mqtt(ocean).send_proto_set.call_count == 0


async def test_refused_mode_change_ends_not_confirmed_and_never_switches_on(
    hass: HomeAssistant,
) -> None:
    """The rod keeps its old mode (the app blocks the change in one state): the
    mode write times out as an error and the run switch is not written.

    Mutation probe: ignoring the mode confirmation and going straight on to
    `run_on=True` sends a second frame and swallows the error.
    """
    ocean = _ocean(hass, heating_rod_mode=1, heating_rod_run_state=0)

    with patch(WINDOW, 0.1):
        task = asyncio.create_task(ocean.async_set_powerglow_operating_mode("solar"))
        await asyncio.sleep(0.02)
        _report(ocean, heating_rod_mode=1)  # the old mode, reported again
        with pytest.raises(HomeAssistantError) as err:
            await task

    assert err.value.translation_key == "powerglow_not_confirmed"
    assert _mqtt(ocean).send_proto_set.call_count == 1
    assert ocean._pending_write is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"hr_run_stat": 0, "hr_mode": 0}, "off"),
        ({"hr_run_stat": 0, "hr_mode": 1}, "off"),
        ({"hr_run_stat": 1, "hr_mode": 0}, "solar"),
        ({"hr_run_stat": 1, "hr_mode": 1}, "manual"),
    ],
)
def test_operating_mode_is_derived_from_run_state_and_mode(
    raw: dict[str, Any], expected: str
) -> None:
    """The select reads one key: off while the run state is 0, else the mode.

    Mutation probe: swapping the two names in `_HEATING_ROD_MODE_NAMES`, or
    testing the run state after the mode, turns a case red.
    """
    assert remap_heating_rod_keys(raw)["heating_rod_operating_mode"] == expected


@pytest.mark.parametrize(
    "raw",
    [
        {"hr_mode": 1},
        {"hr_run_stat": 1},
        {"hr_run_stat": 0},
        {"hr_run_stat": 1, "hr_mode": 2},
        {"hr_target_power": 2000},
    ],
)
def test_operating_mode_is_absent_when_the_report_cannot_tell(
    raw: dict[str, Any],
) -> None:
    """A report missing an input, or carrying a mode other than 0 and 1, leaves
    the key out so a known state is never overwritten by a guess.

    Mutation probe: defaulting a missing mode to "solar" makes the
    `{"hr_run_stat": 1}` case yield a value.
    """
    assert "heating_rod_operating_mode" not in remap_heating_rod_keys(raw)


async def test_a_running_rod_reporting_run_state_2_counts_as_on(
    hass: HomeAssistant,
) -> None:
    """The parser reads every non-zero run state as on, so a rod that runs and
    reports 2 is already on: Manual on a Manual rod sends nothing, and Solar on
    it writes the mode only, never the run switch.

    Mutation probe: skipping the run step only on `run_state == 1` (the old
    test) sends a redundant run-on write in both cases.
    """
    ocean = _ocean(hass, heating_rod_mode=1, heating_rod_run_state=2)

    await ocean.async_set_powerglow_operating_mode("manual")
    assert _mqtt(ocean).send_proto_set.call_count == 0

    task = asyncio.create_task(ocean.async_set_powerglow_operating_mode("solar"))
    await asyncio.sleep(0.05)
    assert _mqtt(ocean).send_proto_set.call_count == 1
    assert _published_pdata(ocean) == _expected_pdata(ocean, mode=0)
    _report(
        ocean,
        heating_rod_run_state=2,
        heating_rod_mode=0,
        heating_rod_operating_mode="solar",
    )
    await task
    assert _mqtt(ocean).send_proto_set.call_count == 1
    assert ocean._pending_write is None


async def test_a_stale_stored_mode_cannot_make_manual_succeed_under_solar(
    hass: HomeAssistant,
) -> None:
    """The store says Manual but the rod already runs in Solar: only the run
    step is sent, and a report of "solar" must not confirm it. Only a report
    that shows the rod in Manual does.

    Mutation probe: confirming the run step on `heating_rod_run_state == 1`
    (the old read-back) resolves the write on the first, Solar, report and the
    `not task.done()` assertion fails.
    """
    ocean = _ocean(hass, heating_rod_mode=1, heating_rod_run_state=0)

    task = asyncio.create_task(ocean.async_set_powerglow_operating_mode("manual"))
    await asyncio.sleep(0.05)
    assert _mqtt(ocean).send_proto_set.call_count == 1
    assert _published_pdata(ocean) == _expected_pdata(ocean, run_on=True)

    _report(
        ocean,
        heating_rod_run_state=1,
        heating_rod_mode=0,
        heating_rod_operating_mode="solar",
    )
    await asyncio.sleep(0.05)
    assert not task.done()
    assert ocean._pending_write is not None

    _report(
        ocean,
        heating_rod_run_state=1,
        heating_rod_mode=1,
        heating_rod_operating_mode="manual",
    )
    await task
    assert ocean._pending_write is None


async def test_a_report_from_another_rod_does_not_confirm(
    hass: HomeAssistant,
) -> None:
    """A write is tied to the rod it addressed: the other rod on the same
    PowerOcean reporting the same value, and a report with no serial at all,
    leave it pending until the addressed rod answers.

    Mutation probe: dropping `require=("heating_rod_sn", rod_sn)` from the
    record in `_async_powerglow_write` (or the `record.require` check in
    `_resolve_pending_write`) lets the other rod's frame confirm.
    """
    ocean = _ocean(hass)
    task = asyncio.create_task(ocean.async_set_powerglow_value("target_power", 1000))
    await asyncio.sleep(0.05)

    ocean._apply_data(
        {"heating_rod_sn": OTHER_ROD_SN, "heating_rod_target_power_w": 1000.0}
    )
    ocean._apply_data({"heating_rod_target_power_w": 1000.0})
    await asyncio.sleep(0.05)
    assert not task.done()
    assert ocean._pending_write is not None

    _report(ocean, heating_rod_target_power_w=1000.0)
    await task
    assert ocean._pending_write is None


@pytest.mark.parametrize("option", ["Solar", "auto", "on", ""])
async def test_an_unknown_option_is_refused_before_anything_is_sent(
    hass: HomeAssistant, option: str
) -> None:
    """Any string that is not off/solar/manual used to fall through to Manual
    and switch the element on.

    Mutation probe: removing the membership check at the top of
    `async_set_powerglow_operating_mode` sends a Manual write for each case.
    """
    ocean = _ocean(hass, heating_rod_mode=0, heating_rod_run_state=0)

    with pytest.raises(ValueError):
        await ocean.async_set_powerglow_operating_mode(option)  # type: ignore[arg-type]

    assert _mqtt(ocean).send_proto_set.call_count == 0
    assert ocean._pending_write is None


async def test_the_envelope_is_addressed_to_the_powerocean_and_the_pdata_to_the_rod(
    hass: HomeAssistant,
) -> None:
    """Two different serials: the frame leaves with the coordinator's own
    `device_sn` in header field 25 and the rod's serial in pdata field 1.

    Mutation probe: passing the rod serial as the envelope's `device_sn` in
    `_async_powerglow_write` puts `ROD_SN` into field 25.
    """
    ocean = _ocean(hass)
    assert ocean.device_sn != ROD_SN

    task = asyncio.create_task(ocean.async_set_powerglow_value("target_power", 1000))
    await asyncio.sleep(0.05)
    payload = _mqtt(ocean).send_proto_set.call_args_list[0].args[0]
    ((_, _, header),) = _iter_fields(payload)
    header_fields = {num: raw for num, _, raw in _iter_fields(header)}
    pdata = next(raw for num, _, raw in _iter_fields(header) if num == 1)
    pdata_fields = {num: raw for num, _, raw in _iter_fields(pdata)}

    assert header_fields[25] == ocean.device_sn.encode("ascii")
    assert pdata_fields[1] == ROD_SN.encode("ascii")

    _report(ocean, heating_rod_target_power_w=1000.0)
    await task


async def test_a_publish_that_is_not_delivered_clears_the_record(
    hass: HomeAssistant,
) -> None:
    """A publish that reports failure ends in its own error and leaves no
    record behind, so the next write is accepted instead of refused as in
    progress until the entry reloads.

    Mutation probe: removing `self._clear_pending_write(record)` from the
    `except BaseException` block of `_async_powerglow_write` (set_commands.py)
    leaves the record set, and the second call is refused as
    `powerglow_write_in_progress`.
    """
    ocean = _ocean(hass)
    _mqtt(ocean).send_proto_set.return_value = False

    with pytest.raises(HomeAssistantError) as err:
        await ocean.async_set_powerglow_value("target_power", 1000)

    assert err.value.translation_key == "powerglow_not_delivered"
    assert _mqtt(ocean).send_proto_set.call_count == 1
    assert ocean._pending_write is None

    _mqtt(ocean).send_proto_set.return_value = True
    task = asyncio.create_task(ocean.async_set_powerglow_value("target_power", 1000))
    await asyncio.sleep(0.05)
    assert _mqtt(ocean).send_proto_set.call_count == 2
    _report(ocean, heating_rod_target_power_w=1000.0)
    await task
    assert ocean._pending_write is None


@pytest.mark.parametrize("situation", ["disconnected", "no_client", "standard_mode"])
async def test_offline_paths_are_refused_without_a_record(
    hass: HomeAssistant, situation: str
) -> None:
    """No live Enhanced Mode connection means no write: a dropped broker, a
    client that does not exist yet, and a Standard Mode coordinator are all
    `powerglow_offline`, with nothing sent and no record left behind.

    Mutation probe: dropping the `is_connected()` operand of the check in
    `_async_powerglow_write` sends into the disconnected mock (the first case
    then waits for a confirmation instead of raising); dropping `not
    self._enhanced_mode` does the same for the third.
    """
    ocean = _ocean(hass)
    if situation == "disconnected":
        _mqtt(ocean).is_connected.return_value = False
    elif situation == "no_client":
        ocean._mqtt_client = None
    else:
        ocean._enhanced_mode = False

    with pytest.raises(HomeAssistantError) as err:
        await ocean.async_set_powerglow_value("target_power", 1000)

    assert err.value.translation_key == "powerglow_offline"
    assert ocean._pending_write is None
    if ocean._mqtt_client is not None:
        assert _mqtt(ocean).send_proto_set.call_count == 0


async def test_a_frame_without_the_key_does_not_confirm_from_the_store(
    hass: HomeAssistant,
) -> None:
    """The store already holds the target (put there directly, as a stale
    reading would be); a frame that does not carry the key must not confirm
    from it, only a frame that does.

    Mutation probe: making `_resolve_pending_write` fall back to
    `self._device_data` when the key is missing from the frame confirms the
    write on the temperature-only frame and `not task.done()` fails.
    """
    ocean = _ocean(hass, heating_rod_target_power_w=1000.0)
    task = asyncio.create_task(ocean.async_set_powerglow_value("target_power", 1000))
    await asyncio.sleep(0.05)

    _report(ocean, heating_rod_water_temp_c=60.0)
    await asyncio.sleep(0.05)
    assert not task.done()
    assert ocean._pending_write is not None

    _report(ocean, heating_rod_target_power_w=1000.0)
    await task
    assert ocean._pending_write is None
