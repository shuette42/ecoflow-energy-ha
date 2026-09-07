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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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
        from custom_components.ecoflow_energy.const import WAVE3_STALE_THRESHOLD_S

        coordinator = _coordinator(hass, {})
        assert coordinator._stale_threshold_s() == WAVE3_STALE_THRESHOLD_S
        assert WAVE3_STALE_THRESHOLD_S >= 2 * 120 + 20

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
        mqtt.resend_initial_requests.assert_not_called()

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

    def test_the_old_api_is_the_fallback(self) -> None:
        from custom_components.ecoflow_energy.coordinator.state_apply import _registry_device

        registry = MagicMock(spec=["async_get_device"])
        registry.async_get_device.return_value = "entry"
        assert _registry_device(registry, "AC71TEST00000052", "cfg1") == "entry"
        registry.async_get_device.assert_called_once_with(
            identifiers={(DOMAIN, "AC71TEST00000052")}
        )
