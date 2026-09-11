"""Controls for the PowerOcean feed-to-grid schedule (#381).

The read side of this list is `remap_tou_task_keys`, the device's second task
list (cmd_func 96, cmd_id 14), independent of the charge schedule's own index
space. This file is the write side: a switch that arms and disarms a slot,
and a number that changes the export power, both routed through the same
lock, seed/rollback and `DeviceValueNotReported` checks the charge schedule
uses (`_powerocean_schedule_families`, ADR-027 addendum).

Two things differ from the charge schedule. The ceiling this family checks a
write against is a real device reading (`ems_feed_power_limit_w`), not a
pack-count-derived guess, so an unreported ceiling refuses the write rather
than falling back to a model default. And the recurrence echoed back on a
power write only requires three fields, not four: `time_param` is allowed to
be absent, because the daily task never carries it on any captured frame.

The frames are the reporter's own (#381 capture), replayed through the ingest
loop, and the written bytes are compared against the recorded write bodies
rather than against what the builder is understood to produce.
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
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)
from custom_components.ecoflow_energy.number import (
    async_setup_entry as number_setup,
)
from custom_components.ecoflow_energy.switch import (
    async_setup_entry as switch_setup,
)
from tests.test_powerocean_timer_task import _header
from tests.test_powerocean_timer_task_write import pdata_of

from .conftest import add_entities_collector

POWEROCEAN_DEVICE: dict[str, Any] = {
    "sn": "HJ31TEST00000001",
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}

GET_REPLY = f"/app/user123/{POWEROCEAN_DEVICE['sn']}/thing/property/get_reply"

# --- Fixture payloads --------------------------------------------------
#
# tests/fixtures/powerocean/j327_tou_task_write_masked.json, `lists_96_14`
# (indexed by `utc`) and `writes_96_143`. Slots 2 and 3 are a standing
# schedule for the whole capture; slot 4 is created at 12:04:06 and deleted
# again at 12:04:55.

# 12:00:39, and byte-identical again at 12:04:55 after slot 4 is deleted:
# slot 3 (armed, 3500 W, running, window 11:30-17:00) and slot 2 (armed,
# 2100 W, not running, window 08:00-11:30, 17:00-19:00).
LIST_SLOTS_2_3 = bytes.fromhex(
    "0a17100218032001280230ac1b3801420808411a04b285f01f"
    "0a1b100218022001280230b4103800420c08411a08e083c815fc87d023"
)

# 12:04:22: the same two slots, plus slot 4 (armed, 2600 W, not running,
# daily at 21:00-21:30 and 22:00-22:30, no time_param on the wire).
LIST_SLOTS_2_3_4 = bytes.fromhex(
    "0a17100218032001280230ac1b3801420808411a04b285f01f"
    "0a1b100218022001280230b4103800420c08411a08e083c815fc87d023"
    "0a1b100218042001280230a8143800420c08411a08ec89a828a88a982a"
)

# 12:04:55: slot 4 gone again, byte-identical to the 12:00:39 list.
LIST_SLOTS_2_3_AGAIN = LIST_SLOTS_2_3

# writes_96_143, the recorded bodies a builder must reproduce byte for byte.
ENABLE_SLOT4_PDATA = bytes.fromhex("100218042001")
DISABLE_SLOT4_PDATA = bytes.fromhex("10021804")
POWER_SLOT4_2600_PDATA = bytes.fromhex(
    "100218042001280230a814420c08411a08ec89a828a88a982a"
)


class FakeMqtt(MagicMock):
    """Records what the coordinator hands the broker.

    Only the two methods the SET path calls. The payload is kept so the test
    can decode what actually went out rather than trust the return value.

    Subclasses `MagicMock` so the assignment to the coordinator's
    `EcoFlowMQTTClient | None`-typed attribute type-checks; the methods below
    are real overrides and shadow Mock's auto-attribute behaviour.
    """

    def __init__(self, connected: bool = True, delivers: bool = True) -> None:
        super().__init__()
        self._connected = connected
        self.delivers = delivers
        self.sent: list[bytes] = []

    def is_connected(self) -> bool:
        return self._connected

    def send_proto_set(self, payload: bytes, wait: bool = False) -> bool:
        self.sent.append(payload)
        return self.delivers


def _bundle(payload: bytes) -> bytes:
    """The feed-list reply, wrapped once on cmd_id 14.

    Unlike the charge schedule's 96/10 list, this family has no observed
    two-copy quirk to reproduce, and the ingest's first-copy-wins bookkeeping
    treats a single copy the same way it treats a duplicate.
    """
    return _header(96, 14, payload)


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
    """Push one feed-list frame through the ingest loop and the real apply path."""
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
    """Replay one feed-list frame, then run both control platforms over it."""
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
    await switch_setup(hass, entry, add_entities_collector(switches))
    await number_setup(hass, entry, add_entities_collector(numbers))
    return coordinator, switches, numbers, mqtt


def _feed_keys(entities: list[Any]) -> set[str]:
    return {
        entity._definition.key
        for entity in entities
        if hasattr(entity, "_definition")
        and entity._definition.key.startswith("feed_schedule_")
    }


def _plain_schedule_keys(entities: list[Any]) -> set[str]:
    """Keys of the charge-schedule family, to prove it stayed untouched."""
    return {
        entity._definition.key
        for entity in entities
        if hasattr(entity, "_definition")
        and entity._definition.key.startswith("schedule_")
        and not entity._definition.key.startswith("feed_schedule_")
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
        value: int | bytes
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


def _cmd_id(payload: bytes) -> int:
    """Decode the header's cmd_id (field 9), the family write travels on."""
    headers, _ = decode_header_message(payload)
    assert len(headers) == 1, f"expected one header, got {len(headers)}"
    return int(headers[0]["cmd_id"])


class TestTheSwitch:
    async def test_only_the_reported_slots_become_switches(
        self, hass: HomeAssistant
    ) -> None:
        """Slots 2 and 3 exist; the charge schedule never reported, so it
        gets no switch of its own even though every slot has a definition."""
        _, switches, _, _ = await _setup(hass, LIST_SLOTS_2_3)

        assert _feed_keys(switches) == {
            "feed_schedule_2_enabled",
            "feed_schedule_3_enabled",
        }
        assert _plain_schedule_keys(switches) == set()

    async def test_a_newly_reported_slot_becomes_a_switch(
        self, hass: HomeAssistant
    ) -> None:
        _, switches, _, _ = await _setup(hass, LIST_SLOTS_2_3_4)

        assert _feed_keys(switches) == {
            "feed_schedule_2_enabled",
            "feed_schedule_3_enabled",
            "feed_schedule_4_enabled",
        }
        assert _by_key(switches, "feed_schedule_4_enabled").is_on is True

    async def test_turning_a_slot_off_sends_exactly_the_disable_bytes(
        self, hass: HomeAssistant
    ) -> None:
        _, switches, _, mqtt = await _setup(hass, LIST_SLOTS_2_3_4)
        switch = _prepare(_by_key(switches, "feed_schedule_4_enabled"), hass)

        await switch.async_turn_off()

        assert len(mqtt.sent) == 1
        assert _cmd_id(mqtt.sent[0]) == 143
        assert pdata_of(mqtt.sent[0]) == DISABLE_SLOT4_PDATA
        assert _fields(mqtt.sent[0]) == {2: 2, 3: 4}
        assert switch.is_on is False

    async def test_turning_a_slot_on_sends_exactly_the_enable_bytes(
        self, hass: HomeAssistant
    ) -> None:
        _, switches, _, mqtt = await _setup(hass, LIST_SLOTS_2_3_4)
        switch = _prepare(_by_key(switches, "feed_schedule_4_enabled"), hass)

        await switch.async_turn_on()

        assert len(mqtt.sent) == 1
        assert _cmd_id(mqtt.sent[0]) == 143
        assert pdata_of(mqtt.sent[0]) == ENABLE_SLOT4_PDATA
        assert _fields(mqtt.sent[0]) == {2: 2, 3: 4, 4: 1}
        assert switch.is_on is True

    async def test_a_failed_arm_leaves_the_stored_flag_alone(
        self, hass: HomeAssistant
    ) -> None:
        """The seed goes in before the send so a power write in the same lock
        window sees it. A send that fails has to take it back out again."""
        coordinator, switches, _, _ = await _setup(
            hass, LIST_SLOTS_2_3_4, delivers=False
        )
        switch = _prepare(_by_key(switches, "feed_schedule_4_enabled"), hass)

        with pytest.raises(HomeAssistantError):
            await switch.async_turn_off()

        assert coordinator.device_data["feed_schedule_4_enabled"] is True


class TestTheNumber:
    async def test_only_the_reported_slots_become_numbers(
        self, hass: HomeAssistant
    ) -> None:
        _, _, numbers, _ = await _setup(hass, LIST_SLOTS_2_3_4)

        assert _feed_keys(numbers) == {
            "feed_schedule_2_power_w",
            "feed_schedule_3_power_w",
            "feed_schedule_4_power_w",
        }
        assert _by_key(numbers, "feed_schedule_4_power_w").native_value == 2600

    async def test_the_ceiling_follows_the_devices_own_feed_limit(
        self, hass: HomeAssistant
    ) -> None:
        """Unlike the charge schedule's model-derived ceiling, this one is a
        live reading, and it is not clipped to the declared default - neither
        on the slider nor on the write, which checks the same reading again
        rather than trusting the entity's own bounds."""
        coordinator, _, numbers, mqtt = await _setup(hass, LIST_SLOTS_2_3_4)
        number = _prepare(_by_key(numbers, "feed_schedule_4_power_w"), hass)

        coordinator._apply_data({"ems_feed_power_limit_w": 5000})
        assert number.native_min_value == 100
        assert number.native_max_value == 5000
        assert number.native_step == 100
        assert number.native_unit_of_measurement == "W"

        coordinator._apply_data({"ems_feed_power_limit_w": 8000})
        assert number.native_max_value == 8000

        # Above the declared 5000 W default, but within the reported 8000 W
        # ceiling - only reaches the device if the write path checks the
        # live reading rather than the declared range.
        await number.async_set_native_value(6000)
        assert len(mqtt.sent) == 1
        assert number.native_value == 6000

    async def test_with_no_reading_the_slider_keeps_the_declared_range_and_write_waits(
        self, hass: HomeAssistant
    ) -> None:
        """Before the device has ever reported its feed limit, the slider
        still needs bounds to offer - the declared default - but the write
        itself has nothing to check the value against and refuses outright."""
        _, _, numbers, mqtt = await _setup(hass, LIST_SLOTS_2_3_4)
        number = _prepare(_by_key(numbers, "feed_schedule_4_power_w"), hass)

        assert number.native_min_value == 100
        assert number.native_max_value == 5000

        with pytest.raises(HomeAssistantError) as raised:
            await number.async_set_native_value(2600)

        assert raised.value.translation_key == "set_command_not_ready"
        assert mqtt.sent == []

    async def test_writing_the_power_sends_the_recorded_full_body(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _, numbers, mqtt = await _setup(hass, LIST_SLOTS_2_3_4)
        coordinator._apply_data({"ems_feed_power_limit_w": 5000})
        number = _prepare(_by_key(numbers, "feed_schedule_4_power_w"), hass)

        await number.async_set_native_value(2600)

        assert len(mqtt.sent) == 1
        assert _cmd_id(mqtt.sent[0]) == 143
        assert pdata_of(mqtt.sent[0]) == POWER_SLOT4_2600_PDATA
        fields = _fields(mqtt.sent[0])
        assert fields[4] == 1  # still armed
        assert fields[5] == 2  # task_type, echoed
        assert 8 in fields
        assert number.native_value == 2600

    async def test_a_value_off_grid_or_above_the_reading_is_refused(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _, numbers, mqtt = await _setup(hass, LIST_SLOTS_2_3_4)
        coordinator._apply_data({"ems_feed_power_limit_w": 5000})
        number = _prepare(_by_key(numbers, "feed_schedule_4_power_w"), hass)

        with pytest.raises(HomeAssistantError):
            await number.async_set_native_value(2650)
        with pytest.raises(HomeAssistantError):
            await number.async_set_native_value(5100)

        assert mqtt.sent == []
        assert coordinator.device_data["feed_schedule_4_power_w"] == 2600

    async def test_a_failed_power_write_puts_the_stored_power_back(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _, numbers, _ = await _setup(
            hass, LIST_SLOTS_2_3_4, delivers=False
        )
        coordinator._apply_data({"ems_feed_power_limit_w": 5000})
        number = _prepare(_by_key(numbers, "feed_schedule_4_power_w"), hass)

        with pytest.raises(HomeAssistantError):
            await number.async_set_native_value(3200)

        assert coordinator.device_data["feed_schedule_4_power_w"] == 2600


class TestRetraction:
    """What happens after the owner deletes the slot in the app.

    The reporter's own capture on #381 ends exactly this way: slot 4 is
    created, changed, and deleted again a few frames later. The device stops
    listing the task, and the parser retracts every key of that slot to
    None - which leaves the switch and the number standing rather than
    removing them, since a key that is present and None still counts as a
    reading. Both controls therefore refuse the write rather than send it.
    """

    async def test_a_deleted_slot_refuses_the_write(self, hass: HomeAssistant) -> None:
        coordinator, switches, _, mqtt = await _setup(hass, LIST_SLOTS_2_3_4)
        switch = _prepare(_by_key(switches, "feed_schedule_4_enabled"), hass)

        _report(coordinator, LIST_SLOTS_2_3_AGAIN)
        assert coordinator.device_data["feed_schedule_4_enabled"] is None

        with pytest.raises(HomeAssistantError) as raised:
            await switch.async_turn_on()

        assert raised.value.translation_key == "set_command_gone"
        assert mqtt.sent == []
        assert coordinator.device_data["feed_schedule_2_enabled"] is True
        assert coordinator.device_data["feed_schedule_3_enabled"] is True


class TestTheSensors:
    """The window and running readings, published straight to `device_data`.

    Neither is created by the switch or number platforms, so they are read
    off the coordinator's own store rather than off an entity.
    """

    async def test_the_window_and_running_readings(self, hass: HomeAssistant) -> None:
        coordinator, _, _, _ = await _setup(hass, LIST_SLOTS_2_3_4)

        assert (
            coordinator.device_data["feed_schedule_4_window"]
            == "21:00-21:30, 22:00-22:30"
        )
        assert coordinator.device_data["feed_schedule_3_running"] is True
        assert coordinator.device_data["feed_schedule_2_running"] is False


class TestFamilyGuard:
    async def test_an_unknown_family_raises(self, hass: HomeAssistant) -> None:
        coordinator, _, _, _ = await _setup(hass, LIST_SLOTS_2_3)

        with pytest.raises(ValueError):
            await coordinator.async_set_powerocean_schedule_armed("bogus", 1, True)
