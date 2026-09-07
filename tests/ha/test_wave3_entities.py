"""Entity-set tests for the EcoFlow WAVE 3 (AC71) portable AC unit.

The parser tests (PLAN-047) prove the field map against a 24-hour capture.
These run frames from the same fixture through the coordinator, because the
wiring this phase adds - the device type, the two mqtt_ingest dispatch
branches, the sensor/binary_sensor blocks, and the WAVE3-specific state_apply
handling (active-mode re-derivation, firmware-to-registry, energy
integration) - is exactly the part a parser test cannot cover on its own.
"""

from __future__ import annotations

import binascii
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.binary_sensor import (
    async_setup_entry as binary_sensor_setup,
)
from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_WAVE3,
    DOMAIN,
    MODE_ENHANCED,
    WAVE3_BINARY_SENSORS,
    WAVE3_SENSORS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ecoflow_energy.ecoflow.const import (
    get_device_name,
    get_device_type,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

CAPTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "wave3"
    / "ac71_frames_plan046.json"
)

# One index per shape from the 24h capture (PLAN-047):
FULL_DISPLAY_INDEX = 0  # 254/21, masked, full field dump incl. mode_info
RUNTIME_UPLOAD_INDEX = 1  # 254/22, firmware + outdoor temperature
SLEEP_INCREMENTAL_INDEX = 12  # 254/21, field 212 only (sleep-state flip)
AC_POWER_INCREMENTAL_INDEX = 14  # 254/21, field 53 only (ac_input_power_w)

_CLOCK = "custom_components.ecoflow_energy.coordinator.time.monotonic"

WAVE3_DEVICE: dict[str, Any] = {
    "sn": "AC71TEST00000052",
    "name": "",
    "product_name": "",
    "device_type": DEVICE_TYPE_WAVE3,
    "online": 1,
}


def _entry(device: dict[str, Any]) -> MockConfigEntry:
    """Build an Enhanced-mode entry for one device."""
    return MockConfigEntry(
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


async def _setup_entities(
    hass: HomeAssistant,
    platform_setup,
    device: dict[str, Any],
    reported: dict[str, Any] | None = None,
) -> list[Any]:
    """Run one platform's setup and return the definition-driven entities."""
    entry = _entry(device)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    if reported:
        coordinator.async_set_updated_data(dict(reported))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {device["sn"]: coordinator}

    created: list[Any] = []
    await platform_setup(hass, entry, created.extend)
    return [entity for entity in created if hasattr(entity, "_definition")]


def _frame(index: int) -> bytes:
    frames = json.loads(CAPTURE.read_text())["frames"]
    return binascii.unhexlify(frames[index]["hex"])


def _mode_change_frame(field_number: int, value: int) -> bytes:
    """Build a synthetic, unmasked 254/21 frame carrying one scalar field.

    A real incremental DisplayPropertyUpload that reports a bare mode switch
    looks like this: one field, no envelope masking. Field 6 (enc_type) is
    left absent, which `_pdata_candidates` in stream_proto.py already treats
    as "not masked" - no seq/xor construction needed.
    """
    pdata = encode_field_varint(field_number, value)
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 254)
        + encode_field_varint(9, 21)
    )
    return encode_field_bytes(1, header)


class TestWave3Routing:
    def test_ac71_is_its_own_device_type(self) -> None:
        assert get_device_type("", "AC71TEST00000052") == DEVICE_TYPE_WAVE3

    def test_display_name_is_wave_3(self) -> None:
        assert get_device_name("", "AC71TEST00000052") == "WAVE 3 (0052)"


class TestWave3EntitySet:
    async def test_the_wave3_gets_its_24_sensors_and_6_binary_sensors(
        self, hass: HomeAssistant
    ) -> None:
        entities = await _setup_entities(hass, sensor_setup, WAVE3_DEVICE)
        keys = {entity._definition.key for entity in entities}
        assert len(entities) == 24
        assert keys == {sensor.key for sensor in WAVE3_SENSORS}

        binary_entities = await _setup_entities(
            hass, binary_sensor_setup, WAVE3_DEVICE
        )
        binary_keys = {entity._definition.key for entity in binary_entities}
        assert len(binary_entities) == 6
        assert binary_keys == {sensor.key for sensor in WAVE3_BINARY_SENSORS}


class TestTheCaptureReachesTheEntities:
    """The point of the whole phase: a frame the AC71 sent, in, and Home
    Assistant states out - including the WAVE3-only state_apply handling."""

    async def test_a_full_upload_fills_the_sensors(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(WAVE3_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, WAVE3_DEVICE)

        parsed = coordinator._parse_message(
            f"/app/device/property/{WAVE3_DEVICE['sn']}", _frame(FULL_DISPLAY_INDEX)
        )
        assert parsed is not None
        with patch(_CLOCK, return_value=1000.0):
            coordinator._apply_data(parsed)

        assert coordinator.data["temp_ambient_c"] == pytest.approx(21.61)
        assert coordinator.data["operating_mode"] == "fan"
        assert coordinator.data["airflow_speed_pct"] == 40
        assert coordinator.data["target_temp_c"] is None
        assert coordinator.data["running"] is False

    async def test_a_mode_change_in_an_incremental_frame_moves_the_setpoints(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(WAVE3_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, WAVE3_DEVICE)

        full = coordinator._parse_message(
            f"/app/device/property/{WAVE3_DEVICE['sn']}", _frame(FULL_DISPLAY_INDEX)
        )
        assert full is not None
        with patch(_CLOCK, return_value=1000.0):
            coordinator._apply_data(full)

        # field 486 = 1 -> _operating_mode_raw -> operating_mode "cooling".
        # No other field is present, so only the accumulated state (from the
        # frame above) can supply the cooling setpoints - proving the
        # derivation reads _device_data, not this message alone.
        mode_switch = coordinator._parse_message(
            f"/app/device/property/{WAVE3_DEVICE['sn']}",
            _mode_change_frame(486, 1),
        )
        assert mode_switch is not None
        assert mode_switch == {"operating_mode": "cooling"}
        with patch(_CLOCK, return_value=1002.0):
            coordinator._apply_data(mode_switch)

        assert coordinator.data["operating_mode"] == "cooling"
        assert coordinator.data["target_temp_c"] == pytest.approx(26.0)
        assert coordinator.data["operating_submode"] == "normal"
        assert coordinator.data["airflow_speed_pct"] == 40

    async def test_the_energy_sensor_integrates_from_ac_input_power(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(WAVE3_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, WAVE3_DEVICE)

        parsed = coordinator._parse_message(
            f"/app/device/property/{WAVE3_DEVICE['sn']}",
            _frame(AC_POWER_INCREMENTAL_INDEX),
        )
        assert parsed is not None
        assert parsed["ac_input_power_w"] == pytest.approx(14.95, abs=0.01)

        # MAX_GAP_S caps one Riemann step at 420s, and the published sensor
        # rounds to 2 decimals: a single 420s step at 14.95W (~0.0017 kWh)
        # would round away to 0.00. Three steps clear the 0.005 kWh floor.
        for t in (1000.0, 1420.0, 1840.0, 2260.0):
            with patch(_CLOCK, return_value=t):
                coordinator._apply_data(dict(parsed))

        # The published value is rounded to 2 decimals (state_apply.py
        # `_integrate_energy`), which a 1260s/14.95W total (~0.0052 kWh)
        # rounds to - so the scale check reads the integrator directly,
        # not the quantized published field.
        assert coordinator.data["ac_input_energy_kwh"] == 0.01
        raw_total = coordinator._energy_integrator.get_total(
            "ac_input_energy_kwh"
        )
        assert raw_total == pytest.approx(0.00523, abs=0.0002)

        energy_def = next(
            d for d in WAVE3_SENSORS if d.key == "ac_input_energy_kwh"
        )
        assert energy_def.device_class == "energy"
        assert energy_def.state_class == "total_increasing"
        assert energy_def.unit == "kWh"

    async def test_the_firmware_reaches_the_device_registry(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(WAVE3_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, WAVE3_DEVICE)
        registry = dr.async_get(hass)
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, WAVE3_DEVICE["sn"])},
            name="WAVE 3 (0052)",
        )

        parsed = coordinator._parse_message(
            f"/app/device/property/{WAVE3_DEVICE['sn']}",
            _frame(RUNTIME_UPLOAD_INDEX),
        )
        assert parsed is not None
        assert parsed["firmware_version"] == "v1.1.0.104"
        with patch(_CLOCK, return_value=1000.0):
            coordinator._apply_data(parsed)

        device = registry.async_get_device(identifiers={(DOMAIN, WAVE3_DEVICE["sn"])})
        assert device is not None
        assert device.sw_version == "v1.1.0.104"
        assert "firmware_version" not in coordinator.data

    async def test_a_repeated_firmware_frame_does_not_reset_the_change_clock(
        self, hass: HomeAssistant
    ) -> None:
        """PLAN-047 review F1: the firmware pop must run before
        `_note_value_change`, or a byte-identical repeat of the runtime
        frame - one every ~300s - looks like a value the device just sent
        for the first time, and the staleness detector never fires."""
        entry = _entry(WAVE3_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, WAVE3_DEVICE)

        parsed = coordinator._parse_message(
            f"/app/device/property/{WAVE3_DEVICE['sn']}",
            _frame(RUNTIME_UPLOAD_INDEX),
        )
        assert parsed is not None

        for t in (1000.0, 1300.0, 1600.0):
            with patch(_CLOCK, return_value=t):
                coordinator._apply_data(dict(parsed))

        # Two of the three applications were byte-identical repeats.
        assert coordinator.unchanged_updates == 2

    async def test_the_registry_catches_up_after_a_late_registration(
        self, hass: HomeAssistant
    ) -> None:
        """PLAN-047 review F2: `_apply_data` is driven by MQTT independently
        of platform setup, so a firmware frame can beat device registration.
        The registry write must not be gated on a one-shot `_sw_version`
        comparison, or the miss is unrecoverable for the config entry's
        lifetime."""
        entry = _entry(WAVE3_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, WAVE3_DEVICE)

        parsed = coordinator._parse_message(
            f"/app/device/property/{WAVE3_DEVICE['sn']}",
            _frame(RUNTIME_UPLOAD_INDEX),
        )
        assert parsed is not None

        # The firmware frame arrives before the device is registered.
        with patch(_CLOCK, return_value=1000.0):
            coordinator._apply_data(dict(parsed))

        registry = dr.async_get(hass)
        assert (
            registry.async_get_device(identifiers={(DOMAIN, WAVE3_DEVICE["sn"])})
            is None
        )

        # Platform setup registers the device afterwards, as it does in
        # real use.
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, WAVE3_DEVICE["sn"])},
            name="WAVE 3 (0052)",
        )

        with patch(_CLOCK, return_value=1300.0):
            coordinator._apply_data(dict(parsed))

        device = registry.async_get_device(identifiers={(DOMAIN, WAVE3_DEVICE["sn"])})
        assert device is not None
        assert device.sw_version == "v1.1.0.104"


class TestWave3ActiveModeInputs:
    """PLAN-047 review F3: the active-mode source keys must come from the
    parser's own map, not a hand-copied frozenset that can drift out of
    step with it."""

    def test_state_apply_imports_the_parsers_active_mode_inputs(self) -> None:
        from custom_components.ecoflow_energy.coordinator import state_apply
        from custom_components.ecoflow_energy.ecoflow.parsers import wave3_proto

        assert (
            state_apply.WAVE3_ACTIVE_MODE_INPUTS
            is wave3_proto.WAVE3_ACTIVE_MODE_INPUTS
        )


class TestWave3Diagnostics:
    """A device that reports fine but shows up as skipped in a diagnostics
    download sends every future reporter down the wrong path."""

    async def test_the_wave3_is_a_device_not_a_skipped_one(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(WAVE3_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, WAVE3_DEVICE)
        parsed = coordinator._parse_message(
            f"/app/device/property/{WAVE3_DEVICE['sn']}", _frame(FULL_DISPLAY_INDEX)
        )
        assert parsed is not None
        coordinator._device_data.update(parsed)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            WAVE3_DEVICE["sn"]: coordinator
        }

        diag = await async_get_config_entry_diagnostics(hass, entry)

        assert diag["skipped_devices"] == []
        assert len(diag["devices"]) == 1
        assert diag["devices"][0]["device_sn"].startswith("AC71")
