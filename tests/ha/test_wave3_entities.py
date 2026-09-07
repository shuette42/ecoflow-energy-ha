"""Entity-set tests for the EcoFlow WAVE 3 (AC71) portable AC unit.

The parser tests (PLAN-047) prove the field map against a 24-hour capture.
These run frames from the same fixture through the coordinator, because the
wiring this phase adds - the device type, the two mqtt_ingest dispatch
branches, the sensor/binary_sensor blocks, and the WAVE3-specific state_apply
handling (active-mode re-derivation, firmware-to-registry, energy
integration) - is exactly the part a parser test cannot cover on its own.
"""

from __future__ import annotations

import asyncio
import binascii
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    HVACAction,
    HVACMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.binary_sensor import (
    async_setup_entry as binary_sensor_setup,
)
from custom_components.ecoflow_energy.climate import (
    EcoFlowWave3Climate,
    async_setup_entry as climate_setup,
)
from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_WAVE3,
    DOMAIN,
    MODE_ENHANCED,
    WAVE3_BINARY_SENSORS,
    WAVE3_NUMBERS,
    WAVE3_SELECTS,
    WAVE3_SENSORS,
    WAVE3_SWITCHES,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ecoflow_energy.ecoflow.const import (
    get_device_name,
    get_device_type,
)
from custom_components.ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)
from custom_components.ecoflow_energy.ecoflow.wave3_commands import Wave3WriteRefused
from custom_components.ecoflow_energy.number import (
    EcoFlowNumber,
    async_setup_entry as number_setup,
)
from custom_components.ecoflow_energy.select import (
    EcoFlowSelect,
    async_setup_entry as select_setup,
)
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup
from custom_components.ecoflow_energy.switch import (
    EcoFlowSwitch,
    async_setup_entry as switch_setup,
)

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

# Same shape as WAVE3_DEVICE, a Delta 3 instead - the climate platform is
# forwarded for every config entry, so this is what proves it stays off a
# device that has no thermostat.
DELTA3_DEVICE: dict[str, Any] = {
    "sn": "D3M1TEST00000099",
    "name": "",
    "product_name": "",
    "device_type": DEVICE_TYPE_DELTA3,
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


def _coordinator(
    hass: HomeAssistant, data: dict[str, Any] | None = None
) -> EcoFlowDeviceCoordinator:
    """A WAVE 3 coordinator seeded with `data`, entry already added to hass."""
    entry = _entry(WAVE3_DEVICE)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, WAVE3_DEVICE)
    coordinator._device_data = dict(data or {})
    coordinator.async_set_updated_data(dict(coordinator._device_data))
    return coordinator


def _switch(coordinator: EcoFlowDeviceCoordinator, key: str) -> EcoFlowSwitch:
    defn = next(d for d in WAVE3_SWITCHES if d.key == key)
    entity = EcoFlowSwitch(coordinator, defn)
    entity.async_write_ha_state = MagicMock()
    entity.entity_id = f"switch.{key}"
    return entity


def _number(coordinator: EcoFlowDeviceCoordinator, key: str) -> EcoFlowNumber:
    defn = next(d for d in WAVE3_NUMBERS if d.key == key)
    entity = EcoFlowNumber(coordinator, defn)
    entity.async_write_ha_state = MagicMock()
    entity.entity_id = f"number.{key}"
    return entity


def _select(coordinator: EcoFlowDeviceCoordinator, key: str) -> EcoFlowSelect:
    defn = next(d for d in WAVE3_SELECTS if d.key == key)
    entity = EcoFlowSelect(coordinator, defn)
    entity.async_write_ha_state = MagicMock()
    entity.entity_id = f"select.{key}"
    return entity


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
    async def test_the_wave3_gets_18_sensors_5_binary_4_switches_5_numbers_5_selects(
        self, hass: HomeAssistant
    ) -> None:
        entities = await _setup_entities(hass, sensor_setup, WAVE3_DEVICE)
        keys = {entity._definition.key for entity in entities}
        assert len(entities) == 18
        assert keys == {sensor.key for sensor in WAVE3_SENSORS}

        binary_entities = await _setup_entities(
            hass, binary_sensor_setup, WAVE3_DEVICE
        )
        binary_keys = {entity._definition.key for entity in binary_entities}
        assert len(binary_entities) == 5
        assert binary_keys == {sensor.key for sensor in WAVE3_BINARY_SENSORS}

        switch_entities = await _setup_entities(hass, switch_setup, WAVE3_DEVICE)
        switch_keys = {entity._definition.key for entity in switch_entities}
        assert len(switch_entities) == 4
        assert switch_keys == {defn.key for defn in WAVE3_SWITCHES}

        number_entities = await _setup_entities(hass, number_setup, WAVE3_DEVICE)
        number_keys = {entity._definition.key for entity in number_entities}
        assert len(number_entities) == 5
        assert number_keys == {defn.key for defn in WAVE3_NUMBERS}

        select_entities = await _setup_entities(hass, select_setup, WAVE3_DEVICE)
        select_keys = {entity._definition.key for entity in select_entities}
        assert len(select_entities) == 5
        assert select_keys == {defn.key for defn in WAVE3_SELECTS}

        # No key does double duty across the five entity sets - each reading
        # or control lives on exactly one platform.
        all_keys = [
            *keys,
            *binary_keys,
            *switch_keys,
            *number_keys,
            *select_keys,
        ]
        assert len(all_keys) == len(set(all_keys))


class TestWave3Controls:
    """PLAN-047 Phase B: the WAVE 3 control entities write over the same
    app WebSocket ConfigWrite envelope as Delta 3, with a refusal path
    (Wave3WriteRefused) for values that do not fit the device's current
    mode, and an optimistic hold on the switch so a slow device push does
    not flicker the UI back to the pre-write state."""

    async def test_a_switch_write_awaits_the_wave3_set_with_key_and_value(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(hass, {"running": False})
        entity = _switch(coordinator, "power")

        with patch.object(
            coordinator, "async_send_wave3_set", AsyncMock(return_value=True)
        ) as sent:
            await entity.async_turn_on()
            sent.assert_awaited_once_with("power", True)

            await entity.async_turn_off()
            sent.assert_awaited_with("power", False)

    async def test_a_refused_number_write_raises_and_leaves_the_value_alone(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(
            hass, {"target_temp_c": None, "operating_mode": "fan"}
        )
        entity = _number(coordinator, "target_temp_c")

        with patch.object(
            coordinator,
            "async_send_wave3_set",
            AsyncMock(side_effect=Wave3WriteRefused("no setpoint in fan mode")),
        ):
            with pytest.raises(HomeAssistantError) as excinfo:
                await entity.async_set_native_value(22.0)

        assert excinfo.value.translation_key == "set_value_rejected"
        assert (
            excinfo.value.translation_placeholders["reason"]
            == "no setpoint in fan mode"
        )
        assert entity.native_value is None

    async def test_a_select_write_translates_the_option_to_its_wire_value(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(hass, {"screen_off_time_s": 10})
        entity = _select(coordinator, "screen_off_time_s")

        with patch.object(
            coordinator, "async_send_wave3_set", AsyncMock(return_value=True)
        ) as sent:
            await entity.async_select_option("never")

        sent.assert_awaited_once_with("screen_off_time_s", 0)
        assert entity.current_option == "never"

    async def test_the_optimistic_hold_expires_onto_a_store_that_already_agrees(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(hass, {"running": True})
        entity = _switch(coordinator, "power")

        with (
            patch(
                "custom_components.ecoflow_energy.switch.time.monotonic",
                return_value=1000.0,
            ),
            patch.object(
                coordinator, "async_send_wave3_set", AsyncMock(return_value=True)
            ),
        ):
            await entity.async_turn_off()
            assert entity.is_on is False

            # The device pushes its own confirmation (212 within ~2s on real
            # hardware) while the 5s hold is still active - it must not be
            # allowed to flicker the switch back on mid-hold.
            coordinator.async_set_updated_data({"running": True})
            entity._handle_coordinator_update()
            assert entity.is_on is False

        with patch(
            "custom_components.ecoflow_energy.switch.time.monotonic",
            return_value=1006.0,
        ):
            # Hold expired onto a store that already carries the pushed
            # value - not the stale pre-write one.
            assert entity.is_on is True


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


def _wired(
    hass: HomeAssistant, data: dict[str, Any]
) -> tuple[EcoFlowDeviceCoordinator, MagicMock]:
    """A WAVE 3 coordinator with only the MQTT client mocked.

    Nothing sits between the entity and the client: the platform branch, the
    coordinator method and the frame builder all run for real.
    """
    coordinator = _coordinator(hass, data)
    mock_mqtt = MagicMock()
    mock_mqtt.is_connected.return_value = True
    mock_mqtt.send_proto_set.return_value = True
    coordinator._mqtt_client = mock_mqtt
    return coordinator, mock_mqtt


def _sent_pdata(mock_mqtt: MagicMock) -> str:
    payload = mock_mqtt.send_proto_set.call_args[0][0]
    headers, _ = decode_header_message(payload)
    return headers[0]["pdata"]


class TestTheEntitiesReachTheWire:
    """Home Assistant hands every number over as a float and every select as
    a label. Driven from the entity with only the client mocked, each write
    has to come out as the frame the app itself sends for that setting (the
    vectors are from the 2026-09-07 app capture and the probe writes)."""

    async def test_a_brightness_write_from_the_ui_is_the_app_frame(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, mqtt = _wired(
            hass, {"screen_brightness_pct": 100, "operating_mode": "fan"}
        )
        entity = _number(coordinator, "screen_brightness_pct")
        await entity.async_set_native_value(54.0)
        assert _sent_pdata(mqtt) == "7036"

    async def test_a_fan_speed_write_from_the_ui_is_the_app_frame(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, mqtt = _wired(
            hass, {"airflow_speed_pct": 40, "operating_mode": "fan"}
        )
        entity = _number(coordinator, "airflow_speed_pct")
        await entity.async_set_native_value(60.0)
        assert _sent_pdata(mqtt) == "d8093c"

    async def test_a_setpoint_write_from_the_ui_is_the_app_frame(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, mqtt = _wired(
            hass, {"target_temp_c": 24.0, "operating_mode": "cooling"}
        )
        entity = _number(coordinator, "target_temp_c")
        await entity.async_set_native_value(27.0)
        assert _sent_pdata(mqtt) == "e5090000d841"

    async def test_the_power_switch_sends_the_two_app_triggers(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, mqtt = _wired(hass, {"running": False})
        entity = _switch(coordinator, "power")
        await entity.async_turn_on()
        assert _sent_pdata(mqtt) == "2001"
        await entity.async_turn_off()
        assert _sent_pdata(mqtt) == "e00a01"

    async def test_a_select_write_from_the_ui_is_the_app_frame(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, mqtt = _wired(
            hass, {"screen_off_time_s": 10, "operating_mode": "fan"}
        )
        entity = _select(coordinator, "screen_off_time_s")
        await entity.async_select_option("never")
        assert _sent_pdata(mqtt) == "6000"
        mode = _select(coordinator, "operating_mode")
        await mode.async_select_option("cooling")
        assert _sent_pdata(mqtt) == "c80901"


class TestStandbyCadence:
    """In standby the WAVE 3 pushes its full status every 120 s and nothing
    in between. Under the 35 s default the coordinator re-sent its initial
    requests every stale interval and the unit answered a full state every
    ~40 s (four `stale_reactivate` events in two minutes on the production
    log of 2026-09-07). The threshold has to sit above two idle uploads."""

    _AVAIL_CLOCK = "custom_components.ecoflow_energy.coordinator.availability.time.monotonic"

    async def test_the_stale_threshold_covers_two_idle_uploads(
        self, hass: HomeAssistant
    ) -> None:
        from custom_components.ecoflow_energy.const import (
            SOFT_UNAVAILABLE_S,
            STALE_THRESHOLD_S,
            WAVE3_SOFT_UNAVAILABLE_S,
            WAVE3_STALE_THRESHOLD_S,
        )

        idle = _coordinator(hass, {"running": False})
        assert idle._stale_threshold_s() == WAVE3_STALE_THRESHOLD_S
        assert idle._soft_unavailable_s() == WAVE3_SOFT_UNAVAILABLE_S
        assert WAVE3_STALE_THRESHOLD_S >= 2 * 120 + 20
        # The band between stale and soft keeps its width (the Smart Plug pair
        # moved together as well): a unit missing two idle uploads is stale,
        # not degraded.
        assert WAVE3_SOFT_UNAVAILABLE_S - WAVE3_STALE_THRESHOLD_S >= SOFT_UNAVAILABLE_S - STALE_THRESHOLD_S

        # No upload yet reads as idle, the slower of the two cadences.
        assert _coordinator(hass, {})._stale_threshold_s() == WAVE3_STALE_THRESHOLD_S

        # A running unit pushes every 2 s and keeps the default watch, so a
        # silent session while it runs is repaired as fast as anywhere else.
        running = _coordinator(hass, {"running": True})
        assert running._stale_threshold_s() == STALE_THRESHOLD_S
        assert running._soft_unavailable_s() == SOFT_UNAVAILABLE_S

    async def test_a_hundred_seconds_of_silence_is_not_stale(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(hass, {})
        mqtt = MagicMock()
        mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mqtt
        coordinator._last_mqtt_ts = 1000.0
        coordinator._log_event = MagicMock()
        # The check reschedules itself on the real clock; keep it out of the
        # test's teardown, where the mocked client would meet a real age.
        coordinator._schedule_stale_check = MagicMock()
        mqtt.reconnect_attempts = 0

        with patch(self._AVAIL_CLOCK, return_value=1100.0):
            coordinator._check_stale()
        assert not any(
            call.args and call.args[0] == "stale_reactivate"
            for call in coordinator._log_event.call_args_list
        )
        # The reactivation itself is handed to the executor, so its mock is a
        # scheduling race and not an oracle; the event is what the check
        # records synchronously, and the positive control below proves the
        # assertion above can fail.

        # Positive control: past the threshold the cheap remedy still runs.
        with patch(self._AVAIL_CLOCK, return_value=1000.0 + 300.0):
            coordinator._check_stale()
        assert any(
            call.args and call.args[0] == "stale_reactivate"
            for call in coordinator._log_event.call_args_list
        )


class TestRegistryLookup:
    """Home Assistant 2026.9 deprecates `async_get_device` for identifier
    lookups and warns per call site; the replacement takes the owning
    config entry. The oldest supported release has only the old call."""

    def test_the_new_api_is_used_when_the_registry_has_it(self) -> None:
        from custom_components.ecoflow_energy.coordinator.state_apply import _registry_device

        registry = MagicMock()
        registry.async_get_device_by_identifier.return_value = "entry"
        assert _registry_device(registry, "AC71TEST00000052", "cfg1") == "entry"
        registry.async_get_device_by_identifier.assert_called_once_with(
            (DOMAIN, "AC71TEST00000052"), "cfg1"
        )
        registry.async_get_device.assert_not_called()

    def test_the_call_shape_matches_the_registry_that_has_it(self) -> None:
        """The two positional arguments come from reading the 2026.9 source;
        wherever the running Home Assistant has the method, bind the call
        against its real signature so a renamed or reordered parameter fails
        here and not on an owner's log."""
        import inspect

        from homeassistant.helpers import device_registry as dr

        lookup = getattr(dr.DeviceRegistry, "async_get_device_by_identifier", None)
        if lookup is None:
            pytest.skip("this Home Assistant has only async_get_device")
        signature = inspect.signature(lookup)
        bound = signature.bind(None, (DOMAIN, "AC71TEST00000052"), "cfg1")
        assert bound.arguments["identifier"] == (DOMAIN, "AC71TEST00000052")
        assert bound.arguments["config_entry_id"] == "cfg1"

    def test_the_old_api_is_the_fallback(self) -> None:
        from custom_components.ecoflow_energy.coordinator.state_apply import _registry_device

        registry = MagicMock(spec=["async_get_device"])
        registry.async_get_device.return_value = "entry"
        assert _registry_device(registry, "AC71TEST00000052", "cfg1") == "entry"
        registry.async_get_device.assert_called_once_with(
            identifiers={(DOMAIN, "AC71TEST00000052")}
        )


def _climate(coordinator: EcoFlowDeviceCoordinator) -> EcoFlowWave3Climate:
    """Build one WAVE 3 climate entity directly, same shape as `_switch`."""
    entity = EcoFlowWave3Climate(coordinator)
    entity.async_write_ha_state = MagicMock()
    entity.entity_id = "climate.wave_3"
    return entity


async def _setup_all(
    hass: HomeAssistant, platform_setup, device: dict[str, Any]
) -> list[Any]:
    """Run one platform's setup and return every entity created.

    A copy of `_setup_entities` without the `hasattr(entity, "_definition")`
    filter: the climate entity is not definition-driven, so that filter
    would silently drop it from the result. `_setup_entities` stays
    unchanged - other tests depend on its filter.
    """
    entry = _entry(device)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {device["sn"]: coordinator}

    created: list[Any] = []
    await platform_setup(hass, entry, created.extend)
    return created


def _all_pdata(mock_mqtt: MagicMock) -> list[str]:
    """Every `send_proto_set` call's pdata, in order.

    `_sent_pdata` only returns the last call; several of the climate tests
    below assert a two-frame sequence (power then mode, or mode then
    setpoint), where the order itself is the thing under test.
    """
    result = []
    for call in mock_mqtt.send_proto_set.call_args_list:
        payload = call[0][0]
        headers, _ = decode_header_message(payload)
        result.append(headers[0]["pdata"])
    return result


class TestWave3ClimateEntitySet:
    """The climate platform is forwarded for every config entry in this
    integration, so it has to create exactly one entity for a WAVE 3 and
    none at all for anything that is not one."""

    async def test_the_climate_platform_creates_exactly_one_entity(
        self, hass: HomeAssistant
    ) -> None:
        entities = await _setup_all(hass, climate_setup, WAVE3_DEVICE)
        assert len(entities) == 1
        entity = entities[0]
        assert entity.unique_id.endswith("_climate")
        assert entity.name is None

    async def test_the_climate_platform_skips_a_delta_3_device(
        self, hass: HomeAssistant
    ) -> None:
        entities = await _setup_all(hass, climate_setup, DELTA3_DEVICE)
        assert entities == []


class TestTheClimateReachesTheWire:
    """Built on `_wired`: only the MQTT client is mocked, so the frame that
    reaches it is the real one the coordinator's write path builds. Each
    case seeds exactly the state it needs (the vectors are from the
    2026-09-07 app capture, PLAN-047 Phase C)."""

    async def test_switching_mode_from_off_sends_power_then_mode(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, mqtt = _wired(hass, {"running": False, "operating_mode": "fan"})
        entity = _climate(coordinator)
        await entity.async_set_hvac_mode(HVACMode.COOL)
        assert _all_pdata(mqtt) == ["2001", "c80901"]

    async def test_switching_to_off_sends_standby(self, hass: HomeAssistant) -> None:
        coordinator, mqtt = _wired(
            hass, {"running": True, "operating_mode": "cooling"}
        )
        entity = _climate(coordinator)
        await entity.async_set_hvac_mode(HVACMode.OFF)
        assert _all_pdata(mqtt) == ["e00a01"]

    async def test_switching_to_off_on_an_already_standing_by_unit_sends_nothing(
        self, hass: HomeAssistant
    ) -> None:
        """E8: the on-path already guards on `running` before powering on
        (line below checks it); the off-path used to send a standby frame
        unconditionally, one frame the user did not ask for (ADR-021
        D-C8)."""
        coordinator, mqtt = _wired(
            hass, {"running": False, "operating_mode": "cooling"}
        )
        entity = _climate(coordinator)
        await entity.async_set_hvac_mode(HVACMode.OFF)
        assert _all_pdata(mqtt) == []

    async def test_switching_mode_on_a_running_unit_sends_one_frame(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, mqtt = _wired(
            hass, {"running": True, "operating_mode": "cooling"}
        )
        entity = _climate(coordinator)
        await entity.async_set_hvac_mode(HVACMode.HEAT)
        assert _all_pdata(mqtt) == ["c80902"]

    async def test_a_setpoint_write_is_the_app_frame(self, hass: HomeAssistant) -> None:
        coordinator, mqtt = _wired(
            hass,
            {"running": True, "operating_mode": "cooling", "target_temp_c": 24.0},
        )
        entity = _climate(coordinator)
        await entity.async_set_temperature(temperature=22.0)
        assert _all_pdata(mqtt) == ["e5090000b041"]

    async def test_a_setpoint_write_off_the_gated_mode_is_refused(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, mqtt = _wired(hass, {"running": True, "operating_mode": "fan"})
        entity = _climate(coordinator)
        with pytest.raises(HomeAssistantError) as excinfo:
            await entity.async_set_temperature(temperature=22.0)
        assert excinfo.value.translation_key == "set_value_rejected"
        assert _all_pdata(mqtt) == []

    async def test_a_fan_speed_write_is_the_app_frame(self, hass: HomeAssistant) -> None:
        coordinator, mqtt = _wired(
            hass,
            {"running": True, "operating_mode": "fan", "airflow_speed_pct": 40},
        )
        entity = _climate(coordinator)
        await entity.async_set_fan_mode("60")
        assert _all_pdata(mqtt) == ["d8093c"]

    async def test_a_preset_write_is_the_app_frame(self, hass: HomeAssistant) -> None:
        coordinator, mqtt = _wired(
            hass, {"running": True, "operating_mode": "cooling"}
        )
        entity = _climate(coordinator)
        await entity.async_set_preset_mode("sleep")
        assert _all_pdata(mqtt) == ["d00903"]

    async def test_a_preset_the_app_never_writes_is_refused(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, mqtt = _wired(
            hass, {"running": True, "operating_mode": "cooling"}
        )
        entity = _climate(coordinator)
        with pytest.raises(HomeAssistantError) as excinfo:
            await entity.async_set_preset_mode("normal")
        assert excinfo.value.translation_key == "set_value_rejected"
        assert _all_pdata(mqtt) == []

    async def test_a_humidity_write_is_the_app_frame(self, hass: HomeAssistant) -> None:
        coordinator, mqtt = _wired(
            hass,
            {
                "running": True,
                "operating_mode": "dehumidify",
                "target_humidity_pct": 49.0,
            },
        )
        entity = _climate(coordinator)
        await entity.async_set_humidity(55)
        assert _all_pdata(mqtt) == ["ed0900005c42"]

    async def test_a_mode_switch_bundled_with_a_setpoint_sends_mode_then_setpoint(
        self, hass: HomeAssistant
    ) -> None:
        """E2 fix: the target mode is passed to the write gate straight from
        the gesture, so the setpoint write is checked against `cooling`
        rather than the pre-switch `dehumidify` still sitting in
        accumulated state.

        The device reports the new mode while the gesture waits, which is
        what happens on hardware about a second after the mode frame.
        """
        coordinator, mqtt = _wired(
            hass, {"running": True, "operating_mode": "dehumidify"}
        )
        entity = _climate(coordinator)

        def _report_mode(*args: Any, **kwargs: Any) -> bool:
            coordinator._device_data["operating_mode"] = "cooling"
            coordinator.async_set_updated_data(dict(coordinator._device_data))
            mqtt.send_proto_set.side_effect = None
            return True

        mqtt.send_proto_set.side_effect = _report_mode
        await entity.async_set_temperature(temperature=19.5, hvac_mode=HVACMode.COOL)
        assert _all_pdata(mqtt) == ["c80901", "e50900009c41"]

    async def test_a_bundled_setpoint_waits_for_the_device_to_report_the_mode(
        self, hass: HomeAssistant
    ) -> None:
        """Measured on hardware 2026-09-07: a setpoint sent 13 ms behind a
        mode switch is acknowledged and dropped, while the same setpoint
        sent once the mode has settled is applied. So the gesture waits for
        the device's own report of the new mode before the second frame.
        """
        coordinator, mqtt = _wired(
            hass, {"running": True, "operating_mode": "dehumidify"}
        )
        entity = _climate(coordinator)
        seen: list[str | None] = []
        loop = asyncio.get_running_loop()

        def _report_later() -> None:
            coordinator._device_data["operating_mode"] = "cooling"
            coordinator.async_set_updated_data(dict(coordinator._device_data))

        def _record(*args: Any, **kwargs: Any) -> bool:
            seen.append(coordinator.data.get("operating_mode"))
            if len(seen) == 1:
                # The device answers a mode write on its own schedule, well
                # after the write returns. Scheduling it rather than setting
                # it inline is what makes this test able to fail: without
                # the wait, the second frame goes out before this lands.
                loop.call_soon_threadsafe(loop.call_later, 0.2, _report_later)
            return True

        mqtt.send_proto_set.side_effect = _record
        await entity.async_set_temperature(temperature=19.5, hvac_mode=HVACMode.COOL)
        # The mode frame goes out while the device still reports the old
        # mode; the setpoint frame only after the new one has been reported.
        assert seen == ["dehumidify", "cooling"]

    async def test_a_bundled_setpoint_is_sent_anyway_when_the_mode_never_arrives(
        self, hass: HomeAssistant
    ) -> None:
        """A device that never reports the switch must not swallow the
        value: the wait expires and the write is attempted, because a
        refusal would be worse than a write the device may still take."""
        coordinator, mqtt = _wired(
            hass, {"running": True, "operating_mode": "dehumidify"}
        )
        entity = _climate(coordinator)
        with patch(
            "custom_components.ecoflow_energy.climate.MODE_SETTLE_TIMEOUT_S", 0.05
        ), patch(
            "custom_components.ecoflow_energy.climate.MODE_SETTLE_POLL_S", 0.01
        ):
            await entity.async_set_temperature(
                temperature=19.5, hvac_mode=HVACMode.COOL
            )
        assert _all_pdata(mqtt) == ["c80901", "e50900009c41"]

    async def test_a_mode_switch_into_a_mode_without_that_value_still_refuses_it(
        self, hass: HomeAssistant
    ) -> None:
        """E2's fan case: the target mode override does not exempt the
        write from its own mode gate. Switching into `fan` sends the mode
        frame, but the setpoint has no place in `fan` either way, so the
        second write is still refused and no second frame goes out."""
        coordinator, mqtt = _wired(
            hass, {"running": True, "operating_mode": "cooling"}
        )
        entity = _climate(coordinator)
        with patch(
            "custom_components.ecoflow_energy.climate.MODE_SETTLE_TIMEOUT_S", 0.05
        ), patch(
            "custom_components.ecoflow_energy.climate.MODE_SETTLE_POLL_S", 0.01
        ), pytest.raises(HomeAssistantError) as excinfo:
            await entity.async_set_temperature(
                temperature=22.0, hvac_mode=HVACMode.FAN_ONLY
            )
        assert excinfo.value.translation_key == "set_value_rejected"
        assert _all_pdata(mqtt) == ["c80903"]

    async def test_the_band_write_is_the_single_app_frame_for_that_pair(
        self, hass: HomeAssistant
    ) -> None:
        """PC-A's verdict is BAND CAPTURED: 158 (upper) and 159 (lower)
        always travel in one ConfigWrite. Upper 21.9 / lower 17.7 is the
        first pair the capture recorded, frame 66. E1 fix: the band's own
        range check accepts this 0.1-grid pair; only the 0.5-grid step
        check (carried over from target_temp_c) used to refuse it."""
        coordinator, mqtt = _wired(
            hass, {"running": True, "operating_mode": "constant_temp"}
        )
        entity = _climate(coordinator)
        await entity.async_set_temperature(target_temp_low=17.7, target_temp_high=21.9)
        assert _all_pdata(mqtt) == ["f5093333af41fd099a998d41"]

    def test_build_band_write_accepts_the_apps_own_captured_pair(self) -> None:
        """Same E1 fix, exercised directly against the builder."""
        from custom_components.ecoflow_energy.ecoflow.wave3_commands import (
            build_band_write,
        )

        payload = build_band_write(17.7, 21.9, WAVE3_DEVICE["sn"], seq=1)
        headers, _ = decode_header_message(payload)
        assert headers[0]["pdata"] == "f5093333af41fd099a998d41"

    async def test_a_band_narrower_than_the_apps_own_minimum_is_refused(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, mqtt = _wired(
            hass, {"running": True, "operating_mode": "constant_temp"}
        )
        entity = _climate(coordinator)
        with pytest.raises(HomeAssistantError) as excinfo:
            await entity.async_set_temperature(
                target_temp_low=18.0, target_temp_high=21.0
            )
        assert excinfo.value.translation_key == "set_value_rejected"
        assert _all_pdata(mqtt) == []

    async def test_the_band_is_not_writable_until_a_frame_is_on_record(
        self, hass: HomeAssistant
    ) -> None:
        """A one-sided card call (only the high slider moved) has to read
        the other limit out of accumulated state. Before the device has
        ever reported either limit there is nothing to read, and the write
        has to refuse rather than guess one."""
        coordinator, mqtt = _wired(
            hass, {"running": True, "operating_mode": "constant_temp"}
        )
        entity = _climate(coordinator)
        with pytest.raises(HomeAssistantError) as excinfo:
            await entity.async_set_temperature(target_temp_high=24.0)
        assert excinfo.value.translation_key == "set_command_not_ready"
        assert _all_pdata(mqtt) == []


class TestTheClimateReadsTheDevice:
    """Coordinator state in, HA climate properties out. Frame 0 (the full
    upload) already has its readings asserted by
    `test_a_full_upload_fills_the_sensors`; the values used here are exactly
    those, not numbers invented for this file - `humi_ambient_pct` (63.01,
    same as `test_a_standby_full_upload_decodes_the_criteria` in
    test_wave3_parser.py) included."""

    async def test_reading_the_full_upload_matches_the_asserted_sensors(
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

        entity = _climate(coordinator)

        assert entity.current_temperature == pytest.approx(21.61)
        # 63.01, the same frame's asserted value in test_wave3_parser.py's
        # test_a_standby_full_upload_decodes_the_criteria - a literal, not
        # the entity's own input, so a rounding-mode change would be caught.
        assert entity.current_humidity == 63
        assert entity.hvac_mode == HVACMode.OFF
        assert entity.hvac_action == HVACAction.OFF

    async def test_a_reported_mode_while_the_unit_sleeps_does_not_show_as_running(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(
            hass, {"running": False, "operating_mode": "cooling"}
        )
        entity = _climate(coordinator)
        assert entity.hvac_mode == HVACMode.OFF
        assert entity.hvac_action == HVACAction.OFF

    async def test_constant_temp_reports_the_band_and_no_single_setpoint_or_action(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(
            hass,
            {
                "running": True,
                "operating_mode": "constant_temp",
                "constant_temp_lower_limit_c": 18.0,
                "constant_temp_upper_limit_c": 24.0,
            },
        )
        entity = _climate(coordinator)
        assert entity.hvac_mode == HVACMode.HEAT_COOL
        assert entity.hvac_action is None
        assert entity.target_temperature is None
        assert entity.target_temperature_low == 18.0
        assert entity.target_temperature_high == 24.0

    async def test_the_rendered_band_attributes_are_not_rounded_to_the_setpoint_step(
        self, hass: HomeAssistant
    ) -> None:
        """E3: `state_attributes` is what Home Assistant actually publishes,
        and that is where `display_temp` rounds to `precision` - a property
        read never exercises it. A 0.5 precision would turn 21.9 into 22.0
        and 17.7 into 17.5; the fix removes `_attr_precision` so Celsius
        falls back to HA's own default, `PRECISION_TENTHS`, which is on the
        band's own 0.1 grid."""
        coordinator = _coordinator(
            hass,
            {
                "running": True,
                "operating_mode": "constant_temp",
                "constant_temp_lower_limit_c": 17.7,
                "constant_temp_upper_limit_c": 21.9,
            },
        )
        entity = _climate(coordinator)
        entity.hass = hass
        attrs = entity.state_attributes
        assert attrs[ATTR_TARGET_TEMP_HIGH] == 21.9
        assert attrs[ATTR_TARGET_TEMP_LOW] == 17.7

    async def test_a_fan_speed_off_the_five_rungs_does_not_leak_into_the_attribute(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(
            hass,
            {"running": True, "operating_mode": "fan", "airflow_speed_pct": 55},
        )
        entity = _climate(coordinator)
        assert entity.fan_mode is None

    async def test_an_unreported_setpoint_is_none_not_zero(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(hass, {"running": True, "operating_mode": "cooling"})
        entity = _climate(coordinator)
        assert entity.target_temperature is None


class TestTheClimateHasNoOptimisticHold:
    """The brief for this phase asked for a hold mirroring the switch
    platform's (`climate.time.monotonic` patched, a write holding its value
    across a contradicting coordinator update). The shipped `climate.py`
    has none: it imports no clock, and every property reads
    `self.coordinator.data` fresh on each access (ADR-021 D-C1, stated in
    the module's own docstring). A test written to expect a hold would
    fail against the entity as delivered - not because the entity is wrong,
    but because the design deliberately has no state to hold. This class
    checks the design that shipped instead: a write in flight does not
    change what the entity reports until the device's own push does."""

    async def test_a_write_does_not_change_what_hvac_mode_reports_until_the_device_confirms(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _mqtt = _wired(
            hass, {"running": True, "operating_mode": "cooling"}
        )
        entity = _climate(coordinator)
        await entity.async_set_hvac_mode(HVACMode.OFF)
        # No optimistic write followed that call - the coordinator's own
        # state is unchanged, and the entity reports exactly that.
        assert entity.hvac_mode == HVACMode.COOL
