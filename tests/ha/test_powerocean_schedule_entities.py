"""Entities for a PowerOcean scheduled charge task (#328).

A PowerOcean has a schedule only when its owner created one in the app, and
most owners never do. The readings are therefore gated the same way the
heating rod and the wallbox are: the definition exists for every slot the
device can number, and the entity is created on the first report that carries
that slot. What the platforms have to get right is the empty case, the
Standard Mode case, and the schedule that disappears again - a deleted task
must leave the entity unknown rather than frozen on the last window it had.

The frames are the reporter's own, replayed through the ingest loop rather
than hand-written, so the states asserted here are the ones the device sent.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.binary_sensor import (
    async_setup_entry as binary_sensor_setup,
)
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
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

from tests.test_powerocean_timer_task import (
    LIST_ARMED_1000W,
    LIST_ARMED_1500W,
    LIST_EMPTY,
    _header,
)

POWEROCEAN_DEVICE: dict[str, Any] = {
    "sn": "HJ31TEST00000001",
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}

GET_REPLY = f"/app/user123/{POWEROCEAN_DEVICE['sn']}/thing/property/get_reply"


def _bundle(payload: bytes) -> bytes:
    """The get-all reply carries the task list twice, as the device sends it."""
    return _header(96, 10, payload) + _header(96, 10, payload)


def _entry(enhanced: bool = True) -> MockConfigEntry:
    if enhanced:
        data = {
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [POWEROCEAN_DEVICE],
        }
        unique_id = "test@example.com"
    else:
        data = {
            CONF_AUTH_METHOD: AUTH_METHOD_DEVELOPER,
            CONF_MODE: MODE_STANDARD,
            CONF_ACCESS_KEY: "test_ak",
            CONF_SECRET_KEY: "test_sk",
            CONF_DEVICES: [POWEROCEAN_DEVICE],
        }
        unique_id = "test_ak"
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data=data,
        unique_id=unique_id,
    )


async def _setup(
    hass: HomeAssistant,
    payload: bytes | None = None,
    enhanced: bool = True,
) -> tuple[EcoFlowDeviceCoordinator, list[Any], list[Any]]:
    """Replay one task-list frame, then run both platforms over the result."""
    entry = _entry(enhanced)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN_DEVICE)
    if payload is not None:
        _report(coordinator, payload)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        POWEROCEAN_DEVICE["sn"]: coordinator
    }

    sensors: list[Any] = []
    binary_sensors: list[Any] = []
    await sensor_setup(hass, entry, sensors.extend)
    await binary_sensor_setup(hass, entry, binary_sensors.extend)
    return coordinator, sensors, binary_sensors


def _report(coordinator: EcoFlowDeviceCoordinator, payload: bytes) -> None:
    """Push one bundle through the ingest loop and the real apply path.

    Deliberately not `set_device_value` followed by `async_set_updated_data`:
    that reaches the same state by a route no message ever takes, and it skips
    every step between the parse and the entity - the monotonic guard, the
    value-change bookkeeping and the merge into the held data. Retracting a
    key to None is exactly the case one of those could swallow, so the test
    that asserts the retraction has to travel the path production travels.

    `_apply_data` is what the MQTT thread hands the parsed frame to, and the
    empty guard is the one the dispatcher applies before it.
    """
    parsed = coordinator._parse_message(GET_REPLY, _bundle(payload))
    if parsed:
        coordinator._apply_data(parsed)


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


class TestOneReportedSchedule:
    async def test_exactly_the_slot_the_device_reported_becomes_an_entity(
        self, hass: HomeAssistant
    ) -> None:
        """Slot 1 exists, slots 2 to 8 do not, so only slot 1 gets entities."""
        _, sensors, binary_sensors = await _setup(hass, LIST_ARMED_1500W)

        assert _schedule_keys(sensors) == {"schedule_1_window"}
        assert _schedule_keys(binary_sensors) == {"schedule_1_running"}

    async def test_the_window_reads_as_the_device_set_it(
        self, hass: HomeAssistant
    ) -> None:
        _, sensors, _ = await _setup(hass, LIST_ARMED_1500W)

        assert _by_key(sensors, "schedule_1_window").native_value == "20:00-20:30"

    async def test_running_follows_the_devices_own_flag(
        self, hass: HomeAssistant
    ) -> None:
        """The 19:06:32 frame: the window is open, so the task is running."""
        _, _, binary_sensors = await _setup(hass, LIST_ARMED_1500W)

        assert _by_key(binary_sensors, "schedule_1_running").is_on is True

    async def test_an_armed_task_before_its_window_is_not_running(
        self, hass: HomeAssistant
    ) -> None:
        """The 18:59:22 frame. Armed and running are two separate flags, and
        a schedule that has not started yet must not read as charging."""
        _, sensors, binary_sensors = await _setup(hass, LIST_ARMED_1000W)

        assert _by_key(binary_sensors, "schedule_1_running").is_on is False
        assert _by_key(sensors, "schedule_1_window").native_value == "20:00-20:30"

    async def test_the_unique_id_carries_the_slot_number(
        self, hass: HomeAssistant
    ) -> None:
        """Two schedules would otherwise share one identity."""
        _, sensors, binary_sensors = await _setup(hass, LIST_ARMED_1500W)
        serial = POWEROCEAN_DEVICE["sn"]

        assert (
            _by_key(sensors, "schedule_1_window").unique_id
            == f"{serial}_schedule_1_window"
        )
        assert (
            _by_key(binary_sensors, "schedule_1_running").unique_id
            == f"{serial}_schedule_1_running"
        )


class TestNoSchedule:
    async def test_a_powerocean_without_a_schedule_gets_no_schedule_entities(
        self, hass: HomeAssistant
    ) -> None:
        """The majority case, and the reason the readings are gated at all."""
        _, sensors, binary_sensors = await _setup(hass)

        assert _schedule_keys(sensors) == set()
        assert _schedule_keys(binary_sensors) == set()

    async def test_an_empty_task_list_is_still_no_entities(
        self, hass: HomeAssistant
    ) -> None:
        """An empty list is a report, not a silence, and it names no slot."""
        _, sensors, binary_sensors = await _setup(hass, LIST_EMPTY)

        assert _schedule_keys(sensors) == set()
        assert _schedule_keys(binary_sensors) == set()

    async def test_the_other_powerocean_readings_are_unaffected(
        self, hass: HomeAssistant
    ) -> None:
        _, sensors, _ = await _setup(hass)
        keys = {
            entity._definition.key
            for entity in sensors
            if hasattr(entity, "_definition")
        }

        assert "soc_pct" in keys
        assert "mppt_pv1_power_w" in keys


class TestStandardMode:
    async def test_developer_keys_get_no_schedule_entities(
        self, hass: HomeAssistant
    ) -> None:
        """Both the task list and the command that changes it live on the app
        channel, so with developer keys these could only ever read unknown.
        The keys are seeded here on purpose: absence of data would pass this
        test on its own and prove nothing about the mode filter."""
        entry = _entry(enhanced=False)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN_DEVICE)
        coordinator.set_device_value("schedule_1_window", "20:00-20:30")
        coordinator.set_device_value("schedule_1_running", True)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            POWEROCEAN_DEVICE["sn"]: coordinator
        }

        sensors: list[Any] = []
        binary_sensors: list[Any] = []
        await sensor_setup(hass, entry, sensors.extend)
        await binary_sensor_setup(hass, entry, binary_sensors.extend)

        assert coordinator.enhanced_mode is False
        assert _schedule_keys(sensors) == set()
        assert _schedule_keys(binary_sensors) == set()


class TestScheduleAppearsAndDisappears:
    async def test_a_schedule_created_later_adds_its_entities(
        self, hass: HomeAssistant
    ) -> None:
        """The owner creates the task while Home Assistant is running. Without
        the watcher on each platform the entities would only show up after a
        reload."""
        coordinator, sensors, binary_sensors = await _setup(hass, LIST_EMPTY)
        assert _schedule_keys(sensors) == set()

        _report(coordinator, LIST_ARMED_1500W)
        await hass.async_block_till_done()

        assert _schedule_keys(sensors) == {"schedule_1_window"}
        assert _schedule_keys(binary_sensors) == {"schedule_1_running"}
        assert _by_key(sensors, "schedule_1_window").native_value == "20:00-20:30"

    async def test_a_deleted_schedule_reads_unknown_rather_than_stale(
        self, hass: HomeAssistant
    ) -> None:
        """The device stops mentioning a task it no longer holds, so nothing
        retracts the window unless the read path does. Leave it standing and
        the entity keeps advertising a charge window that no longer exists."""
        coordinator, sensors, binary_sensors = await _setup(hass, LIST_ARMED_1500W)
        window = _by_key(sensors, "schedule_1_window")
        running = _by_key(binary_sensors, "schedule_1_running")
        assert window.native_value == "20:00-20:30"

        _report(coordinator, LIST_EMPTY)

        assert window.native_value is None
        assert running.is_on is None
