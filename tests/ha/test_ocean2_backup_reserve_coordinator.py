"""Coordinator side of the Ocean 2 Backup Reserve write (PLAN-151, brief B1).

The payload builder and the acknowledgement parser are unit-tested in
`tests/test_ocean2_commands.py`. These tests prove the wiring around them:
the snapshot read before the write, the ceiling taken from it, the read-back
requests that follow the write, and the refusal check on the shared set_reply
topic.

Time is a hand-driven loop clock: `hass.loop.call_later` is replaced by a
recorder, so a test says "35 s later" by advancing it and the timer callbacks
run in order. The write itself waits on a real asyncio event, answered by
`_apply_data` the way the MQTT thread's frame is on a live device.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_OCEAN2,
    DOMAIN,
    MODE_ENHANCED,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.coordinator.set_commands import (
    DeviceValueNotReported,
)
from custom_components.ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

_CLOCK = "custom_components.ecoflow_energy.coordinator.time.monotonic"
_SNAPSHOT_TIMEOUT = (
    "custom_components.ecoflow_energy.coordinator.set_commands."
    "_OCEAN2_SNAPSHOT_TIMEOUT_S"
)

SERIAL = "RE11TEST00000001"
DEVICE: dict[str, Any] = {
    "sn": SERIAL,
    "name": "",
    "product_name": "",
    "device_type": DEVICE_TYPE_OCEAN2,
    "online": 1,
}
SET_REPLY_TOPIC = f"/app/user123/{SERIAL}/thing/property/set_reply"

# A snapshot as the parser delivers it: reserve (field 10) and the device's own
# SoC ratio (field 92).
SNAPSHOT_5_80: dict[str, Any] = {"backup_reserve_pct": 5, "edev_soc_ratio_pct": 80}


class _Handle:
    """What `loop.call_later` hands back."""

    def __init__(self, when: float, callback: Callable[..., Any], args: tuple) -> None:
        self.when = when
        self.callback = callback
        self.args = args
        self.fired = False
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def live(self) -> bool:
        return not self._cancelled and not self.fired


class _Timers:
    """A loop clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 0.0
        self.handles: list[_Handle] = []

    def call_later(
        self, delay: float, callback: Callable[..., Any], *args: Any
    ) -> _Handle:
        handle = _Handle(self.now + delay, callback, args)
        self.handles.append(handle)
        return handle

    def pending(self) -> list[float]:
        """Seconds until each live read-back timer fires, soonest first."""
        return sorted(
            h.when - self.now
            for h in self.handles
            if h.live and h.callback.__name__.startswith("_ocean2")
        )

    def advance(self, seconds: float) -> None:
        end = self.now + seconds
        while True:
            due = [h for h in self.handles if h.live and h.when <= end]
            if not due:
                break
            handle = min(due, key=lambda h: h.when)
            self.now = handle.when
            handle.fired = True
            handle.callback(*handle.args)
        self.now = end


@pytest.fixture
def timers(hass: HomeAssistant) -> Iterator[_Timers]:
    clock = _Timers()
    with patch.object(hass.loop, "call_later", side_effect=clock.call_later):
        yield clock


def _coordinator(
    hass: HomeAssistant, serial: str = SERIAL
) -> tuple[EcoFlowDeviceCoordinator, MagicMock]:
    device = {**DEVICE, "sn": serial}
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [device],
        },
        unique_id="test@example.com",
    )
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    mqtt = MagicMock()
    mqtt.is_connected.return_value = True
    mqtt.send_proto_set.return_value = True
    mqtt.send_get_all.return_value = True
    coordinator._mqtt_client = mqtt
    return coordinator, mqtt


def _pdata(reserve: int, ratio: int) -> str:
    return (encode_field_varint(10, reserve) + encode_field_varint(92, ratio)).hex()


def _sent_pdata(mqtt: MagicMock) -> str:
    headers, _ = decode_header_message(mqtt.send_proto_set.call_args[0][0])
    return str(headers[0]["pdata"])


def _ack(cmd_ok: bool) -> bytes:
    """A 254/38 acknowledgement; a refusal is a payload without `cmd_ok`."""
    pdata = encode_field_varint(10, 23) + encode_field_varint(92, 80)
    if cmd_ok:
        pdata += encode_field_varint(2, 1)
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 254)
        + encode_field_varint(9, 38)
        + encode_field_varint(12, 1)
    )
    return encode_field_bytes(1, header)


async def _write(
    hass: HomeAssistant,
    coordinator: EcoFlowDeviceCoordinator,
    mqtt: MagicMock,
    reserve: int,
    answer: dict[str, Any],
) -> bool:
    """Run a write and answer its get-all with `answer`, as the device does."""
    wanted = mqtt.send_get_all.call_count + 1
    task = hass.async_create_task(coordinator.async_set_ocean2_backup_reserve(reserve))
    for _ in range(300):
        if mqtt.send_get_all.call_count >= wanted:
            break
        await asyncio.sleep(0.01)
    assert mqtt.send_get_all.call_count >= wanted, "the write never asked the device"
    coordinator._apply_data(dict(answer))
    return await task


async def _write_held_in_publish(
    hass: HomeAssistant,
    coordinator: EcoFlowDeviceCoordinator,
    mqtt: MagicMock,
    reserve: int,
    answer: dict[str, Any],
) -> tuple[asyncio.Future[bool], threading.Event]:
    """Start a write and hold it inside its publish.

    The publish runs in an executor thread, so a test can act on the loop
    while the write coroutine is still awaiting it. Returns the write task and
    the event that lets the publish return. Progress is signalled to the loop
    with `call_soon_threadsafe`: `asyncio.sleep` would sit on the patched
    `call_later` and never wake. `mqtt.send_proto_set` is replaced, so its
    mock call history is gone for the rest of the test.
    """
    asked = asyncio.Event()
    started = asyncio.Event()
    release = threading.Event()

    def _answer_get_all() -> bool:
        hass.loop.call_soon_threadsafe(asked.set)
        return True

    def _held_publish(*args: Any, **kwargs: Any) -> bool:
        hass.loop.call_soon_threadsafe(started.set)
        release.wait(5)
        return True

    mqtt.send_get_all.side_effect = _answer_get_all
    # A plain function, not a Mock: the harness runs a Mock target inline on
    # the loop thread, which would leave nothing to act on during the publish.
    mqtt.send_proto_set = _held_publish
    task = hass.async_create_task(coordinator.async_set_ocean2_backup_reserve(reserve))
    try:
        await asyncio.wait_for(asked.wait(), 5)
        coordinator._apply_data(dict(answer))
        await asyncio.wait_for(started.wait(), 5)
    except BaseException:
        release.set()
        raise
    return task, release


def _integration_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """WARNING and above from this integration only.

    asyncio logs its own slow-callback WARNING on a loaded machine, which is
    not the integration's noise and made these tests fail under `-n 8`.
    """
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and r.name.startswith("custom_components.ecoflow_energy")
    ]


async def _settle(hass: HomeAssistant) -> None:
    """Let executor jobs started by timer callbacks finish."""
    await hass.async_block_till_done()


class TestWritePath:
    """B1.1 - B1.3: the snapshot before the write and what it permits."""

    async def test_write_publishes_both_fields_seeds_and_arms_the_readback(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data(dict(SNAPSHOT_5_80))

        ok = await _write(hass, coordinator, mqtt, 23, SNAPSHOT_5_80)

        assert ok is True
        mqtt.send_proto_set.assert_called_once()
        assert _sent_pdata(mqtt) == _pdata(23, 80)
        assert coordinator.data["backup_reserve_pct"] == 23
        assert timers.pending() == [8.0, 30.0, 35.0]
        assert mqtt.send_get_all.call_count == 1  # the read before the write

        timers.advance(8)
        await _settle(hass)

        assert mqtt.send_get_all.call_count == 2  # the +8 s read-back request

    async def test_the_ceiling_is_the_fresh_snapshot_not_the_stored_value(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data({"backup_reserve_pct": 5, "edev_soc_ratio_pct": 70})

        ok = await _write(hass, coordinator, mqtt, 23, SNAPSHOT_5_80)

        assert ok is True
        assert _sent_pdata(mqtt) == _pdata(23, 80)

    @pytest.mark.parametrize(
        "frame",
        [
            # A frame that carries field 10 but not field 92 must not wake the
            # waiter, or the stored 70 would stand in for a fresh reading.
            {"backup_reserve_pct": 5},
            # A snapshot whose field 92 is unknown.
            {"edev_soc_ratio_pct": None},
        ],
    )
    async def test_no_usable_snapshot_raises_and_publishes_nothing(
        self, hass: HomeAssistant, timers: _Timers, frame: dict[str, Any]
    ) -> None:
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data({"backup_reserve_pct": 5, "edev_soc_ratio_pct": 70})

        with patch(_SNAPSHOT_TIMEOUT, 0.05), pytest.raises(DeviceValueNotReported):
            await _write(hass, coordinator, mqtt, 23, frame)

        mqtt.send_proto_set.assert_not_called()
        assert timers.pending() == []

    async def test_silence_for_five_seconds_raises_and_publishes_nothing(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data(dict(SNAPSHOT_5_80))

        with patch(_SNAPSHOT_TIMEOUT, 0.05), pytest.raises(DeviceValueNotReported):
            await coordinator.async_set_ocean2_backup_reserve(23)

        assert mqtt.send_get_all.call_count == 1
        mqtt.send_proto_set.assert_not_called()
        assert coordinator.data["backup_reserve_pct"] == 5

    async def test_a_reserve_above_the_fresh_ratio_is_refused(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data(dict(SNAPSHOT_5_80))

        with pytest.raises(ValueError):
            await _write(hass, coordinator, mqtt, 81, SNAPSHOT_5_80)

        mqtt.send_proto_set.assert_not_called()
        assert coordinator.data["backup_reserve_pct"] == 5
        # Control: the ratio itself is allowed, so the ceiling is inclusive and
        # the refusal above is not a lock that stayed held.
        assert await _write(hass, coordinator, mqtt, 80, SNAPSHOT_5_80) is True
        assert _sent_pdata(mqtt) == _pdata(80, 80)

    async def test_the_floor_is_the_models_own_zero_only_on_the_re41(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        """RE41 write 0 is accepted and sent; the RE11 refuses 0 and asks nothing."""
        re41, re41_mqtt = _coordinator(hass, "RE41TEST00000001")
        re41._apply_data(dict(SNAPSHOT_5_80))

        # Not `_write`: a refusal ends the task at once, and that helper's
        # sleeps sit on the patched `call_later` and would never wake.
        task = hass.async_create_task(re41.async_set_ocean2_backup_reserve(0))
        started = time.perf_counter()
        while (
            not re41_mqtt.send_get_all.call_count
            and not task.done()
            and time.perf_counter() - started < 3
        ):
            await asyncio.sleep(0)
        assert re41_mqtt.send_get_all.call_count == 1, "0 was refused before asking"
        re41._apply_data(dict(SNAPSHOT_5_80))
        assert await task is True
        assert _sent_pdata(re41_mqtt) == _pdata(0, 80)
        assert re41.data["backup_reserve_pct"] == 0

        re11, re11_mqtt = _coordinator(hass)
        with pytest.raises(ValueError, match="1..100"):
            await re11.async_set_ocean2_backup_reserve(0)
        re11_mqtt.send_get_all.assert_not_called()
        re11_mqtt.send_proto_set.assert_not_called()

    async def test_the_re43_floor_is_one_and_its_write_is_the_reserve_frame(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        """RE43 refuses 0 like the RE11 (no 0 on record) and sends 10 and 92.

        The app's own RE43 write also carries an explicit field 1 = 0 that this
        frame does not; whether the unit accepts our shape is what the first
        write from Home Assistant on an RE43 settles.
        """
        re43, mqtt = _coordinator(hass, "RE43TEST00000001")
        re43._apply_data(dict(SNAPSHOT_5_80))

        with pytest.raises(ValueError, match="1..100"):
            await re43.async_set_ocean2_backup_reserve(0)
        mqtt.send_get_all.assert_not_called()
        mqtt.send_proto_set.assert_not_called()

        assert await _write(hass, re43, mqtt, 30, SNAPSHOT_5_80) is True
        assert _sent_pdata(mqtt) == _pdata(30, 80)

    async def test_the_re41_raises_field_92_with_the_reserve(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        """RE41 reporting 92 = 5 writes reserve 60 and sends {10: 60, 92: 60}.

        The app's own RE41 writes raise 92 with the reserve ({10: 5, 92: 5},
        {10: 100, 92: 100}); sending 5 back would leave the reserve stuck at the
        unit's own limit.
        """
        re41, mqtt = _coordinator(hass, "RE41TEST00000001")
        snapshot = {"backup_reserve_pct": 5, "edev_soc_ratio_pct": 5}
        re41._apply_data(dict(snapshot))

        assert await _write(hass, re41, mqtt, 60, snapshot) is True

        mqtt.send_proto_set.assert_called_once()
        assert _sent_pdata(mqtt) == _pdata(60, 60)
        assert re41.data["backup_reserve_pct"] == 60

    async def test_the_re41_never_lowers_field_92(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        """RE41 reporting 92 = 100 writes reserve 0 and sends {10: 0, 92: 100}."""
        re41, mqtt = _coordinator(hass, "RE41TEST00000001")
        snapshot = {"backup_reserve_pct": 40, "edev_soc_ratio_pct": 100}
        re41._apply_data(dict(snapshot))

        assert await _write(hass, re41, mqtt, 0, snapshot) is True

        assert _sent_pdata(mqtt) == _pdata(0, 100)
        assert re41.data["backup_reserve_pct"] == 0

    async def test_the_re41_still_needs_a_reported_field_92(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        """No reading of 92 means no write: the unit's current value is unknown."""
        re41, mqtt = _coordinator(hass, "RE41TEST00000001")
        re41._apply_data({"backup_reserve_pct": 5, "edev_soc_ratio_pct": 70})

        with patch(_SNAPSHOT_TIMEOUT, 0.05), pytest.raises(DeviceValueNotReported):
            await _write(hass, re41, mqtt, 60, {"edev_soc_ratio_pct": None})

        mqtt.send_proto_set.assert_not_called()

    @pytest.mark.parametrize("serial", ["RE11TEST00000001", "RE43TEST00000001"])
    async def test_other_models_keep_the_reserve_at_or_below_field_92(
        self, hass: HomeAssistant, timers: _Timers, serial: str
    ) -> None:
        """RE11 and RE43 reporting 92 = 5 refuse reserve 60 and publish nothing."""
        coordinator, mqtt = _coordinator(hass, serial)
        snapshot = {"backup_reserve_pct": 3, "edev_soc_ratio_pct": 5}
        coordinator._apply_data(dict(snapshot))

        with pytest.raises(ValueError, match="above the device's own reading"):
            await _write(hass, coordinator, mqtt, 60, snapshot)

        mqtt.send_proto_set.assert_not_called()
        assert coordinator.data["backup_reserve_pct"] == 3

    @pytest.mark.parametrize("serial", ["RE17TEST00000001", "RE42TEST00000001"])
    async def test_a_model_without_a_row_is_refused_before_anything_is_asked(
        self, hass: HomeAssistant, timers: _Timers, serial: str
    ) -> None:
        coordinator, mqtt = _coordinator(hass, serial)

        with pytest.raises(ValueError, match="not available"):
            await coordinator.async_set_ocean2_backup_reserve(23)

        mqtt.send_get_all.assert_not_called()
        mqtt.send_proto_set.assert_not_called()

    @pytest.mark.parametrize("value", [0, 101, -5])
    async def test_a_value_outside_the_range_asks_nothing(
        self, hass: HomeAssistant, timers: _Timers, value: int
    ) -> None:
        coordinator, mqtt = _coordinator(hass)

        with pytest.raises(ValueError):
            await coordinator.async_set_ocean2_backup_reserve(value)

        mqtt.send_get_all.assert_not_called()
        mqtt.send_proto_set.assert_not_called()


class TestReadbackSequence:
    """B1.4 - B1.6: a newer write, shutdown, and the three outcomes."""

    async def test_a_second_write_replaces_the_first_sequence(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data(dict(SNAPSHOT_5_80))

        await _write(hass, coordinator, mqtt, 23, SNAPSHOT_5_80)
        first = [h for h in timers.handles if h.live]
        timers.advance(3)
        await _write(hass, coordinator, mqtt, 30, SNAPSHOT_5_80)

        assert len(first) == 3
        assert all(h.cancelled() for h in first)
        assert timers.pending() == [8.0, 30.0, 35.0]
        reads = mqtt.send_get_all.call_count  # one before each write

        timers.advance(5)  # t = 8: where the first write's request would be
        await _settle(hass)
        assert mqtt.send_get_all.call_count == reads

        timers.advance(3)  # t = 11: eight seconds after the second write
        await _settle(hass)
        assert mqtt.send_get_all.call_count == reads + 1

    async def test_shutdown_cancels_the_readback(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data(dict(SNAPSHOT_5_80))
        await _write(hass, coordinator, mqtt, 23, SNAPSHOT_5_80)
        armed = [h for h in timers.handles if h.live]
        assert len(armed) == 3

        await coordinator.async_shutdown()
        timers.advance(35)
        await _settle(hass)

        assert all(h.cancelled() for h in armed)
        assert mqtt.send_get_all.call_count == 1  # only the read before the write
        assert coordinator._device_data["backup_reserve_pct"] == 23
        assert not any(e["type"] == "readback_missing" for e in coordinator.event_log)

    async def test_the_timers_of_a_replaced_write_leave_the_new_one_alone(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        """A newer write swaps the record before its publish returns.

        The older write's timers can fire inside that window. They act on
        their own write, which is no longer the record, so they do nothing:
        no get-all, no restore, no `readback_missing` for a write that is
        still being confirmed.
        """
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data(dict(SNAPSHOT_5_80))
        await _write(hass, coordinator, mqtt, 23, SNAPSHOT_5_80)
        first = coordinator._ocean2_writes.get("backup_reserve_pct")
        old = [h for h in timers.handles if h.live]
        assert first is not None and len(old) == 3

        task, release = await _write_held_in_publish(
            hass, coordinator, mqtt, 30, SNAPSHOT_5_80
        )
        try:
            second = coordinator._ocean2_writes.get("backup_reserve_pct")
            assert second is not None and second is not first
            # The second write's own get-all answer counted for the first
            # record; undo that so the first write looks like one nothing
            # ever confirmed, which is what its +35 s timer acts on.
            first.snapshot_seen = False
            asks = mqtt.send_get_all.call_count
            for handle in old:
                handle.fired = True
                handle.callback(*handle.args)
        finally:
            release.set()
        assert await task is True
        await _settle(hass)

        assert mqtt.send_get_all.call_count == asks
        assert not any(e["type"] == "readback_missing" for e in coordinator.event_log)
        assert coordinator.data["backup_reserve_pct"] == 30
        assert coordinator._ocean2_writes.get("backup_reserve_pct") is second
        assert timers.pending() == [8.0, 30.0, 35.0]

    async def test_a_shutdown_during_the_publish_arms_no_readback(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data(dict(SNAPSHOT_5_80))

        task, release = await _write_held_in_publish(
            hass, coordinator, mqtt, 23, SNAPSHOT_5_80
        )
        try:
            await coordinator.async_shutdown()
        finally:
            release.set()
        await task

        # Looked at before any timer is advanced: a handle that has fired is
        # no longer live, so afterwards "nothing pending" proves nothing.
        assert timers.pending() == []
        timers.advance(35)
        await _settle(hass)

        assert mqtt.send_get_all.call_count == 1  # only the read before the write
        assert not any(e["type"] == "readback_missing" for e in coordinator.event_log)

    async def test_no_snapshot_after_the_request_retries_then_restores(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data(dict(SNAPSHOT_5_80))
        await _write(hass, coordinator, mqtt, 23, SNAPSHOT_5_80)

        # The app's own poll during the apply window still reads the old
        # value. It came before the +8 s request, so it is not the answer.
        timers.advance(2)
        coordinator._apply_data({"backup_reserve_pct": 5})
        coordinator._apply_data({"backup_reserve_pct": 23})  # then the seed again
        timers.advance(6)
        await _settle(hass)
        assert mqtt.send_get_all.call_count == 2  # read before + request at +8

        timers.advance(22)  # t = 30
        await _settle(hass)
        assert mqtt.send_get_all.call_count == 3  # the retry
        assert coordinator.data["backup_reserve_pct"] == 23

        timers.advance(5)  # t = 35
        await _settle(hass)
        assert coordinator.data["backup_reserve_pct"] == 5
        assert coordinator._device_data["backup_reserve_pct"] == 5
        missing = [e for e in coordinator.event_log if e["type"] == "readback_missing"]
        assert [e["detail"] for e in missing] == ["field=10"]

    async def test_a_snapshot_after_the_request_ends_the_sequence(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data(dict(SNAPSHOT_5_80))
        await _write(hass, coordinator, mqtt, 23, SNAPSHOT_5_80)

        timers.advance(8)
        await _settle(hass)
        # The device clamped the write: what it reports is the value.
        coordinator._apply_data({"backup_reserve_pct": 20})

        timers.advance(27)
        await _settle(hass)

        assert mqtt.send_get_all.call_count == 2  # no retry
        assert coordinator.data["backup_reserve_pct"] == 20  # no restore
        assert not any(e["type"] == "readback_missing" for e in coordinator.event_log)

    async def test_an_absent_pre_write_value_is_forgotten_not_set_to_none(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data({"edev_soc_ratio_pct": 80})

        await _write(hass, coordinator, mqtt, 23, {"edev_soc_ratio_pct": 80})
        assert coordinator.data["backup_reserve_pct"] == 23

        timers.advance(35)
        await _settle(hass)

        assert "backup_reserve_pct" not in coordinator.data
        assert "backup_reserve_pct" not in coordinator._device_data


class TestAcknowledgement:
    """B1.7: the refusal check on the topic shared with the vendor app."""

    async def test_a_refusal_of_our_own_write_warns_logs_and_restores(
        self,
        hass: HomeAssistant,
        timers: _Timers,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data(dict(SNAPSHOT_5_80))
        await _write(hass, coordinator, mqtt, 23, SNAPSHOT_5_80)

        with caplog.at_level(logging.DEBUG):
            coordinator._on_mqtt_message(SET_REPLY_TOPIC, _ack(False))
        await asyncio.sleep(0)  # the restore is queued with call_soon_threadsafe
        await _settle(hass)

        warnings = _integration_warnings(caplog)
        assert len(warnings) == 1
        assert coordinator.device_tag in warnings[0].getMessage()
        assert SERIAL not in caplog.text
        rejected = [e for e in coordinator.event_log if e["type"] == "set_rejected"]
        assert [e["detail"] for e in rejected] == ["field=10"]
        assert coordinator.data["backup_reserve_pct"] == 5

    async def test_a_refusal_of_a_replaced_write_changes_nothing(
        self, hass: HomeAssistant, timers: _Timers
    ) -> None:
        """The refusal is handed over as a write; by the time it runs, a newer
        write may own the reserve in the store."""
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data(dict(SNAPSHOT_5_80))
        await _write(hass, coordinator, mqtt, 23, SNAPSHOT_5_80)
        first = coordinator._ocean2_writes.get("backup_reserve_pct")
        assert first is not None
        await _write(hass, coordinator, mqtt, 30, SNAPSHOT_5_80)
        assert coordinator.data["backup_reserve_pct"] == 30

        coordinator._ocean2_reject_write(first)

        assert coordinator.data["backup_reserve_pct"] == 30
        assert first.rejected is False

        # Control: the same call for the write that is current does restore.
        current = coordinator._ocean2_writes.get("backup_reserve_pct")
        assert current is not None
        coordinator._ocean2_reject_write(current)
        assert coordinator.data["backup_reserve_pct"] == 5
        assert current.rejected is True

    async def test_a_refusal_we_did_not_send_is_debug_only(
        self,
        hass: HomeAssistant,
        timers: _Timers,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        coordinator, _ = _coordinator(hass)
        coordinator._apply_data(dict(SNAPSHOT_5_80))

        with caplog.at_level(logging.DEBUG):
            coordinator._on_mqtt_message(SET_REPLY_TOPIC, _ack(False))
        await asyncio.sleep(0)
        await _settle(hass)

        assert not _integration_warnings(caplog)
        assert not any(e["type"] == "set_rejected" for e in coordinator.event_log)
        assert "did not send" in caplog.text
        assert coordinator.data["backup_reserve_pct"] == 5

    async def test_a_refusal_after_the_window_is_not_ours(
        self,
        hass: HomeAssistant,
        timers: _Timers,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data(dict(SNAPSHOT_5_80))
        await _write(hass, coordinator, mqtt, 23, SNAPSHOT_5_80)
        write = coordinator._ocean2_writes.get("backup_reserve_pct")
        assert write is not None

        with (
            caplog.at_level(logging.DEBUG),
            patch(_CLOCK, return_value=write.sent_at + 31),
        ):
            coordinator._on_mqtt_message(SET_REPLY_TOPIC, _ack(False))
        await asyncio.sleep(0)
        await _settle(hass)

        assert not _integration_warnings(caplog)
        assert coordinator.data["backup_reserve_pct"] == 23

        # Control: inside the window the same reply is ours.
        with patch(_CLOCK, return_value=write.sent_at + 29):
            coordinator._on_mqtt_message(SET_REPLY_TOPIC, _ack(False))
        await asyncio.sleep(0)
        await _settle(hass)
        assert coordinator.data["backup_reserve_pct"] == 5

    async def test_an_accepted_write_keeps_its_value_and_stays_quiet(
        self,
        hass: HomeAssistant,
        timers: _Timers,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        coordinator, mqtt = _coordinator(hass)
        coordinator._apply_data(dict(SNAPSHOT_5_80))
        await _write(hass, coordinator, mqtt, 23, SNAPSHOT_5_80)

        with caplog.at_level(logging.DEBUG):
            coordinator._on_mqtt_message(SET_REPLY_TOPIC, _ack(True))
        await asyncio.sleep(0)
        await _settle(hass)

        assert not _integration_warnings(caplog)
        assert not any(e["type"] == "set_rejected" for e in coordinator.event_log)
        assert "Setting applied on" in caplog.text
        assert coordinator.data["backup_reserve_pct"] == 23
