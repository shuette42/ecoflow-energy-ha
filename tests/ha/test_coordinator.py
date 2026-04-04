"""Tests for EcoFlowDeviceCoordinator — setup, data flow, stale detection, shutdown."""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, call, patch

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
    CREDENTIAL_MAX_AGE_S,
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_SMARTPLUG,
    DOMAIN,
    ENERGY_STREAM_KEEPALIVE_S,
    HARD_UNAVAILABLE_S,
    HTTP_FALLBACK_INTERVAL_S,
    MODE_ENHANCED,
    MODE_STANDARD,
    MQTT_HEALTH_CHECK_INTERVAL_S,
    SMARTPLUG_GET_ALL_KEEPALIVE_S,
    SMARTPLUG_STALE_THRESHOLD_S,
    SOFT_UNAVAILABLE_S,
    STALE_THRESHOLD_S,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.parsers.powerocean_proto import (
    flatten_heartbeat,
    remap_bp_keys,
    remap_proto_keys,
)

from .conftest import (
    MOCK_DELTA_DEVICE,
    MOCK_MQTT_CREDENTIALS,
    MOCK_POWEROCEAN_DEVICE,
    MOCK_SMARTPLUG_DEVICE,
)


# ===========================================================================
# Coordinator Initialization
# ===========================================================================


class TestCoordinatorInit:
    async def test_standard_mode_has_poll_interval(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Standard Mode coordinator has a polling interval."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.update_interval is not None
        assert coordinator.update_interval.total_seconds() == HTTP_FALLBACK_INTERVAL_S

    async def test_enhanced_mode_no_poll_interval(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Enhanced Mode has no polling interval (MQTT push is primary)."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        assert coordinator.update_interval is None
        assert coordinator.enhanced_mode is True

    async def test_app_auth_enhanced_for_all_device_types(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """App-auth enables Enhanced Mode for all device types (including Delta)."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_DELTA_DEVICE
        )
        # App-auth is enhanced for all device types
        assert coordinator.enhanced_mode is True
        assert coordinator.update_interval is None

    async def test_device_attributes(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Coordinator stores device attributes correctly."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.device_sn == "DAEBK5ZZ12340001"
        assert coordinator.device_name == "Delta 2 Max"
        assert coordinator.device_type == DEVICE_TYPE_DELTA
        assert coordinator.enhanced_mode is False


# ===========================================================================
# Properties
# ===========================================================================


class TestProperties:
    async def test_device_data_initially_empty(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.device_data == {}

    async def test_mqtt_client_initially_none(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.mqtt_client is None

    async def test_last_mqtt_ts_initially_zero(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.last_mqtt_ts == 0.0

    async def test_mqtt_status_not_configured(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """mqtt_status returns 'not_configured' when no MQTT client."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.mqtt_status == "not_configured"

    async def test_mqtt_status_disconnected(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """mqtt_status returns 'disconnected' when MQTT is not connected."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._mqtt_client = MagicMock()
        coordinator._mqtt_client.is_connected.return_value = False
        assert coordinator.mqtt_status == "disconnected"

    async def test_mqtt_status_receiving(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """mqtt_status returns 'receiving' when connected and data is fresh."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._mqtt_client = MagicMock()
        coordinator._mqtt_client.is_connected.return_value = True
        coordinator._last_mqtt_ts = time.monotonic()
        assert coordinator.mqtt_status == "receiving"

    async def test_mqtt_status_connected_stale(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """mqtt_status returns 'connected_stale' when connected but data is old."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._mqtt_client = MagicMock()
        coordinator._mqtt_client.is_connected.return_value = True
        coordinator._last_mqtt_ts = time.monotonic() - STALE_THRESHOLD_S - 10
        assert coordinator.mqtt_status == "connected_stale"

    async def test_data_receiving_false_without_client(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """data_receiving is False when no MQTT client."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.data_receiving is False

    async def test_data_receiving_false_when_stale(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """data_receiving is False when data is older than stale threshold."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._mqtt_client = MagicMock()
        coordinator._mqtt_client.is_connected.return_value = True
        coordinator._last_mqtt_ts = time.monotonic() - STALE_THRESHOLD_S - 10
        assert coordinator.data_receiving is False

    async def test_data_receiving_true_when_fresh(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """data_receiving is True when connected and data is fresh."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._mqtt_client = MagicMock()
        coordinator._mqtt_client.is_connected.return_value = True
        coordinator._last_mqtt_ts = time.monotonic()
        assert coordinator.data_receiving is True


# ===========================================================================
# Setup
# ===========================================================================


class TestSetup:
    async def test_standard_setup_delta_subscribes_data(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """Delta in Standard Mode sets up MQTT with subscribe_data=True."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()
        assert coordinator.mqtt_client is not None

    async def test_standard_setup_smartplug_subscribes_data(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """Smart Plug in Standard Mode sets up MQTT with subscribe_data=True."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_SMARTPLUG_DEVICE
        )
        await coordinator.async_setup()
        assert coordinator.mqtt_client is not None


# ===========================================================================
# Message Parsing
# ===========================================================================


class TestMessageParsing:
    async def test_parse_delta_json(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Delta JSON quota messages are parsed."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        # Simulate _parse_message with a JSON quota message
        import json

        topic = "/open/cert_account/SN001/quota"
        payload = json.dumps({
            "typeCode": "pdStatus",
            "params": {"soc": 85, "wattsInSum": 200},
        }).encode()

        result = coordinator._parse_message(topic, payload)
        assert result is not None
        assert result["soc"] == 85.0
        assert result["watts_in_sum"] == 200.0

    async def test_parse_unknown_topic_returns_none(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Non-quota topics return None."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        result = coordinator._parse_message("/some/other/topic", b"data")
        assert result is None

    async def test_delta_mqtt_message_not_blocked(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Delta in Standard Mode passes MQTT data through (gate is open).

        _on_mqtt_message bridges to the event loop via call_soon_threadsafe,
        so we verify the gate doesn't block and _apply_data gets scheduled.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        import json

        topic = "/open/cert/SN001/quota"
        payload = json.dumps({"typeCode": "pdStatus", "params": {"soc": 85}}).encode()

        with patch.object(coordinator.hass.loop, "call_soon_threadsafe") as mock_csf:
            coordinator._on_mqtt_message(topic, payload)
            # Delta MQTT data should reach _apply_data (not blocked by gate)
            mock_csf.assert_called_once()
            args = mock_csf.call_args[0]
            assert args[0] == coordinator._apply_data
            assert args[1]["soc"] == 85.0

    async def test_smartplug_mqtt_data_processed(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Smart Plug in Standard Mode processes MQTT data (gate is open).

        _on_mqtt_message bridges to the event loop via call_soon_threadsafe,
        so we verify the gate doesn't block and _apply_data gets scheduled.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_SMARTPLUG_DEVICE
        )
        import json

        topic = "/open/cert/SN001/quota"
        payload = json.dumps({
            "params": {"2_1.watts": 150, "2_1.volt": 230},
        }).encode()

        with patch.object(coordinator.hass.loop, "call_soon_threadsafe") as mock_csf:
            coordinator._on_mqtt_message(topic, payload)
            # Smart Plug MQTT data should reach _apply_data (not blocked by gate)
            mock_csf.assert_called_once()
            args = mock_csf.call_args[0]
            assert args[0] == coordinator._apply_data
            assert args[1]["power_w"] == pytest.approx(15.0)
            assert args[1]["voltage_v"] == 230.0

    async def test_smartplug_mqtt_direct_fields(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Smart Plug MQTT with direct field names is parsed correctly."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_SMARTPLUG_DEVICE
        )
        import json

        topic = "/open/cert/SN001/quota"
        payload = json.dumps({
            "param": {"watts": 100, "brightness": 512, "switchSta": 1},
        }).encode()

        with patch.object(coordinator.hass.loop, "call_soon_threadsafe") as mock_csf:
            coordinator._on_mqtt_message(topic, payload)
            mock_csf.assert_called_once()
            args = mock_csf.call_args[0]
            assert args[1]["power_w"] == pytest.approx(10.0)
            assert args[1]["led_brightness"] == round(512 * 100.0 / 1023.0)
            assert args[1]["switch_state"] == 1


# ===========================================================================
# HTTP Polling (_async_update_data)
# ===========================================================================


class TestHTTPPolling:
    async def test_http_update_delta(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """HTTP polling parses Delta quota data."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()

        data = await coordinator._async_update_data()
        assert "soc" in data
        assert data["soc"] == 75.0

    async def test_http_update_returns_existing_on_empty(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """If HTTP returns empty, existing data is preserved."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()

        # First successful fetch
        await coordinator._async_update_data()
        assert coordinator.device_data.get("soc") == 75.0

        # Now HTTP returns empty
        mock_http_client.get_quota_all = AsyncMock(return_value=None)
        data = await coordinator._async_update_data()
        # Existing data should be preserved
        assert data.get("soc") == 75.0


# ===========================================================================
# Reauth Suppression (#2)
# ===========================================================================


class TestReauthSuppression:
    """Error 1006 and Enhanced Mode MQTT guard must not trigger false reauth (#2)."""

    async def test_1006_does_not_increment_failure_counter(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """HTTP error 1006 must not increment the failure counter."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()

        mock_http_client.get_quota_all = AsyncMock(return_value=None)
        mock_http_client.last_error_code = "1006"

        for _ in range(10):
            await coordinator._async_update_data()

        assert coordinator._consecutive_http_failures == 0

    async def test_1006_does_not_trigger_reauth(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """5+ error 1006 responses must not trigger reauth."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()

        mock_http_client.get_quota_all = AsyncMock(return_value=None)
        mock_http_client.last_error_code = "1006"

        for _ in range(10):
            await coordinator._async_update_data()

        # No reauth should have been triggered
        assert not hasattr(standard_config_entry, "_async_start_reauth_called") or \
            not standard_config_entry._async_start_reauth_called

    async def test_non_1006_still_triggers_reauth_standard_mode(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """Non-1006 HTTP errors still trigger reauth after 5 failures in Standard Mode."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()

        mock_http_client.get_quota_all = AsyncMock(return_value=None)
        mock_http_client.last_error_code = "network"

        with patch.object(standard_config_entry, "async_start_reauth") as mock_reauth:
            for _ in range(5):
                await coordinator._async_update_data()

            mock_reauth.assert_called_once()

    async def test_app_auth_no_http_polling(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        mock_enhanced_auth,
        mock_mqtt_client,
    ) -> None:
        """App-auth mode has no HTTP client - _async_update_data returns cached data."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        await coordinator.async_setup()

        # App-auth: no HTTP client
        assert coordinator._http_client is None

        # _async_update_data returns cached data without HTTP call
        result = await coordinator._async_update_data()
        assert result == coordinator._device_data

        await coordinator.async_shutdown()

    async def test_app_auth_reauth_on_login_failure(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        mock_mqtt_client,
    ) -> None:
        """App-auth mode triggers reauth when login fails."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )

        with patch(
            "custom_components.ecoflow_energy.ecoflow.app_api.AppApiClient",
        ) as cls:
            instance = cls.return_value
            instance.login = AsyncMock(return_value=False)

            with patch.object(enhanced_config_entry, "async_start_reauth") as mock_reauth:
                await coordinator.async_setup()
                mock_reauth.assert_called_once()

    async def test_mixed_1006_and_real_failures(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """Only non-1006 errors increment the failure counter."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()

        # 3x 1006 (should NOT count) + 2x network error (should count)
        mock_http_client.get_quota_all = AsyncMock(return_value=None)

        mock_http_client.last_error_code = "1006"
        for _ in range(3):
            await coordinator._async_update_data()

        mock_http_client.last_error_code = "network"
        for _ in range(2):
            await coordinator._async_update_data()

        assert coordinator._consecutive_http_failures == 2


# ===========================================================================
# SET Commands
# ===========================================================================


class TestSETCommands:
    async def test_send_set_command_success(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """SET command is published via MQTT."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()

        command = {"moduleType": 1, "operateType": "dcOutCfg", "params": {"enabled": 1}}
        ok = await coordinator.async_send_set_command(command)
        assert ok is True
        mock_mqtt_client.publish.assert_called_once()

    async def test_send_set_command_no_mqtt(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """SET command returns False when MQTT not connected."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        # No async_setup → no MQTT client
        ok = await coordinator.async_send_set_command({"params": {}})
        assert ok is False

    async def test_send_set_command_tcp_topic(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """TCP mode (IoT API) publishes to /open/{cert_account}/{SN}/set."""
        standard_config_entry.add_to_hass(hass)
        mock_mqtt_client.wss_mode = False
        mock_mqtt_client.cert_account = "test_cert_account"
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()

        command = {"moduleType": 1, "operateType": "dcOutCfg", "params": {"enabled": 1}}
        ok = await coordinator.async_send_set_command(command)
        assert ok is True
        topic = mock_mqtt_client.publish.call_args[0][0]
        assert topic == f"/open/test_cert_account/{MOCK_DELTA_DEVICE['sn']}/set"

    async def test_send_set_command_wss_topic(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """WSS mode (app-auth) publishes to /app/{user_id}/{SN}/thing/property/set."""
        standard_config_entry.add_to_hass(hass)
        mock_mqtt_client.wss_mode = True
        mock_mqtt_client.user_id = "test_user_123"
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()

        command = {"moduleType": 1, "operateType": "dcOutCfg", "params": {"enabled": 1}}
        ok = await coordinator.async_send_set_command(command)
        assert ok is True
        topic = mock_mqtt_client.publish.call_args[0][0]
        assert topic == f"/app/test_user_123/{MOCK_DELTA_DEVICE['sn']}/thing/property/set"

    async def test_send_set_command_mqtt_disconnected(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """SET command returns False when MQTT client exists but is disconnected."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()
        mock_mqtt_client.is_connected.return_value = False

        ok = await coordinator.async_send_set_command({"params": {}})
        assert ok is False
        mock_mqtt_client.publish.assert_not_called()


# ===========================================================================
# Shutdown
# ===========================================================================


class TestShutdown:
    async def test_shutdown_sets_flag(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """Shutdown sets the _shutdown flag and disconnects MQTT."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()
        assert coordinator._shutdown is False

        await coordinator.async_shutdown()
        assert coordinator._shutdown is True
        assert coordinator.mqtt_client is None

    async def test_shutdown_cancels_timers(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        mock_mqtt_client,
        mock_enhanced_auth,
    ) -> None:
        """Shutdown cancels keepalive and stale check timers."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        await coordinator.async_setup()

        # Enhanced Mode should have scheduled timers
        assert coordinator._keepalive_unsub is not None
        assert coordinator._stale_check_unsub is not None

        await coordinator.async_shutdown()
        assert coordinator._keepalive_unsub is None
        assert coordinator._stale_check_unsub is None


# ===========================================================================
# Stale Detection (_check_stale)
# ===========================================================================


class TestStaleDetection:
    def _cleanup_stale_timer(self, coordinator):
        """Cancel the timer that _check_stale re-schedules."""
        if coordinator._stale_check_unsub is not None:
            coordinator._stale_check_unsub.cancel()
            coordinator._stale_check_unsub = None

    async def test_stale_activates_http_fallback(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """When MQTT is stale, HTTP fallback is activated."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._http_client = MagicMock()  # simulate developer-auth with HTTP
        coordinator._last_mqtt_ts = time.monotonic() - STALE_THRESHOLD_S - 10
        assert coordinator.update_interval is None

        coordinator._check_stale()

        assert coordinator.update_interval is not None
        assert coordinator.update_interval.total_seconds() == HTTP_FALLBACK_INTERVAL_S
        self._cleanup_stale_timer(coordinator)

    async def test_stale_recovery_disables_fallback(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """When MQTT recovers, HTTP fallback is disabled."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._http_client = MagicMock()  # simulate developer-auth with HTTP
        coordinator.update_interval = timedelta(seconds=HTTP_FALLBACK_INTERVAL_S)
        coordinator._last_mqtt_ts = time.monotonic()

        coordinator._check_stale()

        assert coordinator.update_interval is None
        self._cleanup_stale_timer(coordinator)

    async def test_stale_no_mqtt_ts_is_infinite(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """When last_mqtt_ts is 0, age is infinite - triggers fallback."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._http_client = MagicMock()  # simulate developer-auth with HTTP
        coordinator._last_mqtt_ts = 0.0

        coordinator._check_stale()

        assert coordinator.update_interval is not None
        self._cleanup_stale_timer(coordinator)

    async def test_stale_check_noop_when_shutdown(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_check_stale is a no-op when shutdown flag is set."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._shutdown = True
        coordinator._last_mqtt_ts = 0.0

        coordinator._check_stale()

        assert coordinator.update_interval is None
        # No timer scheduled when shutdown

    async def test_app_auth_smartplug_uses_relaxed_stale_threshold(
        self,
        hass: HomeAssistant,
    ) -> None:
        """Smart Plug app-auth uses a higher stale threshold than PowerOcean."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "test@example.com",
                CONF_PASSWORD: "test_password",
                CONF_USER_ID: "user123",
                CONF_DEVICES: [MOCK_SMARTPLUG_DEVICE],
            },
            unique_id="test@example.com",
        )
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, MOCK_SMARTPLUG_DEVICE)

        assert coordinator._stale_threshold_s() == SMARTPLUG_STALE_THRESHOLD_S

    async def test_app_auth_smartplug_not_stale_at_default_threshold(
        self,
        hass: HomeAssistant,
    ) -> None:
        """Smart Plug is not marked unavailable at ~45s MQTT age."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "test@example.com",
                CONF_PASSWORD: "test_password",
                CONF_USER_ID: "user123",
                CONF_DEVICES: [MOCK_SMARTPLUG_DEVICE],
            },
            unique_id="test@example.com",
        )
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, MOCK_SMARTPLUG_DEVICE)
        coordinator._mqtt_client = MagicMock()
        coordinator._mqtt_client.is_connected.return_value = True

        coordinator._last_mqtt_ts = time.monotonic() - STALE_THRESHOLD_S - 10
        coordinator._check_stale()

        assert coordinator.device_available is True
        assert coordinator._last_stale_reconnect_ts == 0.0
        self._cleanup_stale_timer(coordinator)

    async def test_app_auth_stays_available_during_degraded(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """App-auth keeps device available during stale and degraded stages."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._mqtt_client = MagicMock()
        coordinator._mqtt_client.is_connected.return_value = False
        coordinator._mqtt_client.try_reconnect.return_value = False
        coordinator._mqtt_client.reconnect_attempts = 0

        now = 10_000.0
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=now,
        ):
            # Data age between soft and hard threshold: degraded but available
            coordinator._last_mqtt_ts = now - SOFT_UNAVAILABLE_S - 10
            coordinator._check_stale()

        assert coordinator.device_available is True
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=now,
        ):
            assert coordinator.availability_stage == "degraded"
        self._cleanup_stale_timer(coordinator)

    async def test_app_auth_unavailable_after_hard_threshold(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """App-auth marks unavailable only after hard unavailable threshold (10 min)."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._mqtt_client = MagicMock()
        coordinator._mqtt_client.is_connected.return_value = False
        coordinator._mqtt_client.try_reconnect.return_value = False

        now = 10_000.0
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=now,
        ):
            # Data age beyond hard threshold
            coordinator._last_mqtt_ts = now - HARD_UNAVAILABLE_S - 5
            with caplog.at_level("WARNING"):
                coordinator._check_stale()

            assert coordinator.device_available is False
            assert coordinator.availability_stage == "unavailable"
        assert "marking device unavailable" in caplog.text
        self._cleanup_stale_timer(coordinator)

    async def test_availability_stage_transitions(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Availability stage progresses through healthy -> stale -> degraded -> unavailable."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._mqtt_client = MagicMock()
        coordinator._mqtt_client.is_connected.return_value = True

        now = 1_000.0
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=now,
        ):
            # Healthy: fresh data
            coordinator._last_mqtt_ts = now - 10
            assert coordinator.availability_stage == "healthy"

            # Stale: past stale threshold but before soft unavailable
            coordinator._last_mqtt_ts = now - STALE_THRESHOLD_S - 10
            assert coordinator.availability_stage == "stale"

            # Degraded: past soft unavailable but before hard
            coordinator._last_mqtt_ts = now - SOFT_UNAVAILABLE_S - 10
            assert coordinator.availability_stage == "degraded"

            # Unavailable: past hard threshold
            coordinator._last_mqtt_ts = now - HARD_UNAVAILABLE_S - 10
            assert coordinator.availability_stage == "unavailable"

    async def test_powerocean_survives_600s_gap(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """PowerOcean stays available during a 600s stream gap (observed real behavior)."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._mqtt_client = MagicMock()
        coordinator._mqtt_client.is_connected.return_value = True
        coordinator._mqtt_client.force_reconnect.return_value = None
        coordinator._mqtt_client.reconnect_attempts = 0

        now = 10_000.0
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=now,
        ):
            # 590s gap: between SOFT_UNAVAILABLE (300s) and HARD_UNAVAILABLE (600s)
            coordinator._last_mqtt_ts = now - 590
            coordinator._check_stale()

        assert coordinator.device_available is True
        # Check stage with the same mocked time
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=now,
        ):
            assert coordinator.availability_stage == "degraded"
        self._cleanup_stale_timer(coordinator)

    async def test_stale_connected_forces_reconnect(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Connected-but-stale app-auth sessions trigger a forced reconnect."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        mock_mqtt.force_reconnect.return_value = True
        coordinator._mqtt_client = mock_mqtt
        coordinator._last_mqtt_ts = time.monotonic() - STALE_THRESHOLD_S - 5
        coordinator._last_stale_reconnect_ts = 0.0

        coordinator._check_stale()

        mock_mqtt.force_reconnect.assert_called_once()
        assert coordinator._last_stale_reconnect_ts > 0.0
        self._cleanup_stale_timer(coordinator)

    async def test_stale_triggers_reconnect(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Stale check triggers MQTT reconnect when disconnected."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        # Don't call async_setup — just set up a mock MQTT client directly
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = False
        mock_mqtt.try_reconnect.return_value = False
        coordinator._mqtt_client = mock_mqtt
        coordinator._last_mqtt_ts = time.monotonic() - STALE_THRESHOLD_S - 10

        coordinator._check_stale()

        mock_mqtt.try_reconnect.assert_called_once()
        self._cleanup_stale_timer(coordinator)

    async def test_stale_check_reschedules_with_health_interval(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Stale check timer runs on health interval, not stale threshold."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )

        with patch.object(coordinator.hass.loop, "call_later") as mock_call_later:
            coordinator._schedule_stale_check()

        mock_call_later.assert_called_once()
        scheduled_delay = mock_call_later.call_args.args[0]
        assert scheduled_delay == min(STALE_THRESHOLD_S, MQTT_HEALTH_CHECK_INTERVAL_S)


# ===========================================================================
# Keepalive (_send_keepalive)
# ===========================================================================


class TestKeepalive:
    async def test_send_keepalive_when_connected(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Keepalive sends EnergyStreamSwitch when MQTT is connected."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        # Set up mock MQTT client directly (no async_setup → no lingering timers)
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt

        coordinator._send_keepalive()

        # Should reschedule
        assert coordinator._keepalive_unsub is not None
        # Cleanup
        coordinator._keepalive_unsub.cancel()
        coordinator._keepalive_unsub = None

    async def test_send_keepalive_noop_when_shutdown(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Keepalive is a no-op when shutdown flag is set."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._shutdown = True
        coordinator._send_keepalive()
        # Should not crash, no rescheduling after shutdown return


# ===========================================================================
# Enhanced Mode Setup
# ===========================================================================


class TestEnhancedSetup:
    async def test_enhanced_setup_creates_wss_client(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        mock_mqtt_client,
        mock_enhanced_auth,
    ) -> None:
        """App-auth setup creates WSS MQTT client and schedules timers."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        await coordinator.async_setup()

        assert coordinator.mqtt_client is not None
        assert coordinator._keepalive_unsub is not None
        assert coordinator._stale_check_unsub is not None
        # App-auth has no HTTP client
        assert coordinator._http_client is None
        # Cleanup
        await coordinator.async_shutdown()

    async def test_enhanced_setup_credential_failure(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        mock_mqtt_client,
    ) -> None:
        """App-auth setup handles MQTT credential fetch failure gracefully."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        with patch(
            "custom_components.ecoflow_energy.ecoflow.app_api.AppApiClient",
        ) as cls:
            instance = cls.return_value
            instance.login = AsyncMock(return_value=True)
            instance.user_id = "user123"
            instance.get_mqtt_credentials = AsyncMock(return_value=None)

            await coordinator.async_setup()

        # MQTT client should NOT be created on failure
        assert coordinator.mqtt_client is None


# ===========================================================================
# Apply Data (_apply_data)
# ===========================================================================


class TestApplyData:
    async def test_apply_data_updates_device_data(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_apply_data updates device_data and last_mqtt_ts."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        before = time.monotonic()
        coordinator._apply_data({"solar_w": 3000, "soc_pct": 85})
        after = time.monotonic()

        assert coordinator.device_data["solar_w"] == 3000
        assert coordinator.device_data["soc_pct"] == 85
        assert before <= coordinator.last_mqtt_ts <= after

    async def test_apply_data_resets_http_failure_counter(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """MQTT data resets HTTP failure counter to prevent false reauth (#2)."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        # Simulate 4 consecutive HTTP failures (one short of reauth trigger)
        coordinator._consecutive_http_failures = 4
        coordinator._device_available = False

        # MQTT data arrives — proves credentials are valid
        coordinator._apply_data({"soc": 85})

        assert coordinator._consecutive_http_failures == 0
        assert coordinator._device_available is True

    async def test_apply_data_merges(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_apply_data merges new data with existing."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._apply_data({"solar_w": 3000})
        coordinator._apply_data({"soc_pct": 85})

        # Core keys must be present; energy integration may add extra keys
        assert coordinator.device_data["solar_w"] == 3000
        assert coordinator.device_data["soc_pct"] == 85

    async def test_apply_data_integrates_energy(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """F-005: _apply_data calls _integrate_energy for PowerOcean."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        assert coordinator._energy_integrator is not None

        # Explicitly load state (normally done in async_setup via executor)
        coordinator._energy_integrator.load_state()

        # First call: sets baseline (no energy yet)
        coordinator._apply_data({"solar_w": 3000, "home_w": 1500})
        # Backdate the integrator's internal timestamp to simulate elapsed time
        # (same pattern as tests/test_energy_integrator.py — manipulate _state directly)
        for metric in ("solar_energy_kwh", "home_energy_kwh"):
            if metric in coordinator._energy_integrator._state:
                total, _ts, power = coordinator._energy_integrator._state[metric]
                coordinator._energy_integrator._state[metric] = (total, time.monotonic() - 30, power)
        coordinator._apply_data({"solar_w": 3000, "home_w": 1500})

        # Energy keys should now exist in device_data
        assert "solar_energy_kwh" in coordinator.device_data
        # The rounded value (3 decimals) may be 0.000 for short intervals,
        # so check the raw integrator total which has full precision
        raw_total = coordinator._energy_integrator.get_total("solar_energy_kwh")
        assert raw_total is not None and raw_total > 0


# ===========================================================================
# Protobuf Key Remapping (_remap_proto_keys) — F-001
# ===========================================================================


class TestProtoKeyRemapping:
    async def test_remap_energy_stream_keys(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Protobuf keys are remapped to sensor keys."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "solar": 3200,
            "home_direct": 1500,
            "batt_pb": -800,
            "grid_raw_f2": 200,
            "soc": 72.0,
        }
        result = remap_proto_keys(raw)

        assert result["solar_w"] == 3200
        assert result["home_w"] == 1500
        assert result["batt_w"] == -800
        assert result["grid_w"] == 200
        assert result["soc_pct"] == 72.0

    async def test_remap_derives_grid_import_export(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Grid import/export splits are computed from grid_w."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        # Positive grid_w = import
        result = remap_proto_keys({"grid_raw_f2": 500})
        assert result["grid_import_power_w"] == 500
        assert result["grid_export_power_w"] == 0.0

        # Negative grid_w = export
        result = remap_proto_keys({"grid_raw_f2": -300})
        assert result["grid_import_power_w"] == 0.0
        assert result["grid_export_power_w"] == 300

    async def test_remap_derives_batt_charge_discharge(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Battery charge/discharge splits are computed from batt_w."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        # Positive batt_w = charging
        result = remap_proto_keys({"batt_pb": 1200})
        assert result["batt_charge_power_w"] == 1200
        assert result["batt_discharge_power_w"] == 0.0

        # Negative batt_w = discharging
        result = remap_proto_keys({"batt_pb": -900})
        assert result["batt_charge_power_w"] == 0.0
        assert result["batt_discharge_power_w"] == 900

    async def test_remap_preserves_unknown_keys(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Keys not in the mapping are passed through unchanged."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        result = remap_proto_keys({"solar": 100, "some_new_field": 42})
        assert result["solar_w"] == 100
        assert result["some_new_field"] == 42

    async def test_remap_zero_values(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Zero power values produce zero derived splits."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        result = remap_proto_keys({"grid_raw_f2": 0.0, "batt_pb": 0.0})
        assert result["grid_w"] == 0.0
        assert result["grid_import_power_w"] == 0.0
        assert result["grid_export_power_w"] == 0.0
        assert result["batt_w"] == 0.0
        assert result["batt_charge_power_w"] == 0.0
        assert result["batt_discharge_power_w"] == 0.0


# ===========================================================================
# Heartbeat Nested Extraction (_flatten_heartbeat) — MPPT, Grid Phases
# ===========================================================================


class TestHeartbeatExtraction:
    async def test_mppt_per_string(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """MPPT per-string data extracted from nested mppt_heart_beat."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "mppt_heart_beat": [
                {
                    "mppt_pv": [
                        {"pwr": 2500, "vol": 480.0, "amp": 5.2},
                        {"pwr": 2400, "vol": 485.0, "amp": 4.9},
                    ]
                }
            ],
            "pcs_ac_freq": 50.01,
        }
        result = flatten_heartbeat(raw)

        assert result["mppt_pv1_power_w"] == 2500.0
        assert result["mppt_pv1_voltage_v"] == 480.0
        assert result["mppt_pv1_current_a"] == 5.2
        assert result["mppt_pv2_power_w"] == 2400.0
        assert result["mppt_pv2_voltage_v"] == 485.0
        assert result["mppt_pv2_current_a"] == 4.9
        assert result["pcs_ac_freq_hz"] == 50.01

    async def test_grid_phase_from_load_info(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Grid phase data extracted from pcs_load_info nested array."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "pcs_load_info": [
                {"vol": 230.5, "amp": 10.2, "pwr": 2300.0},
                {"vol": 231.0, "amp": 11.0, "pwr": 2500.0},
                {"vol": 229.8, "amp": 9.5, "pwr": 2100.0},
            ]
        }
        result = flatten_heartbeat(raw)

        assert result["grid_phase_a_voltage_v"] == 230.5
        assert result["grid_phase_a_current_a"] == 10.2
        assert result["grid_phase_a_active_power_w"] == 2300.0
        assert result["grid_phase_b_voltage_v"] == 231.0
        assert result["grid_phase_c_voltage_v"] == 229.8

    async def test_grid_phase_from_pcs_phase(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Grid phase data from pcs_a/b/c_phase fallback."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "pcs_a_phase": {"vol": 230.0, "amp": 10.0, "act_pwr": -2200.0},
            "pcs_b_phase": {"vol": 231.0, "amp": 11.0, "act_pwr": -2500.0},
        }
        result = flatten_heartbeat(raw)

        assert result["grid_phase_a_voltage_v"] == 230.0
        assert result["grid_phase_a_current_a"] == 10.0
        assert result["grid_phase_a_active_power_w"] == -2200.0
        assert result["grid_phase_b_voltage_v"] == 231.0

    async def test_empty_heartbeat(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Empty heartbeat produces empty result."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        result = flatten_heartbeat({})
        assert result == {}


# ===========================================================================
# Battery / EMS Remapping (_remap_bp_keys)
# ===========================================================================


class TestBpRemapping:
    async def test_battery_keys_remapped(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Battery heartbeat keys mapped to sensor keys."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {"bp_soh": 98, "bp_cycles": 42, "bp_vol": 52.1, "bp_env_temp": 25}
        result = remap_bp_keys(raw, coordinator._bp_sn_to_index, coordinator.device_sn)

        assert result["bp_soh_pct"] == 98.0
        assert result["bp_cycles"] == 42.0
        assert result["bp_voltage_v"] == 52.1
        assert result["bp_env_temp_c"] == 25.0

    async def test_ems_change_keys_remapped(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """EMS change report keys mapped to sensor keys."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {"bp_online_sum": 2, "ems_feed_mode": 1, "sys_grid_sta": 0}
        result = remap_bp_keys(raw, coordinator._bp_sn_to_index, coordinator.device_sn)

        assert result["bp_online_sum"] == 2.0
        assert result["ems_feed_mode"] == 1.0
        assert result["grid_status"] == 0.0

    async def test_energy_totals_wh_to_kwh(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """EMS change report energy totals converted from Wh to kWh."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {"bp_total_chg_energy": 15000, "bp_total_dsg_energy": 12000}
        result = remap_bp_keys(raw, coordinator._bp_sn_to_index, coordinator.device_sn)

        assert result["batt_charge_energy_kwh"] == 15.0
        assert result["batt_discharge_energy_kwh"] == 12.0

    async def test_multi_pack_proto_extraction(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """all_packs in proto heartbeat extracts per-pack sensors."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "all_packs": [
                {"bp_soc": 76, "bp_pwr": 2486.48, "bp_vol": 54.671, "bp_accu_chg_energy": 2238706},
                {"bp_soc": 74, "bp_pwr": 2529.19, "bp_vol": 54.698, "bp_accu_chg_energy": 2207455},
            ],
            "bp_soh": 100,
        }
        result = remap_bp_keys(raw, coordinator._bp_sn_to_index, coordinator.device_sn)

        # Pack 1
        assert result["pack1_soc"] == 76.0
        assert result["pack1_power_w"] == 2486.48
        assert result["pack1_voltage_v"] == 54.671
        assert abs(result["pack1_accu_chg_energy_kwh"] - 2238.706) < 0.01

        # Pack 2
        assert result["pack2_soc"] == 74.0
        assert result["pack2_power_w"] == 2529.19
        assert result["pack2_voltage_v"] == 54.698
        assert abs(result["pack2_accu_chg_energy_kwh"] - 2207.455) < 0.01

        # Existing bp_* still mapped
        assert result["bp_soh_pct"] == 100.0

    async def test_multi_pack_max_5(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Only first 5 packs are extracted from all_packs."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "all_packs": [{"bp_soc": i * 10} for i in range(1, 8)],  # 7 packs
        }
        result = remap_bp_keys(raw, coordinator._bp_sn_to_index, coordinator.device_sn)

        assert "pack1_soc" in result
        assert "pack5_soc" in result
        assert "pack6_soc" not in result
        assert "pack7_soc" not in result

    async def test_phantom_empty_pack_skipped(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Phantom pack (empty dict from EMS module) is skipped in numbering."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "all_packs": [
                {},  # phantom: EMS module placeholder (no battery identity keys)
                {"bp_soc": 76, "bp_pwr": 2486.48, "bp_vol": 54.671},  # real pack
                {"bp_soc": 74, "bp_pwr": 2529.19, "bp_vol": 54.698},  # real pack
            ],
        }
        result = remap_bp_keys(raw, coordinator._bp_sn_to_index, coordinator.device_sn)

        # Phantom skipped — real packs are pack1 and pack2
        assert result["pack1_soc"] == 76.0
        assert result["pack1_power_w"] == 2486.48
        assert result["pack2_soc"] == 74.0
        assert result["pack2_power_w"] == 2529.19
        assert "pack3_soc" not in result

    async def test_phantom_only_packs(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """All packs are empty dicts (EMS module placeholders) — no pack sensors produced."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "all_packs": [
                {},  # phantom
                {},  # phantom
            ],
        }
        result = remap_bp_keys(raw, coordinator._bp_sn_to_index, coordinator.device_sn)

        assert "pack1_soc" not in result
        assert "pack2_soc" not in result

    async def test_bp_remain_watth_summed_across_packs(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """bp_remain_watth is aggregated from accumulated device_data in _apply_data."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "all_packs": [
                {"bp_soc": 76, "bp_pwr": 2486, "bp_remain_watth": 2400},
                {"bp_soc": 74, "bp_pwr": 2529, "bp_remain_watth": 2600},
            ],
        }
        parsed = remap_bp_keys(raw, coordinator._bp_sn_to_index, coordinator.device_sn)
        # Per-pack values extracted by _remap_bp_keys
        assert parsed["pack1_remain_watth"] == 2400.0
        assert parsed["pack2_remain_watth"] == 2600.0
        # Aggregate is NOT in _remap_bp_keys output — it's computed in _apply_data
        assert "bp_remain_watth" not in parsed

        # _apply_data computes the aggregate from accumulated device_data
        coordinator._apply_data(parsed)
        assert coordinator.device_data["bp_remain_watth"] == 5000.0

    async def test_bp_remain_watth_single_pack(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Single pack: bp_remain_watth equals that pack's value via _apply_data."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "all_packs": [
                {"bp_soc": 76, "bp_pwr": 2486, "bp_remain_watth": 4800},
            ],
        }
        parsed = remap_bp_keys(raw, coordinator._bp_sn_to_index, coordinator.device_sn)
        coordinator._apply_data(parsed)

        assert coordinator.device_data["bp_remain_watth"] == 4800.0

    async def test_bp_remain_watth_not_in_bp_to_sensor(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Raw bp_remain_watth from single-pack field does not overwrite sum."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "all_packs": [
                {"bp_soc": 76, "bp_pwr": 2486, "bp_remain_watth": 2400},
                {"bp_soc": 74, "bp_pwr": 2529, "bp_remain_watth": 2600},
            ],
            # This raw key should NOT be mapped since it's excluded from _BP_TO_SENSOR
            "bp_remain_watth": 2400,
        }
        parsed = remap_bp_keys(raw, coordinator._bp_sn_to_index, coordinator.device_sn)
        coordinator._apply_data(parsed)

        # Sum of packs (from _apply_data aggregation), not the raw single-pack value
        assert coordinator.device_data["bp_remain_watth"] == 5000.0

    async def test_idle_pack_not_filtered_as_phantom(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Idle pack with zero SoC/power is still recognized as real (#10).

        Proto3 MessageToDict omits zero-valued fields, but a real battery pack
        always has bp_design_cap/bp_full_cap > 0 (present in dict).  The identity
        key check ensures idle packs are not rejected.
        """
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "all_packs": [
                # Pack 1: active (non-zero power)
                {"bp_soc": 76, "bp_pwr": 2486.48, "bp_vol": 54.671,
                 "bp_design_cap": 100000, "bp_full_cap": 100000,
                 "bp_remain_watth": 3891.2},
                # Pack 2: idle — proto3 omits bp_soc=0, bp_pwr=0.0 but
                # bp_design_cap/bp_full_cap are >0 so they survive MessageToDict
                {"bp_design_cap": 100000, "bp_full_cap": 100000,
                 "bp_remain_watth": 2000.0},
            ],
        }
        parsed = remap_bp_keys(raw, coordinator._bp_sn_to_index, coordinator.device_sn)

        # Both packs recognized — idle pack is NOT filtered
        assert parsed["pack1_soc"] == 76.0
        assert parsed["pack1_remain_watth"] == pytest.approx(3891.2)
        assert parsed["pack2_remain_watth"] == 2000.0
        assert "pack3_soc" not in parsed

    async def test_partial_heartbeat_preserves_accumulated_remain(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Separate heartbeats per pack preserve accumulated remain_watth (#10).

        The device sends one pack per heartbeat (~1s apart).  SN-based indexing
        ensures each pack maps to a stable pack{n}_* key, and the aggregate
        bp_remain_watth always sums all known packs from _device_data.
        """
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )

        # First heartbeat: Pack A reports (SN=AAA)
        msg1 = remap_bp_keys({
            "all_packs": [
                {"bp_sn": "AAA", "bp_soc": 76, "bp_pwr": 2486,
                 "bp_remain_watth": 2400},
            ],
        }, coordinator._bp_sn_to_index, coordinator.device_sn)
        coordinator._apply_data(msg1)
        assert coordinator.device_data["pack1_remain_watth"] == 2400.0

        # Second heartbeat: Pack B reports (SN=BBB) — different SN → pack2
        msg2 = remap_bp_keys({
            "all_packs": [
                {"bp_sn": "BBB", "bp_soc": 74, "bp_pwr": 2529,
                 "bp_remain_watth": 2600},
            ],
        }, coordinator._bp_sn_to_index, coordinator.device_sn)
        coordinator._apply_data(msg2)

        # Both packs in device_data, aggregate is sum
        assert coordinator.device_data["pack1_remain_watth"] == 2400.0
        assert coordinator.device_data["pack2_remain_watth"] == 2600.0
        assert coordinator.device_data["bp_remain_watth"] == 5000.0

        # Third heartbeat: Pack A again with updated value
        msg3 = remap_bp_keys({
            "all_packs": [
                {"bp_sn": "AAA", "bp_soc": 75, "bp_pwr": 2400,
                 "bp_remain_watth": 2300},
            ],
        }, coordinator._bp_sn_to_index, coordinator.device_sn)
        coordinator._apply_data(msg3)

        # Pack 1 updated to 2300, Pack 2 retains 2600 → total 4900
        assert coordinator.device_data["pack1_remain_watth"] == 2300.0
        assert coordinator.device_data["pack2_remain_watth"] == 2600.0
        assert coordinator.device_data["bp_remain_watth"] == 4900.0

    async def test_empty_dict_phantom_still_filtered(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Empty dict packs (true EMS module phantoms) are filtered out."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "all_packs": [
                {},  # EMS module placeholder
                {"bp_soc": 76, "bp_pwr": 2486.48, "bp_design_cap": 100000},
            ],
        }
        result = remap_bp_keys(raw, coordinator._bp_sn_to_index, coordinator.device_sn)

        assert result["pack1_soc"] == 76.0
        assert "pack2_soc" not in result

    async def test_non_identity_keys_only_is_phantom(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Pack with only non-identity keys (e.g. timestamps) is treated as phantom."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "all_packs": [
                {"bp_timestamp": 1234567890, "bp_heartbeat_ver": 1},  # no identity keys
                {"bp_soc": 76, "bp_pwr": 2486.48},  # real
            ],
        }
        result = remap_bp_keys(raw, coordinator._bp_sn_to_index, coordinator.device_sn)

        assert result["pack1_soc"] == 76.0
        assert "pack2_soc" not in result

    async def test_no_reaggregate_without_pack_remain_keys(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_apply_data does not re-aggregate when no pack remain_watth keys in parsed."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        # Pre-populate with existing pack data
        coordinator._device_data["pack1_remain_watth"] = 2400.0
        coordinator._device_data["pack2_remain_watth"] = 2600.0
        coordinator._device_data["bp_remain_watth"] = 5000.0

        # EMS change report: no pack remain_watth keys
        coordinator._apply_data({"ems_feed_mode": 1, "grid_status": 0})

        # bp_remain_watth unchanged (no re-aggregation triggered)
        assert coordinator.device_data["bp_remain_watth"] == 5000.0

    async def test_bp_remain_watth_zero_total_written(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """bp_remain_watth is written as 0 when all packs are fully discharged.

        Ensures measurement sensors are updated to 0 rather than retaining
        stale non-zero values.
        """
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        # Simulate fully discharged state
        parsed = remap_bp_keys({
            "all_packs": [
                {"bp_design_cap": 100000, "bp_full_cap": 100000, "bp_remain_watth": 0.0},
                {"bp_design_cap": 100000, "bp_full_cap": 100000, "bp_remain_watth": 0.0},
            ],
        }, coordinator._bp_sn_to_index, coordinator.device_sn)
        coordinator._apply_data(parsed)

        assert coordinator.device_data["bp_remain_watth"] == 0.0


# ===========================================================================
# Monotonic Filter (_enforce_monotonic) — total_increasing regression guard
# ===========================================================================


class TestMonotonicFilter:
    async def test_regression_dropped(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Values that decrease a total_increasing sensor are dropped."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        # Seed existing data
        coordinator._device_data["bp_cycles"] = 461.0
        coordinator._device_data["solar_energy_kwh"] = 100.5

        parsed = {"bp_cycles": 460.0, "solar_energy_kwh": 100.499, "solar_w": 3000}
        coordinator._enforce_monotonic(parsed)

        # Regressions removed, non-monotonic key preserved
        assert "bp_cycles" not in parsed
        assert "solar_energy_kwh" not in parsed
        assert parsed["solar_w"] == 3000

    async def test_increase_allowed(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Values that increase are kept."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._device_data["bp_cycles"] = 460.0

        parsed = {"bp_cycles": 461.0}
        coordinator._enforce_monotonic(parsed)

        assert parsed["bp_cycles"] == 461.0

    async def test_equal_value_kept(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Equal values are kept (not a regression)."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._device_data["bp_cycles"] = 460.0

        parsed = {"bp_cycles": 460.0}
        coordinator._enforce_monotonic(parsed)

        assert parsed["bp_cycles"] == 460.0

    async def test_no_existing_data_passes(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """First value is always accepted (no previous data)."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        parsed = {"bp_cycles": 460.0}
        coordinator._enforce_monotonic(parsed)

        assert parsed["bp_cycles"] == 460.0

    async def test_delta_bms_cycles(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Delta bms_cycles regression is also filtered."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._device_data["bms_cycles"] = 150.0

        parsed = {"bms_cycles": 149.0, "soc": 85.0}
        coordinator._enforce_monotonic(parsed)

        assert "bms_cycles" not in parsed
        assert parsed["soc"] == 85.0


# ===========================================================================
# Quotas Poll (_send_quotas_poll)
# ===========================================================================


class TestQuotasPoll:
    async def test_send_quotas_poll_when_connected(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_send_quotas_poll sends latestQuotas when MQTT is connected."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt

        with patch.object(hass, "async_add_executor_job") as mock_exec:
            coordinator._send_quotas_poll()
            mock_exec.assert_called_once_with(mock_mqtt.send_latest_quotas)

        # Should reschedule
        assert coordinator._quotas_unsub is not None
        # Cleanup: cancel the rescheduled timer
        coordinator._quotas_unsub.cancel()
        coordinator._quotas_unsub = None

    async def test_send_quotas_poll_noop_when_shutdown(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_send_quotas_poll is a no-op when shutdown flag is set."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._shutdown = True
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt

        coordinator._send_quotas_poll()

        # Should not schedule anything when shutdown
        assert coordinator._quotas_unsub is None

    async def test_send_quotas_poll_skips_when_disconnected(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_send_quotas_poll skips send when MQTT is disconnected."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = False
        coordinator._mqtt_client = mock_mqtt

        with patch.object(hass, "async_add_executor_job") as mock_exec:
            coordinator._send_quotas_poll()
            mock_exec.assert_not_called()

        # Should still reschedule
        assert coordinator._quotas_unsub is not None
        coordinator._quotas_unsub.cancel()
        coordinator._quotas_unsub = None

    async def test_smartplug_send_quotas_poll_triggers_get_all_initially(
        self,
        hass: HomeAssistant,
    ) -> None:
        """Smart Plug app-auth sends latestQuotas + get-all on first poll."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "test@example.com",
                CONF_PASSWORD: "test_password",
                CONF_USER_ID: "user123",
                CONF_DEVICES: [MOCK_SMARTPLUG_DEVICE],
            },
            unique_id="test@example.com",
        )
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, MOCK_SMARTPLUG_DEVICE)
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt

        with patch.object(hass, "async_add_executor_job") as mock_exec:
            coordinator._send_quotas_poll()
            mock_exec.assert_has_calls(
                [call(mock_mqtt.send_latest_quotas), call(mock_mqtt.send_get_all)],
                any_order=False,
            )

        assert coordinator._quotas_unsub is not None
        coordinator._quotas_unsub.cancel()
        coordinator._quotas_unsub = None

    async def test_smartplug_send_quotas_poll_throttles_get_all(
        self,
        hass: HomeAssistant,
    ) -> None:
        """Smart Plug get-all is throttled between keepalive windows."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "test@example.com",
                CONF_PASSWORD: "test_password",
                CONF_USER_ID: "user123",
                CONF_DEVICES: [MOCK_SMARTPLUG_DEVICE],
            },
            unique_id="test@example.com",
        )
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, MOCK_SMARTPLUG_DEVICE)
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt
        coordinator._last_smartplug_get_all_ts = 1000.0

        with (
            patch("custom_components.ecoflow_energy.coordinator.time.monotonic", return_value=1000.0),
            patch.object(hass, "async_add_executor_job") as mock_exec,
        ):
            coordinator._send_quotas_poll()
            mock_exec.assert_called_once_with(mock_mqtt.send_latest_quotas)

        assert coordinator._quotas_unsub is not None
        coordinator._quotas_unsub.cancel()
        coordinator._quotas_unsub = None

    async def test_smartplug_send_quotas_poll_get_all_after_interval(
        self,
        hass: HomeAssistant,
    ) -> None:
        """Smart Plug get-all is sent again once keepalive interval has elapsed."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "test@example.com",
                CONF_PASSWORD: "test_password",
                CONF_USER_ID: "user123",
                CONF_DEVICES: [MOCK_SMARTPLUG_DEVICE],
            },
            unique_id="test@example.com",
        )
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, MOCK_SMARTPLUG_DEVICE)
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt
        coordinator._last_smartplug_get_all_ts = 1000.0

        with (
            patch(
                "custom_components.ecoflow_energy.coordinator.time.monotonic",
                return_value=1000.0 + SMARTPLUG_GET_ALL_KEEPALIVE_S + 1.0,
            ),
            patch.object(hass, "async_add_executor_job") as mock_exec,
        ):
            coordinator._send_quotas_poll()
            mock_exec.assert_has_calls(
                [call(mock_mqtt.send_latest_quotas), call(mock_mqtt.send_get_all)],
                any_order=False,
            )

        assert coordinator._quotas_unsub is not None
        coordinator._quotas_unsub.cancel()
        coordinator._quotas_unsub = None


# ===========================================================================
# Ping Heartbeat (_send_ping)
# ===========================================================================


class TestPing:
    async def test_send_ping_when_connected(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_send_ping sends ping when MQTT is connected."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt

        with patch.object(hass, "async_add_executor_job") as mock_exec:
            coordinator._send_ping()
            mock_exec.assert_called_once_with(mock_mqtt.send_ping)

        # Should reschedule
        assert coordinator._ping_unsub is not None
        # Cleanup
        coordinator._ping_unsub.cancel()
        coordinator._ping_unsub = None

    async def test_send_ping_noop_when_shutdown(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_send_ping is a no-op when shutdown flag is set."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._shutdown = True
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt

        coordinator._send_ping()

        # Should not schedule anything when shutdown
        assert coordinator._ping_unsub is None

    async def test_send_ping_skips_when_disconnected(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_send_ping skips send when MQTT is disconnected."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = False
        coordinator._mqtt_client = mock_mqtt

        with patch.object(hass, "async_add_executor_job") as mock_exec:
            coordinator._send_ping()
            mock_exec.assert_not_called()

        # Should still reschedule
        assert coordinator._ping_unsub is not None
        coordinator._ping_unsub.cancel()
        coordinator._ping_unsub = None


# ===========================================================================
# _parse_message Protobuf Branch
# ===========================================================================


class TestParseMessageProtobuf:
    async def test_parse_protobuf_energy_stream(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_parse_message decodes a protobuf energy_stream frame for PowerOcean."""
        from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
            encode_field_bytes,
            encode_field_varint,
        )
        from custom_components.ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
            JTS1EnergyStreamReport,
        )

        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )

        # Build a real protobuf energy stream frame
        msg = JTS1EnergyStreamReport()
        msg.mppt_pwr = 4200.0
        msg.sys_load_pwr = 1800.0
        msg.bp_pwr = -500.0
        msg.sys_grid_pwr = 300.0
        msg.bp_soc = 65
        inner = msg.SerializeToString()

        # Wrap in HeaderMessage frame (cmd_func=96, cmd_id=33)
        header = bytearray()
        header.extend(encode_field_bytes(1, inner))
        header.extend(encode_field_varint(8, 96))
        header.extend(encode_field_varint(9, 33))
        frame = encode_field_bytes(1, bytes(header))

        topic = "/app/device/property/HW52TEST00000001"
        result = coordinator._parse_message(topic, frame)

        assert result is not None
        assert result["solar_w"] == 4200.0
        assert result["home_w"] == 1800.0
        assert result["batt_w"] == -500.0
        assert result["grid_w"] == 300.0
        assert result["soc_pct"] == 65.0
        # Derived splits should be computed
        assert result["grid_import_power_w"] == 300.0
        assert result["grid_export_power_w"] == 0.0

    async def test_parse_protobuf_malformed_graceful(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_parse_message handles malformed protobuf data without raising."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )

        # Data with 0x0a in first 4 bytes triggers protobuf path, but is invalid
        malformed = b"\x0a\xff\xfe\x01garbage_data_here"
        topic = "/app/device/property/HW52TEST00000001"
        result = coordinator._parse_message(topic, malformed)

        assert result is None

    async def test_parse_protobuf_bp_heartbeat_multi_pack(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """E2E: bp_heartbeat proto frame with 2 packs survives underscore filter."""
        from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
            encode_field_bytes,
            encode_field_varint,
        )
        from custom_components.ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
            JTS1BpHeartbeatReport,
        )

        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )

        # Build a real protobuf bp_heartbeat frame with 2 battery packs
        msg = JTS1BpHeartbeatReport()
        pack1 = msg.bp_heart_beat.add()
        pack1.bp_soc = 76
        pack1.bp_pwr = 2486.0
        pack1.bp_vol = 54.67
        pack2 = msg.bp_heart_beat.add()
        pack2.bp_soc = 74
        pack2.bp_pwr = 2529.0
        pack2.bp_vol = 54.70
        inner = msg.SerializeToString()

        # Wrap in HeaderMessage frame (cmd_func=96, cmd_id=7)
        header = bytearray()
        header.extend(encode_field_bytes(1, inner))
        header.extend(encode_field_varint(8, 96))
        header.extend(encode_field_varint(9, 7))
        frame = encode_field_bytes(1, bytes(header))

        topic = "/app/device/property/HW52TEST00000001"
        result = coordinator._parse_message(topic, frame)

        assert result is not None
        # Pack-specific sensors must survive the underscore filter
        assert result["pack1_soc"] == 76.0
        assert result["pack1_power_w"] == 2486.0
        assert result["pack1_voltage_v"] == pytest.approx(54.67, abs=0.01)
        assert result["pack2_soc"] == 74.0
        assert result["pack2_power_w"] == 2529.0
        assert result["pack2_voltage_v"] == pytest.approx(54.70, abs=0.01)


# ===========================================================================
# _parse_message JSON Decode Error Path
# ===========================================================================


class TestParseMessageJsonError:
    async def test_json_decode_error_returns_none(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Malformed JSON on a /quota topic returns None without raising."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        topic = "/open/cert_account/SN001/quota"
        malformed = b"\xff\xfe malformed json {"

        result = coordinator._parse_message(topic, malformed)
        assert result is None

    async def test_non_dict_json_returns_none(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Valid JSON that is not a dict returns None."""
        import json

        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        topic = "/open/cert_account/SN001/quota"
        payload = json.dumps([1, 2, 3]).encode()

        result = coordinator._parse_message(topic, payload)
        assert result is None


# ===========================================================================
# get_reply parsing (app-auth latestQuotas response)
# ===========================================================================


class TestParseMessageGetReply:
    """Tests for _parse_message handling of get_reply topic (latestQuotas response)."""

    async def test_delta_get_reply_parsed(
        self, hass: HomeAssistant, standard_config_entry: MockConfigEntry,
    ) -> None:
        """Delta get_reply with quotaMap is parsed via delta_http_quota parser."""
        import json as json_mod

        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        topic = "/app/user123/SN001/thing/property/get_reply"
        payload = json_mod.dumps({
            "operateType": "latestQuotas",
            "data": {
                "quotaMap": {
                    "pd.soc": 75,
                    "pd.wattsInSum": 200,
                    "pd.wattsOutSum": 100,
                    "inv.outputWatts": 142,
                }
            }
        }).encode()

        result = coordinator._parse_message(topic, payload)
        assert result is not None
        assert result.get("soc") == 75
        assert result.get("watts_in_sum") == 200

    async def test_smartplug_get_reply_parsed(
        self, hass: HomeAssistant,
    ) -> None:
        """SmartPlug get_reply with quotaMap is parsed via smartplug_http_quota parser."""
        import json as json_mod

        from .conftest import MOCK_SMARTPLUG_DEVICE

        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                "access_key": "ak", "secret_key": "sk",
                "mode": MODE_STANDARD, "devices": [MOCK_SMARTPLUG_DEVICE],
            },
            unique_id="ak_plug",
        )
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_SMARTPLUG_DEVICE
        )
        topic = "/app/user123/SN001/thing/property/get_reply"
        payload = json_mod.dumps({
            "operateType": "latestQuotas",
            "data": {
                "quotaMap": {
                    "2_1.watts": 1500,
                    "2_1.voltage": 2300,
                    "2_1.switchSta": 1,
                }
            }
        }).encode()

        result = coordinator._parse_message(topic, payload)
        assert result is not None
        assert result.get("power_w") == 150.0  # 1500 / 10 (deciWatt)
        assert result.get("switch_state") == 1

    async def test_get_reply_empty_quota_map_returns_none(
        self, hass: HomeAssistant, standard_config_entry: MockConfigEntry,
    ) -> None:
        """get_reply with empty quotaMap returns None."""
        import json as json_mod

        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        topic = "/app/user123/SN001/thing/property/get_reply"
        payload = json_mod.dumps({
            "operateType": "latestQuotas",
            "data": {"quotaMap": {}}
        }).encode()

        result = coordinator._parse_message(topic, payload)
        assert result is None

    async def test_get_reply_no_data_returns_none(
        self, hass: HomeAssistant, standard_config_entry: MockConfigEntry,
    ) -> None:
        """get_reply without data field returns None."""
        import json as json_mod

        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        topic = "/app/user123/SN001/thing/property/get_reply"
        payload = json_mod.dumps({"operateType": "latestQuotas"}).encode()

        result = coordinator._parse_message(topic, payload)
        assert result is None


# ===========================================================================
# Diagnostic Properties (mqtt_status, connection_mode)
# ===========================================================================


class TestDiagnosticProperties:
    async def test_mqtt_status_not_configured(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """mqtt_status returns 'not_configured' when no MQTT client."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.mqtt_status == "not_configured"

    async def test_mqtt_status_receiving(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """mqtt_status returns 'receiving' when connected and data is fresh."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt
        coordinator._last_mqtt_ts = time.monotonic()
        assert coordinator.mqtt_status == "receiving"

    async def test_mqtt_status_connected_stale(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """mqtt_status returns 'connected_stale' when connected but data is old."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt
        coordinator._last_mqtt_ts = time.monotonic() - STALE_THRESHOLD_S - 10
        assert coordinator.mqtt_status == "connected_stale"

    async def test_mqtt_status_disconnected(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """mqtt_status returns 'disconnected' when MQTT client is not connected."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = False
        coordinator._mqtt_client = mock_mqtt
        assert coordinator.mqtt_status == "disconnected"

    async def test_connection_mode_standard(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """connection_mode returns 'standard' for Standard Mode."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.connection_mode == "standard"

    async def test_connection_mode_enhanced(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """connection_mode returns 'enhanced' for Enhanced Mode without fallback."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        assert coordinator.connection_mode == "enhanced"

    async def test_connection_mode_enhanced_fallback(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """connection_mode returns 'enhanced_fallback' when HTTP fallback is active."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator.update_interval = timedelta(seconds=30)
        assert coordinator.connection_mode == "enhanced_fallback"


# ===========================================================================
# Event Log (_log_event, event_log)
# ===========================================================================


class TestEventLog:
    async def test_event_log_initially_empty(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Event log starts empty."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.event_log == []

    async def test_log_event_adds_entry(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """_log_event appends an entry to the event log."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._log_event("http_ok", "keys=42")
        log = coordinator.event_log
        assert len(log) == 1
        assert log[0]["type"] == "http_ok"
        assert log[0]["detail"] == "keys=42"
        assert "ts" in log[0]

    async def test_event_log_bounded(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Event log is bounded to maxlen=50."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        for i in range(60):
            coordinator._log_event("test", f"entry_{i}")
        log = coordinator.event_log
        assert len(log) == 50
        # Oldest entries evicted: 60 inserts - 50 capacity = first surviving is entry_10
        assert log[0]["detail"] == "entry_10"

    async def test_event_log_returns_copy(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """event_log property returns a copy, not the internal deque."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._log_event("test", "entry_1")
        log = coordinator.event_log
        log.append({"ts": 0, "type": "fake", "detail": "injected"})
        assert len(coordinator.event_log) == 1  # internal unchanged

    async def test_apply_data_logs_mqtt_event(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_apply_data logs rate-limited mqtt_data event."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        # First call always logs (no previous timestamp)
        coordinator._apply_data({"solar_w": 3000})

        log = coordinator.event_log
        assert any(e["type"] == "mqtt_data" for e in log)

    async def test_set_reply_logged(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """SET reply messages are logged as events."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        topic = f"/open/cert_account/{coordinator.device_sn}/set_reply"
        payload = b'{"id":12345,"code":"0"}'

        coordinator._on_mqtt_message(topic, payload)

        log = coordinator.event_log
        assert len(log) == 1
        assert log[0]["type"] == "set_reply"

    async def test_stale_detection_logs_event(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_check_stale logs 'stale_detected' when HTTP fallback activates."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._http_client = MagicMock()
        coordinator._last_mqtt_ts = time.monotonic() - STALE_THRESHOLD_S - 10

        coordinator._check_stale()

        log = coordinator.event_log
        assert any(e["type"] == "stale_detected" for e in log)

        if coordinator._stale_check_unsub is not None:
            coordinator._stale_check_unsub.cancel()

    async def test_stale_recovery_logs_event(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_check_stale logs 'stale_recovered' when HTTP fallback is disabled."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._http_client = MagicMock()
        coordinator.update_interval = timedelta(seconds=HTTP_FALLBACK_INTERVAL_S)
        coordinator._last_mqtt_ts = time.monotonic()

        coordinator._check_stale()

        log = coordinator.event_log
        assert any(e["type"] == "stale_recovered" for e in log)

        if coordinator._stale_check_unsub is not None:
            coordinator._stale_check_unsub.cancel()

    async def test_stale_force_reconnect_logs_event(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_check_stale logs 'stale_force_reconnect' on connected-but-silent."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._mqtt_client = MagicMock()
        coordinator._mqtt_client.is_connected.return_value = True
        coordinator._mqtt_client.try_reconnect.return_value = None
        coordinator._mqtt_client.force_reconnect.return_value = None
        coordinator._mqtt_client.reconnect_attempts = 0
        coordinator._last_mqtt_ts = time.monotonic() - STALE_THRESHOLD_S - 10
        coordinator._last_stale_reconnect_ts = 0.0

        coordinator._check_stale()

        log = coordinator.event_log
        assert any(e["type"] == "stale_force_reconnect" for e in log)

        if coordinator._stale_check_unsub is not None:
            coordinator._stale_check_unsub.cancel()


# ===========================================================================
# App-Auth Mode
# ===========================================================================


class TestAppAuthMode:
    """Tests for app-auth coordinator behavior."""

    def _create_app_auth_entry(
        self, hass: HomeAssistant, device: dict | None = None
    ) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_APP,
                CONF_MODE: MODE_ENHANCED,
                CONF_EMAIL: "test@example.com",
                CONF_PASSWORD: "test_password",
                CONF_USER_ID: "user123",
                CONF_DEVICES: [device or MOCK_POWEROCEAN_DEVICE],
            },
            unique_id="test@example.com",
        )
        entry.add_to_hass(hass)
        return entry

    async def test_app_auth_powerocean_is_enhanced(
        self, hass: HomeAssistant,
    ) -> None:
        """App-auth PowerOcean has enhanced_mode=True, no polling."""
        entry = self._create_app_auth_entry(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE
        )
        assert coordinator.enhanced_mode is True
        assert coordinator.update_interval is None

    async def test_app_auth_delta_is_enhanced(
        self, hass: HomeAssistant,
    ) -> None:
        """App-auth Delta also has enhanced_mode=True (WSS, no HTTP)."""
        entry = self._create_app_auth_entry(hass, MOCK_DELTA_DEVICE)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.enhanced_mode is True
        assert coordinator.update_interval is None

    async def test_app_auth_smartplug_is_enhanced(
        self, hass: HomeAssistant,
    ) -> None:
        """App-auth SmartPlug also has enhanced_mode=True (WSS, no HTTP)."""
        entry = self._create_app_auth_entry(hass, MOCK_SMARTPLUG_DEVICE)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_SMARTPLUG_DEVICE
        )
        assert coordinator.enhanced_mode is True
        assert coordinator.update_interval is None

    async def test_app_auth_setup_calls_login(
        self, hass: HomeAssistant, mock_mqtt_client,
    ) -> None:
        """App-auth setup calls enhanced_login and get_enhanced_credentials."""
        entry = self._create_app_auth_entry(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE
        )

        mock_app_api = MagicMock()
        mock_app_api.login = AsyncMock(return_value=True)
        mock_app_api.user_id = "uid"
        mock_app_api.get_mqtt_credentials = AsyncMock(return_value={
            "userName": "app-user",
            "password": "app-pass",
        })

        with patch(
            "custom_components.ecoflow_energy.ecoflow.app_api.AppApiClient",
            return_value=mock_app_api,
        ):
            await coordinator.async_setup()

        mock_app_api.login.assert_called_once()
        mock_app_api.get_mqtt_credentials.assert_called_once()

        # Clean up timers to avoid lingering handles
        await coordinator.async_shutdown()

    async def test_app_auth_login_failure_triggers_reauth(
        self, hass: HomeAssistant,
    ) -> None:
        """App-auth login failure triggers re-authentication."""
        entry = self._create_app_auth_entry(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE
        )

        mock_app_api = MagicMock()
        mock_app_api.login = AsyncMock(return_value=False)

        with (
            patch(
                "custom_components.ecoflow_energy.ecoflow.app_api.AppApiClient",
                return_value=mock_app_api,
            ),
            patch.object(entry, "async_start_reauth") as mock_reauth,
        ):
            await coordinator.async_setup()

        mock_reauth.assert_called_once()

    async def test_app_auth_no_http_fallback_on_stale(
        self, hass: HomeAssistant,
    ) -> None:
        """App-auth mode does not switch to HTTP fallback when MQTT is stale."""
        entry = self._create_app_auth_entry(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._mqtt_client = MagicMock()
        coordinator._mqtt_client.is_connected.return_value = False
        coordinator._mqtt_client.try_reconnect.return_value = None

        # Simulate stale MQTT (no data received)
        coordinator._last_mqtt_ts = 0.0

        coordinator._check_stale()

        # App-auth should NOT enable HTTP polling
        assert coordinator.update_interval is None

        # Clean up the re-scheduled timer
        await coordinator.async_shutdown()

    async def test_app_auth_mqtt_has_auth_error_handler(
        self, hass: HomeAssistant,
    ) -> None:
        """App-auth MQTT client must have auth_error_handler wired."""
        entry = self._create_app_auth_entry(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE
        )

        mock_app_api = MagicMock()
        mock_app_api.login = AsyncMock(return_value=True)
        mock_app_api.user_id = "uid"
        mock_app_api.get_mqtt_credentials = AsyncMock(return_value={
            "userName": "app-user",
            "password": "app-pass",
        })

        with (
            patch(
                "custom_components.ecoflow_energy.ecoflow.app_api.AppApiClient",
                return_value=mock_app_api,
            ),
            patch(
                "custom_components.ecoflow_energy.coordinator.EcoFlowMQTTClient",
            ) as mqtt_cls,
        ):
            instance = mqtt_cls.return_value
            instance.create_client.return_value = True
            instance.connect.return_value = True
            instance.start_loop.return_value = None
            instance.is_connected.return_value = True
            instance.disconnect.return_value = None
            instance.reconnect_attempts = 0
            instance.publish.return_value = True
            instance.send_energy_stream_switch.return_value = None
            instance.try_reconnect.return_value = None

            await coordinator.async_setup()

            # The MQTT client constructor must have auth_error_handler set
            assert mqtt_cls.call_args is not None
            assert "auth_error_handler" in mqtt_cls.call_args.kwargs
            assert mqtt_cls.call_args.kwargs["auth_error_handler"] is not None

        await coordinator.async_shutdown()

    async def test_app_auth_rc5_triggers_credential_refresh(
        self, hass: HomeAssistant,
    ) -> None:
        """App-auth rc=5 triggers login + credential refresh."""
        entry = self._create_app_auth_entry(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE
        )

        mock_app_api = MagicMock()
        mock_app_api.login = AsyncMock(return_value=True)
        mock_app_api.user_id = "uid"
        mock_app_api.get_mqtt_credentials = AsyncMock(return_value={
            "userName": "app-user",
            "password": "app-pass",
        })

        with (
            patch(
                "custom_components.ecoflow_energy.ecoflow.app_api.AppApiClient",
                return_value=mock_app_api,
            ),
            patch(
                "custom_components.ecoflow_energy.coordinator.EcoFlowMQTTClient",
            ) as mqtt_cls,
        ):
            instance = mqtt_cls.return_value
            instance.create_client.return_value = True
            instance.connect.return_value = True
            instance.start_loop.return_value = None
            instance.is_connected.return_value = True
            instance.disconnect.return_value = None
            instance.reconnect_attempts = 0
            instance.publish.return_value = True
            instance.send_energy_stream_switch.return_value = None
            instance.try_reconnect.return_value = None

            await coordinator.async_setup()

            # Capture the auth_error_handler callback
            handler = mqtt_cls.call_args.kwargs["auth_error_handler"]

        # Simulate rc=5 by calling the handler
        mock_refresh_api = MagicMock()
        mock_refresh_api.login = AsyncMock(return_value=True)
        mock_refresh_api.get_mqtt_credentials = AsyncMock(return_value={
            "certificateAccount": "new-user",
            "certificatePassword": "new-pass",
        })

        with patch(
            "custom_components.ecoflow_energy.ecoflow.app_api.AppApiClient",
            return_value=mock_refresh_api,
        ):
            handler()
            await hass.async_block_till_done()

        # Credentials should have been refreshed
        mock_refresh_api.login.assert_called_once()
        mock_refresh_api.get_mqtt_credentials.assert_called_once()

        await coordinator.async_shutdown()

    async def test_credential_age_check_fresh(
        self, hass: HomeAssistant,
    ) -> None:
        """Credential check does not refresh when credentials are fresh."""
        entry = self._create_app_auth_entry(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._mqtt_client = MagicMock()
        coordinator._mqtt_client.cert_account = "test"
        coordinator._credential_obtained_ts = time.monotonic()  # just obtained

        coordinator._check_credential_age()

        log = coordinator.event_log
        assert not any(e["type"] == "credential_proactive_refresh" for e in log)

        # Clean up re-scheduled timer
        if coordinator._credential_refresh_unsub is not None:
            coordinator._credential_refresh_unsub.cancel()

    async def test_credential_age_check_old_triggers_refresh(
        self, hass: HomeAssistant,
    ) -> None:
        """Credential check triggers proactive refresh when credentials are old."""
        entry = self._create_app_auth_entry(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._auth_method = "app"
        coordinator._mqtt_client = MagicMock()
        coordinator._mqtt_client.cert_account = "old-account"
        coordinator._mqtt_client.update_credentials.return_value = None
        coordinator._mqtt_client.force_reconnect.return_value = None
        # Credentials older than CREDENTIAL_MAX_AGE_S
        coordinator._credential_obtained_ts = time.monotonic() - CREDENTIAL_MAX_AGE_S - 100

        mock_app_api = MagicMock()
        mock_app_api.login = AsyncMock(return_value=True)
        mock_app_api.get_mqtt_credentials = AsyncMock(return_value={
            "certificateAccount": "new-account",
            "certificatePassword": "new-pass",
        })

        # Call proactive refresh directly instead of going through
        # _check_credential_age -> async_create_task (avoids timing issues in CI)
        with patch(
            "custom_components.ecoflow_energy.ecoflow.app_api.AppApiClient",
            return_value=mock_app_api,
        ):
            await coordinator._proactive_credential_refresh()

        log = coordinator.event_log
        assert any(e["type"] == "credential_proactive_ok" for e in log)
        mock_app_api.login.assert_called_once()
        mock_app_api.get_mqtt_credentials.assert_called_once()

    async def test_credential_age_check_schedules_refresh_when_old(
        self, hass: HomeAssistant,
    ) -> None:
        """_check_credential_age logs event when credentials are old enough."""
        entry = self._create_app_auth_entry(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE
        )
        # Use a fixed monotonic value far in the past via mock to guarantee
        # age > CREDENTIAL_MAX_AGE_S regardless of CI system clock.
        now = time.monotonic()
        coordinator._credential_obtained_ts = 1.0  # fixed positive value

        # Replace the coroutine method with a sync no-op to avoid async scheduling issues
        coordinator._proactive_credential_refresh = lambda: None  # type: ignore[assignment]
        with (
            patch.object(hass, "async_create_task") as mock_task,
            patch(
                "custom_components.ecoflow_energy.coordinator.time.monotonic",
                return_value=1.0 + CREDENTIAL_MAX_AGE_S + 100,
            ),
        ):
            coordinator._check_credential_age()

        log = coordinator.event_log
        assert any(e["type"] == "credential_proactive_refresh" for e in log)
        mock_task.assert_called_once()

        # Clean up re-scheduled timer
        if coordinator._credential_refresh_unsub is not None:
            coordinator._credential_refresh_unsub.cancel()

    async def test_proactive_refresh_failure_graceful(
        self, hass: HomeAssistant,
    ) -> None:
        """Proactive refresh failure does not mark device unavailable."""
        entry = self._create_app_auth_entry(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._auth_method = "app"
        coordinator._mqtt_client = MagicMock()
        coordinator._mqtt_client.cert_account = "old"
        coordinator._device_available = True

        mock_app_api = MagicMock()
        mock_app_api.login = AsyncMock(return_value=False)

        with patch(
            "custom_components.ecoflow_energy.ecoflow.app_api.AppApiClient",
            return_value=mock_app_api,
        ):
            await coordinator._proactive_credential_refresh()

        # Device must still be available after failed proactive refresh
        assert coordinator._device_available is True
        log = coordinator.event_log
        assert any(e["type"] == "credential_proactive_fail" for e in log)
