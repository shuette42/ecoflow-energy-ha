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
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

from homeassistant.core import HomeAssistant

from .const import RAW_FRAME_LOG_MAX, RAW_FRAME_MAX_BYTES
from .ecoflow.cloud_mqtt import EcoFlowMQTTClient
from .ecoflow.frame_capture import build_frame_entry, decode_cmd_headers, is_proto_frame

_LOGGER = logging.getLogger(__name__)


class UnroutedDeviceProbe:
    """Listen-only frame capture for one unsupported device.

    Deliberately minimal: no keep-alive timers, no re-subscribe logic, no
    SET path. A diagnostics helper must never become a second,
    half-maintained coordinator.

    The connection is opened with ``listen_only=True``, which is what
    actually holds the no-write promise: the shared client fires get-all
    and latestQuotas at the device on every connect otherwise. Note that
    paho reconnects on its own after a drop, so the probe is quiet in the
    sense that it adds no logic of its own, not in the sense that the
    socket stays down.
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
        self._frames: deque[dict[str, Any]] = deque(maxlen=RAW_FRAME_LOG_MAX)
        self._topics: set[str] = set()
        # Written on the Paho thread, read on the event loop when diagnostics
        # are downloaded. On CPython the reads happen to be safe already:
        # sorted(set) and list(deque) run entirely in C, so no other Python
        # thread can observe a half-built container. That is an implementation
        # detail of the GIL, not a property of the code - it does not hold on
        # a free-threaded build. Cheap enough to not depend on.
        self._capture_lock = threading.Lock()
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
        )

    @property
    def frames(self) -> list[dict[str, Any]]:
        """Return the captured frames for diagnostics export."""
        with self._capture_lock:
            return list(self._frames)

    @property
    def topics(self) -> list[str]:
        """Return which topics delivered data.

        Tells a reader whether the device is silent or simply undecodable,
        which are two very different problems.
        """
        with self._capture_lock:
            return sorted(self._topics)

    @property
    def connected(self) -> bool:
        """Return whether the listen-only session is up."""
        return self._client.is_connected()

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
        if not self._client.create_client():
            return False
        if not self._client.connect():
            return False
        self._client.start_loop()
        return True

    async def async_stop(self) -> None:
        """Disconnect the listen-only session."""
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
            with self._capture_lock:
                self._topics.add(self._mask_topic(topic))
                self._frames.append(entry)
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
        if await probe.async_start():
            probes.append(probe)
            _LOGGER.debug(
                "Capturing raw data for unsupported device %s... for diagnostics",
                sn[:4],
            )
    return probes
