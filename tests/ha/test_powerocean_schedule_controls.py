"""Controls for a PowerOcean scheduled charge task (#328).

The read side of the schedule is covered next door in
`test_powerocean_schedule_entities`. This file is about the write side: a
switch that arms and disarms a slot, and a number that changes its charge
power.

Two things decide whether these are safe. The first is the gate: a PowerOcean
whose owner never made a schedule must get no controls at all, and neither
must one running on developer keys, where the command has no channel to travel
on. The second is what the power write carries. A full body hands the whole
task back to the device, so four fields it owns travel with it, and every one
of them comes from the last read. A slot that has not reported one of them
refuses the write rather than composing a value, because a composed
recurrence would silently move the days and times the owner set in the app.

The frames are the reporter's own, replayed through the ingest loop, and the
payloads the writes produce are decoded with the integration's own decoder
rather than compared as opaque bytes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    AUTH_METHOD_DEVELOPER,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SECRET_KEY,
    CONF_USER_ID,
    DEVICE_TYPE_POWEROCEAN,
    DOMAIN,
    MODE_ENHANCED,
    MODE_STANDARD,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.number import (
    async_setup_entry as number_setup,
)
from custom_components.ecoflow_energy.switch import (
    async_setup_entry as switch_setup,
)

from tests.test_powerocean_timer_task import (
    LIST_ARMED_1000W,
    LIST_ARMED_1500W,
    LIST_DISARMED_1500W,
    LIST_EMPTY,
    _header,
)
from tests.test_powerocean_timer_task_write import pdata_of

POWEROCEAN_DEVICE: dict[str, Any] = {
    "sn": "HJ31TEST00000001",
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}

GET_REPLY = f"/app/user123/{POWEROCEAN_DEVICE['sn']}/thing/property/get_reply"


class FakeMqtt:
    """Records what the coordinator hands the broker.

    Only the two methods the SET path calls. The payload is kept so the test
    can decode what actually went out rather than trust the return value.
    """

    def __init__(self, connected: bool = True, delivers: bool = True) -> None:
        self._connected = connected
        self.delivers = delivers
        self.sent: list[bytes] = []

    def is_connected(self) -> bool:
        return self._connected

    def send_proto_set(self, payload: bytes, wait: bool = False) -> bool:
        self.sent.append(payload)
        return self.delivers


def _bundle(payload: bytes) -> bytes:
    """The get-all reply carries the task list twice, as the device sends it."""
    return _header(96, 10, payload) + _header(96, 10, payload)


def _entry(
    enhanced: bool = True, device: dict[str, Any] | None = None
) -> MockConfigEntry:
    device = device or POWEROCEAN_DEVICE
    if enhanced:
        data = {
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [device],
        }
        unique_id = "test@example.com"
    else:
        data = {
            CONF_AUTH_METHOD: AUTH_METHOD_DEVELOPER,
            CONF_MODE: MODE_STANDARD,
            CONF_ACCESS_KEY: "test_ak",
            CONF_SECRET_KEY: "test_sk",
            CONF_DEVICES: [device],
        }
        unique_id = "test_ak"
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data=data,
        unique_id=unique_id,
    )


def _report(
    coordinator: EcoFlowDeviceCoordinator, payload: bytes, sn: str | None = None
) -> None:
    """Push one bundle through the ingest loop and the real apply path."""
    topic = GET_REPLY if sn is None else f"/app/user123/{sn}/thing/property/get_reply"
    parsed = coordinator._parse_message(topic, _bundle(payload))
    if parsed:
        coordinator._apply_data(parsed)


async def _setup(
    hass: HomeAssistant,
    payload: bytes | None = None,
    enhanced: bool = True,
    delivers: bool = True,
    sn: str | None = None,
) -> tuple[EcoFlowDeviceCoordinator, list[Any], list[Any], FakeMqtt]:
    """Replay one task-list frame, then run both control platforms over it."""
    device = POWEROCEAN_DEVICE if sn is None else {**POWEROCEAN_DEVICE, "sn": sn}
    entry = _entry(enhanced, device)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    mqtt = FakeMqtt(delivers=delivers)
    coordinator._mqtt_client = mqtt
    if payload is not None:
        _report(coordinator, payload, sn)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {device["sn"]: coordinator}

    switches: list[Any] = []
    numbers: list[Any] = []
    await switch_setup(hass, entry, switches.extend)
    await number_setup(hass, entry, numbers.extend)
    return coordinator, switches, numbers, mqtt


def _schedule_keys(entities: list[Any]) -> set[str]:
    return {
        entity._definition.key
        for entity in entities
        if hasattr(entity, "_definition")
        and entity._definition.key.startswith("schedule_")
    }


def _by_key(entities: list[Any], key: str) -> Any:
    return next(
        entity
        for entity in entities
        if getattr(entity, "_definition", None) is not None
        and entity._definition.key == key
    )


def _prepare(entity: Any, hass: HomeAssistant) -> Any:
    """Give an entity the wiring a service call would have given it.

    The state write is stubbed because these entities are built directly
    rather than through an entity platform, which is also the only thing the
    real write needs. The value under test is read back off the entity
    property, not off the state machine.
    """
    entity.hass = hass
    entity.entity_id = f"domain.{entity._definition.key}"
    entity.async_write_ha_state = MagicMock()
    return entity


def _fields(payload: bytes) -> dict[int, Any]:
    """Decode the written body into {field number: value}.

    Hand-parsed rather than run through a message definition, because what
    matters here is which fields are present at all: the enable flag is
    expressed by its own absence, and a decoder that fills in defaults would
    hide exactly the thing under test.
    """
    body = pdata_of(payload)
    out: dict[int, Any] = {}
    pos = 0
    while pos < len(body):
        key, pos = _varint(body, pos)
        field, wire = key >> 3, key & 0x07
        if wire == 0:
            value, pos = _varint(body, pos)
        elif wire == 2:
            length, pos = _varint(body, pos)
            value = body[pos : pos + length]
            pos += length
        else:  # pragma: no cover - the builder emits no other wire types
            raise AssertionError(f"unexpected wire type {wire}")
        out[field] = value
    return out


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


class TestTheSwitch:
    async def test_only_the_reported_slot_becomes_a_switch(
        self, hass: HomeAssistant
    ) -> None:
        """Slot 1 exists, slots 2 to 8 do not."""
        _, switches, _, _ = await _setup(hass, LIST_ARMED_1500W)

        assert _schedule_keys(switches) == {"schedule_1_enabled"}

    async def test_the_switch_reads_the_devices_own_arming(
        self, hass: HomeAssistant
    ) -> None:
        _, armed_switches, _, _ = await _setup(hass, LIST_ARMED_1500W)
        assert _by_key(armed_switches, "schedule_1_enabled").is_on is True

    async def test_a_disarmed_schedule_reads_off(self, hass: HomeAssistant) -> None:
        """An absent enable flag is disarmed, not unknown."""
        _, switches, _, _ = await _setup(hass, LIST_DISARMED_1500W)

        assert _by_key(switches, "schedule_1_enabled").is_on is False

    async def test_arming_sends_the_short_enable_frame(
        self, hass: HomeAssistant
    ) -> None:
        _, switches, _, mqtt = await _setup(hass, LIST_DISARMED_1500W)
        switch = _prepare(_by_key(switches, "schedule_1_enabled"), hass)

        await switch.async_turn_on()

        assert len(mqtt.sent) == 1
        fields = _fields(mqtt.sent[0])
        assert fields == {2: 2, 3: 1, 4: 1}
        assert switch.is_on is True

    async def test_disarming_omits_the_enable_field_entirely(
        self, hass: HomeAssistant
    ) -> None:
        """A false bool is not serialised, and an explicit 4=0 is a no-op that
        would leave the schedule armed while the switch showed off."""
        _, switches, _, mqtt = await _setup(hass, LIST_ARMED_1500W)
        switch = _prepare(_by_key(switches, "schedule_1_enabled"), hass)

        await switch.async_turn_off()

        fields = _fields(mqtt.sent[0])
        assert fields == {2: 2, 3: 1}
        assert 4 not in fields
        assert switch.is_on is False

    async def test_the_arming_a_power_write_carries_comes_from_the_switch(
        self, hass: HomeAssistant
    ) -> None:
        """Arm, then change the power inside the same window. The power frame
        has to carry the flag the switch just sent, not the one the last
        report held, or the write would disarm what was just armed."""
        _, switches, numbers, mqtt = await _setup(hass, LIST_DISARMED_1500W)
        switch = _prepare(_by_key(switches, "schedule_1_enabled"), hass)
        number = _prepare(_by_key(numbers, "schedule_1_power_w"), hass)

        await switch.async_turn_on()
        await number.async_set_native_value(1200)

        assert _fields(mqtt.sent[1])[4] == 1

    async def test_a_rejected_arm_leaves_the_stored_flag_alone(
        self, hass: HomeAssistant
    ) -> None:
        """The seed goes in before the send so a power write in the same lock
        window sees it. A send that fails has to take it back out again."""
        coordinator, switches, _, _ = await _setup(
            hass, LIST_DISARMED_1500W, delivers=False
        )
        switch = _prepare(_by_key(switches, "schedule_1_enabled"), hass)

        with pytest.raises(HomeAssistantError):
            await switch.async_turn_on()

        assert coordinator.device_data["schedule_1_enabled"] is False

    async def test_the_unique_id_carries_the_slot_number(
        self, hass: HomeAssistant
    ) -> None:
        _, switches, numbers, _ = await _setup(hass, LIST_ARMED_1500W)
        serial = POWEROCEAN_DEVICE["sn"]

        assert (
            _by_key(switches, "schedule_1_enabled").unique_id
            == f"{serial}_schedule_1_enabled"
        )
        assert (
            _by_key(numbers, "schedule_1_power_w").unique_id
            == f"{serial}_schedule_1_power_w"
        )


class TestTheNumber:
    async def test_only_the_reported_slot_becomes_a_number(
        self, hass: HomeAssistant
    ) -> None:
        _, _, numbers, _ = await _setup(hass, LIST_ARMED_1500W)

        assert _schedule_keys(numbers) == {"schedule_1_power_w"}

    async def test_the_number_reads_the_reported_power(
        self, hass: HomeAssistant
    ) -> None:
        _, _, numbers, _ = await _setup(hass, LIST_ARMED_1000W)

        assert _by_key(numbers, "schedule_1_power_w").native_value == 1000

    async def test_writing_the_power_keeps_the_schedule_armed(
        self, hass: HomeAssistant
    ) -> None:
        """Measured on hardware: the combined frame changes the watts and the
        device's own task list still reports the task as armed afterwards."""
        _, _, numbers, mqtt = await _setup(hass, LIST_ARMED_1000W)
        number = _prepare(_by_key(numbers, "schedule_1_power_w"), hass)

        await number.async_set_native_value(1500)

        fields = _fields(mqtt.sent[0])
        assert fields[7] == 1500
        assert fields[4] == 1
        assert number.native_value == 1500

    async def test_a_disarmed_schedule_stays_disarmed_through_a_power_write(
        self, hass: HomeAssistant
    ) -> None:
        """The flag travels as it was read. Sending 4=1 here would arm a
        schedule the owner had switched off."""
        _, _, numbers, mqtt = await _setup(hass, LIST_DISARMED_1500W)
        number = _prepare(_by_key(numbers, "schedule_1_power_w"), hass)

        await number.async_set_native_value(1200)

        assert 4 not in _fields(mqtt.sent[0])

    async def test_the_echoed_fields_go_out_as_the_device_reported_them(
        self, hass: HomeAssistant
    ) -> None:
        """Task type, repeat kind, repeat parameter and the window block all
        belong to the device. A write hands them straight back."""
        _, _, numbers, mqtt = await _setup(hass, LIST_ARMED_1000W)
        number = _prepare(_by_key(numbers, "schedule_1_power_w"), hass)

        await number.async_set_native_value(1500)

        fields = _fields(mqtt.sent[0])
        assert fields[6] == 1
        assert fields[8] == 68
        assert fields[9] == 1037597
        assert fields[10] == bytes.fromhex("b089b826")

    async def test_a_failed_send_puts_the_stored_power_back(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _, numbers, _ = await _setup(
            hass, LIST_ARMED_1000W, delivers=False
        )
        number = _prepare(_by_key(numbers, "schedule_1_power_w"), hass)

        with pytest.raises(HomeAssistantError):
            await number.async_set_native_value(1500)

        assert coordinator.device_data["schedule_1_power_w"] == 1000

    async def test_the_range_is_the_one_the_app_offers(
        self, hass: HomeAssistant
    ) -> None:
        """The floor is 100 W per online pack, the ceiling comes from the
        model, and the step is the 100 W grid every value on file sits on."""
        coordinator, _, numbers, _ = await _setup(hass, LIST_ARMED_1000W)
        number = _by_key(numbers, "schedule_1_power_w")

        assert number.native_min_value == 100
        assert number.native_step == 100
        assert number.native_unit_of_measurement == "W"

        coordinator._apply_data({"bp_online_sum": 2})
        assert number.native_min_value == 200

        # HJ31, and the whole point is that the ceiling is not the builder's
        # 30000 W guard.
        assert number.native_max_value == 10000

    async def test_a_pack_count_the_device_has_not_sent_keeps_the_one_pack_floor(
        self, hass: HomeAssistant
    ) -> None:
        """Never zero: the app cannot send it and no device was ever asked."""
        coordinator, _, numbers, _ = await _setup(hass, LIST_ARMED_1000W)
        number = _by_key(numbers, "schedule_1_power_w")

        assert "bp_online_sum" not in coordinator.device_data
        assert number.native_min_value == 100

    @pytest.mark.parametrize(
        ("sn", "ceiling"),
        [
            ("HJ31TEST00000001", 10000),
            ("J329TEST00000001", 6000),
            ("R372TEST00000001", 29900),
            # A three-phase prefix nobody has mapped falls back to what its
            # family carries, not to the builder's guard.
            ("HJ39TEST00000001", 10000),
        ],
    )
    async def test_the_ceiling_follows_the_model(
        self, hass: HomeAssistant, sn: str, ceiling: int
    ) -> None:
        _, _, numbers, _ = await _setup(hass, LIST_ARMED_1000W, sn=sn)
        number = _by_key(numbers, "schedule_1_power_w")

        assert number.native_max_value == ceiling

    async def test_a_value_off_the_hundred_watt_grid_is_refused(
        self, hass: HomeAssistant
    ) -> None:
        """Refused rather than rounded. The app rounds because its slider
        cannot leave the grid; a service call can, and quietly moving the
        number would put a setpoint on the wire nobody asked for."""
        coordinator, _, numbers, mqtt = await _setup(hass, LIST_ARMED_1000W)
        number = _prepare(_by_key(numbers, "schedule_1_power_w"), hass)

        with pytest.raises(HomeAssistantError):
            await number.async_set_native_value(1234)

        assert mqtt.sent == []
        assert coordinator.device_data["schedule_1_power_w"] == 1000

        await number.async_set_native_value(1500)
        assert len(mqtt.sent) == 1
        assert coordinator.device_data["schedule_1_power_w"] == 1500

    async def test_a_value_outside_the_models_range_is_refused(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _, numbers, mqtt = await _setup(
            hass, LIST_ARMED_1000W, sn="J329TEST00000001"
        )
        number = _prepare(_by_key(numbers, "schedule_1_power_w"), hass)

        with pytest.raises(HomeAssistantError):
            await number.async_set_native_value(6100)

        assert mqtt.sent == []
        assert coordinator.device_data["schedule_1_power_w"] == 1000


@pytest.mark.parametrize(
    "missing",
    ["schedule_1_type", "schedule_1_time_mode", "schedule_1_time_param",
     "schedule_1_time_table"],
)
async def test_a_slot_missing_an_echo_field_refuses_the_write(
    hass: HomeAssistant, missing: str
) -> None:
    """The refusal is the whole design. Every one of these four is part of the
    task the device owns, and a value composed here would go back as though
    the owner had set it."""
    coordinator, _, numbers, mqtt = await _setup(hass, LIST_ARMED_1000W)
    number = _prepare(_by_key(numbers, "schedule_1_power_w"), hass)
    coordinator.device_data[missing] = None

    with pytest.raises(HomeAssistantError):
        await number.async_set_native_value(1500)

    assert mqtt.sent == []
    assert coordinator.device_data["schedule_1_power_w"] == 1000


class TestNoSchedule:
    async def test_a_powerocean_without_a_schedule_gets_no_controls(
        self, hass: HomeAssistant
    ) -> None:
        """The majority case, and the reason the controls are gated at all."""
        _, switches, numbers, _ = await _setup(hass)

        assert _schedule_keys(switches) == set()
        assert _schedule_keys(numbers) == set()

    async def test_an_empty_task_list_is_still_no_controls(
        self, hass: HomeAssistant
    ) -> None:
        _, switches, numbers, _ = await _setup(hass, LIST_EMPTY)

        assert _schedule_keys(switches) == set()
        assert _schedule_keys(numbers) == set()

    async def test_the_other_powerocean_controls_are_unaffected(
        self, hass: HomeAssistant
    ) -> None:
        _, _, numbers, _ = await _setup(hass)
        keys = {
            entity._definition.key
            for entity in numbers
            if hasattr(entity, "_definition")
        }

        assert "backup_reserve" in keys
        assert "solar_surplus_threshold" in keys

    async def test_a_schedule_created_later_gets_its_controls(
        self, hass: HomeAssistant
    ) -> None:
        """An owner can make a schedule in the app while Home Assistant runs,
        so the platforms keep watching instead of deciding once at startup."""
        added: list[Any] = []
        entry = _entry()
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN_DEVICE)
        coordinator._mqtt_client = FakeMqtt()
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            POWEROCEAN_DEVICE["sn"]: coordinator
        }
        await switch_setup(hass, entry, added.extend)
        await number_setup(hass, entry, added.extend)
        assert _schedule_keys(added) == set()

        _report(coordinator, LIST_ARMED_1500W)
        await hass.async_block_till_done()

        assert _schedule_keys(added) == {
            "schedule_1_enabled",
            "schedule_1_power_w",
        }


class TestStandardMode:
    async def test_developer_keys_get_no_schedule_controls(
        self, hass: HomeAssistant
    ) -> None:
        """The command travels on the app channel, so on developer keys these
        could only ever fail. The keys are seeded on purpose: absence of data
        would pass this test on its own and prove nothing about the filter."""
        entry = _entry(enhanced=False)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN_DEVICE)
        coordinator.set_device_value("schedule_1_enabled", True)
        coordinator.set_device_value("schedule_1_power_w", 1500)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            POWEROCEAN_DEVICE["sn"]: coordinator
        }

        switches: list[Any] = []
        numbers: list[Any] = []
        await switch_setup(hass, entry, switches.extend)
        await number_setup(hass, entry, numbers.extend)

        assert _schedule_keys(switches) == set()
        assert _schedule_keys(numbers) == set()


class TestADeletedSlot:
    """What happens after the owner deletes the schedule in the app.

    The device stops listing the task, and the parser retracts every key of
    that slot to None. That does not take the controls away: a key that is
    present and None still counts as a reading, so the switch and the number
    stay where they are. Both therefore have to refuse rather than send, or
    the switch would name a slot the device no longer holds and then show a
    deleted schedule as armed. The reporter's own capture on #328 ends with a
    deletion, so this is the ordinary end of a schedule's life.
    """

    async def test_the_switch_refuses_a_slot_the_device_dropped(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, switches, _, mqtt = await _setup(hass, LIST_ARMED_1500W)
        switch = _prepare(_by_key(switches, "schedule_1_enabled"), hass)

        _report(coordinator, LIST_EMPTY)
        assert coordinator.device_data["schedule_1_enabled"] is None

        with pytest.raises(HomeAssistantError):
            await switch.async_turn_on()

        assert mqtt.sent == []
        assert coordinator.device_data["schedule_1_enabled"] is None

    async def test_the_number_refuses_a_slot_the_device_dropped(
        self, hass: HomeAssistant
    ) -> None:
        """The same for the power write, asserted rather than left to fall out
        of the echo fields being retracted alongside the flag."""
        coordinator, _, numbers, mqtt = await _setup(hass, LIST_ARMED_1000W)
        number = _prepare(_by_key(numbers, "schedule_1_power_w"), hass)

        _report(coordinator, LIST_EMPTY)

        with pytest.raises(HomeAssistantError):
            await number.async_set_native_value(1500)

        assert mqtt.sent == []

    async def test_the_number_says_gone_rather_than_try_again(
        self, hass: HomeAssistant
    ) -> None:
        """A deleted schedule and an unreported one need different words.

        Both refuse through the same exception, so the wording is the only
        thing that tells an owner whether waiting helps. A schedule the device
        has stopped reporting will not come back on its own, and the retry
        message would send its owner to watch an entity that stays empty.
        """
        coordinator, _, numbers, _ = await _setup(hass, LIST_ARMED_1000W)
        number = _prepare(_by_key(numbers, "schedule_1_power_w"), hass)

        _report(coordinator, LIST_EMPTY)

        with pytest.raises(HomeAssistantError) as raised:
            await number.async_set_native_value(1500)

        assert raised.value.translation_key == "set_command_gone"

    async def test_a_failed_power_write_does_not_invent_the_reading(
        self, hass: HomeAssistant
    ) -> None:
        """Rolling back to a value that was never there has to leave the key
        absent. Putting it back as a present None would build the control for
        a slot the device has never described, and nothing retracts it again.
        """
        coordinator, _, numbers, _ = await _setup(
            hass, LIST_ARMED_1000W, delivers=False
        )
        number = _prepare(_by_key(numbers, "schedule_1_power_w"), hass)
        del coordinator.device_data["schedule_1_power_w"]
        coordinator.data.pop("schedule_1_power_w", None)

        with pytest.raises(HomeAssistantError):
            await number.async_set_native_value(1500)

        assert "schedule_1_power_w" not in coordinator.device_data
        assert "schedule_1_power_w" not in (coordinator.data or {})


class TestTheArmingHold:
    """A frame that was already in flight must not undo the write it crossed.

    The power write reads the arming flag out of the store and carries it as
    field 4. So a device report that lands between an arming write and the
    next power change does more than blink the switch: it feeds the old flag
    straight back onto the wire, re-arming a schedule the owner just switched
    off. The hold is short because the device acknowledges in about 0.2 s and
    its task list already carries the change - only a frame that left before
    the write arrived can still disagree.
    """

    async def test_a_stale_report_cannot_revert_a_flag_just_written(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, switches, numbers, mqtt = await _setup(
            hass, LIST_ARMED_1500W
        )
        switch = _prepare(_by_key(switches, "schedule_1_enabled"), hass)
        number = _prepare(_by_key(numbers, "schedule_1_power_w"), hass)

        await switch.async_turn_off()
        _report(coordinator, LIST_ARMED_1500W)
        await number.async_set_native_value(1200)

        assert coordinator.device_data["schedule_1_enabled"] is False
        assert 4 not in _fields(mqtt.sent[1])

    async def test_a_report_that_agrees_ends_the_hold(
        self, hass: HomeAssistant
    ) -> None:
        """The device confirming the write is the end of it, not the clock."""
        coordinator, switches, _, _ = await _setup(hass, LIST_DISARMED_1500W)
        switch = _prepare(_by_key(switches, "schedule_1_enabled"), hass)

        await switch.async_turn_on()
        assert coordinator._schedule_armed_latch

        _report(coordinator, LIST_ARMED_1500W)

        assert coordinator._schedule_armed_latch == {}
        assert coordinator.device_data["schedule_1_enabled"] is True

    async def test_the_device_wins_once_the_hold_is_over(
        self, hass: HomeAssistant
    ) -> None:
        """A schedule the owner arms in the app afterwards has to show up."""
        coordinator, switches, _, _ = await _setup(hass, LIST_DISARMED_1500W)
        switch = _prepare(_by_key(switches, "schedule_1_enabled"), hass)

        await switch.async_turn_on()
        coordinator._schedule_armed_latch["schedule_1_enabled"] = (True, 0.0)
        _report(coordinator, LIST_DISARMED_1500W)

        assert coordinator.device_data["schedule_1_enabled"] is False
        assert coordinator._schedule_armed_latch == {}
