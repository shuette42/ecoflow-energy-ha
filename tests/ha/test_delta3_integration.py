"""End-to-end integration test for the Delta 3 Max Plus (Standard mode).

Sets up a full Home Assistant config entry for a Delta 3 device, feeds a
realistic HTTP quota response through the coordinator, and asserts that the
expected entities exist with the expected states, that the device reports as
available, and that the setup produces no WARNING/ERROR log records for the
integration.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_MODE,
    CONF_SECRET_KEY,
    DOMAIN,
    MODE_STANDARD,
)

from .conftest import MOCK_DELTA3_DEVICE, MOCK_MQTT_CREDENTIALS

# Realistic GET /quota/all response for a Delta 3 Max Plus. Values are
# plausible for a unit charging from AC while feeding a 12V load.
DELTA3_QUOTA_FIXTURE: dict = {
    "cmsBattSoc": 82.0,
    "bmsBattSoc": 81.4,
    "cmsChgRemTime": 96,
    "cmsDsgRemTime": 4200,
    "powInSumW": 640.3,
    "powOutSumW": 48.7,
    "powGetAcIn": 638.0,
    "powGetPv": 0.0,
    "powGetPv2": 0.0,
    "powGet12v": 47.6,
    "powGetTypec1": 0.0,
    "powGetTypec2": 0.0,
    "powGetTypec3": 0.0,
    "powGetQcusb1": 0.0,
    "powGetQcusb2": 0.0,
    "cmsMaxChgSoc": 100,
    "cmsMinDsgSoc": 5,
    "backupReverseSoc": 20,
    "xboostEn": 1,
    "enBeep": 0,
    "energyBackupEn": 1,
    "bypassOutDisable": 0,
    # Charging (2) with AC not flowing out (4), 12V flowing (0).
    "cmsChgDsgState": 2,
    "flowInfoAcOut": 4,
    "flowInfoAc2Out": 4,
    "flowInfo12v": 0,
    "powGetAcOutList": {"powGetAcOutItem": [0, 0, 0]},
}


def _delta3_config_entry() -> MockConfigEntry:
    """Standard-mode config entry carrying a single Delta 3 device."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_ACCESS_KEY: "test_ak",
            CONF_SECRET_KEY: "test_sk",
            CONF_MODE: MODE_STANDARD,
            CONF_DEVICES: [MOCK_DELTA3_DEVICE],
        },
        unique_id="test_ak",
    )


class TestDelta3EndToEnd:
    async def test_delta3_entities_populated_from_quota(
        self,
        hass: HomeAssistant,
        mock_mqtt_client,
        caplog,
    ) -> None:
        """Full setup of a Delta 3 device yields populated, available entities."""
        caplog.set_level(logging.WARNING, logger="custom_components.ecoflow_energy")

        entry = _delta3_config_entry()
        entry.add_to_hass(hass)

        with (
            patch(
                "custom_components.ecoflow_energy.coordinator.setup.IoTApiClient",
            ) as iot_cls,
            patch(
                "custom_components.ecoflow_energy.coordinator.setup.EcoFlowHTTPQuota",
            ) as http_cls,
        ):
            iot = iot_cls.return_value
            iot.get_mqtt_credentials = AsyncMock(return_value=MOCK_MQTT_CREDENTIALS)
            iot.get_device_list = AsyncMock(return_value=[MOCK_DELTA3_DEVICE])
            iot.refresh_credentials = AsyncMock(return_value=MOCK_MQTT_CREDENTIALS)

            http = http_cls.return_value
            http.get_quota_all = AsyncMock(return_value=dict(DELTA3_QUOTA_FIXTURE))
            http.last_error_code = None

            result = await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        assert result is True

        coordinators = hass.data[DOMAIN][entry.entry_id]
        sn = MOCK_DELTA3_DEVICE["sn"]
        assert sn in coordinators
        coordinator = coordinators[sn]
        assert coordinator.device_available is True

        registry = er.async_get(hass)

        def state_for(platform: str, key: str) -> str:
            entity_id = registry.async_get_entity_id(
                platform, DOMAIN, f"{sn}_{key}"
            )
            assert entity_id is not None, f"missing entity {platform} {key}"
            state = hass.states.get(entity_id)
            assert state is not None, f"no state for {entity_id}"
            return state.state

        # SoC sensor: 82.0 -> clean int 82.
        assert state_for("sensor", "cms_batt_soc") == "82"

        # Power sensor: input total 640.3 -> 640.
        assert state_for("sensor", "pow_in_sum_w") == "640"

        # 12V power sensor: 47.6 -> 48.
        assert state_for("sensor", "dc_12v_out_w") == "48"

        # Charge/discharge enum: raw 2 -> translated option key "charging".
        assert state_for("sensor", "chg_dsg_state") == "charging"

        # Output states surface as switches, not read-only sensors: the same
        # field drives the switch state, so 12V active (value != 4) is "on" and
        # AC output inactive (value 4) is "off".
        assert state_for("switch", "dc_12v_out_switch") == "on"
        assert state_for("switch", "ac_out_switch") == "off"

        # Controls exist with the vendor-documented ranges.
        assert state_for("number", "max_charge_soc") is not None

        # None of the spot-checked entities should be unavailable.
        for platform, key in (
            ("sensor", "cms_batt_soc"),
            ("sensor", "pow_in_sum_w"),
            ("sensor", "chg_dsg_state"),
            ("switch", "dc_12v_out_switch"),
        ):
            assert state_for(platform, key) not in ("unavailable", "unknown")

        # Zero-noise logging: no WARNING/ERROR from the integration during setup.
        integration_problems = [
            r
            for r in caplog.records
            if r.name.startswith("custom_components.ecoflow_energy")
            and r.levelno >= logging.WARNING
        ]
        assert not integration_problems, (
            f"unexpected WARNING/ERROR logs: "
            f"{[r.getMessage() for r in integration_problems]}"
        )


class TestDelta3EnhancedModeRouting:
    """The coordinator must route Delta 3 protobuf frames to the parser.

    Enhanced Mode delivers the device state as protobuf on the app topic.
    Before this path existed the frames were decoded but dropped, so the
    device stayed empty in Home Assistant.
    """

    def _coordinator(self, hass: HomeAssistant):
        from custom_components.ecoflow_energy.const import (
            AUTH_METHOD_APP,
            CONF_EMAIL,
            CONF_PASSWORD,
            CONF_USER_ID,
            MODE_ENHANCED,
        )
        from custom_components.ecoflow_energy.coordinator import (
            EcoFlowDeviceCoordinator,
        )

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "test@example.com",
                CONF_PASSWORD: "test_password",
                CONF_USER_ID: "user123",
                CONF_DEVICES: [MOCK_DELTA3_DEVICE],
            },
            unique_id="test@example.com",
        )
        entry.add_to_hass(hass)
        return EcoFlowDeviceCoordinator(hass, entry, MOCK_DELTA3_DEVICE)

    @staticmethod
    def _frame(cmd_func: int, cmd_id: int, inner: bytes) -> bytes:
        from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
            encode_field_bytes,
            encode_field_varint,
        )

        header = bytearray()
        header.extend(encode_field_bytes(1, inner))
        header.extend(encode_field_varint(8, cmd_func))
        header.extend(encode_field_varint(9, cmd_id))
        return encode_field_bytes(1, bytes(header))

    async def test_status_frame_is_parsed(self, hass: HomeAssistant) -> None:
        from custom_components.ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
            Delta3DisplayProperty,
        )

        coordinator = self._coordinator(hass)

        msg = Delta3DisplayProperty()
        msg.pow_in_sum_w = 640.0
        msg.pow_get_ac_in = 638.0
        msg.cms_batt_soc = 82.0
        msg.cms_chg_dsg_state = 2
        msg.cms_chg_rem_time = 96
        inner = msg.SerializeToString()

        topic = f"/app/device/property/{MOCK_DELTA3_DEVICE['sn']}"
        result = coordinator._parse_message(topic, self._frame(254, 21, inner))

        assert result is not None
        assert result["pow_in_sum_w"] == 640
        assert result["ac_in_w"] == 638
        assert result["cms_batt_soc"] == 82
        assert result["chg_dsg_state"] == "charging"
        assert result["chg_remain_time_min"] == 96

    async def test_battery_heartbeat_is_parsed(self, hass: HomeAssistant) -> None:
        from custom_components.ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
            Delta3CmsHeartbeat,
        )

        coordinator = self._coordinator(hass)

        msg = Delta3CmsHeartbeat()
        msg.v1p0.f32_lcd_show_soc = 82.4
        msg.v1p0.max_charge_soc = 95
        msg.v1p0.min_dsg_soc = 10
        inner = msg.SerializeToString()

        topic = f"/app/device/property/{MOCK_DELTA3_DEVICE['sn']}"
        result = coordinator._parse_message(topic, self._frame(32, 2, inner))

        assert result is not None
        assert result["cms_batt_soc"] == 82
        # The SoC limits in this frame are not forwarded: their meaning was
        # only ever observed at the default 100/0, where a differing
        # semantic would be invisible. The status frame carries both.
        assert "max_charge_soc_pct" not in result
        assert "min_discharge_soc_pct" not in result


class TestOtherDeviceClassesAreNotMisrouted:
    """A (cmd_func, cmd_id) pair is not unique across device classes.

    The Stream AC Pro uses the very same (254, 21) main status frame as the
    Delta 3 generation, and also emits (32, 2). Routing must happen by device
    type, otherwise a Stream device silently loses its whole telemetry to the
    Delta 3 parser.
    """

    @staticmethod
    def _stream_coordinator(hass: HomeAssistant):
        from custom_components.ecoflow_energy.const import (
            AUTH_METHOD_APP,
            CONF_EMAIL,
            CONF_PASSWORD,
            CONF_USER_ID,
            MODE_ENHANCED,
        )
        from custom_components.ecoflow_energy.coordinator import (
            EcoFlowDeviceCoordinator,
        )

        from .conftest import MOCK_STREAM_DEVICE

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "test@example.com",
                CONF_PASSWORD: "test_password",
                CONF_USER_ID: "user123",
                CONF_DEVICES: [MOCK_STREAM_DEVICE],
            },
            unique_id="test@example.com",
        )
        entry.add_to_hass(hass)
        return EcoFlowDeviceCoordinator(hass, entry, MOCK_STREAM_DEVICE)

    @staticmethod
    def _fixed32(field_number: int, value: float) -> bytes:
        import struct

        from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
            encode_varint,
        )

        return encode_varint((field_number << 3) | 5) + struct.pack("<f", value)

    def _stream_status_frame(self) -> bytes:
        """A real Stream AC Pro (254, 21) frame, same shape as the parser test."""
        from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
            encode_field_varint,
        )

        inner = bytearray()
        inner.extend(encode_field_varint(242, 21))
        inner.extend(encode_field_varint(270, 95))
        inner.extend(encode_field_varint(271, 15))
        inner.extend(self._fixed32(515, 1351.0))
        inner.extend(self._fixed32(516, 309.5))
        inner.extend(self._fixed32(518, 1043.4))
        return TestDelta3EnhancedModeRouting._frame(254, 21, bytes(inner))

    async def test_stream_status_frame_still_reaches_the_stream_parser(
        self, hass: HomeAssistant
    ) -> None:
        from .conftest import MOCK_STREAM_DEVICE

        coordinator = self._stream_coordinator(hass)
        topic = f"/app/device/property/{MOCK_STREAM_DEVICE['sn']}"

        result = coordinator._parse_message(topic, self._stream_status_frame())

        assert result is not None
        assert result["grid_w"] == 1351.0
        assert result["soc_pct"] == 21
        assert result["max_charge_soc_pct"] == 95
        # Delta 3 keys must never appear on a Stream device.
        assert "cms_batt_soc" not in result
        assert "pow_in_sum_w" not in result

    async def test_stream_battery_frame_is_not_parsed_as_delta3(
        self, hass: HomeAssistant
    ) -> None:
        """(32, 2) on a Stream device must not write foreign SoC values."""
        from custom_components.ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
            Delta3CmsHeartbeat,
        )

        from .conftest import MOCK_STREAM_DEVICE

        coordinator = self._stream_coordinator(hass)
        topic = f"/app/device/property/{MOCK_STREAM_DEVICE['sn']}"

        msg = Delta3CmsHeartbeat()
        msg.v1p0.f32_lcd_show_soc = 42.0
        frame = TestDelta3EnhancedModeRouting._frame(32, 2, msg.SerializeToString())

        result = coordinator._parse_message(topic, frame)

        assert result is None or "cms_batt_soc" not in result


class TestDelta3ControlRouting:
    """Controls must work on both channels.

    Developer keys write over the official HTTP endpoint. App logins have no
    HTTP endpoint and write the same setting as a binary frame on the app
    channel instead.
    """

    @staticmethod
    def _app_coordinator(hass: HomeAssistant):
        from custom_components.ecoflow_energy.const import (
            AUTH_METHOD_APP,
            CONF_EMAIL,
            CONF_PASSWORD,
            CONF_USER_ID,
            MODE_ENHANCED,
        )
        from custom_components.ecoflow_energy.coordinator import (
            EcoFlowDeviceCoordinator,
        )

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "test@example.com",
                CONF_PASSWORD: "test_password",
                CONF_USER_ID: "user123",
                CONF_DEVICES: [MOCK_DELTA3_DEVICE],
            },
            unique_id="test@example.com",
        )
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, MOCK_DELTA3_DEVICE)
        coordinator._auth_method = AUTH_METHOD_APP
        return coordinator

    @staticmethod
    def _developer_coordinator(hass: HomeAssistant):
        from custom_components.ecoflow_energy.const import AUTH_METHOD_DEVELOPER
        from custom_components.ecoflow_energy.coordinator import (
            EcoFlowDeviceCoordinator,
        )

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_DEVELOPER,
                CONF_MODE: MODE_STANDARD,
                CONF_ACCESS_KEY: "test_ak",
                CONF_SECRET_KEY: "test_sk",
                CONF_DEVICES: [MOCK_DELTA3_DEVICE],
            },
            unique_id="test_ak",
        )
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, MOCK_DELTA3_DEVICE)
        coordinator._auth_method = AUTH_METHOD_DEVELOPER
        return coordinator

    async def test_app_login_writes_on_the_device_channel(
        self, hass: HomeAssistant, caplog
    ) -> None:
        from unittest.mock import MagicMock

        from custom_components.ecoflow_energy.ecoflow.delta3_commands import (
            build_switch_command,
        )
        from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
            decode_header_message,
        )

        coordinator = self._app_coordinator(hass)
        mqtt = MagicMock()
        mqtt.is_connected.return_value = True
        mqtt.send_proto_set.return_value = True
        coordinator._mqtt_client = mqtt
        coordinator._http_client = None

        with caplog.at_level(logging.WARNING):
            ok = await coordinator.async_send_delta3_set(
                build_switch_command("beeper_switch", True)
            )

        assert ok is True
        mqtt.send_proto_set.assert_called_once()
        frame = mqtt.send_proto_set.call_args[0][0]
        headers, _ = decode_header_message(frame)
        assert headers[0]["pdata"] == "4801"
        assert headers[0]["device_sn"] == MOCK_DELTA3_DEVICE["sn"]
        assert "Standard mode" not in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    async def test_developer_keys_still_write_over_http(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = self._developer_coordinator(hass)
        http = AsyncMock()
        http.set_quota.return_value = {"code": "0"}
        coordinator._http_client = http
        coordinator._mqtt_client = None

        with patch.object(coordinator, "async_request_refresh", AsyncMock()):
            ok = await coordinator.async_send_delta3_set(
                {"cmdId": 17, "params": {"cfgBeepEn": True}}
            )

        assert ok is True
        http.set_quota.assert_awaited_once()

    async def test_no_channel_reports_the_real_reason(
        self, hass: HomeAssistant, caplog
    ) -> None:
        """The old message blamed Standard mode, which is no longer the cause."""
        coordinator = self._app_coordinator(hass)
        mqtt_down = None
        coordinator._mqtt_client = mqtt_down
        coordinator._http_client = None

        with caplog.at_level(logging.WARNING):
            ok = await coordinator.async_send_delta3_set(
                {"cmdId": 17, "params": {"cfgBeepEn": True}}
            )

        assert ok is False
        assert "Standard mode" not in caplog.text
        assert "connection is down" in caplog.text

    async def test_rejected_setting_is_reported(
        self, hass: HomeAssistant, caplog
    ) -> None:
        from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
            encode_field_bytes,
            encode_field_varint,
        )

        def ack_frame(action_id: int, config_ok: int) -> bytes:
            pdata = encode_field_varint(1, action_id) + encode_field_varint(
                2, config_ok
            )
            header = bytearray()
            header.extend(encode_field_bytes(1, pdata))
            header.extend(encode_field_varint(8, 254))
            header.extend(encode_field_varint(9, 18))
            header.extend(encode_field_varint(12, 1))
            return encode_field_bytes(1, bytes(header))

        coordinator = self._app_coordinator(hass)
        topic = f"/app/user123/{MOCK_DELTA3_DEVICE['sn']}/thing/property/set_reply"

        with caplog.at_level(logging.WARNING):
            coordinator._on_mqtt_message(topic, ack_frame(9, 1))
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

        with caplog.at_level(logging.WARNING):
            coordinator._on_mqtt_message(topic, ack_frame(102, 0))
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "rejected a setting" in warnings[0].getMessage()
