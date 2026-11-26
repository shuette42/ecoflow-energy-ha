"""Ordering of an accessory write on a PowerOcean coordinator (PowerPulse 1).

A write publishes its command, then waits for the report that carries the new
value. These tests pin what holds between the two: a report that arrives while
the publish still waits for the broker's ack settles the write (the one fix,
unless the future was cancelled by the shutdown), and four accepted limits,
each pinned as it behaves today: a call cancelled during the publish clears
its record, the confirmation window counts from the end of the publish wait,
one frame merges its messages key by key, and an unmapped charge status leaves
the last known one in place.

The reports are re-encoded from the reporter's real frames
(`tests/fixtures/powerpulse/r372_settings_731_20261113.json`).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.proto import ecocharge_pb2 as pb2
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    _read_varint,
    decode_header_message,
)
from tests.ha.test_powerpulse2_charge_action import _mqtt
from tests.ha.test_wallbox_powerpulse1_controls import (
    FRAMES,
    OTHER_PP1_SN,
    PP1_SN,
    WINDOW,
    _ocean,
    _pp1,
)

# A publisher that never returns on its own is released by the test; this only
# bounds how long a thread can outlive a failing test.
PUBLISH_CEILING_S = 5.0


class _HeldPublish:
    """A `send_proto_set` that waits, in the executor thread, until `release`
    is set and then returns `result`: a publisher still waiting for the
    broker's ack. The caller sets `release` in a `finally`.

    A plain callable, not a `MagicMock` with a blocking `side_effect`: the
    mock holds its own lock for the whole call and a blocked call froze the
    event loop for the full ceiling in every run.
    """

    def __init__(self, result: bool) -> None:
        self.result = result
        self.release = threading.Event()
        self.calls: list[bytes] = []

    def __call__(self, payload: bytes, wait: bool = False) -> bool:
        self.calls.append(payload)
        self.release.wait(PUBLISH_CEILING_S)
        return self.result


def _hold_publish(ocean: EcoFlowDeviceCoordinator, result: bool) -> _HeldPublish:
    publisher = _HeldPublish(result)
    _mqtt(ocean).send_proto_set = publisher
    return publisher


async def _until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    """Wait for `condition` on the loop; the publish runs in another thread."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition never held"
        await asyncio.sleep(0.01)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _report(label: str, **changes: Any) -> bytes:
    """One 209/8 header of a real frame, its report changed field by field.

    A value of `None` clears the field. The report is XORed with the low byte
    of the header's sequence number, as on the wire; everything else in the
    header stays as recorded.
    """
    raw = bytes.fromhex(FRAMES[label]["hex"])
    (header, *_), _ = decode_header_message(raw)
    assert (header["cmd_func"], header["cmd_id"]) == (209, 8)
    key = header["seq"] & 0xFF
    _, i = _read_varint(raw, 0)
    length, i = _read_varint(raw, i)
    assert length is not None
    head = raw[i : i + length]
    _, j = _read_varint(head, 0)
    pdata_len, j = _read_varint(head, j)
    assert pdata_len is not None
    assert head[0] == 0x0A, "pdata is the first field of the header"
    report = pb2.EVChargingParamReport.FromString(
        bytes(b ^ key for b in head[j : j + pdata_len])
    )
    for field, value in changes.items():
        if value is None:
            report.ClearField(field)
        else:
            setattr(report, field, value)
    new_pdata = bytes(b ^ key for b in report.SerializeToString())
    new_head = b"\x0a" + _varint(len(new_pdata)) + new_pdata + head[j + pdata_len :]
    return b"\x0a" + _varint(len(new_head)) + new_head


def _apply_frame(ocean: EcoFlowDeviceCoordinator, frame: bytes) -> dict[str, Any]:
    topic = f"/app/user123/{ocean.device_sn}/thing/property"
    parsed = ocean._parse_message(topic, frame)
    assert parsed is not None
    ocean._apply_data(parsed)
    return parsed


def _started(hass: HomeAssistant) -> EcoFlowDeviceCoordinator:
    """A PowerOcean that has reported a wallbox with plug-and-play off."""
    ocean = _ocean(hass)
    ocean._apply_data(_pp1(ocean, "get_all_reply_start"))
    return ocean


async def test_a_report_during_the_publish_confirms_even_if_the_ack_fails(
    hass: HomeAssistant,
) -> None:
    """The confirming report settles the write: a late ack timeout changes
    nothing and the call returns, although the publish itself reported a
    failure after the device had already reported the value."""
    ocean = _started(hass)
    publisher = _hold_publish(ocean, result=False)
    try:
        task = asyncio.create_task(
            ocean.async_set_wallbox_setting("ev_plug_and_play", True)
        )
        await _until(lambda: len(publisher.calls) == 1)
        ocean._apply_data(_pp1(ocean, "report_after_set_f4_1"))
        publisher.release.set()
        await asyncio.wait_for(task, 2.0)
    finally:
        publisher.release.set()
    assert ocean._pending_write is None


async def test_a_failed_ack_without_a_report_is_not_delivered(
    hass: HomeAssistant,
) -> None:
    """The negative control of the test above: the publish returns False and no
    report arrived, so the write is not delivered and its record is cleared."""
    ocean = _started(hass)
    publisher = _hold_publish(ocean, result=False)
    publisher.release.set()
    with pytest.raises(HomeAssistantError) as err:
        await ocean.async_set_wallbox_setting("ev_plug_and_play", True)
    assert err.value.translation_key == "wallbox_not_delivered"
    assert len(publisher.calls) == 1
    assert ocean._pending_write is None


async def test_a_future_cancelled_at_shutdown_is_not_a_confirmation(
    hass: HomeAssistant,
) -> None:
    """A future cancelled by the shutdown while the publish is held is not a
    confirming report: the call fails as not delivered and does not leak a
    `CancelledError`. The shutdown steps are done by hand, mirroring
    `async_shutdown` (coordinator/setup.py), because the real one drains
    in-flight writes and would wait on the held publish thread."""
    ocean = _started(hass)
    publisher = _hold_publish(ocean, result=False)
    try:
        task = asyncio.create_task(
            ocean.async_set_wallbox_setting("ev_plug_and_play", True)
        )
        await _until(lambda: len(publisher.calls) == 1)
        record = ocean._pending_write
        assert record is not None
        ocean._shutdown = True
        if not record.future.done():
            record.future.cancel()
        ocean._pending_write = None
        assert record.future.cancelled()
        publisher.release.set()
        with pytest.raises(HomeAssistantError) as err:
            await asyncio.wait_for(task, 2.0)
    finally:
        publisher.release.set()
    assert err.value.translation_key == "wallbox_not_delivered"
    assert ocean._pending_write is None


async def test_a_cancelled_write_clears_its_record_and_a_second_write_goes_out(
    hass: HomeAssistant,
) -> None:
    """Accepted limit: a call cancelled while its command is being sent clears
    its record at once, so a second write is accepted and published while the
    first publish still runs in its thread. A record that outlived a cancelled
    call would refuse every later write; the second write is checked against
    the state reported before the cancelled one took effect."""
    ocean = _started(hass)
    publisher = _hold_publish(ocean, result=True)
    second: asyncio.Task[None] | None = None
    try:
        first = asyncio.create_task(
            ocean.async_set_wallbox_setting("ev_plug_and_play", True)
        )
        await _until(lambda: len(publisher.calls) == 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert ocean._pending_write is None
        second = asyncio.create_task(
            ocean.async_set_wallbox_setting("ev_continuous_charging", True)
        )
        await _until(lambda: len(publisher.calls) == 2)
    finally:
        publisher.release.set()
        if second is not None:
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)


async def test_the_window_counts_from_the_end_of_the_publish_wait(
    hass: HomeAssistant,
) -> None:
    """Accepted limit: the confirmation window counts from the end of the
    publish wait, so a write can take the publish acknowledgement timeout plus
    the window after it was sent, the same as the PowerPulse 2 writes. With a
    0.3 s window and a publish held for 0.4 s, a report 0.1 s after the release
    confirms the write although more than the window passed since it started."""
    ocean = _started(hass)
    publisher = _hold_publish(ocean, result=True)
    loop = asyncio.get_running_loop()
    try:
        with patch(WINDOW, 0.3):
            started = loop.time()
            task = asyncio.create_task(
                ocean.async_set_wallbox_setting("ev_plug_and_play", True)
            )
            await _until(lambda: len(publisher.calls) == 1)
            await asyncio.sleep(0.4)
            publisher.release.set()
            # The lock is released once the publish wait is over; only then
            # does the confirmation window start.
            await _until(lambda: not ocean._pending_write_lock.locked())
            # A statement about the scenario, not about the sleep above: the
            # report lands more than the window after the call started.
            assert loop.time() - started > 0.3
            ocean._apply_data(_pp1(ocean, "report_after_set_f4_1"))
            await asyncio.wait_for(task, 2.0)
    finally:
        publisher.release.set()
    assert ocean._pending_write is None


async def test_one_frame_with_two_wallboxes_is_merged_and_confirms_the_write(
    hass: HomeAssistant,
) -> None:
    """Accepted limit: the messages of one frame are merged key by key before
    the resolver sees them, so the serial and the value are only known to
    belong together while a frame carries one accessory of a kind (one per
    kind is all that is on record). A frame whose first 209/8 report is another
    wallbox's new value and whose second names the addressed wallbox without
    any setting therefore confirms the write."""
    ocean = _started(hass)
    other = _report("report_after_set_f4_1", ev_sn=OTHER_PP1_SN)
    addressed = _report(
        "report_after_set_f4_1",
        ev_sn=PP1_SN,
        ev_plug_and_play=None,
        work_mode=None,
        use_grid_first=None,
        switch_bits=None,
    )
    with patch(WINDOW, 0.3):
        task = asyncio.create_task(
            ocean.async_set_wallbox_setting("ev_plug_and_play", True)
        )
        await _until(lambda: ocean._pending_write is not None)
        _apply_frame(ocean, other + addressed)
        await asyncio.wait_for(task, 1.0)
    assert ocean._pending_write is None


async def test_an_unmapped_charge_status_leaves_the_last_known_one_standing(
    hass: HomeAssistant,
) -> None:
    """Accepted limit: a status the integration does not know is dropped from
    the report, because an unknown value would break the enum sensor and
    clearing the key would change what the sensor shows. The last known status
    stays in the store for every later reader of it."""
    ocean = _started(hass)
    _apply_frame(ocean, _report("report_after_set_f4_1", ev_sn=PP1_SN))
    assert ocean._device_data.get("ev_charge_status") == "available"
    unmapped = _report("report_after_set_f4_1", ev_sn=PP1_SN, charging_status=99)
    _apply_frame(ocean, unmapped)
    assert ocean._device_data.get("ev_charge_status") == "available"
