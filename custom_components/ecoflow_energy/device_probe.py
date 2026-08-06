"""Diagnostics capture for devices that have no parser yet.

A device the integration cannot classify is skipped: it gets no
coordinator, no MQTT connection, and therefore no entities. That is
correct behaviour, but it also means the one thing needed to add support
for the model - the bytes it actually sends - can never be collected from
a normal installation.

The Standard-mode answer to that is the raw quota capture in
``diagnostics.py``, which asks the Developer API for the device's quota.
For several models that route is closed: the API answers error 1006 and
returns nothing (observed on Ocean 2 / RE11 and the SM3A smart meter).
Those devices are only reachable through the app channel.

This module fills that gap. In app-auth mode it opens a listen-only MQTT
connection per skipped device, captures the raw frames into a bounded
buffer, and hands them to diagnostics. It creates no entities, sends no
commands, and writes nothing to the device.

The buffer is bucketed by message type rather than being one shared ring.
A capture runs for up to 24 hours and the frames worth having are the
rare ones, so a shared ring is the wrong shape: the most frequent message
type fills it with its own tail and evicts everything a parser would be
built from. ``TypedFrameBuffer`` gives every message type its own budget
and keeps one frame per time slot within it, widening the slot as the
recording grows rather than dropping its start. See that class for the
full reasoning.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    PROBE_WATCHDOG_INTERVAL_S,
    RAW_FRAME_KEYS_MAX,
    RAW_FRAME_MAX_BYTES,
    RAW_FRAME_PER_KEY_MAX,
)
from .ecoflow.cloud_mqtt import EcoFlowMQTTClient
from .ecoflow.frame_capture import (
    TypedFrameBuffer,
    build_frame_entry,
    decode_cmd_headers,
    frame_key,
    is_proto_frame,
)

_LOGGER = logging.getLogger(__name__)


class UnroutedDeviceProbe:
    """Listen-only frame capture for one unsupported device.

    Deliberately minimal: no re-subscribe logic, no SET path. A diagnostics
    helper must never become a second, half-maintained coordinator.

    The connection is opened with ``listen_only=True``, which is what
    actually holds the no-write promise: the shared client fires get-all
    and latestQuotas at the device on every connect otherwise.

    It does need one thing a purely passive helper would not: its own
    reconnect. Paho retries after a drop on its own, but it retries with
    the client id it was created with, and this broker rejects a client id
    that has already been used. Only ``force_reconnect`` builds a fresh
    one, and the coordinator is what normally drives it - a skipped device
    has no coordinator. Without the watchdog the first disconnect in a
    24 hour window is final, which is how a capture that ran all day
    arrives holding nothing.

    Every outcome is recorded rather than only logged at debug: the whole
    point of the capture is to be read by someone who does not have the
    hardware, and "it did not connect" is not an answer they can act on.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        device_sn: str,
        product_name: str,
        cert_account: str,
        cert_password: str,
        user_id: str,
    ) -> None:
        self.hass = hass
        self.device_sn = device_sn
        self.product_name = product_name
        self._user_id = user_id
        self._cert_account = cert_account
        self._frames = TypedFrameBuffer(RAW_FRAME_KEYS_MAX, RAW_FRAME_PER_KEY_MAX)
        self._topics: set[str] = set()
        # Written on the Paho thread, read on the event loop when diagnostics
        # are downloaded. On CPython the reads happen to be safe already:
        # sorted(set) and list(deque) run entirely in C, so no other Python
        # thread can observe a half-built container. That is an implementation
        # detail of the GIL, not a property of the code - it does not hold on
        # a free-threaded build. Cheap enough to not depend on.
        self._capture_lock = threading.Lock()
        # Set once the probe is told to stop, checked on every reconnect
        # path. Without it the watchdog can resurrect a probe mid-unload:
        # the timer's cancel runs in the entry's on_unload callbacks, which
        # Home Assistant processes only after async_unload_entry returns,
        # and async_unload_entry awaits in between - a tick landing in one
        # of those awaits would find the session down (it was just
        # disconnected) and rebuild it, leaving a live connection and a
        # paho thread that nothing ever tears down.
        self._stopped = False
        # Connection history. Written from the paho thread via the status
        # callback, read on the event loop when diagnostics are downloaded,
        # so it shares the capture lock rather than relying on the GIL.
        self._started_at = time.time()
        self._connect_calls = 0
        self._connects = 0
        self._disconnects = 0
        self._last_rc: int | None = None
        self._last_reason = ""
        self._last_connect_at: float | None = None
        self._last_disconnect_at: float | None = None
        self._client = EcoFlowMQTTClient(
            certificate_account=cert_account,
            certificate_password=cert_password,
            device_sn=device_sn,
            message_handler=self._on_message,
            user_id=user_id,
            wss_mode=True,
            enhanced_mode=False,
            # This is the flag that makes "listen-only" true. enhanced_mode
            # alone does not: it only suppresses the energy stream switch,
            # while _on_connect still fires get-all and latestQuotas at the
            # device on every connect.
            listen_only=True,
            status_handler=self._on_status,
        )

    @property
    def frames(self) -> list[dict[str, Any]]:
        """Return the captured frames for diagnostics export, oldest first."""
        with self._capture_lock:
            return self._frames.frames()

    @property
    def sampling(self) -> dict[str, Any]:
        """Return what the probe heard versus what it kept.

        A short frame list has two very different causes - a device that
        says almost nothing, and a device so chatty that the sampling
        thinned it out - and a reader cannot tell them apart from the
        frames alone.
        """
        with self._capture_lock:
            return self._frames.stats()

    @property
    def topics(self) -> list[str]:
        """Return which topics delivered data.

        Tells a reader whether the device is silent or simply undecodable,
        which are two very different problems.
        """
        with self._capture_lock:
            return sorted(self._topics)

    @property
    def connection(self) -> dict[str, Any]:
        """Return what happened to the connection, in plain terms.

        A capture that comes back empty has several causes that look
        identical from the frame list: the broker refused the login, the
        link went down hours ago and never came back, or the device simply
        had nothing to say. The reader of a diagnostics download has no
        access to the machine it was made on, so anything not recorded
        here is lost.
        """
        now = time.time()
        with self._capture_lock:
            established = self._connects
            last_rc = self._last_rc
            last_reason = self._last_reason
            last_connect_at = self._last_connect_at
            last_disconnect_at = self._last_disconnect_at
            out: dict[str, Any] = {
                "connected": self._client.is_connected(),
                "ever_connected": established > 0,
                "connect_attempts": self._connect_calls,
                "sessions": established,
                "disconnects": self._disconnects,
                "capture_age_s": round(now - self._started_at),
            }

        if last_rc is not None:
            out["last_rc"] = last_rc
            out["last_rc_reason"] = last_reason
        if last_connect_at is not None:
            out["last_connect_age_s"] = round(now - last_connect_at)
        if last_disconnect_at is not None:
            out["last_disconnect_age_s"] = round(now - last_disconnect_at)
        out["verdict"] = self._verdict(out)
        return out

    @staticmethod
    def _verdict(state: dict[str, Any]) -> str:
        """Summarise the connection state in one sentence.

        Written for the person reading an attached diagnostics file, who
        should not have to infer the story from six counters.
        """
        if state["connected"]:
            return "listening"
        if not state["ever_connected"]:
            rc = state.get("last_rc")
            if rc:
                return (
                    f"never connected - the broker refused the session "
                    f"(rc={rc}, {state.get('last_rc_reason', 'unknown')})"
                )
            return "never connected - no reply from the broker"
        return (
            "connected earlier and is down now - the frames below are what "
            "arrived while it was up"
        )

    def _on_status(self, status: str, rc: int, message: str) -> None:
        """Record a connection event (paho thread).

        The client's message is stored as-is: it words each event in the
        vocabulary that fits it (CONNACK reasons for a refused connect,
        "Disconnected (rc=N)" for a drop). Re-deriving the words here from
        the CONNACK table would caption disconnects with connect-refusal
        language - disconnect reason codes are a different namespace, and
        under paho 2.x a routine drop arrives as 128 or 141, which that
        table cannot name. The messages contain no device or account
        identifiers, which is what makes storing them export-safe.
        """
        with self._capture_lock:
            if status == "connected":
                self._connects += 1
                self._last_connect_at = time.time()
                self._last_rc = 0
                self._last_reason = "connected"
            else:
                if status == "disconnected":
                    self._disconnects += 1
                    self._last_disconnect_at = time.time()
                self._last_rc = rc
                self._last_reason = message

    def async_check_connection(self) -> None:
        """Reconnect if the session has dropped (event loop).

        The client's own retry keeps the client id it was created with and
        this broker rejects a used one, so a drop is permanent without
        this. ``try_reconnect`` builds a fresh client id, backs off on its
        own and never gives up for good, which is exactly the behaviour a
        24 hour capture needs.
        """
        if self._stopped or self._client.is_connected():
            return

        async def _run() -> None:
            await self.hass.async_add_executor_job(self._reconnect)

        # A background task rather than a loose executor job: Home Assistant
        # owns it, cancels it at shutdown, and it is visible while it runs.
        # A bare async_add_executor_job started from a callback is tracked by
        # nobody, which is both untidy at shutdown and a race in any test
        # that waits for the work to land.
        self.hass.async_create_background_task(
            _run(), f"ecoflow probe reconnect {self.device_sn[:4]}", eager_start=True
        )

    def _reconnect(self) -> None:
        """Run one reconnect attempt (executor thread)."""
        if self._stopped:
            return
        try:
            # Counted per attempt handed to the client, which may still hold
            # one back briefly under its own backoff - so this reads as "how
            # often a connect was driven", slightly above the number of
            # connects actually tried. The alternative (counting only what
            # the client let through) would make a link retried all day look
            # like one never tried, which is the worse lie.
            with self._capture_lock:
                self._connect_calls += 1
            self._client.try_reconnect()
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Probe reconnect failed for %s...", self.device_sn[:4], exc_info=True
            )
        if self._stopped:
            # A stop landed while the attempt ran. The flag is set before the
            # stop's disconnect starts, so reaching this line means whatever
            # the attempt just built may have been missed by that disconnect
            # - tear it down again. Disconnecting twice is harmless; leaving
            # a session up with no owner is not.
            try:
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Probe post-stop disconnect failed for %s...",
                    self.device_sn[:4],
                    exc_info=True,
                )

    async def async_start(self) -> bool:
        """Connect and subscribe. Returns False if the connection failed.

        A failure here is not an error for the user: the device has no
        support either way. It is recorded in diagnostics and logged at
        debug level.
        """
        try:
            return await self.hass.async_add_executor_job(self._connect)
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Probe connect failed for %s...", self.device_sn[:4], exc_info=True
            )
            return False

    def _connect(self) -> bool:
        """Create and start the MQTT client (executor thread).

        All three steps are required. ``connect()`` only opens the socket;
        without ``start_loop()`` nobody reads it, so CONNACK is never
        processed, the subscribe never happens, and not a single frame
        arrives - while the probe still reports success.
        """
        with self._capture_lock:
            self._connect_calls += 1
        if not self._client.create_client():
            return False
        if not self._client.connect():
            return False
        self._client.start_loop()
        return True

    async def async_stop(self) -> None:
        """Disconnect the listen-only session.

        The flag goes up before the disconnect starts: any reconnect that
        checks it afterwards backs off, and one already past the check
        re-checks after its attempt and tears its own work down again.
        """
        self._stopped = True
        try:
            await self.hass.async_add_executor_job(self._client.disconnect)
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Probe disconnect failed for %s...", self.device_sn[:4], exc_info=True
            )

    def _on_message(self, topic: str, payload: bytes) -> None:
        """Capture one frame (Paho thread).

        Everything is swallowed: this path exists only to help add support
        for a device, and it must never destabilise an installation that
        works fine otherwise.
        """
        try:
            if not is_proto_frame(payload):
                # JSON pushes are captured too: an unsupported device may
                # well speak JSON, and its key names are exactly what a
                # parser would be built from.
                entry = {
                    "format": "json",
                    **build_frame_entry(
                        topic, payload, self._secrets(), RAW_FRAME_MAX_BYTES
                    ),
                }
            else:
                entry = build_frame_entry(
                    topic, payload, self._secrets(), RAW_FRAME_MAX_BYTES
                )
                entry["format"] = "proto"
                entry["cmds"] = decode_cmd_headers(payload)
            # Derived before the lock: the key comes from the payload, which
            # for a JSON push means a mask plus a parse, and the Paho thread
            # should not hold the lock across that.
            key = frame_key(entry, payload, self._secrets())
            with self._capture_lock:
                self._topics.add(self._mask_topic(topic))
                self._frames.add(key, entry)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Probe frame capture failed", exc_info=True)

    def _mask_topic(self, topic: str) -> str:
        """Return the topic with every account identifier removed.

        Topics are exported to diagnostics, which users attach to public
        issue reports. The app topics carry the EcoFlow user id
        (``/app/{user_id}/{sn}/...``) and the open topics carry the
        certificate account, so masking the serial alone is not enough.
        """
        masked = topic
        for secret, placeholder in (
            (self.device_sn, "{sn}"),
            (self._user_id, "{uid}"),
            (self._cert_account, "{acct}"),
        ):
            if secret:
                masked = masked.replace(secret, placeholder)
        return masked

    def _secrets(self) -> list[str]:
        """Return the identifiers to mask out of a stored frame."""
        return [self.device_sn, self._user_id, self._cert_account]


async def async_start_probes(
    hass: HomeAssistant,
    skipped_devices: list[dict[str, str]],
    email: str,
    password: str,
) -> list[UnroutedDeviceProbe]:
    """Start a listen-only probe for every skipped device.

    One app login serves all probes. If the login or the credential fetch
    fails, no probe is started and the caller carries on: this is a
    diagnostics aid, never a setup dependency.
    """
    if not skipped_devices or not email or not password:
        return []

    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from .ecoflow.app_api import AppApiClient

    try:
        app_api = AppApiClient(async_get_clientsession(hass), email, password)
        if not await app_api.login():
            _LOGGER.debug("Probe login failed - no raw capture for skipped devices")
            return []
        creds = await app_api.get_mqtt_credentials()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Probe credential fetch failed", exc_info=True)
        return []

    if not creds:
        return []

    cert_account = creds.get("certificateAccount") or creds.get("userName", "")
    cert_password = creds.get("certificatePassword") or creds.get("password", "")
    user_id = app_api.user_id or ""
    if not cert_account or not cert_password:
        return []

    probes: list[UnroutedDeviceProbe] = []
    for item in skipped_devices:
        sn = item.get("sn")
        if not sn:
            continue
        probe = UnroutedDeviceProbe(
            hass,
            sn,
            item.get("product_name", ""),
            cert_account,
            cert_password,
            user_id,
        )
        started = await probe.async_start()
        # Kept either way. A probe dropped here leaves the diagnostics
        # download saying "no probe running", which reads as a login
        # problem and hides the actual reason, and nothing would ever
        # retry - the watchdog only sees the probes on this list.
        probes.append(probe)
        _LOGGER.debug(
            "Capturing raw data for unsupported device %s... for diagnostics "
            "(initial connect: %s)",
            sn[:4],
            "ok" if started else "failed, will retry",
        )
    return probes


def async_start_probe_watchdog(
    hass: HomeAssistant, probes: list[UnroutedDeviceProbe]
) -> CALLBACK_TYPE:
    """Keep the listen-only sessions alive for the length of the capture.

    Returns the unsubscribe callback. A capture runs for up to 24 hours
    and every WSS session gets dropped in that time; since paho cannot
    re-establish one on its own here (used client ids are refused), a
    capture without this holds only whatever arrived before the first
    drop, and usually nothing at all.
    """

    @callback
    def _check(_now: Any) -> None:
        # Must be a callback: a plain function is run in the executor, where
        # scheduling the reconnect job has no event loop to attach to.
        for probe in probes:
            probe.async_check_connection()

    return async_track_time_interval(
        hass, _check, timedelta(seconds=PROBE_WATCHDOG_INTERVAL_S)
    )
