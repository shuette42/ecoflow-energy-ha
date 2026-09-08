"""Control-path tests for the EcoFlow WAVE 3 (AC71): the coordinator write
method, the config-write ack gate, and the registry retirement of the six
sensors and the running flag that became controls (ADR-020, PLAN-047).

The control table itself (`WAVE3_CONTROLS`, `build_write`, `write_refusal`)
is unit-tested in `tests/test_wave3_commands.py`. These tests prove the
coordinator wiring: that a write actually reaches `send_proto_set` with the
right pdata, that the two refusal gates run before anything is sent, and
that a device ack updates the event log rather than `_device_data`.
"""

from __future__ import annotations

import binascii
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)
from custom_components.ecoflow_energy.ecoflow.wave3_commands import Wave3WriteRefused

CAPTURE = (
    Path(__file__).parent.parent / "fixtures" / "wave3" / "ac71_frames_plan046.json"
)
SET_REPLY_CAPTURE = (
    Path(__file__).parent.parent / "fixtures" / "wave3" / "ac71_set_reply_20260907.json"
)

FULL_DISPLAY_INDEX = 0  # 254/21, masked, full field dump - operating_mode "fan"

_CLOCK = "custom_components.ecoflow_energy.coordinator.time.monotonic"

WAVE3_DEVICE: dict[str, Any] = {
    "sn": "AC71TEST00000052",
    "name": "",
    "product_name": "",
    "device_type": DEVICE_TYPE_WAVE3,
    "online": 1,
}

TOPIC = f"/app/device/property/{WAVE3_DEVICE['sn']}"
SET_REPLY_TOPIC = f"/app/user123/{WAVE3_DEVICE['sn']}/thing/property/set_reply"


def _entry(device: dict[str, Any]) -> MockConfigEntry:
    """Build an Enhanced-mode entry for one device (mirrors test_wave3_entities.py)."""
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


def _coordinator(hass: HomeAssistant) -> EcoFlowDeviceCoordinator:
    entry = _entry(WAVE3_DEVICE)
    entry.add_to_hass(hass)
    return EcoFlowDeviceCoordinator(hass, entry, WAVE3_DEVICE)


def _frame(index: int) -> bytes:
    frames = json.loads(CAPTURE.read_text())["frames"]
    return binascii.unhexlify(frames[index]["hex"])


def _mode_change_frame(field_number: int, value: int) -> bytes:
    """A synthetic, unmasked 254/21 frame carrying one scalar field - the
    same shape used in test_wave3_entities.py for a bare mode-switch report.
    """
    pdata = encode_field_varint(field_number, value)
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 254)
        + encode_field_varint(9, 21)
    )
    return encode_field_bytes(1, header)


def _build_ack_frame(pdata: bytes) -> bytes:
    """A minimal 254/18 ConfigWriteAck header frame carrying `pdata`."""
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 254)
        + encode_field_varint(9, 18)
    )
    return encode_field_bytes(1, header)


def _set_reply_frame() -> bytes:
    """The maintainer's own captured set_reply ack (screen_brightness_pct,
    field 14, config_ok=1)."""
    data = json.loads(SET_REPLY_CAPTURE.read_text())
    return binascii.unhexlify(data["frames"][0]["hex"])


def _connected_mqtt() -> MagicMock:
    mock_mqtt = MagicMock()
    mock_mqtt.is_connected.return_value = True
    mock_mqtt.send_proto_set.return_value = True
    return mock_mqtt


class TestWave3SendSet:
    """C1/C2/C3: the two refusal gates, and the power-switch bypass."""

    async def test_target_temp_write_is_refused_in_fan_mode(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(hass)
        full = coordinator._parse_message(TOPIC, _frame(FULL_DISPLAY_INDEX))
        assert full is not None
        with patch(_CLOCK, return_value=1000.0):
            coordinator._apply_data(full)
        assert coordinator.data["operating_mode"] == "fan"

        mock_mqtt = _connected_mqtt()
        coordinator._mqtt_client = mock_mqtt

        with pytest.raises(Wave3WriteRefused):
            await coordinator.async_send_wave3_set("target_temp_c", 22.0)
        mock_mqtt.send_proto_set.assert_not_called()

    async def test_target_temp_write_reaches_the_device_once_cooling(
        self, hass: HomeAssistant
    ) -> None:
        """Once a mode-switch frame moves accumulated state to cooling, the
        same write publishes one payload - and `target_temp_c` (derived from
        accumulated state, not from this write) is left untouched.
        """
        coordinator = _coordinator(hass)
        full = coordinator._parse_message(TOPIC, _frame(FULL_DISPLAY_INDEX))
        with patch(_CLOCK, return_value=1000.0):
            coordinator._apply_data(full)

        mode_switch = coordinator._parse_message(TOPIC, _mode_change_frame(486, 1))
        assert mode_switch == {"operating_mode": "cooling"}
        with patch(_CLOCK, return_value=1002.0):
            coordinator._apply_data(mode_switch)
        assert coordinator.data["operating_mode"] == "cooling"
        before = coordinator.data["target_temp_c"]

        mock_mqtt = _connected_mqtt()
        coordinator._mqtt_client = mock_mqtt

        ok = await coordinator.async_send_wave3_set("target_temp_c", 22.0)

        assert ok is True
        mock_mqtt.send_proto_set.assert_called_once()
        sent_payload = mock_mqtt.send_proto_set.call_args[0][0]
        headers, _ = decode_header_message(sent_payload)
        assert headers[0]["pdata"] == "e5090000b041"
        assert coordinator.data["target_temp_c"] == before

    async def test_operating_submode_write_is_refused_in_fan_mode(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = _coordinator(hass)
        full = coordinator._parse_message(TOPIC, _frame(FULL_DISPLAY_INDEX))
        with patch(_CLOCK, return_value=1000.0):
            coordinator._apply_data(full)
        assert coordinator.data["operating_mode"] == "fan"

        mock_mqtt = _connected_mqtt()
        coordinator._mqtt_client = mock_mqtt

        with pytest.raises(Wave3WriteRefused):
            await coordinator.async_send_wave3_set("operating_submode", "max")
        mock_mqtt.send_proto_set.assert_not_called()

    async def test_power_writes_publish_the_action_trigger(
        self, hass: HomeAssistant
    ) -> None:
        """Power is not in WAVE3_CONTROLS, so it skips `write_refusal`
        entirely and always reaches the device."""
        coordinator = _coordinator(hass)
        mock_mqtt = _connected_mqtt()
        coordinator._mqtt_client = mock_mqtt

        ok_on = await coordinator.async_send_wave3_set("power", True)
        assert ok_on is True
        on_payload = mock_mqtt.send_proto_set.call_args[0][0]
        on_headers, _ = decode_header_message(on_payload)
        assert on_headers[0]["pdata"] == "2001"

        ok_off = await coordinator.async_send_wave3_set("power", False)
        assert ok_off is True
        off_payload = mock_mqtt.send_proto_set.call_args[0][0]
        off_headers, _ = decode_header_message(off_payload)
        assert off_headers[0]["pdata"] == "e00a01"

    async def test_disconnected_client_publishes_nothing(
        self, hass: HomeAssistant
    ) -> None:
        """A disconnected client returns False and sends no payload - the
        only False return the coordinator method has (PLAN-047 review lens
        A, test gap 2)."""
        coordinator = _coordinator(hass)
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = False
        coordinator._mqtt_client = mock_mqtt

        ok = await coordinator.async_send_wave3_set("beep_enabled", True)

        assert ok is False
        mock_mqtt.send_proto_set.assert_not_called()
        assert coordinator.event_log[-1]["type"] == "set_cmd_fail"

    async def test_table_refusal_from_build_write_logs_set_refused(
        self, hass: HomeAssistant
    ) -> None:
        """A refusal from `build_write` itself (out-of-range, not mode-gated)
        left no trace in the event log before this fix - `write_refusal`
        passes for screen_brightness_pct (modes=None), so this exercises
        `build_write`'s own table check specifically (LOW-1, PLAN-047 review
        lens A)."""
        coordinator = _coordinator(hass)
        mock_mqtt = _connected_mqtt()
        coordinator._mqtt_client = mock_mqtt

        with pytest.raises(Wave3WriteRefused):
            await coordinator.async_send_wave3_set("screen_brightness_pct", 5.0)

        mock_mqtt.send_proto_set.assert_not_called()
        assert coordinator.event_log[-1]["type"] == "set_refused"
        assert coordinator.event_log[-1]["detail"] == "key=screen_brightness_pct"


class TestWave3ConfigWriteAck:
    """C4: the widened ack gate (Delta 3 and WAVE 3 share the ack shape), and
    the WAVE 3-only recency gate that tells a rejection of our own write
    from a rejection of the vendor app's write on the same shared
    set_reply topic (PLAN-047 review lens A, MEDIUM-1)."""

    def test_applied_set_reply_records_no_rejection(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        coordinator = _coordinator(hass)

        with caplog.at_level("DEBUG"):
            coordinator._on_mqtt_message(SET_REPLY_TOPIC, _set_reply_frame())

        assert coordinator.event_log[-1]["type"] == "set_reply"
        assert not any(e["type"] == "set_rejected" for e in coordinator.event_log)
        # Positive control: without it, dropping WAVE 3 out of the ack gate
        # entirely leaves this test green too (PLAN-047 review lens A,
        # LOW-2) - this asserts the gate actually looked at the ack, not
        # merely that no rejection came out the other end.
        assert "Setting applied on" in caplog.text
        assert "(field 14)" in caplog.text

    async def test_rejected_set_reply_for_an_unsent_write_is_not_a_warning(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A rejection this integration never sent - the app's own write on
        the channel we share with it - is DEBUG, not a user-facing warning
        blaming a Home Assistant action nobody took (PLAN-047 review lens A,
        MEDIUM-1)."""
        import logging

        coordinator = _coordinator(hass)
        full_hex = json.loads(SET_REPLY_CAPTURE.read_text())["frames"][0]["hex"]
        # pdata "080e100170..." is action_id=14 (0x0e), config_ok=1 (the "01"
        # right after the field-2 tag "10"). Flipped to 0, nothing else
        # about the frame's shape changes - single-byte varints both ways.
        rejected_hex = full_hex.replace("080e10017036", "080e10007036", 1)
        assert rejected_hex != full_hex
        payload = binascii.unhexlify(rejected_hex)

        with caplog.at_level("DEBUG"):
            coordinator._on_mqtt_message(SET_REPLY_TOPIC, payload)

        assert not any(e["type"] == "set_rejected" for e in coordinator.event_log)
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
        debug_hits = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "did not send" in r.getMessage()
        ]
        assert len(debug_hits) == 1

    async def test_rejected_set_reply_for_our_own_write_still_warns(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The same rejected ack, after this integration actually wrote
        field 14 (screen_brightness_pct) moments earlier - still a warning
        and a set_rejected event, unchanged from before the recency gate."""
        import logging

        coordinator = _coordinator(hass)
        mock_mqtt = _connected_mqtt()
        coordinator._mqtt_client = mock_mqtt
        await coordinator.async_send_wave3_set("screen_brightness_pct", 54.0)

        full_hex = json.loads(SET_REPLY_CAPTURE.read_text())["frames"][0]["hex"]
        rejected_hex = full_hex.replace("080e10017036", "080e10007036", 1)
        payload = binascii.unhexlify(rejected_hex)

        with caplog.at_level("DEBUG"):
            coordinator._on_mqtt_message(SET_REPLY_TOPIC, payload)

        assert coordinator.event_log[-1]["type"] == "set_rejected"
        assert coordinator.event_log[-1]["detail"] == "field=14"
        assert [r for r in caplog.records if r.levelno == logging.WARNING]
