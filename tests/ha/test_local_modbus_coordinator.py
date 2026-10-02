"""Local Modbus coordinator and the entities built on it (PowerOcean).

The client is stubbed with decoded register frames, so these tests cover what
the coordinator and the sensor platform do with a poll: availability after
repeated failures, which entities a local entry creates, how a value the poll
did not deliver renders, and that a lifetime counter never moves backwards.
The second half covers control: the heartbeat (cadence, lapse, refusal, unload)
and the confirmed write of the two settings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from collections.abc import Awaitable, Sequence
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import homeassistant.util.dt as dt_util
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.ecoflow_energy.const import (
    CONF_DEVICES,
    CONF_MODE,
    CONF_UNIT_ID,
    DEVICE_TYPE_POWEROCEAN,
    DOMAIN,
    LOCAL_MODBUS_FAILURES_UNAVAILABLE,
    LOCAL_MODBUS_HEARTBEAT_INTERVAL_S,
    LOCAL_MODBUS_HEARTBEAT_LAPSE_S,
    MODE_LOCAL,
    POWEROCEAN_LOCAL_KEYS,
    POWEROCEAN_LOCAL_SENSOR_DEFS,
)
from custom_components.ecoflow_energy.coordinator import (
    EcoFlowDeviceCoordinator,
    local_modbus,
    modbus_link,
)
from custom_components.ecoflow_energy.coordinator.local_modbus import (
    EcoFlowLocalModbusCoordinator,
)
from custom_components.ecoflow_energy.coordinator.modbus_link import SharedModbusLink
from custom_components.ecoflow_energy.diagnostics import (
    REDACTED,
    async_get_config_entry_diagnostics,
)
from custom_components.ecoflow_energy.ecoflow.modbus_local import (
    BACKUP_RATIO_OFFSET,
    BRIGHTNESS_OFFSET,
    HEARTBEAT_OFFSET,
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
    """Answers each poll from a script: a frame, or an error to raise.

    Writes are recorded in ``writes`` and accepted unless ``write_errors`` holds
    an error for them (consumed in order, one per write). A written register is
    remembered and answered by a one-register read, which is how the
    coordinator confirms a write; ``read_back_override`` makes the device hold
    another value than the one written. While ``gate`` is set and not yet
    released, a write waits before it is recorded, so ``writes`` holds the
    writes that completed.
    """

    def __init__(self, script: list[dict[int, bytes] | Exception]) -> None:
        self._script = list(script)
        self.gate: asyncio.Event | None = None
        self.calls = 0
        # The blocks each poll asked for, in order.
        self.requested: list[tuple[tuple[int, int], ...]] = []
        self.writes: list[tuple[int, int]] = []
        self.write_errors: list[Exception | None] = []
        self.registers: dict[int, int] = {}
        self.read_back_override: int | None = None

    async def read_blocks(self, blocks: Sequence[tuple[int, int]]) -> dict[int, bytes]:
        self.calls += 1
        self.requested.append(tuple(blocks))
        if len(blocks) == 1 and blocks[0][1] == 1 and blocks[0][0] in self.registers:
            offset = blocks[0][0]
            held = self.registers[offset]
            if self.read_back_override is not None:
                held = self.read_back_override
            return {offset: struct.pack(">H", held)}
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def queue(self, *steps: dict[int, bytes] | Exception) -> None:
        """Append poll answers (or errors) to the script."""
        self._script.extend(steps)

    async def write_register(self, offset: int, value: int) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.writes.append((offset, value))
        error = self.write_errors.pop(0) if self.write_errors else None
        if error is not None:
            raise error
        self.registers[offset] = value


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


async def _polled(
    hass: HomeAssistant, entry: MockConfigEntry, *later: Any
) -> tuple[EcoFlowLocalModbusCoordinator, StubClient]:
    """A coordinator whose first poll passed the serial check, as it always is
    by the time a control switch exists. ``later`` is the script after that poll."""
    coordinator, stub = _with_stub(hass, entry, [_frame(DAY), *later])
    await coordinator.async_refresh()
    assert coordinator.last_poll_ok is True
    return coordinator, stub


# Same register values, but the identity block names another device.
OTHER_SERIAL = "HJ31DUMMY0000002"


def _frame_of_another_device(sample: dict[str, Any]) -> dict[int, bytes]:
    answer = _frame(sample)
    answer[0x0003] = OTHER_SERIAL.encode("ascii")
    return answer


async def test_failures_keep_data_until_the_threshold_then_one_success_restores(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """B1: no flicker before the threshold, one WARNING, one INFO, no reauth."""
    # Below two there is no "before the threshold" left to check.
    N = LOCAL_MODBUS_FAILURES_UNAVAILABLE
    assert N >= 2
    caplog.set_level(
        logging.DEBUG, logger="custom_components.ecoflow_energy.coordinator"
    )
    timeout = ModbusTimeoutError("no answer")
    entry = _local_entry()
    # One good poll, N failures to reach the threshold, one more past it, then
    # the device answers again.
    coordinator = _coordinator(
        hass,
        entry,
        [_frame(DAY), *[timeout] * (N + 1), _frame(DAY)],
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

        for failures in range(1, N):
            await coordinator.async_refresh()
            assert coordinator.consecutive_failures == failures
            assert coordinator.device_available is True
            assert coordinator.data["soc_pct"] == 100
            assert _noisy() == []

        await coordinator.async_refresh()
        assert coordinator.consecutive_failures == N
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
        "custom_components.ecoflow_energy.coordinator.local_modbus.create_link",
        return_value=stub,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    registered = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert {item.platform for item in registered} == {DOMAIN}
    # The sensors plus the control entities of the writable mode (their own
    # tests are in test_local_modbus_entities.py); nothing from other platforms.
    assert {item.domain for item in registered} == {
        "sensor",
        "switch",
        "number",
        "binary_sensor",
    }
    unique_ids = {item.unique_id for item in registered if item.domain == "sensor"}
    expected = {f"{SERIAL}_{key}" for key in POWEROCEAN_LOCAL_KEYS | NEW_KEYS}
    expected.add(f"{SERIAL}_connection_mode")
    assert unique_ids == expected
    assert {item.unique_id for item in registered if item.domain != "sensor"} == {
        f"{SERIAL}_{key}"
        for key in (
            "modbus_control",
            "local_backup_reserve",
            "local_indicator_brightness",
            "modbus_control_active",
        )
    }
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
        "custom_components.ecoflow_energy.coordinator.local_modbus.create_link",
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


async def test_a_device_held_with_other_link_settings_is_a_setup_error_with_the_reason(
    hass: HomeAssistant,
) -> None:
    """The refusal is final: the entry fails with the reason and is not retried."""
    entry = _local_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ecoflow_energy.coordinator.local_modbus.create_link",
        side_effect=HomeAssistantError("held with other link settings"),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    # A bare HomeAssistantError would also end in SETUP_ERROR, with the reason
    # "Unknown error": the reason is what tells the two apart.
    assert entry.reason is not None
    assert "other link settings" in entry.reason
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_local_diagnostics_carry_the_link_and_the_data_without_the_serial(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local branch reports the link (host redacted), poll health and readings."""
    monkeypatch.setattr(modbus_link, "SHARED_CONNECTION", False)
    monkeypatch.setattr(modbus_link, "SHARED_CONNECTION_REASON", "no shared module")
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
        "connection_backend": "own",
        "own_client_reason": "no shared module",
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


@pytest.mark.parametrize(
    ("shared", "backend", "reason_keys"),
    [(True, "shared", []), (False, "own", ["own_client_reason"])],
    ids=["shared", "own"],
)
async def test_local_diagnostics_name_the_connection_the_entry_runs_on(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    shared: bool,
    backend: str,
    reason_keys: list[str],
) -> None:
    """Both backends are reported; only the own client carries its reason."""
    monkeypatch.setattr(modbus_link, "SHARED_CONNECTION", shared)
    monkeypatch.setattr(
        modbus_link,
        "SHARED_CONNECTION_REASON",
        "" if shared else "No module named 'modbus_connection'",
    )
    entry = _local_entry()
    assert await _set_up_local_entry(hass, entry, [_frame(DAY)])

    config = (await async_get_config_entry_diagnostics(hass, entry))["config_entry"]

    assert config["connection_backend"] == backend
    assert [key for key in config if key.startswith("own_client")] == reason_keys
    if reason_keys:
        assert config["own_client_reason"] == "No module named 'modbus_connection'"
    assert "modbus.example.test" not in json.dumps(config)
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
    assert coordinator.device_info["sw_version"] == "5.1.37.10"


async def test_the_serial_is_read_again_after_the_device_was_unavailable(
    hass: HomeAssistant,
) -> None:
    """Whatever answers after an outage is checked, not trusted."""
    N = LOCAL_MODBUS_FAILURES_UNAVAILABLE
    timeout = ModbusTimeoutError("no answer")
    coordinator, stub = _with_stub(
        hass, _local_entry(), [_frame(DAY), *[timeout] * N, _frame(DAY)]
    )

    # One good poll, N failures (the serial is not asked for while the device
    # still counts as available), then the first answer after the outage.
    for _ in range(N + 2):
        await coordinator.async_refresh()

    assert stub.requested == [
        SETUP_BLOCKS + POLL_BLOCKS,
        *[POLL_BLOCKS] * N,
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
    N = LOCAL_MODBUS_FAILURES_UNAVAILABLE
    timeout = ModbusTimeoutError("no answer")
    coordinator = _coordinator(
        hass,
        _local_entry(),
        [
            _frame(DAY),
            *[timeout] * N,
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

    # One good poll, then N failures: the device is unavailable.
    for _ in range(N + 1):
        await coordinator.async_refresh()
    assert coordinator.device_available is False
    assert _mismatch_warnings() == []

    await coordinator.async_refresh()
    # The other device's readings (NIGHT: 2 %) never reach the data.
    assert coordinator.consecutive_failures == N + 1
    assert coordinator.device_available is False
    assert coordinator.data["soc_pct"] == 100
    assert len(_mismatch_warnings()) == 1
    assert OTHER_SERIAL not in _mismatch_warnings()[0]

    await coordinator.async_refresh()
    assert coordinator.consecutive_failures == N + 2
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
    N = LOCAL_MODBUS_FAILURES_UNAVAILABLE
    rejected = ModbusExceptionResponse(2, 0x0870)
    coordinator = _coordinator(hass, _local_entry(), [_frame(DAY), *[rejected] * N])

    # One good poll and the first rejected read: still below the threshold.
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert coordinator.last_error is not None
    assert "ModbusExceptionResponse" in coordinator.last_error
    assert "0x0870" in coordinator.last_error
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []

    # The remaining N - 1 rejected reads reach the threshold.
    for _ in range(N - 1):
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


# ----------------------------------------------------------------------
# Control: heartbeat and confirmed writes
# ----------------------------------------------------------------------


class Clock:
    """The heartbeat's clock, moved by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(local_modbus, "_monotonic", fake)
    return fake


async def _advance(hass: HomeAssistant, clock: Clock, seconds: float) -> None:
    """Move the fake clock and fire the heartbeat's sleep timer if it is due."""
    clock.now += seconds
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()
    await asyncio.sleep(0)


def _control_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and "Local Modbus" in record.getMessage()
    ]


def _beats(stub: StubClient) -> int:
    return sum(1 for write in stub.writes if write == (HEARTBEAT_OFFSET, 1))


# A gated write is released by the test only after the call under test returned,
# so a call that wrongly writes waits on the gate for good. Bounding the await
# turns that hang into a failure that names the test. Never put the fake-clock
# `_advance` inside it: firing the due timers would fire this timeout too.
_GATE_TIMEOUT_S = 5


async def _bounded[T](awaitable: Awaitable[T]) -> T:
    """Await ``awaitable`` for at most ``_GATE_TIMEOUT_S`` of real time."""
    async with asyncio.timeout(_GATE_TIMEOUT_S):
        return await awaitable


async def test_control_on_writes_the_first_beat_and_reports_on_after_the_ack(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator, stub = _with_stub(hass, _local_entry(), [])
    assert coordinator.control_enabled is False
    assert stub.writes == []

    await coordinator.async_set_control(True)

    assert stub.writes == [(HEARTBEAT_OFFSET, 1)]
    assert coordinator.control_enabled is True
    await coordinator.async_set_control(False)


async def test_a_beat_follows_every_interval_and_not_before(
    hass: HomeAssistant, clock: Clock
) -> None:
    assert LOCAL_MODBUS_HEARTBEAT_INTERVAL_S == 15
    coordinator, stub = await _polled(hass, _local_entry())
    await coordinator.async_set_control(True)
    await hass.async_block_till_done()
    assert _beats(stub) == 1

    # Each advance is measured from the moment the timer was set, so one second
    # short of the interval leaves the timer asleep and the full interval wakes it.
    for beats in (2, 3):
        await _advance(hass, clock, LOCAL_MODBUS_HEARTBEAT_INTERVAL_S - 1)
        assert _beats(stub) == beats - 1
        await _advance(hass, clock, LOCAL_MODBUS_HEARTBEAT_INTERVAL_S)
        assert _beats(stub) == beats
    assert coordinator.control_enabled is True
    await coordinator.async_set_control(False)


async def test_control_off_stops_the_beats_and_writes_nothing(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator, stub = await _polled(hass, _local_entry())
    await coordinator.async_set_control(True)
    await hass.async_block_till_done()
    await _advance(hass, clock, 15)
    assert _beats(stub) == 2

    await coordinator.async_set_control(False)

    assert coordinator.control_enabled is False
    assert coordinator._heartbeat_task is None
    for _ in range(5):
        await _advance(hass, clock, 15)
    assert stub.writes == [(HEARTBEAT_OFFSET, 1)] * 2


@pytest.mark.parametrize(
    "error",
    [
        ModbusTimeoutError("no answer"),
        ModbusConnectError("refused"),
        ModbusExceptionResponse(2, HEARTBEAT_OFFSET),
    ],
)
async def test_a_failed_first_beat_raises_and_leaves_control_off(
    hass: HomeAssistant, clock: Clock, error: Exception
) -> None:
    coordinator, stub = _with_stub(hass, _local_entry(), [])
    stub.write_errors = [error]

    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_control(True)

    assert coordinator.control_enabled is False
    assert coordinator._heartbeat_task is None
    await _advance(hass, clock, 60)
    assert stub.writes == [(HEARTBEAT_OFFSET, 1)]


async def test_a_failed_beat_is_retried_and_logged_at_debug_only(
    hass: HomeAssistant, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(
        logging.DEBUG, logger="custom_components.ecoflow_energy.coordinator"
    )
    coordinator, stub = await _polled(hass, _local_entry())
    stub.write_errors = [None, ModbusTimeoutError("no answer"), None]
    await coordinator.async_set_control(True)
    await hass.async_block_till_done()

    await _advance(hass, clock, 15)
    assert coordinator.control_enabled is True
    assert _control_warnings(caplog) == []
    assert any(
        record.levelno == logging.DEBUG and "heartbeat" in record.getMessage()
        for record in caplog.records
    )

    await _advance(hass, clock, 15)
    assert _beats(stub) == 3
    assert coordinator.control_enabled is True
    assert coordinator._last_beat_ack == clock.now
    await coordinator.async_set_control(False)


async def test_sixty_seconds_without_an_ack_turn_control_off_with_one_warning(
    hass: HomeAssistant, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    assert LOCAL_MODBUS_HEARTBEAT_LAPSE_S == 60
    coordinator, stub = await _polled(hass, _local_entry())
    stub.write_errors = [None] + [ModbusTimeoutError("no answer")] * 10
    with patch.object(coordinator, "async_update_listeners") as notify:
        await coordinator.async_set_control(True)
        await hass.async_block_till_done()
        notified_before_lapse = notify.call_count

        for _ in range(3):
            await _advance(hass, clock, 15)
            assert coordinator.control_enabled is True
        assert _control_warnings(caplog) == []
        assert notify.call_count == notified_before_lapse

        await _advance(hass, clock, 15)

        assert coordinator.control_enabled is False
        assert coordinator._heartbeat_task is None
        assert notify.call_count == notified_before_lapse + 1
    warnings = _control_warnings(caplog)
    assert len(warnings) == 1
    assert coordinator.device_tag in warnings[0]
    assert SERIAL not in caplog.text

    # The device answering again does not take control back by itself.
    stub.write_errors = []
    writes_at_lapse = len(stub.writes)
    for _ in range(3):
        await _advance(hass, clock, 15)
    assert len(stub.writes) == writes_at_lapse
    assert coordinator.control_enabled is False
    assert len(_control_warnings(caplog)) == 1


async def test_a_refused_beat_stops_the_heartbeat_at_once_with_one_warning(
    hass: HomeAssistant, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator, stub = await _polled(hass, _local_entry())
    stub.write_errors = [None, ModbusExceptionResponse(2, HEARTBEAT_OFFSET)]
    await coordinator.async_set_control(True)
    await hass.async_block_till_done()

    await _advance(hass, clock, 15)

    assert coordinator.control_enabled is False
    assert coordinator._heartbeat_task is None
    assert len(_control_warnings(caplog)) == 1
    await _advance(hass, clock, 15)
    assert _beats(stub) == 2


async def test_control_is_off_whenever_an_entry_starts(
    hass: HomeAssistant, clock: Clock
) -> None:
    entry = _local_entry()
    first, first_stub = _with_stub(hass, entry, [])
    await first.async_set_control(True)
    assert first.control_enabled is True

    second_stub = StubClient([])
    second = EcoFlowLocalModbusCoordinator(hass, entry, DEVICE_DICT, client=second_stub)

    assert second.control_enabled is False
    await _advance(hass, clock, 15)
    assert second_stub.writes == []
    await first.async_set_control(False)


async def test_unloading_the_entry_cancels_the_heartbeat_and_writes_nothing(
    hass: HomeAssistant, clock: Clock
) -> None:
    entry = _local_entry()
    assert await _set_up_local_entry(hass, entry, [_frame(DAY)])
    coordinator = hass.data[DOMAIN][entry.entry_id][SERIAL]
    stub = coordinator._link
    await coordinator.async_set_control(True)
    await hass.async_block_till_done()
    assert _beats(stub) == 1
    task = coordinator._heartbeat_task
    assert task is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert task.done()
    assert coordinator.control_enabled is False
    await _advance(hass, clock, 60)
    assert _beats(stub) == 1


@pytest.mark.parametrize(
    ("key", "offset"),
    [
        ("ems_backup_ratio_pct", BACKUP_RATIO_OFFSET),
        ("local_indicator_brightness_pct", BRIGHTNESS_OFFSET),
    ],
)
async def test_a_setting_is_applied_only_after_the_register_reads_back(
    hass: HomeAssistant, key: str, offset: int
) -> None:
    coordinator, stub = _with_stub(hass, _local_entry(), [_frame(DAY)])
    await coordinator.async_refresh()
    polls = stub.calls

    await coordinator.async_write_register(key, 40)

    assert stub.writes == [(offset, 40)]
    assert stub.requested[polls:] == [((offset, 1),)]
    assert coordinator.data[key] == 40
    assert coordinator.device_data[key] == 40


async def test_a_value_the_device_does_not_hold_fails_and_changes_nothing(
    hass: HomeAssistant,
) -> None:
    coordinator, stub = _with_stub(hass, _local_entry(), [_frame(DAY)])
    await coordinator.async_refresh()
    before = coordinator.data["ems_backup_ratio_pct"]
    stub.read_back_override = before

    with pytest.raises(HomeAssistantError, match="holds"):
        await coordinator.async_write_register("ems_backup_ratio_pct", before + 1)

    assert stub.writes == [(BACKUP_RATIO_OFFSET, before + 1)]
    assert coordinator.data["ems_backup_ratio_pct"] == before
    assert coordinator.device_data["ems_backup_ratio_pct"] == before


async def test_a_write_that_fails_on_the_wire_is_an_error_and_changes_nothing(
    hass: HomeAssistant,
) -> None:
    coordinator, stub = _with_stub(hass, _local_entry(), [_frame(DAY)])
    await coordinator.async_refresh()
    before = coordinator.data["ems_backup_ratio_pct"]
    stub.write_errors = [ModbusTimeoutError("no answer")]

    with pytest.raises(HomeAssistantError, match="did not take"):
        await coordinator.async_write_register("ems_backup_ratio_pct", before + 1)

    assert coordinator.data["ems_backup_ratio_pct"] == before


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("soc_pct", 50),
        ("ems_backup_ratio_pct", -1),
        ("ems_backup_ratio_pct", 101),
        ("local_indicator_brightness_pct", 101),
    ],
)
async def test_a_foreign_key_or_an_out_of_range_value_is_refused_before_any_write(
    hass: HomeAssistant, key: str, value: int
) -> None:
    coordinator, stub = _with_stub(hass, _local_entry(), [_frame(DAY)])
    await coordinator.async_refresh()

    with pytest.raises(ValueError):
        await coordinator.async_write_register(key, value)

    assert stub.writes == []


@pytest.mark.parametrize("value", [0, 100])
async def test_the_range_limits_themselves_are_accepted(
    hass: HomeAssistant, value: int
) -> None:
    coordinator, stub = _with_stub(hass, _local_entry(), [_frame(DAY)])
    await coordinator.async_refresh()

    await coordinator.async_write_register("local_indicator_brightness_pct", value)

    assert coordinator.data["local_indicator_brightness_pct"] == value


# ----------------------------------------------------------------------
# Control: availability, concurrency, lapse window and error mapping
# ----------------------------------------------------------------------


def _stopped_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The control-stopped WARNINGs; the poll's own availability warning is not one."""
    return [w for w in _control_warnings(caplog) if "control stopped" in w]


@pytest.mark.parametrize(
    "error",
    [ModbusTimeoutError("no answer"), ModbusExceptionResponse(2, 0x0870)],
)
async def test_the_heartbeat_stops_when_polls_fail_while_writes_still_succeed(
    hass: HomeAssistant,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    """Polls fail five times, beats are still acknowledged: nothing more is written."""
    coordinator, stub = await _polled(
        hass, _local_entry(), *[error] * LOCAL_MODBUS_FAILURES_UNAVAILABLE
    )
    await coordinator.async_set_control(True)
    await hass.async_block_till_done()
    for _ in range(LOCAL_MODBUS_FAILURES_UNAVAILABLE):
        await coordinator.async_refresh()
    assert coordinator.device_available is False
    beats = _beats(stub)

    await _advance(hass, clock, 15)

    assert coordinator.control_enabled is False
    assert coordinator._heartbeat_task is None
    assert _beats(stub) == beats
    assert len(_stopped_warnings(caplog)) == 1
    for _ in range(3):
        await _advance(hass, clock, 15)
    assert _beats(stub) == beats
    assert len(_stopped_warnings(caplog)) == 1


async def test_a_beat_is_not_written_to_a_device_that_has_not_passed_the_serial_check(
    hass: HomeAssistant, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator, stub = _with_stub(hass, _local_entry(), [])
    assert coordinator._identity_verified is False
    await coordinator.async_set_control(True)
    await hass.async_block_till_done()

    await _advance(hass, clock, 15)

    assert coordinator.control_enabled is False
    assert _beats(stub) == 1
    assert len(_stopped_warnings(caplog)) == 1


async def test_two_overlapping_turn_ons_start_one_heartbeat_that_turn_off_stops(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator, stub = await _polled(hass, _local_entry())
    stub.gate = asyncio.Event()
    first = asyncio.create_task(coordinator.async_set_control(True))
    second = asyncio.create_task(coordinator.async_set_control(True))
    for _ in range(3):
        await asyncio.sleep(0)
    stub.gate.set()
    await _bounded(asyncio.gather(first, second))
    await hass.async_block_till_done()
    assert _beats(stub) == 1
    assert coordinator.control_enabled is True

    await _bounded(coordinator.async_set_control(False))

    for _ in range(3):
        await _advance(hass, clock, 15)
    assert _beats(stub) == 1
    assert coordinator.control_enabled is False


async def test_a_turn_off_that_arrives_during_the_first_beat_wins_over_the_turn_on(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator, stub = await _polled(hass, _local_entry())
    stub.gate = asyncio.Event()
    turn_on = asyncio.create_task(coordinator.async_set_control(True))
    for _ in range(3):
        await asyncio.sleep(0)

    await _bounded(coordinator.async_set_control(False))
    stub.gate.set()
    await _bounded(turn_on)
    await hass.async_block_till_done()

    assert coordinator.control_enabled is False
    assert coordinator._heartbeat_task is None
    for _ in range(3):
        await _advance(hass, clock, 15)
    # Only the beat that was already in flight reached the device.
    assert _beats(stub) == 1


async def test_a_turn_off_after_two_queued_turn_ons_wins(
    hass: HomeAssistant, clock: Clock
) -> None:
    """The last call to arrive holds, even with a turn-on still waiting for the lock.

    The first turn-on is mid-beat, the second waits behind it for the lock, and
    the turn-off arrives last. The second turn-on must not start a heartbeat
    once it gets the lock: the user's last action was off.
    """
    coordinator, stub = await _polled(hass, _local_entry())
    stub.gate = asyncio.Event()
    first = asyncio.create_task(coordinator.async_set_control(True))
    for _ in range(3):
        await asyncio.sleep(0)
    second = asyncio.create_task(coordinator.async_set_control(True))
    for _ in range(3):
        await asyncio.sleep(0)

    await _bounded(coordinator.async_set_control(False))
    stub.gate.set()
    await _bounded(asyncio.gather(first, second))
    await hass.async_block_till_done()

    assert coordinator.control_enabled is False
    assert coordinator._heartbeat_task is None
    # Only the first turn-on's beat, already in flight, reached the device.
    assert _beats(stub) == 1
    for _ in range(3):
        await _advance(hass, clock, 15)
    assert _beats(stub) == 1


async def test_a_stale_loop_that_lapses_leaves_the_live_heartbeat_alone(
    hass: HomeAssistant, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator, stub = await _polled(hass, _local_entry())
    await coordinator.async_set_control(True)
    await hass.async_block_till_done()
    live = coordinator._heartbeat_task
    assert live is not None

    # This test task is not the registered heartbeat task: it stands in for a
    # loop that was replaced and lapses late.
    coordinator._heartbeat_lapsed("an orphaned loop")

    assert coordinator._heartbeat_task is live
    assert coordinator.control_enabled is True
    assert _control_warnings(caplog) == []
    await coordinator.async_set_control(False)


async def test_a_beat_acknowledged_after_the_window_does_not_take_control_again(
    hass: HomeAssistant, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """ADR decision 3: a lapsed heartbeat is not revived by a device that answers."""
    coordinator, stub = await _polled(hass, _local_entry())
    stub.write_errors = [None] + [ModbusTimeoutError("no answer")] * 3
    await coordinator.async_set_control(True)
    await hass.async_block_till_done()
    for _ in range(3):
        await _advance(hass, clock, 15)
    assert coordinator.control_enabled is True
    written = len(stub.writes)

    # The fourth tick is late (the link was busy). The device would answer it.
    await _advance(hass, clock, 20)

    assert coordinator.control_enabled is False
    assert len(stub.writes) == written
    assert len(_stopped_warnings(caplog)) == 1


async def test_a_beat_stuck_waiting_for_the_link_fails_before_the_window_closes(
    hass: HomeAssistant, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """A poll or a write holding the link must not delay a beat past the lapse."""
    coordinator, stub = await _polled(hass, _local_entry())
    await coordinator.async_set_control(True)
    await hass.async_block_till_done()
    stub.gate = asyncio.Event()

    await _advance(hass, clock, 15)
    assert coordinator.control_enabled is True

    # The beat waits for what is left of the window (45 s) minus the margin.
    await _advance(hass, clock, 44)

    assert coordinator.control_enabled is False
    assert _beats(stub) == 1
    assert len(_stopped_warnings(caplog)) == 1


async def test_an_unload_while_the_first_beat_is_in_flight_starts_no_heartbeat(
    hass: HomeAssistant, clock: Clock
) -> None:
    entry = _local_entry()
    assert await _set_up_local_entry(hass, entry, [_frame(DAY)])
    coordinator = hass.data[DOMAIN][entry.entry_id][SERIAL]
    stub = coordinator._link
    stub.gate = asyncio.Event()
    turn_on = asyncio.create_task(coordinator.async_set_control(True))
    for _ in range(3):
        await asyncio.sleep(0)

    assert await _bounded(hass.config_entries.async_unload(entry.entry_id))
    await hass.async_block_till_done()
    stub.gate.set()
    await _bounded(turn_on)

    assert coordinator.control_enabled is False
    assert coordinator._heartbeat_task is None
    for _ in range(3):
        await _advance(hass, clock, 15)
    assert _beats(stub) == 1


async def test_a_turn_on_that_arrives_after_the_unload_writes_nothing(
    hass: HomeAssistant, clock: Clock
) -> None:
    """A service call racing the unload must not start a beat on a stopped entry."""
    entry = _local_entry()
    assert await _set_up_local_entry(hass, entry, [_frame(DAY)])
    coordinator = hass.data[DOMAIN][entry.entry_id][SERIAL]
    stub = coordinator._link
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    await coordinator.async_set_control(True)

    assert _beats(stub) == 0
    assert coordinator.control_enabled is False
    assert coordinator._heartbeat_task is None
    for _ in range(3):
        await _advance(hass, clock, 15)
    assert _beats(stub) == 0


async def test_an_unexpected_error_on_a_beat_ends_the_heartbeat_with_one_warning(
    hass: HomeAssistant, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator, stub = await _polled(hass, _local_entry())
    stub.write_errors = [None, RuntimeError("raw text naming modbus.example.test")]
    await coordinator.async_set_control(True)
    await hass.async_block_till_done()

    await _advance(hass, clock, 15)

    assert coordinator.control_enabled is False
    assert coordinator._heartbeat_task is None
    warnings = _stopped_warnings(caplog)
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0]
    assert "modbus.example.test" not in warnings[0]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


class _ResetUnit:
    """A shared unit whose socket was reset: a raw OSError, not a library error."""

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        raise ConnectionResetError("reset by peer")

    async def write_register(self, address: int, value: int) -> None:
        raise ConnectionResetError("reset by peer")


@pytest.fixture
def shared_library(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the adapter a library error base, whether or not the real one imported."""
    names = SimpleNamespace(ModbusError=type("LibError", (Exception,), {}))
    monkeypatch.setattr(modbus_link, "mc", names, raising=False)


async def test_a_socket_reset_on_a_beat_through_the_shared_link_is_a_failed_beat(
    hass: HomeAssistant,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
    shared_library: None,
) -> None:
    coordinator, _stub = await _polled(hass, _local_entry())
    await coordinator.async_set_control(True)
    await hass.async_block_till_done()
    coordinator._link = SharedModbusLink(_ResetUnit())

    await _advance(hass, clock, 15)

    # A failed beat is retried; it is not the end of the heartbeat.
    assert coordinator.control_enabled is True
    assert _control_warnings(caplog) == []
    for _ in range(3):
        await _advance(hass, clock, 15)
    assert coordinator.control_enabled is False
    assert len(_stopped_warnings(caplog)) == 1


async def test_a_socket_reset_on_a_poll_through_the_shared_link_is_a_failed_poll(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, shared_library: None
) -> None:
    coordinator, _stub = await _polled(hass, _local_entry())
    coordinator._link = SharedModbusLink(_ResetUnit())

    await coordinator.async_refresh()

    assert coordinator.consecutive_failures == 1
    assert coordinator.last_error is not None
    assert coordinator.last_error.startswith("ModbusConnectError")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_a_mismatching_read_back_puts_the_held_value_on_display_and_raises(
    hass: HomeAssistant,
) -> None:
    coordinator, stub = await _polled(hass, _local_entry())
    before = coordinator.data["ems_backup_ratio_pct"]
    assert before not in (1, 99)
    stub.read_back_override = 99

    with pytest.raises(HomeAssistantError, match="holds"):
        await coordinator.async_write_register("ems_backup_ratio_pct", 1)

    assert coordinator.data["ems_backup_ratio_pct"] == 99
    assert coordinator.device_data["ems_backup_ratio_pct"] == 99


async def test_the_read_back_is_decoded_by_the_polls_own_parser(
    hass: HomeAssistant,
) -> None:
    coordinator, _stub = await _polled(hass, _local_entry())
    seen: list[dict[int, bytes]] = []
    real = local_modbus.parse_registers

    def spy(blocks: Any) -> dict[str, Any]:
        seen.append(dict(blocks))
        return real(blocks)

    with patch.object(local_modbus, "parse_registers", spy):
        await coordinator.async_write_register("ems_backup_ratio_pct", 40)

    assert seen == [{BACKUP_RATIO_OFFSET: struct.pack(">H", 40)}]
    assert coordinator.data["ems_backup_ratio_pct"] == 40

    # A register the parser has no value for is an error, not a guess.
    with (
        patch.object(local_modbus, "parse_registers", lambda blocks: {}),
        pytest.raises(HomeAssistantError, match="no usable value"),
    ):
        await coordinator.async_write_register("ems_backup_ratio_pct", 41)
    assert coordinator.data["ems_backup_ratio_pct"] == 40


async def test_a_refused_shared_unit_fails_setup_without_the_host_in_the_reason(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    host = "modbus.example.test"
    entry = _local_entry()
    entry.add_to_hass(hass)
    refused = HomeAssistantError(
        f"Modbus device {host}:502 is already in use with different link settings"
    )

    with patch(
        "custom_components.ecoflow_energy.coordinator.local_modbus.create_link",
        side_effect=refused,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None
    assert "other link settings" in entry.reason
    assert host not in entry.reason
    # The whole log text, traceback of the chained cause included.
    assert host not in caplog.text
