"""Tests for EcoFlowMQTTClient - subscribe_data, client creation, reconnect, disconnect."""

import logging
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

        # Must subscribe to set_reply only - no data topics
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


class TestCaptureWritesReportsTheSubscription:
    """The flag a diagnostics download shows must be the subscription.

    A capture with no write frame in it means either that the vendor app
    sent none or that nobody was listening, and telling those apart from
    outside cost a round trip on #284. Reporting the constructor argument
    would answer the wrong one of the two: it says a subscription was
    wanted, not that it happened.
    """

    def test_false_before_the_client_ever_connects(self):
        client = _make_client(capture_writes=True, wss_mode=True, user_id="user123")

        assert client.capture_writes is False

    @patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client")
    def test_true_once_the_subscribe_has_run(self, mock_mqtt_cls):
        mock_paho = MagicMock()
        mock_mqtt_cls.return_value = mock_paho
        client = _make_client(capture_writes=True, wss_mode=True, user_id="user123")
        client.client = mock_paho

        client._on_connect(mock_paho, None, None, 0)

        topics = [call[0][0] for call in mock_paho.subscribe.call_args_list]
        assert any(t.endswith("/thing/property/set") for t in topics)
        assert client.capture_writes is True

    @patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client")
    def test_false_without_a_user_id(self, mock_mqtt_cls):
        """The write topic is addressed by account, so there is none to watch."""
        mock_paho = MagicMock()
        mock_mqtt_cls.return_value = mock_paho
        client = _make_client(capture_writes=True, wss_mode=False, user_id="")
        client.client = mock_paho

        client._on_connect(mock_paho, None, None, 0)

        assert client.capture_writes is False

    @patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client")
    def test_false_on_the_set_only_branch(self, mock_mqtt_cls):
        """Standard Mode subscribes to no data topics, and to no writes."""
        mock_paho = MagicMock()
        mock_mqtt_cls.return_value = mock_paho
        client = _make_client(
            capture_writes=True, subscribe_data=False, wss_mode=False, user_id="user123"
        )
        client.client = mock_paho

        client._on_connect(mock_paho, None, None, 0)

        assert client.capture_writes is False


class TestClientCreation:
    def test_tcp_mode_default(self):
        client = _make_client(wss_mode=False)
        assert client._wss_mode is False

    def test_wss_mode_requires_user_id(self):
        """WSS mode needs user_id - without it, falls back to TCP."""
        client = _make_client(wss_mode=True, user_id="")
        assert client._wss_mode is False

    def test_wss_mode_with_user_id(self):
        client = _make_client(wss_mode=True, user_id="user123")
        assert client._wss_mode is True

    def test_empty_credentials_fails(self):
        client = _make_client(certificate_account="", certificate_password="")
        assert client.create_client() is False


class TestBrokerAddress:
    """The address the credentials name, not a compile-time constant (#184)."""

    def test_defaults_stay_the_built_in_broker(self):
        client = _make_client(wss_mode=True, user_id="user123")
        assert client.broker == "mqtt-e.ecoflow.com:8084"

    def test_tcp_default_port(self):
        client = _make_client(wss_mode=False)
        assert client.broker == "mqtt-e.ecoflow.com:8883"

    def test_wss_falls_back_to_tcp_port_without_user_id(self):
        """No user id means no WSS, so the WSS port would be wrong."""
        client = _make_client(wss_mode=True, user_id="")
        assert client.broker.endswith(":8883")

    @patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client")
    def test_connect_uses_the_given_host_and_port(self, mock_mqtt_cls):
        mock_mqtt_cls.return_value = MagicMock()
        client = _make_client(
            wss_mode=True,
            user_id="user123",
            mqtt_host="mqtt-a.ecoflow.com",
            mqtt_port=8085,
            wss_path="/mqtt-us",
        )
        client.create_client()
        client.connect()

        client.client.connect.assert_called_once()
        host, port, _keepalive = client.client.connect.call_args[0]
        assert (host, port) == ("mqtt-a.ecoflow.com", 8085)
        client.client.ws_set_options.assert_called_once_with(path="/mqtt-us")
        assert client.broker == "mqtt-a.ecoflow.com:8085"

    @patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client")
    def test_force_reconnect_keeps_the_address(self, mock_mqtt_cls):
        """A reconnect that fell back to the default would be silent."""
        mock_mqtt_cls.return_value = MagicMock()
        client = _make_client(
            wss_mode=True,
            user_id="user123",
            mqtt_host="mqtt-a.ecoflow.com",
            mqtt_port=8085,
        )
        client.create_client()

        assert client.force_reconnect() is True

        host, port, _keepalive = client.client.connect.call_args[0]
        assert (host, port) == ("mqtt-a.ecoflow.com", 8085)


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

    def test_send_proto_set_noop_tcp(self):
        """TCP mode: send_proto_set should be a no-op."""
        client = _make_client(wss_mode=False)
        assert client.send_proto_set(b"\x00") is False

    def test_send_proto_set_noop_no_user_id(self):
        """WSS mode without user_id: send_proto_set should be a no-op."""
        client = _make_client(wss_mode=True, user_id="")
        assert client.send_proto_set(b"\x00") is False

    def test_send_energy_stream_switch_noop_tcp(self):
        """TCP mode: send_energy_stream_switch should be a no-op."""
        client = _make_client(wss_mode=False)
        assert client.send_energy_stream_switch() is False


class TestPublishDelivery:
    """A queued message is not a delivered message (issue #185)."""

    @staticmethod
    def _connected_client(publish_result):
        client = _make_client()
        client.client = MagicMock()
        client.client.publish.return_value = publish_result
        client.is_connected = lambda: True
        return client

    def test_publish_waits_for_broker_ack(self):
        info = MagicMock()
        info.rc = 0
        info.is_published.return_value = True
        client = self._connected_client(info)

        assert client.publish("test/topic", "payload", wait=True) is True
        info.wait_for_publish.assert_called_once()

    def test_publish_fails_when_broker_never_acks(self):
        """The half-dead socket: paho queues the message and no ack arrives.

        paho does not raise on timeout - `wait_for_publish` returns and
        leaves `is_published()` False, so that is the decisive check.
        """
        info = MagicMock()
        info.rc = 0
        info.is_published.return_value = False
        client = self._connected_client(info)

        assert client.publish("test/topic", "payload", wait=True) is False

    def test_publish_fails_when_message_cannot_be_delivered(self):
        """paho raises for a queue that is full or a dropped message."""
        info = MagicMock()
        info.rc = 0
        info.wait_for_publish.side_effect = RuntimeError("publish failed")
        client = self._connected_client(info)

        assert client.publish("test/topic", "payload", wait=True) is False

    def test_publish_fails_when_not_queued(self):
        """A non-zero return code never reaches the wait at all."""
        info = MagicMock()
        info.rc = 4
        client = self._connected_client(info)

        assert client.publish("test/topic", "payload", wait=True) is False
        info.wait_for_publish.assert_not_called()

    def test_publish_fails_when_queue_is_full(self):
        info = MagicMock()
        info.rc = 0
        info.wait_for_publish.side_effect = ValueError("queue full")
        client = self._connected_client(info)

        assert client.publish("test/topic", "payload", wait=True) is False

    def test_qos_zero_does_not_wait(self):
        info = MagicMock()
        info.rc = 0
        client = self._connected_client(info)

        assert client.publish("test/topic", "payload", qos=0, wait=True) is True
        info.wait_for_publish.assert_not_called()

    def test_background_publish_does_not_wait(self):
        """The ack is read by paho's own network thread.

        Waiting for it from inside a paho callback - which is where the
        post-connect requests run - can never succeed and costs the full
        timeout. Background publishes therefore do not wait at all.
        """
        info = MagicMock()
        info.rc = 0
        client = self._connected_client(info)

        assert client.publish("test/topic", "payload") is True
        info.wait_for_publish.assert_not_called()

    def test_post_connect_requests_never_wait(self):
        """send_get_all/send_latest_quotas run on the paho callback thread."""
        info = MagicMock()
        info.rc = 0
        client = self._connected_client(info)
        client._wss_mode = True
        client._user_id = "user123"

        assert client.send_get_all() is True
        assert client.send_latest_quotas() is True
        info.wait_for_publish.assert_not_called()

    def test_proto_set_waits_when_asked(self):
        """Entity-initiated proto writes report their outcome, so they wait."""
        info = MagicMock()
        info.rc = 0
        info.is_published.return_value = False
        client = self._connected_client(info)
        client._wss_mode = True
        client._user_id = "user123"

        assert client.send_proto_set(b"\x00", wait=True) is False
        info.wait_for_publish.assert_called_once()


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
        """Backoff tiers: attempts 0-3 = 1x, 4-6 = 1.5x, 7+ = 2x (below cap)."""
        # base=1 keeps the tier-2 delay below the 60s cap: 1 * 2^4 * 1.5 = 24
        client = _make_client(base_reconnect_delay=1, max_reconnect_delay=60)

        # Tier 1 (attempts 0-3): base delay
        client.reconnect_attempts = 3
        client.last_reconnect_time = time.monotonic() - 100
        assert client._should_attempt_reconnect() is True

        # Tier 2 (attempts 4-6): 1.5x delay (16 * 1.5 = 24, below cap)
        client.reconnect_attempts = 4
        base = client._get_reconnect_delay()
        assert base * 1.5 < client.max_reconnect_delay
        client.last_reconnect_time = time.monotonic() - (base * 1.5 - 1)
        assert client._should_attempt_reconnect() is False
        client.last_reconnect_time = time.monotonic() - (base * 1.5 + 1)
        assert client._should_attempt_reconnect() is True

    def test_effective_delay_never_exceeds_cap(self):
        """Tier multipliers must not push the effective delay past max_reconnect_delay."""
        client = _make_client(base_reconnect_delay=5, max_reconnect_delay=60)
        client.reconnect_attempts = 7  # tier 3: 2x multiplier
        client.last_reconnect_time = 1000.0

        # 61s elapsed: must be allowed (uncapped 2x would demand 120s)
        with patch(
            "ecoflow_energy.ecoflow.cloud_mqtt.time.monotonic",
            return_value=1000.0 + 61,
        ):
            assert client._should_attempt_reconnect() is True

        # 59s elapsed: still blocked (cap is 60, not less)
        with patch(
            "ecoflow_energy.ecoflow.cloud_mqtt.time.monotonic",
            return_value=1000.0 + 59,
        ):
            assert client._should_attempt_reconnect() is False


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

    @patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client")
    def test_first_failure_is_not_an_error(self, mock_mqtt_cls, caplog):
        """A transient DNS hiccup must not put a red line in the user's log.

        The watchdog retries, and the next attempt usually works. ERROR here
        reports a broken integration for something that fixed itself.
        """
        mock_paho = MagicMock()
        mock_paho.connect.side_effect = OSError("[Errno -3] Try again")
        mock_mqtt_cls.return_value = mock_paho

        client = _make_client(wss_mode=False)
        client.client = MagicMock()
        client.reconnect_attempts = 0

        with caplog.at_level(logging.DEBUG):
            assert client.force_reconnect() is False

        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert any("Force-reconnect failed" in r.message for r in caplog.records)

    @patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client")
    def test_sustained_failure_does_warn(self, mock_mqtt_cls, caplog):
        """Once attempts pile up it is worth telling the user."""
        mock_paho = MagicMock()
        mock_paho.connect.side_effect = OSError("[Errno -3] Try again")
        mock_mqtt_cls.return_value = mock_paho

        client = _make_client(wss_mode=False)
        client.client = MagicMock()
        client.reconnect_attempts = 3

        with caplog.at_level(logging.DEBUG):
            assert client.force_reconnect() is False

        assert [r for r in caplog.records if r.levelno == logging.WARNING]
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


class TestForceReconnectLock:
    def test_force_reconnect_skipped_while_another_in_flight(self):
        """A second force_reconnect from another thread is skipped, not stacked."""
        import threading

        client = _make_client()
        old_paho = MagicMock()
        client.client = old_paho

        acquired = threading.Event()
        release = threading.Event()

        def hold_lock():
            client._client_lock.acquire()
            acquired.set()
            release.wait(5)
            client._client_lock.release()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        assert acquired.wait(5)
        try:
            result = client.force_reconnect()
        finally:
            release.set()
            holder.join(5)

        assert result is False
        # The live client must not have been torn down by the skipped call
        old_paho.loop_stop.assert_not_called()
        old_paho.disconnect.assert_not_called()
        assert client.client is old_paho


class TestPingEchoFilter:
    def _msg(self, topic: str, payload: bytes):
        msg = MagicMock()
        msg.topic = topic
        msg.payload = payload
        return msg

    def test_ping_echo_dropped(self):
        """The broker echo of our own ping must not reach the message handler."""
        handler = MagicMock()
        client = _make_client(message_handler=handler)

        payload = b'{"command": "ping", "value": 123, "deviceSn": "TEST1234SN"}'
        client._on_message(None, None, self._msg("/app/device/property/TEST1234SN", payload))

        handler.assert_not_called()

    def test_ping_echo_compact_spelling_dropped(self):
        handler = MagicMock()
        client = _make_client(message_handler=handler)

        payload = b'{"command":"ping","value":123,"deviceSn":"TEST1234SN"}'
        client._on_message(None, None, self._msg("/app/device/property/TEST1234SN", payload))

        handler.assert_not_called()

    def test_proto_payload_still_dispatched(self):
        """Binary device payloads on the property topic still reach the handler."""
        handler = MagicMock()
        client = _make_client(message_handler=handler)

        payload = b"\x0a\x12\x08\x01"
        client._on_message(None, None, self._msg("/app/device/property/TEST1234SN", payload))

        handler.assert_called_once_with("/app/device/property/TEST1234SN", payload)

    def test_ping_marker_on_other_topic_not_dropped(self):
        """The filter is topic-scoped - same marker on another topic passes through."""
        handler = MagicMock()
        client = _make_client(message_handler=handler)

        payload = b'{"command": "ping"}'
        client._on_message(None, None, self._msg("/open/acct/TEST1234SN/quota", payload))

        handler.assert_called_once()


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

    def test_on_disconnect_reasoncode_normal(self):
        """ReasonCode 0 (normal disconnection) does not schedule a reconnect."""
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.reasoncodes import ReasonCode

        client = _make_client()
        client.connected = True
        mock_paho = MagicMock()
        client.client = mock_paho

        rc = ReasonCode(PacketTypes.DISCONNECT)  # Normal disconnection (0)
        with patch.object(client, "_schedule_reconnect") as mock_sched:
            client._on_disconnect(mock_paho, None, None, rc, None)
            mock_sched.assert_not_called()
        assert client.connected is False

    def test_on_disconnect_reasoncode_error_schedules_reconnect(self):
        """A non-zero ReasonCode schedules a reconnect and does not raise."""
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.reasoncodes import ReasonCode

        status_handler = MagicMock()
        client = _make_client(status_handler=status_handler)
        client.connected = True
        mock_paho = MagicMock()
        client.client = mock_paho

        rc = ReasonCode(PacketTypes.DISCONNECT, identifier=135)  # Not authorized
        with patch.object(client, "_schedule_reconnect") as mock_sched:
            client._on_disconnect(mock_paho, None, None, rc, None)
            mock_sched.assert_called_once()
        # Status handler receives the normalized int, not the ReasonCode object
        status_handler.assert_called_once()
        assert status_handler.call_args[0][1] == 135


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

    def test_auth_error_handler_called_on_rc4(self):
        """rc=4 (bad username/password) also triggers the auth_error_handler."""
        handler = MagicMock()
        client = _make_client(auth_error_handler=handler)
        mock_paho = MagicMock()
        client.client = mock_paho

        client._on_connect(mock_paho, None, None, 4)

        handler.assert_called_once()
        assert client.connected is False

    def test_auth_error_handler_not_called_on_other_rc(self):
        """Non-auth error codes do NOT call auth_error_handler."""
        handler = MagicMock()
        client = _make_client(auth_error_handler=handler)
        mock_paho = MagicMock()
        client.client = mock_paho

        for rc in [1, 2, 3]:
            handler.reset_mock()
            client._on_connect(mock_paho, None, None, rc)
            handler.assert_not_called()

    def test_auth_error_handler_called_on_reasoncode_135(self):
        """paho 2.x VERSION2 delivers ReasonCode objects - 135 (Not authorized)
        must fire the auth-error handler without raising (ReasonCode is unhashable)."""
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.reasoncodes import ReasonCode

        handler = MagicMock()
        client = _make_client(auth_error_handler=handler)
        mock_paho = MagicMock()
        client.client = mock_paho

        rc = ReasonCode(PacketTypes.CONNACK, identifier=135)
        client._on_connect(mock_paho, None, None, rc, None)

        handler.assert_called_once()
        assert client.connected is False

    def test_auth_error_handler_called_on_reasoncode_134(self):
        """ReasonCode 134 (bad user name or password) fires the auth-error handler."""
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.reasoncodes import ReasonCode

        handler = MagicMock()
        client = _make_client(auth_error_handler=handler)
        mock_paho = MagicMock()
        client.client = mock_paho

        rc = ReasonCode(PacketTypes.CONNACK, identifier=134)
        client._on_connect(mock_paho, None, None, rc, None)

        handler.assert_called_once()
        assert client.connected is False

    def test_connect_success_with_reasoncode_0(self):
        """ReasonCode Success (0) is treated as a successful connect."""
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.reasoncodes import ReasonCode

        handler = MagicMock()
        client = _make_client(auth_error_handler=handler, subscribe_data=False)
        mock_paho = MagicMock()
        client.client = mock_paho

        rc = ReasonCode(PacketTypes.CONNACK)  # Success (0)
        client._on_connect(mock_paho, None, None, rc, None)

        handler.assert_not_called()
        assert client.connected is True

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

    def test_update_credentials_propagates_to_live_client(self):
        """A live Paho client gets the new credentials for its auto-reconnect."""
        client = _make_client()
        mock_paho = MagicMock()
        client.client = mock_paho

        client.update_credentials("new_account", "new_password")

        mock_paho.username_pw_set.assert_called_once_with("new_account", "new_password")

    def test_update_credentials_without_client_is_safe(self):
        client = _make_client()
        client.client = None
        client.update_credentials("new_account", "new_password")  # must not raise
        assert client._cert_account == "new_account"


class TestResendInitialRequests:
    """The cheap remedy for a connected-but-silent session."""

    def _connected_client(self, **kwargs):
        client = _make_client(wss_mode=True, user_id="user123", **kwargs)
        client.client = MagicMock()
        client.client.is_connected.return_value = True
        # rc=0 queues the message; is_published() is the broker's ack.
        client.client.publish.return_value = MagicMock(
            rc=0, **{"is_published.return_value": True}
        )
        client.connected = True
        return client

    def test_enhanced_resends_stream_switch_and_requests(self):
        client = self._connected_client(enhanced_mode=True)

        assert client.resend_initial_requests() is True

        topics = [call.args[0] for call in client.client.publish.call_args_list]
        assert any(topic.endswith("/thing/property/set") for topic in topics)
        assert sum(topic.endswith("/thing/property/get") for topic in topics) == 2

    def test_non_enhanced_skips_stream_switch(self):
        client = self._connected_client(enhanced_mode=False)

        assert client.resend_initial_requests() is True

        topics = [call.args[0] for call in client.client.publish.call_args_list]
        assert all(not topic.endswith("/thing/property/set") for topic in topics)

    def test_disconnected_client_sends_nothing(self):
        client = self._connected_client(enhanced_mode=True)
        client.connected = False

        assert client.resend_initial_requests() is False
        client.client.publish.assert_not_called()

    def test_tcp_mode_sends_nothing(self):
        client = _make_client(wss_mode=False, enhanced_mode=True)
        client.client = MagicMock()
        client.connected = True

        assert client.resend_initial_requests() is False
        client.client.publish.assert_not_called()


class TestMaskTopic:
    """Topics reach the log, and the log reaches public issues.

    Every EcoFlow topic carries the serial, and the app and open topics carry
    the user id or the certificate account on top. Reporters are asked to turn
    on debug logging and attach the result, so an unmasked topic publishes all
    three.
    """

    def test_app_topic_masks_serial_and_user_id(self):
        client = _make_client(user_id="9876543210", wss_mode=True)
        masked = client.mask_topic("/app/9876543210/TEST1234SN/thing/property/set")

        assert masked == "/app/{uid}/{sn}/thing/property/set"

    def test_open_topic_masks_serial_and_cert_account(self):
        client = _make_client()
        masked = client.mask_topic("/open/test_account/TEST1234SN/set_reply")

        assert masked == "/open/{acct}/{sn}/set_reply"

    def test_property_topic_masks_serial(self):
        client = _make_client()

        assert client.mask_topic("/app/device/property/TEST1234SN") == (
            "/app/device/property/{sn}"
        )

    def test_empty_identifiers_are_not_substituted(self):
        """An empty user id must not turn every empty string into {uid}."""
        client = _make_client(user_id="")

        assert client.mask_topic("/app/device/property/TEST1234SN") == (
            "/app/device/property/{sn}"
        )


class TestAppWriteSubscription:
    """The set topic is subscribed only while a capture window is open.

    It exists to record what the vendor app writes, which is the only way to
    learn what a device accepts without guessing at the envelope. It is off by
    default because it is wanted as evidence and not as a running cost, and
    subscribing is not publishing: the listen-only guarantee is untouched.
    """

    def _topics(self, **kwargs) -> list[str]:
        with patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client") as mock_cls:
            mock_paho = MagicMock()
            mock_cls.return_value = mock_paho
            client = _make_client(wss_mode=True, user_id="user123", **kwargs)
            client.client = mock_paho
            client._on_connect(mock_paho, None, None, 0)
            return [call[0][0] for call in mock_paho.subscribe.call_args_list]

    def test_off_by_default(self):
        assert not any(t.endswith("/thing/property/set") for t in self._topics())

    def test_subscribed_when_capturing(self):
        topics = self._topics(capture_writes=True)
        assert any(t.endswith("/thing/property/set") for t in topics)

    def test_capturing_does_not_drop_the_data_topics(self):
        """The evidence subscription is additive, never a swap."""
        plain = self._topics()
        capturing = self._topics(capture_writes=True)
        assert set(plain) <= set(capturing)
        assert len(capturing) == len(plain) + 1


class TestNoAccountIdInTheThreadName:
    """A debug log is asked for in public. It must not carry the account id.

    Paho names its network thread after the client id, Python puts the
    thread name in every log record, and the Portal's client id format
    embeds the user id. A reporter's 15 minute log on #219 therefore
    published his account identifier on all 2714 of its lines.
    """

    def _client_with_thread(self, mock_mqtt_cls):
        import threading

        mock_paho = MagicMock()
        mock_mqtt_cls.return_value = mock_paho
        mock_paho._thread = threading.Thread(
            target=lambda: None, name="paho-mqtt-client-WEB_uuid_2049739542351577090_x"
        )
        client = _make_client(device_sn="R371TEST00000001")
        client.client = mock_paho
        return client, mock_paho

    @patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client")
    def test_the_thread_is_renamed_on_start(self, mock_mqtt_cls):
        client, mock_paho = self._client_with_thread(mock_mqtt_cls)

        client.start_loop()

        mock_paho.loop_start.assert_called_once()
        assert mock_paho._thread.name == "ecoflow-mqtt-R371"
        assert "2049739542351577090" not in mock_paho._thread.name

    @patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client")
    def test_only_four_characters_of_the_serial_are_used(self, mock_mqtt_cls):
        """The name is a log field, so it follows the masking convention."""
        client, mock_paho = self._client_with_thread(mock_mqtt_cls)

        client.start_loop()

        assert "R371TEST00000001" not in mock_paho._thread.name

    @patch("ecoflow_energy.ecoflow.cloud_mqtt.mqtt.Client")
    def test_a_missing_thread_does_not_stop_the_connection(self, mock_mqtt_cls):
        """The handle is paho's private attribute, so it may not be there."""
        mock_paho = MagicMock()
        mock_mqtt_cls.return_value = mock_paho
        del mock_paho._thread
        client = _make_client()
        client.client = mock_paho

        client.start_loop()

        mock_paho.loop_start.assert_called_once()


class TestOwnPublishEchoFilter:
    """Our own protobuf publishes come back from the broker.

    Measured on a live PowerOcean 2026-08-27 (#247): the 20-second
    EnergyStreamSwitch keepalive accounted for 316 of 4594 frames in a
    20-minute recording and occupied one of the diagnostics record's twenty
    message-type slots - while the writes the recording was opened for found
    no slot at all.
    """

    def _msg(self, topic: str, payload: bytes):
        msg = MagicMock()
        msg.topic = topic
        msg.payload = payload
        return msg

    def test_echo_of_our_own_publish_is_dropped(self):
        handler = MagicMock()
        client = _make_client(message_handler=handler)
        payload = b"\x0a\x20\x0a\x02\x08\x01\x10\x20\x18\x60\x20\x01"

        client._note_own_publish(payload)
        client._on_message(
            None, None, self._msg("/app/1/TEST1234SN/thing/property/set", payload)
        )

        handler.assert_not_called()

    def test_a_frame_we_never_sent_still_reaches_the_handler(self):
        """The negative control: the filter must not swallow device traffic."""
        handler = MagicMock()
        client = _make_client(message_handler=handler)

        client._note_own_publish(b"\x0a\x02\x08\x01")
        device_frame = b"\x0a\x02\x08\x02"
        client._on_message(
            None, None, self._msg("/app/device/property/TEST1234SN", device_frame)
        )

        handler.assert_called_once_with(
            "/app/device/property/TEST1234SN", device_frame
        )

    def test_the_echo_record_expires(self):
        """A payload that never came back must not pin memory or match later."""
        from ecoflow_energy.ecoflow import cloud_mqtt as mqtt_mod

        handler = MagicMock()
        client = _make_client(message_handler=handler)
        payload = b"\x0a\x02\x08\x01"

        with patch.object(mqtt_mod, "monotonic", return_value=1000.0):
            client._note_own_publish(payload)
        with patch.object(
            mqtt_mod, "monotonic", return_value=1000.0 + mqtt_mod.OWN_ECHO_TTL_S + 1
        ):
            client._on_message(
                None, None, self._msg("/app/device/property/TEST1234SN", payload)
            )

        handler.assert_called_once()

    def test_the_record_is_bounded(self):
        from ecoflow_energy.ecoflow import cloud_mqtt as mqtt_mod

        client = _make_client()
        for i in range(mqtt_mod.OWN_ECHO_MAX + 20):
            client._note_own_publish(f"payload-{i}".encode())

        assert len(client._own_publishes) == mqtt_mod.OWN_ECHO_MAX

    def test_publish_records_what_it_sent(self):
        """The recording happens on the publish path, not at the call sites."""
        client = _make_client()
        client.connected = True
        mock_paho = MagicMock()
        mock_paho.is_connected.return_value = True
        mock_paho.publish.return_value = MagicMock(rc=0)
        client.client = mock_paho

        assert client.publish("/some/topic", b"\x0a\x02\x08\x09") is True
        assert client._is_own_echo(b"\x0a\x02\x08\x09") is True
