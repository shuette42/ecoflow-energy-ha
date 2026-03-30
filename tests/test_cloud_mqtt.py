"""Tests for EcoFlowMQTTClient — subscribe_data, client creation, reconnect, disconnect."""

import time
from unittest.mock import MagicMock, patch

from ecoflow_energy.ecoflow.cloud_mqtt import EcoFlowMQTTClient


def _make_client(**kwargs) -> EcoFlowMQTTClient:
    defaults = dict(
        certificate_account="test_account",
        certificate_password="test_password",
        device_sn="TEST1234SN",
        message_handler=MagicMock(),
        wss_mode=False,
    )
    defaults.update(kwargs)
    return EcoFlowMQTTClient(**defaults)


class TestSubscribeDataFlag:
    def test_default_subscribe_data_true(self):
        client = _make_client()
        assert client._subscribe_data is True

    def test_subscribe_data_false(self):
        client = _make_client(subscribe_data=False)
        assert client._subscribe_data is False

    @patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client")
    def test_standard_mode_no_data_subscriptions(self, mock_mqtt_cls):
        """In Standard Mode (subscribe_data=False), _on_connect subscribes only to set_reply."""
        mock_paho = MagicMock()
        mock_mqtt_cls.return_value = mock_paho

        client = _make_client(subscribe_data=False, wss_mode=False)
        client.client = mock_paho

        # Simulate successful connection (rc=0)
        client._on_connect(mock_paho, None, None, 0)

        # Must subscribe to set_reply only — no data topics
        topics_subscribed = [call[0][0] for call in mock_paho.subscribe.call_args_list]
        assert len(topics_subscribed) == 1
        assert "/set_reply" in topics_subscribed[0]
        assert not any("/quota" in t for t in topics_subscribed)

    @patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client")
    def test_enhanced_mode_subscribes_data_topics(self, mock_mqtt_cls):
        """In Enhanced Mode (subscribe_data=True), _on_connect must subscribe to data topics."""
        mock_paho = MagicMock()
        mock_mqtt_cls.return_value = mock_paho

        client = _make_client(
            subscribe_data=True,
            wss_mode=True,
            user_id="user123",
        )
        client.client = mock_paho

        # Simulate successful connection (rc=0)
        client._on_connect(mock_paho, None, None, 0)

        # Must subscribe to quota, property, and set_reply topics
        topics_subscribed = [call[0][0] for call in mock_paho.subscribe.call_args_list]
        assert any("/quota" in t for t in topics_subscribed), "Missing /quota subscription"
        assert any("/property/" in t for t in topics_subscribed), "Missing /property subscription"
        assert any("/set_reply" in t for t in topics_subscribed), "Missing /set_reply subscription"


class TestClientCreation:
    def test_tcp_mode_default(self):
        client = _make_client(wss_mode=False)
        assert client._wss_mode is False

    def test_wss_mode_requires_user_id(self):
        """WSS mode needs user_id — without it, falls back to TCP."""
        client = _make_client(wss_mode=True, user_id="")
        assert client._wss_mode is False

    def test_wss_mode_with_user_id(self):
        client = _make_client(wss_mode=True, user_id="user123")
        assert client._wss_mode is True

    def test_empty_credentials_fails(self):
        client = _make_client(certificate_account="", certificate_password="")
        assert client.create_client() is False


class TestConnectionStatus:
    def test_not_connected_by_default(self):
        client = _make_client()
        assert client.is_connected() is False
        assert client.connected is False

    def test_get_status_disconnected(self):
        client = _make_client()
        status, attempts, msg = client.get_status()
        assert status == "disconnected"

    def test_publish_fails_when_not_connected(self):
        client = _make_client()
        assert client.publish("test/topic", "payload") is False

    def test_send_energy_stream_switch_noop_tcp(self):
        """TCP mode: send_energy_stream_switch should be a no-op."""
        client = _make_client(wss_mode=False)
        assert client.send_energy_stream_switch() is False


# ===========================================================================
# Reconnect Strategy
# ===========================================================================


class TestReconnectDelay:
    def test_get_reconnect_delay_initial(self):
        client = _make_client()
        client.reconnect_attempts = 0
        delay = client._get_reconnect_delay()
        assert delay == client.base_reconnect_delay

    def test_get_reconnect_delay_exponential(self):
        client = _make_client(base_reconnect_delay=5)
        client.reconnect_attempts = 3
        delay = client._get_reconnect_delay()
        assert delay == 5 * (2 ** 3)  # 40

    def test_get_reconnect_delay_capped(self):
        client = _make_client(base_reconnect_delay=5, max_reconnect_delay=60)
        client.reconnect_attempts = 20
        delay = client._get_reconnect_delay()
        assert delay == 60


class TestShouldAttemptReconnect:
    def test_first_attempt_allowed(self):
        client = _make_client()
        client.reconnect_attempts = 0
        client.last_reconnect_time = 0
        assert client._should_attempt_reconnect() is True

    def test_too_soon_blocked(self):
        client = _make_client(base_reconnect_delay=60)
        client.reconnect_attempts = 1
        client.last_reconnect_time = time.monotonic()  # just now
        assert client._should_attempt_reconnect() is False

    def test_after_delay_allowed(self):
        client = _make_client(base_reconnect_delay=5)
        client.reconnect_attempts = 1
        client.last_reconnect_time = time.monotonic() - 100  # long ago
        assert client._should_attempt_reconnect() is True

    def test_max_attempts_blocked(self):
        client = _make_client(max_reconnect_attempts=3)
        client.reconnect_attempts = 3
        client._last_counter_reset_time = time.monotonic()  # recent reset
        assert client._should_attempt_reconnect() is False

    def test_counter_reset_after_interval(self):
        client = _make_client(max_reconnect_attempts=3)
        client.reconnect_attempts = 3
        client._last_counter_reset_time = time.monotonic() - 2000  # long ago
        client._counter_reset_interval = 1800
        client.last_reconnect_time = 0
        assert client._should_attempt_reconnect() is True
        assert client.reconnect_attempts == 0  # reset happened

    def test_tier_multipliers(self):
        """Backoff tiers: attempts 0-3 = 1x, 4-6 = 1.5x, 7+ = 2x."""
        client = _make_client(base_reconnect_delay=5)

        # Tier 1 (attempts 0-3): base delay
        client.reconnect_attempts = 3
        client.last_reconnect_time = time.monotonic() - 100
        assert client._should_attempt_reconnect() is True

        # Tier 2 (attempts 4-6): 1.5x delay
        client.reconnect_attempts = 5
        base = client._get_reconnect_delay()
        client.last_reconnect_time = time.monotonic() - (base * 1.5 - 1)
        assert client._should_attempt_reconnect() is False
        client.last_reconnect_time = time.monotonic() - (base * 1.5 + 1)
        assert client._should_attempt_reconnect() is True


class TestTryReconnect:
    def test_noop_when_connected(self):
        client = _make_client()
        client.connected = True
        mock_paho = MagicMock()
        mock_paho.is_connected.return_value = True
        client.client = mock_paho
        assert client.try_reconnect() is False

    def test_increments_attempts(self):
        client = _make_client()
        client.connected = False
        client.reconnect_attempts = 0
        client.last_reconnect_time = 0
        with patch.object(client, "force_reconnect", return_value=True):
            client.try_reconnect()
        assert client.reconnect_attempts == 1

    def test_blocked_by_backoff(self):
        client = _make_client(base_reconnect_delay=60)
        client.connected = False
        client.reconnect_attempts = 1
        client.last_reconnect_time = time.monotonic()  # just now
        assert client.try_reconnect() is False


class TestForceReconnect:
    @patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client")
    def test_recreates_client(self, mock_mqtt_cls):
        mock_paho = MagicMock()
        mock_mqtt_cls.return_value = mock_paho

        client = _make_client(wss_mode=False)
        old_paho = MagicMock()
        client.client = old_paho
        client.connected = True

        result = client.force_reconnect()

        old_paho.loop_stop.assert_called_once()
        old_paho.disconnect.assert_called_once()
        assert client.client is not old_paho  # new client created
        assert result is True

    @patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client")
    def test_force_reconnect_creation_failure(self, mock_mqtt_cls):
        """If create_client fails, force_reconnect returns False."""
        client = _make_client(certificate_account="", certificate_password="")
        old_paho = MagicMock()
        client.client = old_paho
        # create_client will fail (empty credentials)
        result = client.force_reconnect()
        assert result is False


class TestDisconnect:
    def test_disconnect_stops_loop(self):
        client = _make_client()
        mock_paho = MagicMock()
        client.client = mock_paho
        client.connected = True

        client.disconnect()

        mock_paho.loop_stop.assert_called_once()
        mock_paho.disconnect.assert_called_once()
        assert client.connected is False

    def test_disconnect_no_client(self):
        """Disconnect with no client is a no-op."""
        client = _make_client()
        client.client = None
        client.disconnect()  # should not raise


class TestOnDisconnect:
    def test_on_disconnect_updates_state(self):
        client = _make_client()
        client.connected = True
        client.last_connect_time = time.monotonic() - 60

        mock_paho = MagicMock()
        client.client = mock_paho

        client._on_disconnect(mock_paho, None, None, 0, None)

        assert client.connected is False
        assert client.last_disconnect_time > 0

    def test_on_disconnect_nonzero_rc_schedules_reconnect(self):
        client = _make_client()
        client.connected = True
        mock_paho = MagicMock()
        client.client = mock_paho

        with patch.object(client, "_schedule_reconnect") as mock_sched:
            client._on_disconnect(mock_paho, None, None, 1, None)
            mock_sched.assert_called_once()


# ===========================================================================
# Auth Error Handler (rc=5 credential refresh)
# ===========================================================================


class TestAuthErrorHandler:
    def test_auth_error_handler_called_on_rc5(self):
        """rc=5 triggers the auth_error_handler callback."""
        handler = MagicMock()
        client = _make_client(auth_error_handler=handler)
        mock_paho = MagicMock()
        client.client = mock_paho

        client._on_connect(mock_paho, None, None, 5)

        handler.assert_called_once()
        assert client.connected is False

    def test_auth_error_handler_not_called_on_rc0(self):
        """Successful connect (rc=0) does NOT call auth_error_handler."""
        handler = MagicMock()
        client = _make_client(auth_error_handler=handler, subscribe_data=False)
        mock_paho = MagicMock()
        client.client = mock_paho

        client._on_connect(mock_paho, None, None, 0)

        handler.assert_not_called()
        assert client.connected is True

    def test_auth_error_handler_not_called_on_other_rc(self):
        """Non-5 error codes do NOT call auth_error_handler."""
        handler = MagicMock()
        client = _make_client(auth_error_handler=handler)
        mock_paho = MagicMock()
        client.client = mock_paho

        for rc in [1, 2, 3, 4]:
            handler.reset_mock()
            client._on_connect(mock_paho, None, None, rc)
            handler.assert_not_called()

    def test_no_handler_on_rc5_is_safe(self):
        """rc=5 without handler does not crash."""
        client = _make_client()  # no auth_error_handler
        mock_paho = MagicMock()
        client.client = mock_paho

        client._on_connect(mock_paho, None, None, 5)  # should not raise
        assert client.connected is False


# ===========================================================================
# Update Credentials
# ===========================================================================


class TestUpdateCredentials:
    def test_update_credentials(self):
        client = _make_client()
        assert client._cert_account == "test_account"

        client.update_credentials("new_account", "new_password")

        assert client._cert_account == "new_account"
        assert client._cert_password == "new_password"
