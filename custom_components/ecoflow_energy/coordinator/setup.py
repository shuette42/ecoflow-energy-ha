"""Coordinator setup, auth bootstrap, MQTT start, and shutdown."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..const import (
    AUTH_METHOD_APP,
    AUTH_METHOD_DEVELOPER,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_SECRET_KEY,
    CONF_USER_ID,
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_POWERSTREAM,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_STREAM,
    HTTP_FALLBACK_INTERVAL_S,
    raw_capture_window_open,
)
from ..ecoflow.broker import broker_from_credentials
from ..ecoflow.cloud_http import EcoFlowHTTPQuota
from ..ecoflow.cloud_mqtt import EcoFlowMQTTClient
from ..ecoflow.iot_api import IoTApiClient

_LOGGER = logging.getLogger(__name__)


class SetupMixin:
    """Mixin providing coordinator setup and teardown."""

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Set up the data source for this device."""
        self._auth_method = self._entry.data.get(CONF_AUTH_METHOD, AUTH_METHOD_DEVELOPER)
        session = async_get_clientsession(self.hass)

        # Load energy integrator state from disk (non-blocking)
        await self.hass.async_add_executor_job(self._energy_integrator.load_state)

        if self._auth_method == AUTH_METHOD_APP:
            await self._setup_app_auth(session)
        else:
            await self._setup_developer_auth(session)

    async def _setup_app_auth(self, session: Any) -> None:
        """Set up using app authentication (email/password, no Developer API keys).

        App-auth always uses WSS MQTT. No HTTP client or IoT API.
        """
        from ..ecoflow.app_api import AppApiClient

        email = self._entry.data.get(CONF_EMAIL, "")
        password = self._entry.data.get(CONF_PASSWORD, "")

        if not email or not password:
            _LOGGER.error("App-auth: missing credentials for %s", self.device_sn[:4])
            self._entry.async_start_reauth(self.hass)
            return

        app_api = AppApiClient(session, email, password)
        if not await app_api.login():
            _LOGGER.warning("App-auth: login failed for %s - triggering re-authentication", self.device_sn[:4])
            self._entry.async_start_reauth(self.hass)
            return

        user_id = app_api.user_id or self._entry.data.get(CONF_USER_ID, "")

        # No IoT API, no HTTP client for app-auth
        self._iot_api = None
        self._http_client = None

        # Fetch portal MQTT credentials (AES-decrypted app-* creds)
        creds = await app_api.get_mqtt_credentials()
        if creds is None:
            _LOGGER.error("App-auth: failed to fetch MQTT credentials for %s", self.device_sn[:4])
            self._entry.async_start_reauth(self.hass)
            return

        cert_account = creds.get("certificateAccount") or creds.get("userName", "")
        cert_password = creds.get("certificatePassword") or creds.get("password", "")
        # The response names the broker these credentials belong to. An
        # account served outside the region the built-in default points at
        # gets refused there, without ever saying why (issue #184).
        broker = broker_from_credentials(creds, wss_mode=True)

        self._mqtt_client = EcoFlowMQTTClient(
            certificate_account=cert_account,
            certificate_password=cert_password,
            device_sn=self.device_sn,
            message_handler=self._on_mqtt_message,
            user_id=user_id,
            mqtt_host=broker.host,
            mqtt_port=broker.port,
            wss_path=broker.path,
            wss_mode=True,
            enhanced_mode=(self._enhanced_mode and self.device_type == DEVICE_TYPE_POWEROCEAN),
            auth_error_handler=self._on_mqtt_auth_error,
            # Read once here, like the buffer depth in core.py: writing the
            # flag reloads the entry and builds a new client, so it never has
            # to change underneath a live subscription.
            capture_writes=raw_capture_window_open(self.config_entry.data),
        )

        self._credential_obtained_ts = time.monotonic()
        await self.hass.async_add_executor_job(self._start_mqtt)

        if self._enhanced_mode:
            if self.device_type == DEVICE_TYPE_POWEROCEAN:
                self._schedule_keepalive()
            # The Delta 3 generation never answers the quota request - it
            # pushes its status frame on its own schedule. The request is
            # kept anyway: it is one small publish every 30 s and it keeps
            # outbound traffic on the connection, which is what the other
            # device families rely on to hold the session open.
            self._schedule_quotas_poll()
        self._schedule_ping()
        self._schedule_stale_check()
        self._schedule_credential_refresh()

        _LOGGER.debug(
            "App-auth setup complete for %s (enhanced=%s)",
            self.device_sn[:4], self._enhanced_mode,
        )

    async def _setup_developer_auth(self, session: Any) -> None:
        """Set up using Developer API keys (existing flow, unchanged)."""
        access_key = self._entry.data.get(CONF_ACCESS_KEY)
        secret_key = self._entry.data.get(CONF_SECRET_KEY)

        if not access_key or not secret_key:
            _LOGGER.error("Developer API keys missing for %s - triggering re-authentication", self.device_sn[:4])
            self._entry.async_start_reauth(self.hass)
            return

        self._iot_api = IoTApiClient(session, access_key, secret_key)

        self._http_client = EcoFlowHTTPQuota(
            session, access_key, secret_key, self.device_sn,
        )

        # Standard Mode: HTTP polling is the primary data source.
        # MQTT is for SET commands only - except Delta, Smart Plug, Stream
        # and PowerStream, which also subscribe to the IoT MQTT /quota topic
        # for real-time push alongside HTTP polling. A subscription that
        # stays silent costs nothing here: with developer keys the HTTP poll
        # is the primary source and a stale MQTT only keeps it running.
        subscribe_mqtt = self.device_type in (
            DEVICE_TYPE_DELTA,
            DEVICE_TYPE_DELTA3,
            DEVICE_TYPE_POWERSTREAM,
            DEVICE_TYPE_SMARTPLUG,
            DEVICE_TYPE_STREAM,
        )
        creds = await self._iot_api.get_mqtt_credentials()
        if creds is not None:
            cert_account = creds.get("certificateAccount", "")
            cert_password = creds.get("certificatePassword", "")
            broker = broker_from_credentials(creds, wss_mode=False)
            self._mqtt_client = EcoFlowMQTTClient(
                certificate_account=cert_account,
                certificate_password=cert_password,
                device_sn=self.device_sn,
                message_handler=self._on_mqtt_message,
                user_id="",
                mqtt_host=broker.host,
                mqtt_port=broker.port,
                wss_mode=False,
                subscribe_data=subscribe_mqtt,
                auth_error_handler=(
                    self._on_mqtt_auth_error if subscribe_mqtt else None
                ),
            )
            self._credential_obtained_ts = time.monotonic()
            await self.hass.async_add_executor_job(self._start_mqtt)
        if subscribe_mqtt:
            _LOGGER.debug(
                "Standard Mode + MQTT push: HTTP every %ds + MQTT real-time for %s",
                HTTP_FALLBACK_INTERVAL_S, self.device_sn[:4],
            )
        else:
            _LOGGER.debug(
                "Standard Mode: HTTP polling every %ds for %s",
                HTTP_FALLBACK_INTERVAL_S, self.device_sn[:4],
            )

    def _start_mqtt(self) -> None:
        """Start the MQTT client (runs in executor thread)."""
        if self._mqtt_client is None:
            return
        if self._mqtt_client.create_client():
            if self._mqtt_client.connect():
                self._mqtt_client.start_loop()
                mode_label = "WSS Enhanced" if self._enhanced_mode else "TCP Standard"
                _LOGGER.info("MQTT started for %s (%s)", self.device_sn[:4], mode_label)
                self._log_event("mqtt_connect", mode_label)
            else:
                _LOGGER.error("MQTT connect failed for %s", self.device_sn[:4])
                self._log_event("mqtt_disconnect", "connect failed")
        else:
            _LOGGER.error("MQTT client creation failed for %s", self.device_sn[:4])
            self._log_event("mqtt_disconnect", "client creation failed")

    async def async_shutdown(self) -> None:
        """Await the one cancellation-safe coordinator cleanup task."""
        if self._shutdown_task is None:
            self._shutdown = True
            self._shutdown_task = self._entry.async_create_task(
                self.hass,
                self._async_shutdown_cleanup(),
                name=f"EcoFlow shutdown {self.device_sn[:4]}",
            )
        await asyncio.shield(self._shutdown_task)

    async def _async_shutdown_cleanup(self) -> None:
        """Drain in-flight writes, then stop MQTT and coordinator resources."""
        for handle in (
            self._keepalive_unsub, self._quotas_unsub, self._ping_unsub,
            self._stale_check_unsub, self._credential_refresh_unsub,
            self._powerocean_soc_debounce_unsub,
        ):
            if handle is not None:
                handle.cancel()
        self._keepalive_unsub = None
        self._quotas_unsub = None
        self._ping_unsub = None
        self._stale_check_unsub = None
        self._credential_refresh_unsub = None
        self._powerocean_soc_debounce_unsub = None

        # Invalidate every claimed cycle before waiting. A publish already
        # executing in HA's thread pool cannot be stopped by cancelling its
        # asyncio wrapper; it is instead allowed to finish, and the post-await
        # shutdown guard prevents its old result from mutating state.
        self._powerocean_soc_generation += 1
        self._powerocean_soc_pending = None
        self._powerocean_soc_pending_revision = 0
        self._powerocean_soc_cycle_open = False
        self._powerocean_soc_before = {}
        self._powerocean_soc_active_revisions.clear()
        self._powerocean_soc_latest_outcome = None
        task_errors: list[BaseException] = []
        while self._powerocean_soc_flush_tasks or self._powerocean_soc_write_tasks:
            tasks = tuple(
                self._powerocean_soc_flush_tasks
                | self._powerocean_soc_write_tasks
            )
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            self._powerocean_soc_flush_tasks.difference_update(tasks)
            self._powerocean_soc_write_tasks.difference_update(tasks)
            task_errors.extend(
                outcome
                for outcome in outcomes
                if isinstance(outcome, BaseException)
            )
        # Set iteration order is deliberately irrelevant to the propagated
        # outcome: task failures are sorted before ordered teardown stages.
        task_errors.sort(
            key=lambda error: (
                type(error).__module__,
                type(error).__qualname__,
                str(error),
            )
        )
        cleanup_errors = task_errors

        if self._mqtt_client is not None:
            try:
                await self.hass.async_add_executor_job(self._mqtt_client.disconnect)
            except BaseException as err:  # noqa: BLE001
                cleanup_errors.append(err)
            finally:
                self._mqtt_client = None
        try:
            await self.hass.async_add_executor_job(
                self._energy_integrator.force_flush
            )
        except BaseException as err:  # noqa: BLE001
            cleanup_errors.append(err)
        try:
            await super().async_shutdown()
        except BaseException as err:  # noqa: BLE001
            cleanup_errors.append(err)

        if cleanup_errors:
            # Task failures are sorted above and teardown stages append in
            # execution order, so every caller observes the same first error.
            raise cleanup_errors[0]

        # Never set this from finally: a failed cleanup must remain failed for
        # every concurrent or later caller.
        self._shutdown_complete.set()
