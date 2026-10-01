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
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.ecoflow_energy.const import (
    CONF_DEVICES,
    CONF_MODE,
    CONF_UNIT_ID,
    DEVICE_TYPE_POWEROCEAN,
    DOMAIN,
    LOCAL_MODBUS_FAILURES_UNAVAILABLE,
    MODE_LOCAL,
    POWEROCEAN_LOCAL_KEYS,
    POWEROCEAN_LOCAL_SENSOR_DEFS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.coordinator.local_modbus import (
    EcoFlowLocalModbusCoordinator,
)
from custom_components.ecoflow_energy.diagnostics import (
    REDACTED,
    async_get_config_entry_diagnostics,
)
from custom_components.ecoflow_energy.ecoflow.modbus_local import (
    ModbusConnectError,
    ModbusExceptionResponse,
    ModbusTimeoutError,
)
from custom_components.ecoflow_energy.ecoflow.parsers.powerocean_modbus import (
    LIFETIME_COUNTER_KEYS,
    POLL_BLOCKS,
    SETUP_BLOCKS,
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
        # The blocks each poll asked for, in order.
        self.requested: list[tuple[tuple[int, int], ...]] = []

    async def read_blocks(self, blocks: Sequence[tuple[int, int]]) -> dict[int, bytes]:
        self.calls += 1
        self.requested.append(tuple(blocks))
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


def _with_stub(
    hass: HomeAssistant, entry: MockConfigEntry, script: list[Any]
) -> tuple[EcoFlowLocalModbusCoordinator, StubClient]:
    entry.add_to_hass(hass)
    stub = StubClient(script)
    return EcoFlowLocalModbusCoordinator(hass, entry, DEVICE_DICT, client=stub), stub


def _coordinator(
    hass: HomeAssistant, entry: MockConfigEntry, script: list[Any]
) -> EcoFlowLocalModbusCoordinator:
    return _with_stub(hass, entry, script)[0]


# Same register values, but the identity block names another device.
OTHER_SERIAL = "HJ31DUMMY0000002"


def _frame_of_another_device(sample: dict[str, Any]) -> dict[int, bytes]:
    answer = _frame(sample)
    answer[0x0003] = OTHER_SERIAL.encode("ascii")
    return answer


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
    # A PV1 voltage the parser drops (not finite): an enabled entity whose key
    # the poll does not deliver.
    stub = StubClient([_frame(DAY, pv1_voltage=float("nan"))])
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
    assert _state("mppt_pv1_voltage_v") == "unknown"
    assert float(_state("mppt_pv2_voltage_v") or "nan") == pytest.approx(
        DAY["pv2_voltage"], abs=0.01
    )
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
    """The local branch reports the link (host redacted), poll health and readings."""
    entry = _local_entry()
    assert await _set_up_local_entry(hass, entry, [_frame(DAY)])
    # The readings hold no serial, so without help the final assertion could not
    # fail: plant the full serial in the data the diagnostics copy.
    coordinator = hass.data[DOMAIN][entry.entry_id][SERIAL]
    coordinator.device_data["identity_probe"] = SERIAL

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["config_entry"] == {
        "mode": MODE_LOCAL,
        "host": REDACTED,
        "port": 502,
        "unit_id": 1,
        "device_count": 1,
    }
    (device,) = diagnostics["devices"]
    assert device["last_poll_ok"] is True
    assert device["consecutive_failures"] == 0
    assert device["device_data"]["soc_pct"] == 100
    # Positive control: the planted value is in the output, but not as the serial.
    assert "identity_probe" in device["device_data"]
    assert device["device_data"]["identity_probe"] != SERIAL
    assert SERIAL not in json.dumps(diagnostics)
    # The host is the owner's own network address: nowhere in the download.
    assert "modbus.example.test" not in json.dumps(diagnostics)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_serial_is_read_on_the_first_poll_only_and_firmware_is_kept(
    hass: HomeAssistant,
) -> None:
    """The identity blocks ride along once; the device registry gets the firmware."""
    coordinator, stub = _with_stub(hass, _local_entry(), [_frame(DAY), _frame(DAY)])
    assert "sw_version" not in coordinator.device_info

    await coordinator.async_refresh()
    await coordinator.async_refresh()

    assert stub.requested == [SETUP_BLOCKS + POLL_BLOCKS, POLL_BLOCKS]
    assert coordinator.device_info["sw_version"] == "37.10.5.1"


async def test_the_serial_is_read_again_after_the_device_was_unavailable(
    hass: HomeAssistant,
) -> None:
    """Whatever answers after an outage is checked, not trusted."""
    timeout = ModbusTimeoutError("no answer")
    coordinator, stub = _with_stub(
        hass, _local_entry(), [_frame(DAY), timeout, timeout, timeout, _frame(DAY)]
    )

    for _ in range(5):
        await coordinator.async_refresh()

    assert stub.requested == [
        SETUP_BLOCKS + POLL_BLOCKS,
        POLL_BLOCKS,
        POLL_BLOCKS,
        POLL_BLOCKS,
        SETUP_BLOCKS + POLL_BLOCKS,
    ]


async def test_another_device_on_the_first_poll_is_a_failed_setup_not_data(
    hass: HomeAssistant,
) -> None:
    """A foreign serial at setup publishes nothing and names no full serial."""
    coordinator = _coordinator(hass, _local_entry(), [_frame_of_another_device(DAY)])

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert coordinator.device_data == {}
    assert coordinator.consecutive_failures == 1
    assert coordinator.last_error is not None
    assert "serial mismatch" in coordinator.last_error
    assert OTHER_SERIAL not in coordinator.last_error
    assert SERIAL not in coordinator.last_error


async def test_another_device_after_an_outage_is_one_warning_and_never_data(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The mismatch counts as a failure, warns once, and clears on a matching answer."""
    caplog.set_level(
        logging.DEBUG, logger="custom_components.ecoflow_energy.coordinator"
    )
    timeout = ModbusTimeoutError("no answer")
    coordinator = _coordinator(
        hass,
        _local_entry(),
        [
            _frame(DAY),
            timeout,
            timeout,
            timeout,
            _frame_of_another_device(NIGHT),
            _frame_of_another_device(NIGHT),
            _frame(NIGHT),
        ],
    )

    def _mismatch_warnings() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING
            and "serial mismatch" in record.getMessage()
        ]

    for _ in range(4):
        await coordinator.async_refresh()
    assert coordinator.device_available is False
    assert _mismatch_warnings() == []

    await coordinator.async_refresh()
    # The other device's readings (NIGHT: 2 %) never reach the data.
    assert coordinator.consecutive_failures == 4
    assert coordinator.device_available is False
    assert coordinator.data["soc_pct"] == 100
    assert len(_mismatch_warnings()) == 1
    assert OTHER_SERIAL not in _mismatch_warnings()[0]

    await coordinator.async_refresh()
    assert coordinator.consecutive_failures == 5
    assert coordinator.data["soc_pct"] == 100
    assert len(_mismatch_warnings()) == 1

    await coordinator.async_refresh()
    assert coordinator.device_available is True
    assert coordinator.consecutive_failures == 0
    assert coordinator.data["soc_pct"] == 2
    # No line at any level carries either full serial number.
    assert SERIAL not in caplog.text
    assert OTHER_SERIAL not in caplog.text


async def test_a_rejected_read_names_its_block_and_warns_without_the_enable_hint(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An exception answer means the register map no longer fits, not Modbus off."""
    caplog.set_level(
        logging.DEBUG, logger="custom_components.ecoflow_energy.coordinator"
    )
    rejected = ModbusExceptionResponse(2, 0x0870)
    coordinator = _coordinator(
        hass, _local_entry(), [_frame(DAY), rejected, rejected, rejected]
    )

    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert coordinator.last_error is not None
    assert "ModbusExceptionResponse" in coordinator.last_error
    assert "0x0870" in coordinator.last_error
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []

    await coordinator.async_refresh()
    await coordinator.async_refresh()
    (record,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    message = record.getMessage()
    assert "rejected a read" in message
    assert "0x0870" in message
    assert "Modbus is enabled" not in message


def _solar(coordinator: EcoFlowLocalModbusCoordinator) -> float | None:
    value = coordinator.data.get("solar_lifetime_energy_kwh")
    return None if value is None else float(value)


async def test_a_first_reading_below_the_restored_counter_is_not_published(
    hass: HomeAssistant,
) -> None:
    """The restored value is the floor: below it nothing is published."""
    restored = DAY["solar_total_kwh"] + 40.0
    coordinator = _coordinator(
        hass,
        _local_entry(),
        [
            _frame(DAY),
            _frame(DAY, solar_total_kwh=restored - 0.5),
            _frame(DAY, solar_total_kwh=restored + 0.5),
        ],
    )
    coordinator.seed_energy_total("solar_lifetime_energy_kwh", restored)

    await coordinator.async_refresh()
    # The other counters have no floor and pass; the one below its floor is absent.
    assert "solar_lifetime_energy_kwh" not in coordinator.data
    assert coordinator.data["grid_import_lifetime_energy_kwh"] == pytest.approx(
        DAY["grid_draw_total_kwh"], abs=1e-3
    )

    await coordinator.async_refresh()
    assert "solar_lifetime_energy_kwh" not in coordinator.data

    await coordinator.async_refresh()
    assert _solar(coordinator) == pytest.approx(restored + 0.5, abs=1e-3)


async def test_a_counter_already_published_below_the_restored_value_is_withdrawn(
    hass: HomeAssistant,
) -> None:
    """Entities are added after the first poll, so the seed arrives late."""
    restored = DAY["solar_total_kwh"] + 40.0
    coordinator = _coordinator(
        hass,
        _local_entry(),
        [_frame(DAY), _frame(DAY), _frame(DAY, solar_total_kwh=restored + 0.5)],
    )
    await coordinator.async_refresh()
    assert _solar(coordinator) == pytest.approx(DAY["solar_total_kwh"], abs=1e-3)

    coordinator.seed_energy_total("solar_lifetime_energy_kwh", restored)
    # Not a counter: a restored value for any other key changes nothing.
    coordinator.seed_energy_total("soc_pct", 1_000_000.0)

    assert "solar_lifetime_energy_kwh" not in coordinator.data
    assert "solar_lifetime_energy_kwh" not in coordinator.device_data
    assert coordinator.data["soc_pct"] == 100

    await coordinator.async_refresh()
    assert "solar_lifetime_energy_kwh" not in coordinator.data

    await coordinator.async_refresh()
    assert _solar(coordinator) == pytest.approx(restored + 0.5, abs=1e-3)


async def test_a_restored_counter_shows_until_the_device_passes_it(
    hass: HomeAssistant,
) -> None:
    """End to end: the entity renders the restored value, then the live one."""
    restored = DAY["solar_total_kwh"] + 40.0
    entity_id = "sensor.ecoflow_powerocean_solar_lifetime_energy"
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(entity_id, str(restored)),
                {"native_value": restored, "native_unit_of_measurement": "kWh"},
            )
        ],
    )
    entry = _local_entry()
    assert await _set_up_local_entry(
        hass, entry, [_frame(DAY), _frame(DAY, solar_total_kwh=restored + 5.0)]
    )

    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == pytest.approx(restored, abs=0.01)

    coordinator = hass.data[DOMAIN][entry.entry_id][SERIAL]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == pytest.approx(restored + 5.0, abs=0.01)
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_the_counters_the_parser_knows_are_the_total_increasing_local_sensors() -> None:
    """One list: a counter sensor the parser does not flag would never be held."""
    sensor_counters = {
        sensor_def.key
        for sensor_def in POWEROCEAN_LOCAL_SENSOR_DEFS
        if sensor_def.state_class == "total_increasing"
    }

    assert sensor_counters == LIFETIME_COUNTER_KEYS
