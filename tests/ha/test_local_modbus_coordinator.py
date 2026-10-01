"""Local Modbus coordinator and the entities built on it (read-only PowerOcean).

The client is stubbed with decoded register frames, so these tests cover what
the coordinator and the sensor platform do with a poll: availability after
repeated failures, which entities a local entry creates, how a value the poll
did not deliver renders, and that a lifetime counter never moves backwards.
"""

from __future__ import annotations

import json
import logging
import struct
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    CONF_DEVICES,
    CONF_MODE,
    CONF_UNIT_ID,
    DEVICE_TYPE_POWEROCEAN,
    DOMAIN,
    LOCAL_MODBUS_FAILURES_UNAVAILABLE,
    MODE_LOCAL,
    POWEROCEAN_LOCAL_KEYS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.coordinator.local_modbus import (
    EcoFlowLocalModbusCoordinator,
)
from custom_components.ecoflow_energy.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ecoflow_energy.ecoflow.modbus_local import (
    ModbusConnectError,
    ModbusTimeoutError,
)
from custom_components.ecoflow_energy.ecoflow.parsers.powerocean_modbus import (
    POLL_BLOCKS,
)
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

from .conftest import add_entities_collector
from .test_powerglow_gating import POWEROCEAN_DEVICE, _entry

_FIXTURE = json.loads(
    (
        Path(__file__).parent.parent / "fixtures" / "powerocean_modbus_frames.json"
    ).read_text()
)
DEVICE = _FIXTURE["device"]
DAY = _FIXTURE["samples"]["day"]
NIGHT = _FIXTURE["samples"]["night"]
SERIAL = DEVICE["serial"]

DEVICE_DICT: dict[str, Any] = {
    "sn": SERIAL,
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}

NEW_KEYS = {
    "solar_lifetime_energy_kwh",
    "grid_import_lifetime_energy_kwh",
    "grid_export_lifetime_energy_kwh",
    "fault_count",
}
# Integrated energy keys of the cloud entry. Local mode publishes none of them.
INTEGRATED_KEYS = {
    "solar_energy_kwh",
    "home_energy_kwh",
    "grid_import_energy_kwh",
    "grid_export_energy_kwh",
}

# Fixture field -> (register offset, wire type), written out by hand from the
# protocol table so the encoding does not borrow the parser's own map.
_REGISTERS: dict[str, tuple[int, str]] = {
    "load_w": (0x0206, "f32"),
    "grid_w": (0x0208, "f32"),
    "solar_w": (0x020A, "f32"),
    "battery_w": (0x020C, "f32"),
    "soc": (0x020E, "u16"),
    "backup_ratio": (0x0217, "u16"),
    "battery_capacity_wh": (0x0227, "u32"),
    "inv_freq": (0x0251, "f32"),
    "pv1_voltage": (0x0253, "f32"),
    "pv2_voltage": (0x0255, "f32"),
    "pv3_voltage": (0x0257, "f32"),
    "pv1_current": (0x0259, "f32"),
    "pv2_current": (0x025B, "f32"),
    "pv3_current": (0x025D, "f32"),
    "fault_count": (0x0800, "u16"),
    "batteries_online": (0x0820, "u16"),
    "battery_1_soc": (0x0821, "u16"),
    "battery_2_soc": (0x0822, "u16"),
    "battery_3_soc": (0x0823, "u16"),
    "grid_draw_total_kwh": (0x0870, "f32"),
    "grid_feed_total_kwh": (0x0880, "f32"),
    "bat_charge_total_kwh": (0x08B0, "f32"),
    "bat_discharge_total_kwh": (0x08C0, "f32"),
    "solar_total_kwh": (0x08D0, "f32"),
}


def _encode(kind: str, value: float) -> bytes:
    """Encode a decoded value the way the device sends it: word-swapped."""
    if kind == "u16":
        return struct.pack(">H", int(value))
    raw = struct.pack(">I", int(value)) if kind == "u32" else struct.pack(">f", value)
    return raw[2:4] + raw[0:2]


def _frame(sample: dict[str, Any], **overrides: float) -> dict[int, bytes]:
    """Register bytes of one full answer: poll blocks plus the identity blocks."""
    values = {**sample, **overrides}
    blocks = {start: bytearray(2 * count) for start, count in POLL_BLOCKS}
    for name, value in values.items():
        offset, kind = _REGISTERS[name]
        data = _encode(kind, value)
        for start, count in POLL_BLOCKS:
            if start <= offset and offset + len(data) // 2 <= start + count:
                low = (offset - start) * 2
                blocks[start][low : low + len(data)] = data
    answer = {start: bytes(raw) for start, raw in blocks.items()}
    answer[0x0000] = struct.pack(
        ">HHH",
        DEVICE["protocol_version"],
        DEVICE["product_category"],
        DEVICE["product_number"],
    )
    answer[0x0003] = SERIAL.encode("ascii")
    answer[0x000B] = bytes(DEVICE["firmware"])
    return answer


class StubClient:
    """Answers each poll from a script: a frame, or an error to raise."""

    def __init__(self, script: list[dict[int, bytes] | Exception]) -> None:
        self._script = list(script)
        self.calls = 0

    async def read_blocks(self, blocks: Sequence[tuple[int, int]]) -> dict[int, bytes]:
        self.calls += 1
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _local_entry() -> MockConfigEntry:
    # Version 3 is the current config entry version: a local entry is created
    # by the flow at that version and must never pass through the v1 -> v3
    # migration, which would add an `auth_method` key to credential-free data.
    return MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            CONF_MODE: MODE_LOCAL,
            CONF_HOST: "modbus.example.test",
            CONF_PORT: 502,
            CONF_UNIT_ID: 1,
            CONF_DEVICES: [DEVICE_DICT],
        },
    )


def _coordinator(
    hass: HomeAssistant, entry: MockConfigEntry, script: list[Any]
) -> EcoFlowLocalModbusCoordinator:
    entry.add_to_hass(hass)
    return EcoFlowLocalModbusCoordinator(
        hass, entry, DEVICE_DICT, client=StubClient(script)
    )


async def test_failures_keep_data_until_the_third_then_one_success_restores(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """B1: no flicker before the third failure, one WARNING, one INFO, no reauth."""
    assert LOCAL_MODBUS_FAILURES_UNAVAILABLE == 3
    caplog.set_level(
        logging.DEBUG, logger="custom_components.ecoflow_energy.coordinator"
    )
    timeout = ModbusTimeoutError("no answer")
    entry = _local_entry()
    coordinator = _coordinator(
        hass,
        entry,
        [_frame(DAY), timeout, timeout, timeout, timeout, _frame(DAY)],
    )

    def _noisy() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.INFO and "Local Modbus" in record.getMessage()
        ]

    with patch.object(entry, "async_start_reauth") as reauth:
        await coordinator.async_refresh()
        assert coordinator.data["soc_pct"] == 100
        assert coordinator.device_available is True
        assert _noisy() == []

        for failures in (1, 2):
            await coordinator.async_refresh()
            assert coordinator.consecutive_failures == failures
            assert coordinator.device_available is True
            assert coordinator.data["soc_pct"] == 100
            assert _noisy() == []

        await coordinator.async_refresh()
        assert coordinator.consecutive_failures == 3
        assert coordinator.device_available is False
        assert coordinator.data["soc_pct"] == 100
        assert len(_noisy()) == 1
        assert "Modbus is enabled" in _noisy()[0]
        assert "single connection" in _noisy()[0]

        await coordinator.async_refresh()
        assert coordinator.device_available is False
        assert len(_noisy()) == 1

        await coordinator.async_refresh()
        assert coordinator.device_available is True
        assert coordinator.consecutive_failures == 0
        assert len(_noisy()) == 2
        assert "answering again" in _noisy()[1]

    reauth.assert_not_called()


async def test_local_entry_creates_the_local_entities_and_a_missing_value_is_unknown(
    hass: HomeAssistant,
) -> None:
    """B2: exactly the shared keys plus four new ones, none of the integrated keys."""
    # Day frame with no battery online: the pack rule withholds every packN_soc,
    # so an enabled entity has no value in the poll.
    stub = StubClient([_frame(DAY, batteries_online=0)])
    entry = _local_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ecoflow_energy.coordinator.local_modbus.ModbusLocalClient",
        return_value=stub,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    registered = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert {item.platform for item in registered} == {DOMAIN}
    assert {item.domain for item in registered} == {"sensor"}
    unique_ids = {item.unique_id for item in registered}
    expected = {f"{SERIAL}_{key}" for key in POWEROCEAN_LOCAL_KEYS | NEW_KEYS}
    expected.add(f"{SERIAL}_connection_mode")
    assert unique_ids == expected
    assert f"{SERIAL}_mqtt_status" not in unique_ids
    assert not {f"{SERIAL}_{key}" for key in INTEGRATED_KEYS} & unique_ids

    def _state(key: str) -> str | None:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_{key}")
        assert entity_id is not None, key
        state = hass.states.get(entity_id)
        return None if state is None else state.state

    # Never 0 for a value the device did not deliver.
    assert _state("pack1_soc") == "unknown"
    assert _state("soc_pct") == "100"
    assert float(_state("solar_lifetime_energy_kwh") or "nan") == pytest.approx(
        DAY["solar_total_kwh"], abs=0.01
    )
    # The diagnostic sensor is disabled by default; the coordinator carries it.
    assert hass.data[DOMAIN][entry.entry_id][SERIAL].connection_mode == "local"

    # Negative control: a cloud PowerOcean builds the integrated keys and none
    # of the four local ones, so the key assertions above can tell them apart.
    cloud_entry = _entry()
    cloud_entry.add_to_hass(hass)
    cloud = EcoFlowDeviceCoordinator(hass, cloud_entry, POWEROCEAN_DEVICE)
    hass.data[DOMAIN][cloud_entry.entry_id] = {POWEROCEAN_DEVICE["sn"]: cloud}
    created: list[Any] = []
    await sensor_setup(hass, cloud_entry, add_entities_collector(created))
    cloud_ids = {entity.unique_id for entity in created}
    cloud_sn = POWEROCEAN_DEVICE["sn"]
    assert {f"{cloud_sn}_{key}" for key in INTEGRATED_KEYS} <= cloud_ids
    assert not {f"{cloud_sn}_{key}" for key in NEW_KEYS} & cloud_ids
    assert f"{cloud_sn}_mqtt_status" in cloud_ids

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_a_lifetime_counter_never_moves_backwards(hass: HomeAssistant) -> None:
    """B3: a lower reading keeps the last value, a higher one passes."""
    counters = {
        "solar_lifetime_energy_kwh": ("solar_total_kwh", 21000.0),
        "grid_import_lifetime_energy_kwh": ("grid_draw_total_kwh", 8000.0),
        "grid_export_lifetime_energy_kwh": ("grid_feed_total_kwh", 10000.0),
        "batt_charge_energy_kwh": ("bat_charge_total_kwh", 6000.0),
        "batt_discharge_energy_kwh": ("bat_discharge_total_kwh", 6000.0),
    }
    lower = dict(counters.values())
    coordinator = _coordinator(
        hass,
        _local_entry(),
        [_frame(DAY), _frame(DAY, soc=40, **lower), _frame(NIGHT)],
    )

    await coordinator.async_refresh()
    for key, (field, _) in counters.items():
        assert coordinator.data[key] == pytest.approx(DAY[field], abs=1e-3), key

    await coordinator.async_refresh()
    for key, (field, _) in counters.items():
        assert coordinator.data[key] == pytest.approx(DAY[field], abs=1e-3), key
    # Only counters are held: an ordinary reading follows the device down.
    assert coordinator.data["soc_pct"] == 40

    await coordinator.async_refresh()
    for key, (field, _) in counters.items():
        assert coordinator.data[key] == pytest.approx(NIGHT[field], abs=1e-3), key
    assert coordinator.data["solar_lifetime_energy_kwh"] > DAY["solar_total_kwh"]


async def _set_up_local_entry(
    hass: HomeAssistant, entry: MockConfigEntry, script: list[Any]
) -> bool:
    """Set the entry up with the Modbus client replaced by a scripted stub."""
    entry.add_to_hass(hass)
    with patch(
        "custom_components.ecoflow_energy.coordinator.local_modbus.ModbusLocalClient",
        return_value=StubClient(script),
    ):
        result = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return result


async def test_an_unreachable_device_at_setup_is_retried_not_failed(
    hass: HomeAssistant,
) -> None:
    """A device that does not answer the first poll is a retry, never a reauth."""
    entry = _local_entry()

    assert not await _set_up_local_entry(
        hass, entry, [ModbusConnectError("connection refused")]
    )

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.entry_id not in hass.data.get(DOMAIN, {})
    assert not hass.config_entries.flow.async_progress()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_local_diagnostics_carry_the_link_and_the_data_without_the_serial(
    hass: HomeAssistant,
) -> None:
    """The local branch reports host, port, unit id, poll health and readings."""
    entry = _local_entry()
    assert await _set_up_local_entry(hass, entry, [_frame(DAY)])

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["config_entry"] == {
        "mode": MODE_LOCAL,
        "host": "modbus.example.test",
        "port": 502,
        "unit_id": 1,
        "device_count": 1,
    }
    (device,) = diagnostics["devices"]
    assert device["last_poll_ok"] is True
    assert device["consecutive_failures"] == 0
    assert device["device_data"]["soc_pct"] == 100
    assert SERIAL not in json.dumps(diagnostics)
    assert await hass.config_entries.async_unload(entry.entry_id)
