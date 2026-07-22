"""Coordinator setup, auth bootstrap, MQTT start, and shutdown."""

from __future__ import annotations

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
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_STREAM,
    HTTP_FALLBACK_INTERVAL_S,
)
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
            _LOGGER.error("App-auth: missing credentials for %s", self.device_sn)
            self._entry.async_start_reauth(self.hass)
            return

        app_api = AppApiClient(session, email, password)
        if not await app_api.login():
            _LOGGER.warning("App-auth: login failed for %s - triggering re-authentication", self.device_sn)
            self._entry.async_start_reauth(self.hass)
            return

        user_id = app_api.user_id or self._entry.data.get(CONF_USER_ID, "")

        # No IoT API, no HTTP client for app-auth
        self._iot_api = None
        self._http_client = None

        # Fetch portal MQTT credentials (AES-decrypted app-* creds)
        creds = await app_api.get_mqtt_credentials()
        if creds is None:
            _LOGGER.error("App-auth: failed to fetch MQTT credentials for %s", self.device_sn)
            self._entry.async_start_reauth(self.hass)
            return

        cert_account = creds.get("certificateAccount") or creds.get("userName", "")
        cert_password = creds.get("certificatePassword") or creds.get("password", "")

        self._mqtt_client = EcoFlowMQTTClient(
            certificate_account=cert_account,
            certificate_password=cert_password,
            device_sn=self.device_sn,
            message_handler=self._on_mqtt_message,
            user_id=user_id,
            wss_mode=True,
            enhanced_mode=(self._enhanced_mode and self.device_type == DEVICE_TYPE_POWEROCEAN),
            auth_error_handler=self._on_mqtt_auth_error,
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
            self.device_sn, self._enhanced_mode,
        )

    async def _setup_developer_auth(self, session: Any) -> None:
        """Set up using Developer API keys (existing flow, unchanged)."""
        access_key = self._entry.data.get(CONF_ACCESS_KEY)
        secret_key = self._entry.data.get(CONF_SECRET_KEY)

        if not access_key or not secret_key:
            _LOGGER.error("Developer API keys missing for %s - triggering re-authentication", self.device_sn)
            self._entry.async_start_reauth(self.hass)
            return

        self._iot_api = IoTApiClient(session, access_key, secret_key)

        self._http_client = EcoFlowHTTPQuota(
            session, access_key, secret_key, self.device_sn,
        )

        # Standard Mode: HTTP polling is the primary data source.
        # MQTT is for SET commands only - except Delta and Smart Plug,
        # which also subscribe to the IoT MQTT /quota topic for
        # real-time push alongside HTTP polling.
        subscribe_mqtt = self.device_type in (
            DEVICE_TYPE_DELTA,
            DEVICE_TYPE_DELTA3,
            DEVICE_TYPE_SMARTPLUG,
            DEVICE_TYPE_STREAM,
        )
        creds = await self._iot_api.get_mqtt_credentials()
        if creds is not None:
            cert_account = creds.get("certificateAccount", "")
            cert_password = creds.get("certificatePassword", "")
            self._mqtt_client = EcoFlowMQTTClient(
                certificate_account=cert_account,
                certificate_password=cert_password,
                device_sn=self.device_sn,
                message_handler=self._on_mqtt_message,
                user_id="",
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
                HTTP_FALLBACK_INTERVAL_S, self.device_sn,
            )
        else:
            _LOGGER.debug(
                "Standard Mode: HTTP polling every %ds for %s",
                HTTP_FALLBACK_INTERVAL_S, self.device_sn,
            )

    def _start_mqtt(self) -> None:
        """Start the MQTT client (runs in executor thread)."""
        if self._mqtt_client is None:
            return
        if self._mqtt_client.create_client():
            if self._mqtt_client.connect():
                self._mqtt_client.start_loop()
                mode_label = "WSS Enhanced" if self._enhanced_mode else "TCP Standard"
                _LOGGER.info("MQTT started for %s (%s)", self.device_sn, mode_label)
                self._log_event("mqtt_connect", mode_label)
            else:
                _LOGGER.error("MQTT connect failed for %s", self.device_sn)
                self._log_event("mqtt_disconnect", "connect failed")
        else:
            _LOGGER.error("MQTT client creation failed for %s", self.device_sn)
            self._log_event("mqtt_disconnect", "client creation failed")

    async def async_shutdown(self) -> None:
        """Stop the MQTT client and cancel timers."""
        self._shutdown = True
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
        if self._mqtt_client is not None:
            await self.hass.async_add_executor_job(self._mqtt_client.disconnect)
            self._mqtt_client = None
        await self.hass.async_add_executor_job(self._energy_integrator.force_flush)
        await super().async_shutdown()

