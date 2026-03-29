"""EcoFlow Cloud MQTT client (Paho-based).

Manages WSS (port 8084) and TCP (port 8883) connections to the EcoFlow broker.
Configuration via constructor — no global config imports.

Threading note: Paho runs its own network thread.  In HA, bridge callbacks
to the event loop with ``hass.loop.call_soon_threadsafe()``.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
from typing import Callable

import paho.mqtt.client as mqtt

from .clientid import generate_client_id
from .const import (
    DEFAULT_COUNTER_RESET_INTERVAL,
    DEFAULT_MAX_RECONNECT_ATTEMPTS,
    DEFAULT_MAX_RECONNECT_DELAY,
    DEFAULT_MQTT_KEEPALIVE,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_WSS_KEEPALIVE,
    MQTT_HOST,
    MQTT_PORT_TCP,
    MQTT_PORT_WSS,
    MQTT_WSS_PATH,
)
from .energy_stream import build_energy_stream_activate_payload

logger = logging.getLogger(__name__)


class EcoFlowMQTTClient:
    """MQTT client for the EcoFlow cloud broker (WSS + TCP)."""

    def __init__(
        self,
        certificate_account: str,
        certificate_password: str,
        device_sn: str,
        message_handler: Callable[[str, bytes], None],
        *,
        user_id: str = "",
        mqtt_host: str = MQTT_HOST,
        wss_mode: bool = True,
        subscribe_data: bool = True,
        status_handler: Callable | None = None,
        auth_error_handler: Callable[[], None] | None = None,
        max_reconnect_attempts: int = DEFAULT_MAX_RECONNECT_ATTEMPTS,
        base_reconnect_delay: int = DEFAULT_RECONNECT_DELAY,
        max_reconnect_delay: int = DEFAULT_MAX_RECONNECT_DELAY,
    ) -> None:
        self._cert_account = certificate_account
        self._cert_password = certificate_password
        self._device_sn = device_sn
        self._user_id = user_id
        self._mqtt_host = mqtt_host
        self.message_handler = message_handler
        self.status_handler = status_handler

        self.client: mqtt.Client | None = None
        self.connected = False
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = max_reconnect_attempts
        self.base_reconnect_delay = base_reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.last_reconnect_time: float = 0
        self.last_connect_time: float = 0
        self.last_disconnect_time: float = 0

        self._auth_error_handler = auth_error_handler
        self._wss_mode = wss_mode and bool(user_id)
        self._subscribe_data = subscribe_data
        self._notified_connected = False
        self._last_counter_reset_time: float = 0
        self._counter_reset_interval = DEFAULT_COUNTER_RESET_INTERVAL

    @property
    def cert_account(self) -> str:
        """Return the certificate account used for MQTT authentication."""
        return self._cert_account

    @property
    def wss_mode(self) -> bool:
        """Return whether this client uses WSS (True) or TCP (False)."""
        return self._wss_mode

    def update_credentials(self, account: str, password: str) -> None:
        """Update stored credentials for next reconnect (e.g. after rc=5)."""
        self._cert_account = account
        self._cert_password = password

    def create_client(self) -> bool:
        """Create and configure the Paho MQTT client."""
        try:
            if not self._cert_account or not self._cert_password:
                logger.error("MQTT: certificate_account or certificate_password missing")
                return False

            if self._wss_mode:
                client_id = generate_client_id(self._user_id)
                logger.debug("WSS MQTT client (port %d)", MQTT_PORT_WSS)
                try:
                    self.client = mqtt.Client(
                        mqtt.CallbackAPIVersion.VERSION2,
                        client_id=client_id,
                        transport="websockets",
                        clean_session=True,
                    )
                except AttributeError:
                    self.client = mqtt.Client(
                        client_id=client_id,
                        transport="websockets",
                        clean_session=True,
                    )
                self.client.ws_set_options(path=MQTT_WSS_PATH)
            else:
                client_id = f"ecoflow_ha_{self._device_sn}"
                logger.debug("TCP MQTT client (port %d)", MQTT_PORT_TCP)
                try:
                    self.client = mqtt.Client(
                        mqtt.CallbackAPIVersion.VERSION2,
                        client_id=client_id,
                        clean_session=True,
                    )
                except AttributeError:
                    self.client = mqtt.Client(
                        client_id=client_id,
                        clean_session=True,
                    )

            self.client.username_pw_set(self._cert_account, self._cert_password)
            self.client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            return True

        except Exception as exc:
            logger.error("MQTT: client creation failed: %s", exc)
            return False

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """Callback on MQTT connection."""
        if rc == 0:
            if self._subscribe_data:
                # Subscribe to data topics (Enhanced Mode: MQTT is primary data source)
                topic_json = f"/open/{self._cert_account}/{self._device_sn}/quota"
                topic_pb = f"/app/device/property/{self._device_sn}"
                client.subscribe(topic_json, qos=1)
                client.subscribe(topic_pb, qos=0)

                if self._user_id:
                    topic_reply = f"/app/{self._user_id}/{self._device_sn}/thing/property/get_reply"
                    client.subscribe(topic_reply, qos=1)

                if not self._notified_connected:
                    self._notified_connected = True
                    logger.debug("MQTT connected — data topics: %s | %s", topic_json, topic_pb)
            else:
                # Standard Mode: no data subscriptions, MQTT is for SET commands only
                if not self._notified_connected:
                    self._notified_connected = True
                    logger.debug("MQTT connected — SET-only mode (no data subscriptions)")

            self.last_connect_time = time.time()
            self.connected = True
            self.reconnect_attempts = 0

            # WSS: send EnergyStreamSwitch + latestQuotas on (re)connect
            # Both are needed immediately to minimize data gap after reconnect
            if self._wss_mode and self._user_id:
                try:
                    payload = build_energy_stream_activate_payload()
                    set_topic = f"/app/{self._user_id}/{self._device_sn}/thing/property/set"
                    client.publish(set_topic, payload, qos=1)
                    logger.debug("EnergyStreamSwitch sent — energy_stream_report activated")
                except Exception as exc:
                    logger.warning("EnergyStreamSwitch error: %s", exc)
                try:
                    self.send_latest_quotas()
                    logger.debug("Post-connect latestQuotas sent — minimizing data gap")
                except Exception as exc:
                    logger.warning("Post-connect latestQuotas error: %s", exc)

            if self.status_handler:
                self.status_handler("connected", 0, "Connected")
        else:
            rc_reasons = {
                1: "Protocol version rejected",
                2: "ClientID rejected",
                3: "Broker unavailable",
                4: "Bad username/password",
                5: "Auth failed (credentials expired?)",
            }
            reason = rc_reasons.get(rc, "unknown error")
            if rc == 5:
                logger.warning("MQTT connect failed: rc=%s (%s) — scheduling credential refresh", rc, reason)
            else:
                logger.error("MQTT connect failed: rc=%s (%s)", rc, reason)
            self.connected = False
            if rc == 5 and self._auth_error_handler:
                self._auth_error_handler()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        """Callback on MQTT disconnect."""
        was_connected = self.connected
        self.connected = False
        self._notified_connected = False

        current_time = time.time()
        duration = current_time - self.last_connect_time if self.last_connect_time > 0 else 0
        self.last_disconnect_time = current_time

        if was_connected or reason_code != 0:
            _log = logger.warning if reason_code != 0 else logger.debug
            _log(
                "MQTT disconnect: rc=%s, was_connected=%s, duration=%.1fs, attempts=%d",
                reason_code, was_connected, duration, self.reconnect_attempts,
            )

        if reason_code != 0:
            self._schedule_reconnect()

        if self.status_handler:
            self.status_handler("disconnected", reason_code, f"Disconnected (rc={reason_code})")

    def _should_attempt_reconnect(self) -> bool:
        """Check if a reconnect attempt should be made. Never gives up permanently."""
        current_time = time.time()

        if self.reconnect_attempts >= self.max_reconnect_attempts:
            if (current_time - self._last_counter_reset_time) >= self._counter_reset_interval:
                self._last_counter_reset_time = current_time
                self.reconnect_attempts = 0
                logger.debug("MQTT: counter reset after %ds — starting new cycle", self._counter_reset_interval)
            else:
                return False

        min_delay = self._get_reconnect_delay()
        if self.reconnect_attempts <= 3:
            pass  # use base delay
        elif self.reconnect_attempts <= 6:
            min_delay *= 1.5
        else:
            min_delay *= 2.0

        return current_time - self.last_reconnect_time >= min_delay

    def _get_reconnect_delay(self) -> float:
        """Calculate delay until next reconnect attempt."""
        return min(
            self.base_reconnect_delay * (2 ** self.reconnect_attempts),
            self.max_reconnect_delay,
        )

    def _schedule_reconnect(self):
        """Signal that a reconnect is needed."""
        logger.debug("MQTT: reconnect scheduled — attempts: %d/%d", self.reconnect_attempts, self.max_reconnect_attempts)

    def _on_message(self, client, userdata, msg):
        """Callback for incoming MQTT messages."""
        try:
            self.message_handler(msg.topic, msg.payload)
        except Exception as exc:
            logger.warning("MQTT message handler error for %s: %s", msg.topic, exc)

    def connect(self) -> bool:
        """Establish the MQTT connection."""
        try:
            if self.is_connected():
                return True

            port = MQTT_PORT_WSS if self._wss_mode else MQTT_PORT_TCP
            keepalive = DEFAULT_WSS_KEEPALIVE if self._wss_mode else DEFAULT_MQTT_KEEPALIVE

            logger.debug("Connecting to %s:%d (%s)", self._mqtt_host, port, "WSS" if self._wss_mode else "TCP")
            self.client.connect(self._mqtt_host, port, keepalive)
            return True
        except Exception as exc:
            logger.warning("MQTT connection error: %s", exc)
            return False

    def try_reconnect(self) -> bool:
        """Attempt reconnect if disconnected and backoff has elapsed."""
        if self.is_connected():
            return False
        if not self._should_attempt_reconnect():
            return False

        self.reconnect_attempts += 1
        self.last_reconnect_time = time.time()

        logger.debug(
            "MQTT: reconnect attempt %d/%d",
            self.reconnect_attempts, self.max_reconnect_attempts,
        )
        return self.force_reconnect()

    def force_reconnect(self) -> bool:
        """Force disconnect + reconnect with new ClientID (WSS).

        Recreates the Paho client instead of manipulating private attributes.
        No blocking sleep — the old connection is torn down synchronously.
        """
        logger.debug("Force-reconnect: disconnecting and recreating client...")
        try:
            self.client.loop_stop()
        except Exception:
            pass
        try:
            self.client.disconnect()
        except Exception:
            pass
        self.connected = False
        self.client = None

        # Recreate the client (generates new ClientID for WSS)
        if not self.create_client():
            logger.error("Force-reconnect: client recreation failed")
            return False

        try:
            port = MQTT_PORT_WSS if self._wss_mode else MQTT_PORT_TCP
            keepalive = DEFAULT_WSS_KEEPALIVE if self._wss_mode else DEFAULT_MQTT_KEEPALIVE
            self.client.connect(self._mqtt_host, port, keepalive)
            self.client.loop_start()
            logger.debug("Force-reconnect: success at %s:%s (%s)", self._mqtt_host, port, "WSS" if self._wss_mode else "TCP")
            return True
        except Exception as exc:
            logger.error("Force-reconnect failed: %s", exc)
            return False

    def disconnect(self) -> None:
        """Disconnect the MQTT client."""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.connected = False

    def start_loop(self) -> None:
        """Start the Paho network loop."""
        if self.client:
            self.client.loop_start()

    def stop_loop(self) -> None:
        """Stop the Paho network loop."""
        if self.client:
            self.client.loop_stop()

    def is_connected(self) -> bool:
        """Check if the client is connected."""
        return self.connected and self.client is not None and self.client.is_connected()

    def publish(self, topic: str, payload: str | bytes, qos: int = 1) -> bool:
        """Publish a message to the EcoFlow cloud broker."""
        if not self.is_connected():
            return False
        try:
            result = self.client.publish(topic, payload, qos=qos)
            return result.rc == 0
        except Exception as exc:
            logger.error("Publish failed (%s): %s", topic, exc)
            return False

    def send_energy_stream_switch(self) -> bool:
        """Send EnergyStreamSwitch to keep energy_stream_report alive (WSS only)."""
        if not self._wss_mode or not self.is_connected() or not self._user_id:
            return False
        try:
            payload = build_energy_stream_activate_payload()
            topic = f"/app/{self._user_id}/{self._device_sn}/thing/property/set"
            return self.publish(topic, payload, qos=1)
        except Exception as exc:
            logger.warning("EnergyStreamSwitch error: %s", exc)
            return False

    def send_latest_quotas(self) -> bool:
        """Send a latestQuotas request (app keepalive)."""
        if not self._user_id or not self.is_connected():
            return False

        topic = f"/app/{self._user_id}/{self._device_sn}/thing/property/get"
        payload = json.dumps({
            "from": "Android",
            "id": str(int(time.time() * 1000)),
            "moduleType": 0,
            "operateType": "latestQuotas",
            "params": {},
            "version": "1.0",
        })
        return self.publish(topic, payload, qos=1)

    def send_ping(self) -> bool:
        """Send a ping heartbeat to the EcoFlow broker."""
        if not self.is_connected():
            return False
        topic = f"/app/device/property/{self._device_sn}"
        payload = json.dumps({
            "command": "ping",
            "value": int(time.time()) % 100000,
            "deviceSn": self._device_sn,
        })
        return self.publish(topic, payload, qos=0)

    def get_status(self) -> tuple:
        """Return the current connection status."""
        if self.is_connected():
            uptime = time.time() - self.last_connect_time if self.last_connect_time > 0 else 0
            return "connected", 0, f"Connected ({int(uptime)}s)"
        return "disconnected", self.reconnect_attempts, f"Disconnected (attempt {self.reconnect_attempts})"
