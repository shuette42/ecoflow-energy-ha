"""ES81 wire packets -> partial merge -> sixteen native read-only sensors."""

import json
import struct
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy import (
    binary_sensor,
    button,
    climate,
    number,
    select,
    switch,
)
from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_MODE,
    CONF_RAW_CAPTURE,
    CONF_RAW_CAPTURE_UNTIL,
    DEVICE_TYPE_SMART_METER_DUAL,
    DOMAIN,
    MODE_ENHANCED,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
    encode_varint,
)
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

from .conftest import add_entities_collector

CAPTURE = (
    Path(__file__).parent.parent / "fixtures/smart_meter_dual/es81_power_frames.json"
)
FRAMES = json.loads(CAPTURE.read_text())["frames"]
DEVICE = {
    "sn": "ES81TEST00000001",
    "product_name": "Dual",
    "device_type": "unknown",
    "online": 1,
}
KEYS = {"grid_w", "grid_l1_w", "grid_l2_w", "grid_l3_w"}
EXTENDED_FRAMES = json.loads(
    CAPTURE.with_name("es81_extended_frames.json").read_text()
)["frames"]
NET_KEYS = {"grid_net_energy_wh"} | {
    f"grid_l{phase}_net_energy_wh" for phase in (1, 2, 3)
}
ALL_KEYS = (
    KEYS
    | NET_KEYS
    | {"grid_import_energy_wh", "grid_export_energy_wh"}
    | {
        f"grid_l{phase}_{quantity}_{suffix}"
        for phase in (1, 2, 3)
        for quantity, suffix in (("voltage", "v"), ("current", "a"))
    }
)


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_DEVICES: [dict(DEVICE)],
            CONF_RAW_CAPTURE: True,
            CONF_RAW_CAPTURE_UNTIL: 1_900_000_000.0,
        },
    )


async def test_packet_ingest_partial_merge_and_sixteen_entities(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    original = dict(entry.data)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, DEVICE)
    assert coordinator.device_type == DEVICE_TYPE_SMART_METER_DUAL
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DEVICE["sn"]: coordinator}
    created: list[Any] = []
    await sensor_setup(hass, entry, add_entities_collector(created))
    sensors = {
        e._definition.key: e
        for e in created
        if hasattr(e, "_definition") and e._definition.entity_category != "diagnostic"
    }
    assert set(sensors) == ALL_KEYS
    assert len(created) == 19
    diagnostics = [e for e in created if not hasattr(e, "_definition")]
    assert len(diagnostics) == 2
    assert all(not e.entity_registry_enabled_default for e in diagnostics)
    powers = {key: sensors[key] for key in KEYS}
    assert all(e.native_value is None for e in powers.values())
    assert all(e.native_unit_of_measurement == "W" for e in powers.values())
    assert all(e.state_class == "measurement" for e in powers.values())
    assert [powers[f"grid_l{i}_w"].translation_key for i in (1, 2, 3)] == [
        f"grid_l{i}_power_w" for i in (1, 2, 3)
    ]
    resources = CAPTURE.parents[3] / "custom_components/ecoflow_energy"
    for path in (
        resources / "strings.json",
        *sorted((resources / "translations").glob("*.json")),
    ):
        names = json.loads(path.read_text())["entity"]["sensor"]
        assert all(names[f"grid_l{i}_power_w"]["name"] for i in (1, 2, 3))
    merged: dict[str, float] = {}
    for index, row in enumerate(FRAMES):
        # Both ingest topic paths must use the ES81 decoder.
        topic = (
            f"/app/device/property/{DEVICE['sn']}"
            if index % 2 == 0
            else f"/app/user/{DEVICE['sn']}/thing/property/get_reply"
        )
        coordinator._on_mqtt_message(topic, bytes.fromhex(row["hex"]))
        await hass.async_block_till_done()
        merged.update(row["expected"])
        assert {k: v for k, v in coordinator.data.items() if k in KEYS} == merged
        assert {k: e.native_value for k, e in powers.items()} == {
            k: int(round(merged[k])) if k in merged else None for k in KEYS
        }
    assert coordinator._power_to_energy == {}
    assert coordinator._energy_from_api == []
    unchanged = dict(coordinator.data)
    for topic, payload in (
        (
            f"/app/user/{DEVICE['sn']}/thing/property/get_reply",
            {"data": {"quotaMap": {"grid_w": 999, "grid_import_energy_wh": 100}}},
        ),
        (
            f"/app/device/property/{DEVICE['sn']}",
            {"params": {"grid_w": 999, "reserve": 0}},
        ),
        (
            f"/open/user/{DEVICE['sn']}/quota",
            {"params": {"grid_w": 999, "grid_l1_w": 999}},
        ),
    ):
        coordinator._on_mqtt_message(topic, json.dumps(payload).encode())
        await hass.async_block_till_done()
        assert coordinator.data == unchanged
    # Binary frames from the wrong device/channel and app writes are not telemetry.
    for topic in (
        f"/open/user/{DEVICE['sn']}/thing/property/get_reply",
        "/app/device/property/ES81OTHER0000001",
        "/app/user/ES81OTHER0000001/thing/property/get_reply",
        f"/app/user/{DEVICE['sn']}/thing/property/set",
        f"/app/user/{DEVICE['sn']}/thing/property/set_reply",
    ):
        coordinator._on_mqtt_message(topic, _telemetry({515: 999}))
        await hass.async_block_till_done()
        assert coordinator.data == unchanged
    assert entry.data == original
    assert coordinator.device_info["identifiers"] == {(DOMAIN, DEVICE["sn"])}


async def test_extended_frames_merge_signed_nets_and_normal_entity_rounding(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, DEVICE)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DEVICE["sn"]: coordinator}
    created: list[Any] = []
    await sensor_setup(hass, entry, add_entities_collector(created))
    sensors = {
        e._definition.key: e
        for e in created
        if hasattr(e, "_definition") and e._definition.entity_category != "diagnostic"
    }
    assert set(sensors) == ALL_KEYS
    assert all(e.native_value is None for e in sensors.values())
    assert all(e.entity_registry_enabled_default for e in sensors.values())
    assert all(
        e.state_class == "total" for key, e in sensors.items() if key in NET_KEYS
    )
    assert all(key not in coordinator._MONOTONIC_KEYS for key in NET_KEYS)
    for key in ("grid_import_energy_wh", "grid_export_energy_wh"):
        assert sensors[key].state_class == "total_increasing"
        assert key in coordinator._MONOTONIC_KEYS
    resources = CAPTURE.parents[3] / "custom_components/ecoflow_energy"
    for path in (
        resources / "strings.json",
        *sorted((resources / "translations").glob("*.json")),
    ):
        names = json.loads(path.read_text())["entity"]["sensor"]
        assert all(names[e.translation_key]["name"] for e in sensors.values())
    merged: dict[str, float] = {}
    decreases = 0
    for index, row in enumerate(EXTENDED_FRAMES):
        topic = (
            f"/app/device/property/{DEVICE['sn']}"
            if index % 2 == 0
            else f"/app/user/{DEVICE['sn']}/thing/property/get_reply"
        )
        for key in NET_KEYS & row["expected"].keys():
            if key in merged and row["expected"][key] < merged[key]:
                decreases += 1
        merged.update(row["expected"])
        coordinator._on_mqtt_message(topic, bytes.fromhex(row["hex"]))
        await hass.async_block_till_done()
        assert coordinator.data == merged
        for key, entity in sensors.items():
            unit = (
                "V"
                if key.endswith("_v")
                else "A"
                if key.endswith("_a")
                else "Wh"
                if key.endswith("_wh")
                else "W"
            )
            assert entity.native_unit_of_measurement == unit
            precision = 1 if unit == "V" else 2 if unit == "A" else 0
            expected = None if key not in merged else round(merged[key], precision)
            assert entity.native_value == expected
            assert entity.unique_id == f"{DEVICE['sn']}_{key}"
    assert decreases > 0
    assert set(merged) == ALL_KEYS
    assert sensors["grid_l1_current_a"].native_value < 0
    assert coordinator._power_to_energy == {} and coordinator._energy_from_api == []
    assert coordinator._energy_integrator.state_snapshot() == {}

    # Existing monotonic filtering protects the directional totals in-session;
    # signed native nets keep real decreases. No reset compensation is invented.
    def scalar(number: int, value: float) -> bytes:
        return encode_varint(number * 8 + 5) + struct.pack("<f", value)

    data = encode_field_bytes(
        773, scalar(4, 1) + scalar(6, 1) + scalar(1, -3) + scalar(7, -4)
    )
    payload = encode_field_bytes(
        1,
        encode_field_bytes(1, data)
        + encode_field_varint(8, 254)
        + encode_field_varint(9, 21),
    )
    coordinator._on_mqtt_message(f"/app/device/property/{DEVICE['sn']}", payload)
    await hass.async_block_till_done()
    assert coordinator.data["grid_import_energy_wh"] == merged["grid_import_energy_wh"]
    assert coordinator.data["grid_export_energy_wh"] == merged["grid_export_energy_wh"]
    assert sensors["grid_l1_net_energy_wh"].native_value == -3
    assert sensors["grid_net_energy_wh"].native_value == -4
    assert coordinator._energy_integrator.state_snapshot() == {}


async def test_native_Wh_restore_is_fallback_and_signed_net_can_fall(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, DEVICE)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DEVICE["sn"]: coordinator}
    created: list[Any] = []
    await sensor_setup(hass, entry, add_entities_collector(created))
    sensors = {
        e._definition.key: e
        for e in created
        if hasattr(e, "_definition") and e._definition.entity_category != "diagnostic"
    }
    # Synthetic counter endpoints straddle midnight. Restore the earlier
    # values, then ingest a later frame with a phase-net decrease.
    before = next(r for r in EXTENDED_FRAMES if r["at"].startswith("2026-11-02T22:24:"))
    after = next(r for r in EXTENDED_FRAMES if r["at"].startswith("2026-11-03T00:12:"))
    for key in ("grid_import_energy_wh", "grid_l1_net_energy_wh"):
        entity = sensors[key]
        entity.hass = hass
        last = MagicMock(native_value=before["expected"][key])
        with (
            patch.object(
                CoordinatorEntity, "async_added_to_hass", new_callable=AsyncMock
            ),
            patch.object(
                entity,
                "async_get_last_sensor_data",
                new_callable=AsyncMock,
                return_value=last,
            ),
        ):
            await entity.async_added_to_hass()
        assert entity.native_value == before["expected"][key]
    assert coordinator._energy_integrator.state_snapshot() == {}
    coordinator._on_mqtt_message(
        f"/app/user/{DEVICE['sn']}/thing/property/get_reply",
        bytes.fromhex(after["hex"]),
    )
    await hass.async_block_till_done()
    assert sensors["grid_l1_net_energy_wh"].native_value == 1285
    assert sensors["grid_import_energy_wh"].native_value == 2761
    assert coordinator._energy_integrator.state_snapshot() == {}


async def test_dual_does_not_create_controls_or_unqualified_binary_sensors(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, DEVICE)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DEVICE["sn"]: coordinator}
    for platform in (binary_sensor, button, climate, number, select, switch):
        created: list[Any] = []
        await platform.async_setup_entry(hass, entry, add_entities_collector(created))
        assert created == []


def _telemetry(
    fields: dict[int, float],
    counters: dict[int, float] | None = None,
    *,
    extra: bytes = b"",
) -> bytes:
    """Hand-built ES81 frame without capture metadata or identifiers."""

    def scalar(number: int, value: float) -> bytes:
        return encode_varint(number * 8 + 5) + struct.pack("<f", value)

    data = b"".join(scalar(number, value) for number, value in fields.items()) + extra
    if counters is not None:
        data += encode_field_bytes(
            773, b"".join(scalar(number, value) for number, value in counters.items())
        )
    return encode_field_bytes(
        1,
        encode_field_bytes(1, data)
        + encode_field_varint(8, 254)
        + encode_field_varint(9, 21),
    )


@pytest.mark.parametrize("channel", ["property", "get_reply"])
async def test_wrong_wire_header_cannot_update_existing_entity(hass, channel):
    """Reject malformed metadata at ingress before it refreshes merged state."""
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, DEVICE)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DEVICE["sn"]: coordinator}
    created: list[Any] = []
    await sensor_setup(hass, entry, add_entities_collector(created))
    sensor = next(
        e
        for e in created
        if getattr(getattr(e, "_definition", None), "key", None) == "grid_w"
    )
    topic = (
        f"/app/device/property/{DEVICE['sn']}"
        if channel == "property"
        else f"/app/user/{DEVICE['sn']}/thing/property/get_reply"
    )
    coordinator._on_mqtt_message(topic, _telemetry({515: 10}))
    await hass.async_block_till_done()
    assert sensor.native_value == 10
    unchanged = dict(coordinator.data)
    data = encode_varint(515 * 8 + 5) + struct.pack("<f", 999)
    payload = encode_field_bytes(
        1,
        encode_field_bytes(1, data)
        + encode_varint(8 * 8 + 5)
        + struct.pack("<f", 254)
        + encode_field_varint(9, 21),
    )
    coordinator._on_mqtt_message(topic, payload)
    await hass.async_block_till_done()
    assert coordinator.data == unchanged
    assert sensor.native_value == 10


@pytest.mark.parametrize("first_native", [0, 10])
async def test_restart_restore_is_display_fallback_not_directional_counter_floor(
    hass: HomeAssistant, first_native: float
) -> None:
    """The current guard is in-session only; no reset convention is inferred."""
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, DEVICE)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DEVICE["sn"]: coordinator}
    created: list[Any] = []
    await sensor_setup(hass, entry, add_entities_collector(created))
    sensors = {
        e._definition.key: e
        for e in created
        if hasattr(e, "_definition") and e._definition.entity_category != "diagnostic"
    }
    entity = sensors["grid_import_energy_wh"]
    entity.hass = hass
    with (
        patch.object(CoordinatorEntity, "async_added_to_hass", new_callable=AsyncMock),
        patch.object(
            entity,
            "async_get_last_sensor_data",
            new_callable=AsyncMock,
            return_value=MagicMock(native_value=1000),
        ),
    ):
        await entity.async_added_to_hass()
    assert entity.native_value == 1000
    assert "grid_import_energy_wh" not in coordinator.device_data
    assert coordinator._energy_integrator.state_snapshot() == {}
    topic = f"/app/device/property/{DEVICE['sn']}"
    # A partial power packet cannot fill any missing native counter with zero.
    coordinator._on_mqtt_message(topic, _telemetry({515: 0}))
    await hass.async_block_till_done()
    assert entity.native_value == 1000
    assert sensors["grid_export_energy_wh"].native_value is None
    coordinator._on_mqtt_message(topic, _telemetry({}, {4: first_native, 6: 0, 7: -5}))
    await hass.async_block_till_done()
    assert entity.native_value == first_native
    assert sensors["grid_export_energy_wh"].native_value == 0
    assert sensors["grid_net_energy_wh"].native_value == -5
    assert coordinator._energy_integrator.state_snapshot() == {}


async def test_out_of_order_totals_clamp_while_signed_readings_follow_arrival(
    hass: HomeAssistant,
) -> None:
    """No packet chronology filter; the generic guard only rejects decreases."""
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, DEVICE)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DEVICE["sn"]: coordinator}
    created: list[Any] = []
    await sensor_setup(hass, entry, add_entities_collector(created))
    sensors = {
        e._definition.key: e
        for e in created
        if hasattr(e, "_definition") and e._definition.entity_category != "diagnostic"
    }
    topic = f"/app/device/property/{DEVICE['sn']}"
    # Newer reading, then a simulated delayed earlier packet.
    for payload in (
        _telemetry({515: 20, 958: -1}, {4: 100, 6: 50, 1: 25, 7: 50}),
        _telemetry({515: -10, 958: 1}, {4: 90, 6: 40, 1: 30, 7: 60}),
    ):
        coordinator._on_mqtt_message(topic, payload)
        await hass.async_block_till_done()
    assert sensors["grid_import_energy_wh"].native_value == 100
    assert sensors["grid_export_energy_wh"].native_value == 50
    assert sensors["grid_l1_net_energy_wh"].native_value == 30
    assert sensors["grid_net_energy_wh"].native_value == 60
    assert sensors["grid_w"].native_value == -10
    assert sensors["grid_l1_current_a"].native_value == 1
    # An explicit reset/zero is parsed, but the existing in-session guard clamps it.
    coordinator._on_mqtt_message(topic, _telemetry({}, {4: 0, 6: 0, 1: 0, 7: 0}))
    await hass.async_block_till_done()
    assert sensors["grid_import_energy_wh"].native_value == 100
    assert sensors["grid_export_energy_wh"].native_value == 50
    assert sensors["grid_l1_net_energy_wh"].native_value == 0
    assert sensors["grid_net_energy_wh"].native_value == 0


async def test_freshness_is_device_wide_and_reconnect_does_not_clear_partial_values(
    hass: HomeAssistant,
) -> None:
    """Transport reconnect is not a new per-field sample or a counter reset."""
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, DEVICE)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DEVICE["sn"]: coordinator}
    created: list[Any] = []
    await sensor_setup(hass, entry, add_entities_collector(created))
    sensors = {
        e._definition.key: e
        for e in created
        if hasattr(e, "_definition") and e._definition.entity_category != "diagnostic"
    }
    client = MagicMock()
    client.is_connected.return_value = True
    coordinator._mqtt_client = client
    topic = f"/app/device/property/{DEVICE['sn']}"
    apply_clock = MagicMock()
    availability_clock = MagicMock()
    apply_clock.monotonic.return_value = 1000.0
    availability_clock.monotonic.return_value = 1000.0
    with (
        patch(
            "custom_components.ecoflow_energy.coordinator.state_apply.time", apply_clock
        ),
        patch(
            "custom_components.ecoflow_energy.coordinator.availability.time",
            availability_clock,
        ),
    ):
        coordinator._on_mqtt_message(topic, _telemetry({515: 50, 956: 230}, {4: 100}))
        await hass.async_block_till_done()
        assert coordinator.availability_stage == "healthy"
        client.is_connected.return_value = False
        assert coordinator.mqtt_status == "disconnected"
        availability_clock.monotonic.return_value = (
            1000 + coordinator._hard_unavailable_s() + 1
        )
        assert coordinator.availability_stage == "unavailable"
        assert sensors["grid_l1_voltage_v"].native_value == 230
        client.is_connected.return_value = True
        assert coordinator.mqtt_status == "connected_stale"
        assert coordinator.availability_stage == "unavailable"
        # A single power update refreshes the device, retaining old voltage/counters.
        apply_clock.monotonic.return_value = availability_clock.monotonic.return_value
        coordinator._on_mqtt_message(topic, _telemetry({515: -25}))
        await hass.async_block_till_done()
        assert coordinator.availability_stage == "healthy"
        assert coordinator.mqtt_status == "receiving"
        assert sensors["grid_w"].native_value == -25
        assert sensors["grid_l1_voltage_v"].native_value == 230
        assert sensors["grid_import_energy_wh"].native_value == 100
        assert sensors["grid_export_energy_wh"].native_value is None
    coordinator._mqtt_client = None


@pytest.mark.parametrize("channel", ["property", "get_reply"])
@pytest.mark.parametrize("value", [-40.25, -40.123456])
async def test_reported_wifi_signal_is_disabled_diagnostic_without_assumed_dbm(
    hass: HomeAssistant, channel: str, value: float
):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, DEVICE)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DEVICE["sn"]: coordinator}
    created: list[Any] = []
    await sensor_setup(hass, entry, add_entities_collector(created))
    sensors = {e._definition.key: e for e in created if hasattr(e, "_definition")}
    assert "wifi_signal_reported" in sensors
    sensor = sensors["wifi_signal_reported"]
    assert sensor.native_value is None
    assert not sensor.entity_registry_enabled_default
    assert sensor.entity_category == "diagnostic"
    assert sensor.native_unit_of_measurement is None
    assert sensor.device_class is None and sensor.state_class is None
    assert sensor.suggested_display_precision == 2
    topic = (
        f"/app/device/property/{DEVICE['sn']}"
        if channel == "property"
        else f"/app/user/{DEVICE['sn']}/thing/property/get_reply"
    )
    raw = struct.unpack("<f", struct.pack("<f", value))[0]
    expected = round(raw, 2)
    coordinator._on_mqtt_message(topic, _telemetry({602: value}))
    await hass.async_block_till_done()
    assert coordinator.data["wifi_signal_reported"] == raw
    assert sensor.native_value == expected
    coordinator._on_mqtt_message(topic, _telemetry({515: 0}))
    await hass.async_block_till_done()
    assert sensor.native_value == expected
    assert sensors["grid_w"].native_value == 0
    ambiguous = b"".join(
        encode_varint(602 * 8 + 5) + struct.pack("<f", signal)
        for signal in (-38.0, -39.0)
    )
    coordinator._on_mqtt_message(topic, _telemetry({515: 12}, extra=ambiguous))
    await hass.async_block_till_done()
    assert sensors["grid_w"].native_value == 12
    assert sensor.native_value == expected
    assert sensor.unique_id == f"{DEVICE['sn']}_wifi_signal_reported"
