"""EcoFlow Cloud MQTT client (Paho-based).

Manages WSS (port 8084) and TCP (port 8883) connections to the EcoFlow broker.
Configuration via constructor - no global config imports.

Threading note: Paho runs its own network thread.  In HA, bridge callbacks
to the event loop with ``hass.loop.call_soon_threadsafe()``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import ssl
import threading
import time
from collections import deque
from time import monotonic
from typing import Any, Callable

import paho.mqtt.client as mqtt

from .broker import BrokerAddress
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

_LOGGER = logging.getLogger(__name__)

# How long a payload we published stays recognisable as our own echo. The
# broker returns it within milliseconds; a minute is generous and bounds the
# window in which an unrelated frame could collide with the same digest.
OWN_ECHO_TTL_S = 60.0

# How many recent publishes stay on file for that comparison. A settings
# burst is a handful of frames; the keepalive adds one every 20 seconds.
OWN_ECHO_MAX = 64

# How long to wait for the broker to acknowledge a QoS-1 publish. Without
# this wait, `publish()` reports success as soon as the message sits in the
# local paho queue, which says nothing about whether it left the machine
# (issue #185). A half-dead socket accepts writes indefinitely and the
# device never sees them.
#
# Only publishes whose result reaches the user are worth waiting for, and
# the wait is opt-in for that reason. The acknowledgement is read by paho's
# network-loop thread, so waiting on that thread - inside `_on_connect`, for
# instance - can never succeed and costs the full timeout twice per connect.
PUBLISH_ACK_TIMEOUT_S = 5.0

# CONNACK return codes in words. These words travel: the status handler
# passes them to the listen-only capture, which exports them verbatim into a
# diagnostics download. That is also why every message handed to a
# status_handler must stay free of serials, account ids and topics - it may
# end up attached to a public issue. The table applies to CONNACK only;
# disconnect reason codes are a different namespace and are not named here.
CONNECT_REASONS = {
    1: "Protocol version rejected",
    2: "ClientID rejected",
    3: "Broker unavailable",
    4: "Bad username/password",
    5: "Auth failed (credentials expired?)",
    134: "Bad username/password",
    135: "Not authorized (credentials expired?)",
}


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
        mqtt_port: int | None = None,
        wss_path: str = MQTT_WSS_PATH,
        wss_mode: bool = True,
        enhanced_mode: bool = False,
        subscribe_data: bool = True,
        listen_only: bool = False,
        capture_writes: bool = False,
        status_handler: Callable | None = None,
        auth_error_handler: Callable[[], None] | None = None,
        max_reconnect_attempts: int = DEFAULT_MAX_RECONNECT_ATTEMPTS,
        base_reconnect_delay: int = DEFAULT_RECONNECT_DELAY,
        max_reconnect_delay: int = DEFAULT_MAX_RECONNECT_DELAY,
    ) -> None:
        self._cert_account = certificate_account
        self._cert_password = certificate_password
        # Subscribing to the topic the vendor app writes on. Off unless the
        # raw capture window is open, because it is only ever wanted as
        # evidence. Subscribing is not publishing: the listen-only guarantee
        # is untouched by it.
        self._capture_writes = capture_writes
        # Set when the subscribe below actually runs, which is what the
        # diagnostics flag reports. The request and the subscription are not
        # the same thing: the topic needs a user id and the data topics, and
        # a client that never connected subscribed to nothing at all.
        self._writes_subscribed = False
        self._device_sn = device_sn
        self._user_id = user_id
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
        # Resolved after _wss_mode, which decides the default port: the
        # caller may hand over a WSS-capable user id and still end up on
        # TCP. The address is kept as one value and rebound as a whole,
        # because it is written from the event loop when credentials are
        # refreshed and read on an executor thread at connect time, and a
        # host from before a change paired with a port from after it would
        # be an address that never existed. A lock is the wrong tool here:
        # the reconnect path already holds one across a blocking connect.
        self._broker = BrokerAddress(
            mqtt_host,
            mqtt_port or (MQTT_PORT_WSS if self._wss_mode else MQTT_PORT_TCP),
            wss_path,
        )
        self._enhanced_mode = enhanced_mode
        self._subscribe_data = subscribe_data
        # Hard guarantee that this connection never transmits. Enforced at
        # publish() so it holds for every path, including the requests
        # _on_connect fires on its own. Consumers that promise not to write
        # to a device must set this - passing enhanced_mode=False only
        # suppresses the energy stream switch, not get-all/latestQuotas.
        self._listen_only = listen_only
        # Payloads we published, for recognising the broker's echo of them.
        # Written from the caller's thread and read on paho's, so it carries
        # its own lock rather than borrowing the client lock.
        self._own_publishes: deque[tuple[float, bytes]] = deque(maxlen=OWN_ECHO_MAX)
        self._own_publish_lock = threading.Lock()
        self._notified_connected = False
        self._last_counter_reset_time: float = 0
        self._counter_reset_interval = DEFAULT_COUNTER_RESET_INTERVAL
        # Guards client swaps (create/connect/force_reconnect/disconnect).
        # Reentrant: force_reconnect holds it while calling create_client.
        self._client_lock = threading.RLock()

    def _log_issue(self, level: str, msg: str, *args: Any) -> None:
        """Report a connection problem at the level it deserves.

        A listen-only link is a diagnostics aid for a device the
        integration does not support. Whether it connects changes nothing
        for the user and there is nothing they could do about it, so its
        failures belong at debug - a warning would be noise about a device
        that has no entities in the first place. Everything else keeps its
        original level.
        """
        if self._listen_only:
            _LOGGER.debug(msg, *args)
        else:
            getattr(_LOGGER, level)(msg, *args)

    def _log_retryable(self, msg: str, *args: Any) -> None:
        """Report a failure that the watchdog is going to retry anyway.

        A single failed reconnect is not a broken integration - a transient
        DNS hiccup produces one, and the next attempt succeeds. Logging it at
        ERROR puts a red line in the user's log for something that fixed
        itself. It only becomes worth reporting once attempts are already
        piling up, which is the same escalation the disconnect path uses.
        """
        if self._listen_only or self.reconnect_attempts == 0:
            _LOGGER.debug(msg, *args)
        else:
            _LOGGER.warning(msg, *args)

    def mask_topic(self, topic: str) -> str:
        """Return a topic with the serial and the account identifiers removed.

        Every EcoFlow topic carries at least the device serial, and the app
        and open topics carry the user id or the certificate account as well.
        Reporters are routinely asked to enable debug logging and attach the
        result to a public issue, so a logged topic publishes all three - the
        same leak class the diagnostics export was fixed for, one layer out.
        Mirrors ``EcoFlowDeviceProbe._mask_topic``, which does this for the
        topics stored in a diagnostics download.
        """
        masked = topic
        for secret, placeholder in (
            (self._device_sn, "{sn}"),
            (self._user_id, "{uid}"),
            (self._cert_account, "{acct}"),
        ):
            if secret:
                masked = masked.replace(secret, placeholder)
        return masked

    @property
    def cert_account(self) -> str:
        """Return the certificate account used for MQTT authentication."""
        return self._cert_account

    @property
    def user_id(self) -> str:
        """Return the user ID used for app-auth MQTT topics."""
        return self._user_id

    @property
    def broker(self) -> str:
        """Return ``host:port`` of the broker this client talks to.

        Exported by the listen-only capture. A capture that never connected
        is unreadable without it: the reason the broker gave is the same
        whether the address was wrong or the credentials were.
        """
        return str(self._broker)

    @property
    def wss_mode(self) -> bool:
        """Return whether this client uses WSS (True) or TCP (False)."""
        return self._wss_mode

    @property
    def capture_writes(self) -> bool:
        """Return whether the vendor app's own writes are being watched.

        This is the subscription rather than the request for one: it turns
        true where the subscribe runs, so a client that never connected, or
        one that took the SET-only branch, reports false honestly.

        Exported because its absence is indistinguishable from silence. A
        capture taken while an owner changed a setting in the app carries no
        write frame in either case: because the app sent none, or because
        this client never subscribed to the topic it sends them on. The
        first is a finding about the device and the second is a finding
        about the version the reporter is running, and one of them cost a
        round trip on #284 before this said so out loud.
        """
        return self._writes_subscribed

    def update_credentials(self, account: str, password: str) -> None:
        """Update stored credentials for next reconnect (e.g. after rc=5).

        Also updates the live Paho client so its internal auto-reconnect
        uses the fresh credentials instead of retrying stale ones until
        the next force_reconnect.
        """
        self._cert_account = account
        self._cert_password = password
        if self.client is not None:
            try:
                self.client.username_pw_set(account, password)
            except Exception as exc:
                _LOGGER.debug("MQTT: live credential update failed: %s", exc)

    def update_broker(self, broker: BrokerAddress) -> bool:
        """Adopt a broker address from a refreshed credential response.

        Returns whether it differs from the one in use. Credentials are
        re-fetched several times over a long-running session, and each
        answer names the broker those credentials are valid at. Keeping the
        address from setup while adopting the new account and password
        would leave the client dialling a server the credentials no longer
        belong to, and the failure mode is the silent one this whole change
        exists to end: the connection is refused and nothing says why.
        """
        if broker == self._broker:
            return False
        _LOGGER.debug("MQTT: broker changed to %s", broker)
        self._broker = broker
        return True

    def create_client(self) -> bool:
        """Create and configure the Paho MQTT client."""
        with self._client_lock:
            return self._create_client_unlocked()

    def _create_client_unlocked(self) -> bool:
        """Create the Paho client. Caller must hold ``_client_lock``."""
        try:
            if not self._cert_account or not self._cert_password:
                self._log_issue("error", "MQTT: certificate_account or certificate_password missing")
                return False

            if self._wss_mode:
                client_id = generate_client_id(self._user_id)
                _LOGGER.debug("WSS MQTT client (%s)", self.broker)
                self.client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION2,
                    client_id=client_id,
                    transport="websockets",
                    clean_session=True,
                )
                self.client.ws_set_options(path=self._broker.path)
            else:
                client_id = f"ecoflow_ha_{self._device_sn}"
                _LOGGER.debug("TCP MQTT client (%s)", self.broker)
                self.client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION2,
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
            self._log_issue("error", "MQTT: client creation failed: %s", exc)
            return False

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """Callback on MQTT connection.

        Under paho-mqtt 2.x VERSION2 callbacks ``rc`` is a ReasonCode object
        (unhashable, MQTT 3.1.1 CONNACK codes mapped to MQTT 5 identifiers:
        4 -> 134, 5 -> 135). Normalize to int first so dict lookups and
        comparisons work for both int and ReasonCode inputs.
        """
        rc_val = rc.value if hasattr(rc, "value") else rc
        if rc_val == 0:
            # Subscribe to SET reply topics (all modes) for command acknowledgement tracking.
            # A listen-only connection sends no commands, so there is nothing to
            # acknowledge - and skipping these keeps the account identifiers out
            # of the captured topic list.
            if not self._listen_only:
                set_reply_topic = f"/open/{self._cert_account}/{self._device_sn}/set_reply"
                client.subscribe(set_reply_topic, qos=1)
                if self._user_id:
                    app_set_reply = f"/app/{self._user_id}/{self._device_sn}/thing/property/set_reply"
                    client.subscribe(app_set_reply, qos=1)

            if self._subscribe_data:
                # Subscribe to data topics (Enhanced Mode: MQTT is primary data source)
                topic_json = f"/open/{self._cert_account}/{self._device_sn}/quota"
                topic_pb = f"/app/device/property/{self._device_sn}"
                client.subscribe(topic_json, qos=1)
                client.subscribe(topic_pb, qos=0)

                if self._user_id:
                    topic_reply = f"/app/{self._user_id}/{self._device_sn}/thing/property/get_reply"
                    client.subscribe(topic_reply, qos=1)

                    if self._capture_writes:
                        # The topic the vendor app publishes its writes on.
                        # The broker delivers them to every subscriber, so
                        # watching one is the only way to learn what a device
                        # accepts without guessing at the envelope. Nothing is
                        # ever published here from this branch, and the frames
                        # are captured rather than parsed: a value an owner
                        # asked for is not a value the device reported.
                        topic_write = (
                            f"/app/{self._user_id}/{self._device_sn}"
                            "/thing/property/set"
                        )
                        client.subscribe(topic_write, qos=1)
                        self._writes_subscribed = True

                if not self._notified_connected:
                    self._notified_connected = True
                    _LOGGER.debug(
                    "MQTT connected - data topics: %s | %s | set_reply",
                    self.mask_topic(topic_json),
                    self.mask_topic(topic_pb),
                )
            else:
                # Standard Mode: no data subscriptions, MQTT is for SET commands only
                if not self._notified_connected:
                    self._notified_connected = True
                    _LOGGER.debug("MQTT connected - SET-only mode (set_reply subscribed)")

            self.last_connect_time = time.monotonic()
            self.connected = True
            self.reconnect_attempts = 0

            # WSS: send initial data requests on (re)connect.
            # These are publishes to the device. They must not fire on a
            # listen-only connection - publish() would refuse them anyway,
            # but the energy stream switch below goes straight to the paho
            # client and would bypass that check.
            if self._wss_mode and self._user_id and not self._listen_only:
                if self._enhanced_mode:
                    # Enhanced: EnergyStreamSwitch + get-all + latestQuotas
                    try:
                        payload = build_energy_stream_activate_payload()
                        set_topic = f"/app/{self._user_id}/{self._device_sn}/thing/property/set"
                        client.publish(set_topic, payload, qos=1)
                        # This path goes straight to paho and would otherwise
                        # never be recorded as ours.
                        self._note_own_publish(payload)
                        _LOGGER.debug("EnergyStreamSwitch sent - energy_stream_report activated")
                    except Exception as exc:
                        _LOGGER.warning("EnergyStreamSwitch error: %s", exc)
                    try:
                        self.send_get_all()
                        _LOGGER.debug("Post-connect get-all sent - requesting full state")
                    except Exception as exc:
                        _LOGGER.warning("Post-connect get-all error: %s", exc)
                    try:
                        self.send_latest_quotas()
                        _LOGGER.debug("Post-connect latestQuotas sent - minimizing data gap")
                    except Exception as exc:
                        _LOGGER.warning("Post-connect latestQuotas error: %s", exc)
                else:
                    # Non-enhanced (SmartPlug, Delta): protobuf get-all + JSON latestQuotas
                    try:
                        self.send_get_all()
                        _LOGGER.debug("Post-connect get-all sent - requesting full state")
                    except Exception as exc:
                        _LOGGER.warning("Post-connect get-all error: %s", exc)
                    try:
                        self.send_latest_quotas()
                        _LOGGER.debug("Post-connect latestQuotas sent - JSON fallback")
                    except Exception as exc:
                        _LOGGER.warning("Post-connect latestQuotas error: %s", exc)

            if self.status_handler:
                self.status_handler("connected", 0, "Connected")
        else:
            reason = CONNECT_REASONS.get(rc_val, "unknown error")
            auth_failure = rc_val in (4, 5, 134, 135)
            if auth_failure:
                self._log_issue("warning", "MQTT connect failed: rc=%s (%s) - scheduling credential refresh", rc_val, reason)
            else:
                self._log_issue("error", "MQTT connect failed: rc=%s (%s)", rc_val, reason)
            self.connected = False
            if auth_failure and self._auth_error_handler:
                self._auth_error_handler()

            # A refused session never reaches _on_disconnect, so without this
            # the only trace of it is a debug line. The listen-only capture
            # has to report the reason to someone who cannot see that log.
            if self.status_handler:
                self.status_handler("connect_failed", rc_val, reason)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        """Callback on MQTT disconnect.

        ``reason_code`` is a ReasonCode object under paho-mqtt 2.x VERSION2
        callbacks - normalize to int before any comparison.
        """
        rc_val = reason_code.value if hasattr(reason_code, "value") else reason_code
        was_connected = self.connected
        self.connected = False
        self._notified_connected = False

        current_time = time.monotonic()
        duration = current_time - self.last_connect_time if self.last_connect_time > 0 else 0
        self.last_disconnect_time = current_time

        if was_connected or rc_val != 0:
            # First disconnect is normal (broker-side rotation) - only warn
            # if previous reconnect attempts are already pending (sustained failure)
            if rc_val != 0 and self.reconnect_attempts > 0 and not self._listen_only:
                _log = _LOGGER.warning
            else:
                _log = _LOGGER.debug
            _log(
                "MQTT disconnect: rc=%s, was_connected=%s, duration=%.1fs, attempts=%d",
                rc_val, was_connected, duration, self.reconnect_attempts,
            )

        if rc_val != 0:
            self._schedule_reconnect()

        if self.status_handler:
            self.status_handler("disconnected", rc_val, f"Disconnected (rc={rc_val})")

    def _should_attempt_reconnect(self) -> bool:
        """Check if a reconnect attempt should be made. Never gives up permanently."""
        current_time = time.monotonic()

        if self.reconnect_attempts >= self.max_reconnect_attempts:
            if (current_time - self._last_counter_reset_time) >= self._counter_reset_interval:
                self._last_counter_reset_time = current_time
                self.reconnect_attempts = 0
                _LOGGER.debug("MQTT: counter reset after %ds - starting new cycle", self._counter_reset_interval)
            else:
                return False

        min_delay = self._get_reconnect_delay()
        if self.reconnect_attempts <= 3:
            pass  # use base delay
        elif self.reconnect_attempts <= 6:
            min_delay *= 1.5
        else:
            min_delay *= 2.0
        # Re-apply the cap: the tier multiplier must not push the effective
        # delay beyond max_reconnect_delay.
        min_delay = min(min_delay, self.max_reconnect_delay)

        return current_time - self.last_reconnect_time >= min_delay

    def _get_reconnect_delay(self) -> float:
        """Calculate delay until next reconnect attempt."""
        return min(
            self.base_reconnect_delay * (2 ** self.reconnect_attempts),
            self.max_reconnect_delay,
        )

    def _schedule_reconnect(self):
        """Signal that a reconnect is needed."""
        _LOGGER.debug("MQTT: reconnect scheduled - attempts: %d/%d", self.reconnect_attempts, self.max_reconnect_attempts)

    # send_ping publishes JSON to the same /app/device/property/{sn} topic the
    # client subscribes to - the broker echoes it back. Marker covers both
    # compact and default json.dumps spellings.
    _PING_ECHO_MARKERS = (b'{"command":"ping"', b'{"command": "ping"')

    def _note_own_publish(self, payload: str | bytes) -> None:
        """Remember a payload we sent, so its echo can be recognised.

        The marker above catches the JSON ping and nothing else, which left
        every protobuf publish of ours coming back as if the device had sent
        it. On a live PowerOcean that is the 20-second EnergyStreamSwitch
        keepalive: 316 frames in 20 minutes, one of them kept in the
        diagnostics record occupying a message-type slot, and the other 315
        counted against the recording as device traffic. An echo is our own
        bytes, so recognising it needs no heuristic about sequence numbers -
        only that we said them first.

        Bounded by count and by age: an echo arrives in milliseconds, and a
        payload that never comes back must not pin memory.
        """
        if isinstance(payload, str):
            payload = payload.encode()
        digest = hashlib.blake2b(payload, digest_size=16).digest()
        with self._own_publish_lock:
            self._own_publishes.append((monotonic(), digest))

    def _is_own_echo(self, payload: bytes) -> bool:
        """Whether this frame is the broker returning something we sent."""
        digest = hashlib.blake2b(payload, digest_size=16).digest()
        now = monotonic()
        with self._own_publish_lock:
            while self._own_publishes and now - self._own_publishes[0][0] > OWN_ECHO_TTL_S:
                self._own_publishes.popleft()
            for _sent_at, seen in self._own_publishes:
                if seen == digest:
                    return True
        return False

    def _on_message(self, client, userdata, msg):
        """Callback for incoming MQTT messages."""
        if (
            msg.topic == f"/app/device/property/{self._device_sn}"
            and msg.payload.startswith(self._PING_ECHO_MARKERS)
        ):
            # Broker echo of our own keepalive ping - not device data
            return
        if self._is_own_echo(msg.payload):
            # Broker echo of one of our own publishes. Dropping it here keeps
            # it out of the parser and out of the diagnostics recording alike.
            return
        _LOGGER.debug(
            "MQTT msg: %s (%d bytes) for %s",
            self.mask_topic(msg.topic),
            len(msg.payload),
            self._device_sn[:4],
        )
        try:
            self.message_handler(msg.topic, msg.payload)
        except Exception as exc:
            self._log_issue(
                "warning",
                "MQTT message handler error for %s: %s",
                self.mask_topic(msg.topic),
                exc,
            )

    def connect(self) -> bool:
        """Establish the MQTT connection."""
        with self._client_lock:
            try:
                if self.is_connected():
                    return True

                keepalive = DEFAULT_WSS_KEEPALIVE if self._wss_mode else DEFAULT_MQTT_KEEPALIVE

                _LOGGER.debug("Connecting to %s (%s)", self.broker, "WSS" if self._wss_mode else "TCP")
                broker = self._broker
                self.client.connect(broker.host, broker.port, keepalive)
                return True
            except Exception as exc:
                self._log_issue("warning", "MQTT connection error: %s", exc)
                return False

    def try_reconnect(self) -> bool:
        """Attempt reconnect if disconnected and backoff has elapsed."""
        if self.is_connected():
            return False
        if not self._should_attempt_reconnect():
            return False

        self.reconnect_attempts += 1
        self.last_reconnect_time = time.monotonic()

        _LOGGER.debug(
            "MQTT: reconnect attempt %d/%d",
            self.reconnect_attempts, self.max_reconnect_attempts,
        )
        return self.force_reconnect()

    def force_reconnect(self) -> bool:
        """Force disconnect + reconnect with new ClientID (WSS).

        Recreates the Paho client instead of manipulating private attributes.
        No blocking sleep - the old connection is torn down synchronously.

        Guarded by a non-blocking lock: overlapping calls (watchdog +
        credential refresh run in separate executor threads) would orphan
        a live Paho client with a running network thread. The second
        caller skips instead.
        """
        if not self._client_lock.acquire(blocking=False):
            _LOGGER.debug("Force-reconnect: skipped - another reconnect already in flight")
            return False
        try:
            _LOGGER.debug("Force-reconnect: disconnecting and recreating client...")
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
            if not self._create_client_unlocked():
                self._log_retryable(
                    "Force-reconnect: client recreation failed"
                )
                return False

            try:
                keepalive = DEFAULT_WSS_KEEPALIVE if self._wss_mode else DEFAULT_MQTT_KEEPALIVE
                broker = self._broker
                self.client.connect(broker.host, broker.port, keepalive)
                self._start_network_loop()
                _LOGGER.debug("Force-reconnect: success at %s (%s)", self.broker, "WSS" if self._wss_mode else "TCP")
                return True
            except Exception as exc:
                self._log_retryable("Force-reconnect failed: %s", exc)
                return False
        finally:
            self._client_lock.release()

    def disconnect(self) -> None:
        """Disconnect the MQTT client."""
        with self._client_lock:
            if self.client:
                self.client.loop_stop()
                self.client.disconnect()
                self.connected = False

    def start_loop(self) -> None:
        """Start the Paho network loop."""
        if self.client:
            self._start_network_loop()

    def _start_network_loop(self) -> None:
        """Start the paho loop, without the account id in the thread name.

        Paho names its network thread after the client id, and Python puts
        the thread name in every log record. The Portal's client id format
        embeds the account's user id, so each debug line carried it - and a
        debug log is the artefact users are asked to attach to public
        issues. A reporter's 15 minute log on #219 published his account id
        on all 2714 of them, next to the device serial fixed alongside this.

        The thread itself is paho's, so the handle is read defensively and a
        failure to rename is never allowed to stop the connection: a log
        line that says too much is a smaller problem than a device that does
        not connect.
        """
        self.client.loop_start()
        thread = getattr(self.client, "_thread", None)
        if thread is None:
            return
        try:
            thread.name = f"ecoflow-mqtt-{self._device_sn[:4]}"
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not rename the MQTT network thread", exc_info=True)

    def stop_loop(self) -> None:
        """Stop the Paho network loop."""
        if self.client:
            self.client.loop_stop()

    def is_connected(self) -> bool:
        """Check if the client is connected.

        Reads a local reference: ``self.client`` is swapped to ``None`` by
        ``force_reconnect`` on an executor thread, and re-reading the
        attribute between the None-check and the call would raise on
        whatever thread asked - the event loop, when diagnostics or the
        probe watchdog are the caller.
        """
        client = self.client
        return self.connected and client is not None and client.is_connected()

    def publish(
        self, topic: str, payload: str | bytes, qos: int = 1, wait: bool = False,
    ) -> bool:
        """Publish a message to the EcoFlow cloud broker.

        Set ``wait`` for a publish whose outcome is reported to the user:
        the return value then means the broker acknowledged the message
        rather than that paho queued it locally. Never set it on a call
        made from a paho callback - see PUBLISH_ACK_TIMEOUT_S.
        """
        if self._listen_only:
            # Single choke point: whatever path got here, nothing leaves.
            _LOGGER.debug(
                "Publish suppressed on listen-only connection (%s)",
                self.mask_topic(topic),
            )
            return False
        if not self.is_connected():
            return False
        try:
            result = self.client.publish(topic, payload, qos=qos)
            if result.rc != 0:
                return False
            self._note_own_publish(payload)
            if not wait or qos == 0:
                # rc == 0 only means paho queued the message locally.
                # Callers that do not report their outcome accept that.
                return True
            # Wait for the broker's PUBACK so a dead socket reports a failure
            # instead of a write that quietly never arrives. paho does not
            # raise on timeout - it returns and leaves is_published() False.
            result.wait_for_publish(timeout=PUBLISH_ACK_TIMEOUT_S)
            return result.is_published()
        except (RuntimeError, ValueError) as exc:
            # paho raises ValueError when the outgoing queue is full and
            # RuntimeError when the message can no longer be delivered.
            _LOGGER.debug("Publish not acknowledged (%s): %s", self.mask_topic(topic), exc)
            return False
        except Exception as exc:
            _LOGGER.error("Publish failed (%s): %s", self.mask_topic(topic), exc)
            return False

    def send_proto_set(self, payload: bytes, wait: bool = False) -> bool:
        """Send a binary protobuf SET command to the device (WSS only).

        Publishes to /app/{user_id}/{sn}/thing/property/set.
        Used by EnergyStreamSwitch and SoC limit SET commands.
        """
        if not self._wss_mode or not self.is_connected() or not self._user_id:
            return False
        topic = f"/app/{self._user_id}/{self._device_sn}/thing/property/set"
        return self.publish(topic, payload, qos=1, wait=wait)

    def send_energy_stream_switch(self) -> bool:
        """Send EnergyStreamSwitch to keep energy_stream_report alive (WSS only)."""
        try:
            payload = build_energy_stream_activate_payload()
            return self.send_proto_set(payload)
        except Exception as exc:
            _LOGGER.warning("EnergyStreamSwitch error: %s", exc)
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

    def send_get_all(self) -> bool:
        """Send a protobuf get-all request to fetch full device state.

        Used for non-Enhanced devices (SmartPlug, Delta) connected via
        app-auth WSS. The device responds with a full heartbeat on the
        get_reply topic.
        """
        if not self._user_id or not self.is_connected():
            return False

        from .energy_stream import build_device_get_all_payload

        topic = f"/app/{self._user_id}/{self._device_sn}/thing/property/get"
        payload = build_device_get_all_payload()
        return self.publish(topic, payload, qos=1)

    def resend_initial_requests(self) -> bool:
        """Re-send the post-connect request set without dropping the socket.

        Some devices stop pushing telemetry while the WSS session stays up
        (observed on PowerOcean Plus units, which only answer right after a
        subscribe). Repeating the post-connect requests restores the data flow
        at a fraction of the cost of a full reconnect, so the stale handler
        tries this first and only tears the session down if it does not help.

        Returns True if at least one request was published.
        """
        if not self._wss_mode or not self._user_id or not self.is_connected():
            return False

        sent = False
        if self._enhanced_mode:
            sent |= self.send_energy_stream_switch()
        sent |= self.send_get_all()
        sent |= self.send_latest_quotas()
        return sent

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
            uptime = time.monotonic() - self.last_connect_time if self.last_connect_time > 0 else 0
            return "connected", 0, f"Connected ({int(uptime)}s)"
        return "disconnected", self.reconnect_attempts, f"Disconnected (attempt {self.reconnect_attempts})"
