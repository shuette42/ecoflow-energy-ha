"""Entities of the writable Local Modbus mode (PowerOcean).

One switch (Modbus control), two numbers (Backup reserve, Indicator brightness)
and one diagnostic binary sensor (Modbus control active), created only for a
Local entry. The register client is the stub of the coordinator tests, so what
is covered is what the entities do with the coordinator: which entities exist,
what they render, and what a service call reaches the device as.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.ecoflow_energy.binary_sensor import (
    async_setup_entry as binary_sensor_setup,
)
from custom_components.ecoflow_energy.const import (
    DOMAIN,
    LOCAL_MODBUS_FAILURES_UNAVAILABLE,
    POWEROCEAN_LOCAL_KEYS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.coordinator.local_modbus import (
    EcoFlowLocalModbusCoordinator,
)
from custom_components.ecoflow_energy.ecoflow.modbus_local import (
    BACKUP_RATIO_OFFSET,
    BRIGHTNESS_OFFSET,
    HEARTBEAT_OFFSET,
    ModbusTimeoutError,
)
from custom_components.ecoflow_energy.number import async_setup_entry as number_setup
from custom_components.ecoflow_energy.switch import async_setup_entry as switch_setup

from .conftest import add_entities_collector
from .test_local_modbus_coordinator import (
    DAY,
    NEW_KEYS,
    SERIAL,
    StubClient,
    _encode,
    _frame,
    _local_entry,
)
from .test_powerglow_gating import POWEROCEAN_DEVICE, _entry

CONTROL_KEYS = (
    "modbus_control",
    "local_backup_reserve",
    "local_indicator_brightness",
    "modbus_control_active",
)
# Bit 11 of the system status word.
CONTROL_ACTIVE_STATUS = 1 << 11
# Bits 4-6 set (the device shows 7 there while Backup Reserve is above 0). Not a
# control flag, and not read as one.
MODE_BITS_STATUS = 0x7 << 4
POLLED_BRIGHTNESS = 60

POWEROCEAN_DOC = (
    Path(__file__).resolve().parents[2] / "documentation" / "entities" / "powerocean.md"
)


def _poll_frame(
    *, brightness: int = POLLED_BRIGHTNESS, status: int | None = 0
) -> dict[int, bytes]:
    """One full answer with the brightness and the system status word set.

    ``status=None`` leaves the block that carries the status word out, so the
    poll does not deliver the key at all.
    """
    frame = _frame(DAY)
    block = bytearray(frame[0x0217])
    low = (0x021C - 0x0217) * 2
    block[low : low + 2] = struct.pack(">H", brightness)
    frame[0x0217] = bytes(block)
    if status is None:
        del frame[0x0206]
    else:
        block = bytearray(frame[0x0206])
        low = (0x0211 - 0x0206) * 2
        block[low : low + 4] = _encode("u32", status)
        frame[0x0206] = bytes(block)
    return frame


async def _setup_local(
    hass: HomeAssistant,
    frame: dict[int, bytes] | None = None,
    entry: MockConfigEntry | None = None,
) -> tuple[MockConfigEntry, StubClient, EcoFlowLocalModbusCoordinator]:
    """Set a local entry up on a stub client whose first poll is ``frame``."""
    stub = StubClient([frame if frame is not None else _poll_frame()])
    entry = entry if entry is not None else _local_entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.ecoflow_energy.coordinator.local_modbus.create_link",
        return_value=stub,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    coordinator = hass.data[DOMAIN][entry.entry_id][SERIAL]
    return entry, stub, coordinator


def _entity_id(hass: HomeAssistant, domain: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, f"{SERIAL}_{key}"
    )
    assert entity_id is not None, f"no {domain} entity for {key}"
    return entity_id


def _state(hass: HomeAssistant, domain: str, key: str) -> str:
    state = hass.states.get(_entity_id(hass, domain, key))
    assert state is not None
    return state.state


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _call(
    hass: HomeAssistant, domain: str, service: str, key: str, **data: Any
) -> None:
    await hass.services.async_call(
        domain,
        service,
        {"entity_id": _entity_id(hass, domain, key), **data},
        blocking=True,
    )


async def test_a_cloud_entry_gets_none_of_the_four_and_a_local_entry_gets_them(
    hass: HomeAssistant,
) -> None:
    """The four entities exist for a Local coordinator and for no cloud one."""
    cloud_entry = _entry()
    cloud_entry.add_to_hass(hass)
    cloud = EcoFlowDeviceCoordinator(hass, cloud_entry, POWEROCEAN_DEVICE)
    hass.data.setdefault(DOMAIN, {})[cloud_entry.entry_id] = {
        POWEROCEAN_DEVICE["sn"]: cloud
    }

    created: list[Any] = []
    for platform_setup in (switch_setup, number_setup, binary_sensor_setup):
        await platform_setup(hass, cloud_entry, add_entities_collector(created))
    cloud_ids = {entity.unique_id for entity in created}
    cloud_sn = POWEROCEAN_DEVICE["sn"]
    # The cloud platforms did run: their own controls are there.
    assert f"{cloud_sn}_backup_reserve" in cloud_ids
    assert not {f"{cloud_sn}_{key}" for key in CONTROL_KEYS} & cloud_ids

    # Control: the same three platform setups hand a Local coordinator the four.
    entry, _stub, _coordinator = await _setup_local(hass)
    local: list[Any] = []
    for platform_setup in (switch_setup, number_setup, binary_sensor_setup):
        await platform_setup(hass, entry, add_entities_collector(local))
    assert {entity.unique_id for entity in local} == {
        f"{SERIAL}_{key}" for key in CONTROL_KEYS
    }
    await _unload(hass, entry)


async def test_a_local_entry_creates_the_four_plus_the_existing_sensors(
    hass: HomeAssistant,
) -> None:
    """Exactly the sensors of the read-only mode plus the four, in the right place."""
    entry, _stub, _coordinator = await _setup_local(hass)
    registry = er.async_get(hass)
    registered = er.async_entries_for_config_entry(registry, entry.entry_id)

    assert {item.unique_id for item in registered} == {
        f"{SERIAL}_{key}"
        for key in POWEROCEAN_LOCAL_KEYS | NEW_KEYS | set(CONTROL_KEYS)
    } | {f"{SERIAL}_connection_mode"}
    assert {item.domain for item in registered} == {
        "sensor",
        "switch",
        "number",
        "binary_sensor",
    }

    by_key = {item.unique_id.removeprefix(f"{SERIAL}_"): item for item in registered}
    assert by_key["modbus_control"].domain == "switch"
    assert by_key["local_backup_reserve"].domain == "number"
    assert by_key["local_indicator_brightness"].domain == "number"
    assert by_key["modbus_control_active"].domain == "binary_sensor"
    assert by_key["modbus_control_active"].entity_category == "diagnostic"
    assert by_key["local_indicator_brightness"].entity_category == "config"
    assert by_key["local_backup_reserve"].entity_category is None
    # The sensor that shows the same register keeps its own entity.
    assert by_key["ems_backup_ratio_pct"].domain == "sensor"
    assert by_key["ems_backup_ratio_pct"].unique_id != (
        by_key["local_backup_reserve"].unique_id
    )
    await _unload(hass, entry)


async def test_the_switch_calls_the_coordinator_and_surfaces_its_error(
    hass: HomeAssistant,
) -> None:
    """On writes one beat and shows on; a refused beat raises and stays off."""
    entry, stub, coordinator = await _setup_local(hass)
    assert _state(hass, "switch", "modbus_control") == "off"

    stub.write_errors = [ModbusTimeoutError("no answer")]
    with pytest.raises(HomeAssistantError):
        await _call(hass, "switch", "turn_on", "modbus_control")
    assert coordinator.control_enabled is False
    assert _state(hass, "switch", "modbus_control") == "off"
    assert stub.writes == [(HEARTBEAT_OFFSET, 1)]

    # Control: the same call without the error takes control.
    await _call(hass, "switch", "turn_on", "modbus_control")
    assert coordinator.control_enabled is True
    assert _state(hass, "switch", "modbus_control") == "on"
    assert stub.writes == [(HEARTBEAT_OFFSET, 1)] * 2

    await _call(hass, "switch", "turn_off", "modbus_control")
    assert coordinator.control_enabled is False
    assert _state(hass, "switch", "modbus_control") == "off"
    # Switching off writes nothing: the unit hands control back by itself.
    assert stub.writes == [(HEARTBEAT_OFFSET, 1)] * 2
    await _unload(hass, entry)


async def test_a_switch_that_is_on_stays_available_so_that_off_stays_reachable(
    hass: HomeAssistant,
) -> None:
    """Home Assistant skips an unavailable entity in a service call: not this one."""
    entry, stub, coordinator = await _setup_local(hass)
    stub.queue(*[ModbusTimeoutError("no answer")] * LOCAL_MODBUS_FAILURES_UNAVAILABLE)
    await _call(hass, "switch", "turn_on", "modbus_control")
    for _ in range(LOCAL_MODBUS_FAILURES_UNAVAILABLE):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.device_available is False
    assert _state(hass, "switch", "modbus_control") == "on"

    await _call(hass, "switch", "turn_off", "modbus_control")

    assert coordinator.control_enabled is False
    # With control off the switch follows the device again.
    assert _state(hass, "switch", "modbus_control") == "unavailable"
    await _unload(hass, entry)


async def test_the_switch_is_off_after_a_restart_even_if_it_was_on(
    hass: HomeAssistant,
) -> None:
    """No restore: control is off on every entry start."""
    entry, _stub, coordinator = await _setup_local(hass)
    await _call(hass, "switch", "turn_on", "modbus_control")
    assert _state(hass, "switch", "modbus_control") == "on"
    entity_id = _entity_id(hass, "switch", "modbus_control")
    await _unload(hass, entry)

    # The restore cache says "on", as it would after a restart during control.
    mock_restore_cache(hass, [State(entity_id, "on")])
    stub = StubClient([_poll_frame()])
    with patch(
        "custom_components.ecoflow_energy.coordinator.local_modbus.create_link",
        return_value=stub,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    fresh = hass.data[DOMAIN][entry.entry_id][SERIAL]
    assert fresh is not coordinator
    assert fresh.control_enabled is False
    assert _state(hass, "switch", "modbus_control") == "off"
    assert stub.writes == []
    await _unload(hass, entry)


async def test_the_numbers_show_the_polled_value(hass: HomeAssistant) -> None:
    """Both numbers render what the poll delivered, under their own units."""
    entry, _stub, _coordinator = await _setup_local(hass)
    assert float(_state(hass, "number", "local_backup_reserve")) == DAY["backup_ratio"]
    assert float(_state(hass, "number", "local_indicator_brightness")) == (
        POLLED_BRIGHTNESS
    )
    for key in ("local_backup_reserve", "local_indicator_brightness"):
        attributes = hass.states.get(_entity_id(hass, "number", key)).attributes  # type: ignore[union-attr]
        assert attributes["unit_of_measurement"] == "%"
        assert (attributes["min"], attributes["max"], attributes["step"]) == (
            0,
            100,
            1,
        )
    await _unload(hass, entry)


async def test_the_numbers_write_through_the_coordinator_without_the_switch(
    hass: HomeAssistant,
) -> None:
    """A write reaches its own register and shows the confirmed value, control off."""
    entry, stub, coordinator = await _setup_local(hass)
    assert coordinator.control_enabled is False

    await _call(hass, "number", "set_value", "local_backup_reserve", value=40)
    assert stub.writes == [(BACKUP_RATIO_OFFSET, 40)]
    assert float(_state(hass, "number", "local_backup_reserve")) == 40

    await _call(hass, "number", "set_value", "local_indicator_brightness", value=25)
    assert stub.writes == [(BACKUP_RATIO_OFFSET, 40), (BRIGHTNESS_OFFSET, 25)]
    assert float(_state(hass, "number", "local_indicator_brightness")) == 25

    # No heartbeat was involved and none was started.
    assert HEARTBEAT_OFFSET not in {offset for offset, _value in stub.writes}
    assert coordinator.control_enabled is False
    await _unload(hass, entry)


async def test_a_write_the_device_does_not_hold_shows_an_error_and_what_it_holds(
    hass: HomeAssistant,
) -> None:
    """A mismatching read-back raises and puts the value the device holds on display."""
    entry, stub, _coordinator = await _setup_local(hass)
    assert DAY["backup_ratio"] != 99
    stub.read_back_override = 99
    with pytest.raises(HomeAssistantError):
        await _call(hass, "number", "set_value", "local_backup_reserve", value=40)
    assert float(_state(hass, "number", "local_backup_reserve")) == 99

    # Control: once the device holds what was written, the same call shows it.
    stub.read_back_override = None
    await _call(hass, "number", "set_value", "local_backup_reserve", value=40)
    assert float(_state(hass, "number", "local_backup_reserve")) == 40
    await _unload(hass, entry)


@pytest.mark.parametrize("bad", [101, -1, 50.5])
async def test_a_number_refuses_a_value_outside_zero_to_one_hundred_or_fractional(
    hass: HomeAssistant, bad: float
) -> None:
    """Nothing reaches the device for 101, -1 or 50.5; the edges 0 and 100 pass."""
    entry, stub, _coordinator = await _setup_local(hass)
    with pytest.raises(HomeAssistantError):
        await _call(hass, "number", "set_value", "local_backup_reserve", value=bad)
    assert stub.writes == []

    for edge in (0, 100):
        await _call(hass, "number", "set_value", "local_backup_reserve", value=edge)
    assert stub.writes == [(BACKUP_RATIO_OFFSET, 0), (BACKUP_RATIO_OFFSET, 100)]
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (CONTROL_ACTIVE_STATUS, "on"),
        (0, "off"),
        # Other bits of the word are not a control flag.
        (MODE_BITS_STATUS, "off"),
        (CONTROL_ACTIVE_STATUS | MODE_BITS_STATUS, "on"),
        # Never off for a word the poll did not deliver.
        (None, "unknown"),
    ],
)
async def test_the_binary_sensor_follows_bit_11_and_is_unknown_without_it(
    hass: HomeAssistant, status: int | None, expected: str
) -> None:
    """True or False from the data key, unknown while the key is absent."""
    entry, _stub, coordinator = await _setup_local(hass, _poll_frame(status=status))
    assert ("modbus_control_active" in coordinator.data) is (status is not None)
    assert _state(hass, "binary_sensor", "modbus_control_active") == expected
    await _unload(hass, entry)


def test_the_documentation_lists_the_four_entities_under_the_local_controls() -> None:
    """Each of the four has its own row in the Local controls section.

    "Backup reserve" shares its name with the Enhanced Mode number, so the
    generic documentation gate cannot tell a missing local row from the existing
    one; this reads the section itself.
    """
    text = POWEROCEAN_DOC.read_text(encoding="utf-8")
    heading = "## Controls - Local Modbus Only"
    assert heading in text
    section = text.split(heading, 1)[1].split("\n---", 1)[0]
    rows = {
        cells[0]: cells[1]
        for cells in (
            [cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in section.splitlines()
            if line.startswith("|")
        )
        if len(cells) > 1
    }
    assert rows["Modbus control"] == "Switch"
    assert rows["Backup reserve"] == "Number"
    assert rows["Indicator brightness"] == "Number"
    assert rows["Modbus control active"] == "Binary sensor"
    assert "locked" in section
    assert "60 seconds" in section
