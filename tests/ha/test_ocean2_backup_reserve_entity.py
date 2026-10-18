"""Entity side of the Ocean 2 Backup Reserve write (PLAN-151, brief B2).

The coordinator write is covered in `test_ocean2_backup_reserve_coordinator.py`,
where the store is filled directly. These tests start one step earlier and one
step later: the readings arrive as real `254/39` frames through
`_on_mqtt_message`, the number is created by the platform setup, and what the
user sees is what the entity writes to the state machine.

`async_write_ha_state` is replaced by a recorder that captures
`native_value` at each call, so "shows 23" means "the last state written
carried 23", not "the property happens to read 23 right now".
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy import const as const_module
from custom_components.ecoflow_energy import number as number_module
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
    OCEAN2_NUMBERS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.coordinator.set_commands import (
    DeviceValueNotReported,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)
from custom_components.ecoflow_energy.number import (
    EcoFlowNumber,
    _get_number_defs,
)
from custom_components.ecoflow_energy.number import (
    async_setup_entry as number_setup,
)

from .conftest import add_entities_collector

SERIAL = "RE11TEST00000001"
DEVICE: dict[str, Any] = {
    "sn": SERIAL,
    "name": "",
    "product_name": "",
    "device_type": DEVICE_TYPE_OCEAN2,
    "online": 1,
}


def _frame(reserve: int | None = None, ratio: int | None = None) -> bytes:
    """A `254/39` telemetry frame carrying field 10 and/or field 92."""
    pdata = b""
    if reserve is not None:
        pdata += encode_field_varint(10, reserve)
    if ratio is not None:
        pdata += encode_field_varint(92, ratio)
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 254)
        + encode_field_varint(9, 39)
    )
    return encode_field_bytes(1, header)


def _push(
    coordinator: EcoFlowDeviceCoordinator,
    reserve: int | None = None,
    ratio: int | None = None,
) -> None:
    """Deliver a frame the way the MQTT client does."""
    topic = f"/app/device/property/{coordinator.device_sn}"
    coordinator._on_mqtt_message(topic, _frame(reserve, ratio))


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


def _refuse(coordinator: EcoFlowDeviceCoordinator) -> None:
    """Deliver the device's refusal on the shared set_reply topic."""
    topic = f"/app/user123/{coordinator.device_sn}/thing/property/set_reply"
    coordinator._on_mqtt_message(topic, _ack(False))


def _setup_coordinator(
    hass: HomeAssistant, serial: str = SERIAL
) -> tuple[EcoFlowDeviceCoordinator, MockConfigEntry, MagicMock]:
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
    coordinator._mqtt_client = mqtt
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {serial: coordinator}
    return coordinator, entry, mqtt


async def _create_numbers(
    hass: HomeAssistant,
    coordinator: EcoFlowDeviceCoordinator,
    entry: MockConfigEntry,
) -> list[Any]:
    created: list[Any] = []
    await number_setup(hass, entry, add_entities_collector(created))
    return created


def _wire(entity: EcoFlowNumber, coordinator: EcoFlowDeviceCoordinator) -> list[Any]:
    """Stand in for `async_added_to_hass`: listener and a state-write recorder.

    Returns the list of values each state write carried.
    """
    shown: list[Any] = []
    entity.async_write_ha_state = MagicMock(  # type: ignore[method-assign,misc]
        side_effect=lambda: shown.append(entity.native_value)
    )
    entity.entity_id = "number.ocean_2_backup_reserve"
    coordinator.async_add_listener(entity._handle_coordinator_update)
    return shown


def _answer_get_all_with(
    coordinator: EcoFlowDeviceCoordinator,
    mqtt: MagicMock,
    reserve: int | None,
    ratio: int | None,
) -> None:
    """Make the device answer the get-all before a write with a real frame."""

    def _send_get_all() -> bool:
        _push(coordinator, reserve, ratio)
        return True

    mqtt.send_get_all.side_effect = _send_get_all


@pytest.fixture
def no_readback_timers(hass: HomeAssistant) -> Any:
    """Keep the +8/+30/+35 s read-back timers from lingering past the test."""
    with patch.object(hass.loop, "call_later", return_value=MagicMock()) as patched:
        yield patched


class _Clock:
    """Stands in for the `time` module as `number.py` sees it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class TestCreation:
    async def test_no_entity_until_a_real_frame_reports_field_10(
        self, hass: HomeAssistant
    ) -> None:
        """B2.1: the number is an accessory, opened by the device's own report."""
        coordinator, entry, _ = _setup_coordinator(hass)

        created = await _create_numbers(hass, coordinator, entry)
        assert created == []

        # Field 92 alone is not the reading the control steers.
        _push(coordinator, reserve=None, ratio=80)
        await hass.async_block_till_done()
        assert created == []

        _push(coordinator, reserve=5, ratio=80)
        await hass.async_block_till_done()

        assert [e._definition.key for e in created] == ["backup_reserve"]
        entity = created[0]
        assert entity.unique_id == f"{SERIAL}_backup_reserve"
        assert entity.native_value == 5
        assert entity.native_min_value == 1
        assert entity.native_step == 1
        assert entity.mode == "slider"


class TestShownValue:
    async def test_a_write_shows_at_once_and_the_device_value_returns_after_the_lock(
        self,
        hass: HomeAssistant,
        no_readback_timers: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """B2.2: set 23, hold it through the lock, then follow the device (5)."""
        coordinator, entry, mqtt = _setup_coordinator(hass)
        _push(coordinator, reserve=5, ratio=80)
        await hass.async_block_till_done()
        created = await _create_numbers(hass, coordinator, entry)
        assert len(created) == 1
        entity: EcoFlowNumber = created[0]
        shown = _wire(entity, coordinator)
        # The first frame after the entity is wired writes its state once; the
        # snapshot the write asks for then repeats the same 5 and is deduplicated.
        _push(coordinator, reserve=5, ratio=80)
        await hass.async_block_till_done()
        assert shown == [5]
        shown.clear()
        clock = _Clock()
        monkeypatch.setattr(number_module, "time", clock)
        _answer_get_all_with(coordinator, mqtt, reserve=5, ratio=80)

        await entity.async_set_native_value(23.0)
        await hass.async_block_till_done()

        assert shown == [23]
        mqtt.send_proto_set.assert_called_once()

        # A frame carrying the old value inside the lock must not reach the state.
        clock.now += 4.0
        _push(coordinator, reserve=5, ratio=80)
        await hass.async_block_till_done()
        assert shown == [23]

        # After the lock the next device frame wins.
        clock.now += 2.0
        _push(coordinator, reserve=5, ratio=80)
        await hass.async_block_till_done()
        assert shown == [23, 5]


class TestRefusalShown:
    async def test_a_refusal_inside_the_lock_shows_the_previous_value(
        self,
        hass: HomeAssistant,
        no_readback_timers: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The refused value must not stay on screen for the rest of the lock."""
        coordinator, entry, mqtt = _setup_coordinator(hass)
        _push(coordinator, reserve=5, ratio=80)
        await hass.async_block_till_done()
        created = await _create_numbers(hass, coordinator, entry)
        entity: EcoFlowNumber = created[0]
        shown = _wire(entity, coordinator)
        _push(coordinator, reserve=5, ratio=80)
        await hass.async_block_till_done()
        shown.clear()
        clock = _Clock()
        monkeypatch.setattr(number_module, "time", clock)
        _answer_get_all_with(coordinator, mqtt, reserve=5, ratio=80)

        await entity.async_set_native_value(23.0)
        await hass.async_block_till_done()
        assert shown == [23]

        clock.now += 1.0  # well inside the 5 s lock
        _refuse(coordinator)
        await hass.async_block_till_done()

        assert coordinator.data["backup_reserve_pct"] == 5
        assert shown == [23, 5]

    async def test_a_refusal_that_arrives_before_the_write_returns_is_never_shown(
        self, hass: HomeAssistant, no_readback_timers: Any
    ) -> None:
        """The reply can beat the coroutine that published the write.

        It lands while the publish is still awaited, so the restore has run
        before the value is seeded. The write then raises instead of seeding
        and showing what the device just refused.
        """
        coordinator, entry, mqtt = _setup_coordinator(hass)
        _push(coordinator, reserve=5, ratio=80)
        await hass.async_block_till_done()
        created = await _create_numbers(hass, coordinator, entry)
        entity: EcoFlowNumber = created[0]
        shown = _wire(entity, coordinator)
        _push(coordinator, reserve=5, ratio=80)
        await hass.async_block_till_done()
        shown.clear()
        _answer_get_all_with(coordinator, mqtt, reserve=5, ratio=80)

        def _publish_and_refuse(*args: Any, **kwargs: Any) -> bool:
            _refuse(coordinator)
            # The refusal is queued on the loop. Return only after the loop
            # has run it, so it beats the coroutine awaiting this publish on
            # every runner, not just where the queue order happens to favour it.
            handled = threading.Event()
            hass.loop.call_soon_threadsafe(handled.set)
            assert handled.wait(5)
            return True

        # A plain function, not a Mock: the harness runs a Mock target inline
        # on the loop thread, where the refusal could not overtake the
        # coroutine that is awaiting the publish.
        mqtt.send_proto_set = _publish_and_refuse

        with pytest.raises(HomeAssistantError) as refused:
            await entity.async_set_native_value(23.0)
        await hass.async_block_till_done()

        assert refused.value.translation_key == "set_value_rejected"
        assert coordinator.data["backup_reserve_pct"] == 5
        assert shown == []
        assert coordinator._ocean2_writes.get("backup_reserve_pct") is None


class TestBounds:
    @pytest.mark.parametrize(
        ("serial", "ratio", "expected"),
        [
            # f92 known: the floor is the model's own row.
            ("RE11TEST00000001", 80, (1, 80)),
            ("RE41TEST00000001", 80, (0, 80)),
            # f92 unknown: the RE11 keeps the declared 1..100, the RE41 starts at 0.
            ("RE11TEST00000001", None, (1, 100)),
            ("RE41TEST00000001", None, (0, 100)),
        ],
    )
    async def test_the_floor_comes_from_the_models_row(
        self,
        hass: HomeAssistant,
        serial: str,
        ratio: int | None,
        expected: tuple[int, int],
    ) -> None:
        coordinator, entry, _ = _setup_coordinator(hass, serial)
        _push(coordinator, reserve=5, ratio=ratio)
        await hass.async_block_till_done()
        created = await _create_numbers(hass, coordinator, entry)
        entity: EcoFlowNumber = created[0]

        assert (entity.native_min_value, entity.native_max_value) == expected
        # The declared minimum is the RE11's and does not move.
        assert OCEAN2_NUMBERS[0].min_value == 1

    @pytest.mark.parametrize(
        ("ratio", "expected_max"),
        [
            (80, 80),
            (100, 100),
            # Unknown 92: the declared range holds (the write is refused then).
            (None, 100),
            # A ratio under the floor would invert the slider: declared range.
            (0, 100),
        ],
    )
    async def test_the_ceiling_follows_field_92(
        self, hass: HomeAssistant, ratio: int | None, expected_max: int
    ) -> None:
        """B2.3: the slider tops out at the device's own SoC ratio."""
        coordinator, entry, _ = _setup_coordinator(hass)
        _push(coordinator, reserve=5, ratio=ratio)
        await hass.async_block_till_done()
        created = await _create_numbers(hass, coordinator, entry)
        entity: EcoFlowNumber = created[0]

        assert entity.native_min_value == 1
        assert entity.native_max_value == expected_max

    async def test_a_moved_ratio_moves_the_published_range(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, entry, _ = _setup_coordinator(hass)
        _push(coordinator, reserve=5, ratio=80)
        await hass.async_block_till_done()
        created = await _create_numbers(hass, coordinator, entry)
        entity: EcoFlowNumber = created[0]
        shown = _wire(entity, coordinator)

        _push(coordinator, reserve=5, ratio=60)
        await hass.async_block_till_done()

        assert entity.native_max_value == 60
        # The value did not move, the range did: the state is rewritten anyway.
        assert shown == [5]

    async def test_a_set_above_the_ceiling_is_refused_and_nothing_is_sent(
        self, hass: HomeAssistant, no_readback_timers: Any
    ) -> None:
        coordinator, entry, mqtt = _setup_coordinator(hass)
        _push(coordinator, reserve=5, ratio=80)
        await hass.async_block_till_done()
        created = await _create_numbers(hass, coordinator, entry)
        entity: EcoFlowNumber = created[0]
        shown = _wire(entity, coordinator)
        _push(coordinator, reserve=5, ratio=80)  # first write of a wired entity
        await hass.async_block_till_done()
        shown.clear()
        _answer_get_all_with(coordinator, mqtt, reserve=5, ratio=80)

        with pytest.raises(HomeAssistantError) as refused:
            await entity.async_set_native_value(81.0)

        assert refused.value.translation_key == "set_value_rejected"
        mqtt.send_proto_set.assert_not_called()
        assert shown == []
        assert coordinator.data["backup_reserve_pct"] == 5

        # Control: the ceiling itself is allowed, so the refusal above is the
        # bound and not a write path that is shut.
        await entity.async_set_native_value(80.0)
        mqtt.send_proto_set.assert_called_once()
        assert shown == [80]


class TestDispatch:
    @pytest.mark.parametrize(
        ("outcome", "translation_key"),
        [
            (DeviceValueNotReported("Ocean 2 backup reserve"), "set_command_not_ready"),
            (ValueError("above the device's own reading"), "set_value_rejected"),
            (False, "set_command_failed"),
        ],
    )
    async def test_each_coordinator_outcome_maps_to_its_own_error(
        self,
        hass: HomeAssistant,
        outcome: Exception | bool,
        translation_key: str,
    ) -> None:
        """B2.4: not reported, rejected and failed are three different messages."""
        coordinator, entry, _ = _setup_coordinator(hass)
        _push(coordinator, reserve=5, ratio=80)
        await hass.async_block_till_done()
        created = await _create_numbers(hass, coordinator, entry)
        entity: EcoFlowNumber = created[0]
        shown = _wire(entity, coordinator)
        if isinstance(outcome, Exception):
            coordinator.async_set_ocean2_backup_reserve = AsyncMock(  # type: ignore[method-assign]
                side_effect=outcome
            )
        else:
            coordinator.async_set_ocean2_backup_reserve = AsyncMock(  # type: ignore[method-assign]
                return_value=outcome
            )

        with pytest.raises(HomeAssistantError) as raised:
            await entity.async_set_native_value(23.0)

        assert raised.value.translation_key == translation_key
        # A refused or failed write must not leave the requested value showing.
        assert shown == []
        coordinator.async_set_ocean2_backup_reserve.assert_awaited_once_with(23)

    async def test_a_confirmed_publish_applies_the_value(
        self, hass: HomeAssistant
    ) -> None:
        """Control for the three above: True is the path that does show it."""
        coordinator, entry, _ = _setup_coordinator(hass)
        _push(coordinator, reserve=5, ratio=80)
        await hass.async_block_till_done()
        created = await _create_numbers(hass, coordinator, entry)
        entity: EcoFlowNumber = created[0]
        shown = _wire(entity, coordinator)
        coordinator.async_set_ocean2_backup_reserve = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )

        await entity.async_set_native_value(23.0)

        assert shown == [23]


class TestGating:
    def test_only_the_re11_re41_and_re43_get_the_number(self) -> None:
        """B2.5: the write is confirmed on three variants, not on the family.

        The RE43's rests on the app's own write alone; a model with
        no write on record, RE17 and RE42, gets nothing.
        """
        for confirmed in ("RE11TEST00000001", "RE41TEST00000001", "RE43TEST00000001"):
            assert _get_number_defs(DEVICE_TYPE_OCEAN2, confirmed) == OCEAN2_NUMBERS
        assert [d.key for d in OCEAN2_NUMBERS] == ["backup_reserve"]
        for serial in (
            "RE17TEST00000001",
            "RE42TEST00000001",
            "",
        ):
            assert _get_number_defs(DEVICE_TYPE_OCEAN2, serial) == [], serial

    def test_the_empty_serial_guard_holds_on_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard must not lean on the prefix set happening to exclude "".

        With the real table, `""[:4]` is never a key, so the guard is only
        visible when a table that contains the empty string is put under it.
        """
        row = const_module.Ocean2Controls(reserve_floor=1, storm_param_full=False)
        monkeypatch.setattr(const_module, "OCEAN2_CONTROLS", {"RE11": row, "": row})

        assert const_module.supports_ocean2_reserve("RE11TEST00000001") is True
        assert const_module.supports_ocean2_reserve("") is False
        assert const_module.supports_ocean2_storm_guard("RE11TEST00000001") is True
        assert const_module.supports_ocean2_storm_guard("") is False

    async def test_a_gated_variant_creates_no_number_even_when_it_reports_field_10(
        self, hass: HomeAssistant
    ) -> None:
        """The accessory gate is not a substitute for the prefix gate."""
        coordinator, entry, _ = _setup_coordinator(hass, "RE17TEST00000001")
        _push(coordinator, reserve=5, ratio=80)
        await hass.async_block_till_done()
        # The frame was ingested: the reading is in the store, the number is not.
        assert coordinator.data["backup_reserve_pct"] == 5

        created = await _create_numbers(hass, coordinator, entry)
        _push(coordinator, reserve=5, ratio=80)
        await hass.async_block_till_done()

        assert created == []
