"""MQTT credential lifecycle for the EcoFlow device coordinator."""

from __future__ import annotations

import logging
import time

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..const import (
    AUTH_METHOD_APP,
    CONF_EMAIL,
    CONF_PASSWORD,
    CREDENTIAL_MAX_AGE_S,
    CREDENTIAL_REFRESH_CHECK_S,
)

_LOGGER = logging.getLogger(__name__)


class CredentialsMixin:
    """Mixin providing reactive and proactive MQTT credential refresh."""

    # ------------------------------------------------------------------
    # Delta MQTT auth error handling (credential refresh on rc=5)
    # ------------------------------------------------------------------

    def _on_mqtt_auth_error(self) -> None:
        """Handle MQTT AUTH error (rc=5) - schedule credential refresh."""
        _LOGGER.warning("MQTT AUTH error for %s - scheduling credential refresh", self.device_sn)
        self._log_event("reauth", "mqtt_auth_error")
        self.hass.loop.call_soon_threadsafe(
            self.hass.async_create_task,
            self._refresh_mqtt_credentials(),
        )

    async def _refresh_mqtt_credentials(self) -> None:
        """Refresh MQTT credentials after AUTH failure."""
        if self._mqtt_client is None:
            return

        auth_method = self._auth_method

        if auth_method == AUTH_METHOD_APP:
            # App-auth: re-login and re-fetch portal credentials
            session = async_get_clientsession(self.hass)
            from ..ecoflow.app_api import AppApiClient

            email = self._entry.data.get(CONF_EMAIL, "")
            password = self._entry.data.get(CONF_PASSWORD, "")
            if not email or not password:
                _LOGGER.warning("App-auth credential refresh failed for %s - no credentials", self.device_sn)
                self._entry.async_start_reauth(self.hass)
                return

            app_api = AppApiClient(session, email, password)
            if not await app_api.login():
                _LOGGER.warning("App-auth credential refresh failed for %s - login failed", self.device_sn)
                self._entry.async_start_reauth(self.hass)
                return

            creds = await app_api.get_mqtt_credentials()
            if creds is not None:
                cert_account = creds.get("certificateAccount") or creds.get("userName", "")
                cert_password = creds.get("certificatePassword") or creds.get("password", "")
                self._mqtt_client.update_credentials(cert_account, cert_password)
                self._credential_obtained_ts = time.monotonic()
                self._log_event("credential_refresh_ok", "app-auth")
                _LOGGER.debug("App-auth MQTT credentials refreshed for %s", self.device_sn)
            else:
                self._log_event("credential_refresh_fail", "app-auth, no credentials")
                _LOGGER.warning("App-auth credential refresh failed for %s - triggering re-authentication", self.device_sn)
                self._entry.async_start_reauth(self.hass)
        else:
            # Developer-auth: use IoT API
            if self._iot_api is None:
                return
            creds = await self._iot_api.refresh_credentials()
            if creds is not None:
                self._mqtt_client.update_credentials(
                    creds.get("certificateAccount", ""),
                    creds.get("certificatePassword", ""),
                )
                self._credential_obtained_ts = time.monotonic()
                self._log_event("credential_refresh_ok", "developer-auth")
                _LOGGER.debug("MQTT credentials refreshed for %s", self.device_sn)
            else:
                self._log_event("credential_refresh_fail", "developer-auth")
                _LOGGER.warning("MQTT credential refresh failed for %s - triggering re-authentication", self.device_sn)
                self._entry.async_start_reauth(self.hass)

    # ------------------------------------------------------------------
    # Proactive credential refresh (before expiry)
    # ------------------------------------------------------------------

    def _schedule_credential_refresh(self) -> None:
        """Schedule periodic credential age check."""
        if self._shutdown:
            return
        self._credential_refresh_unsub = self.hass.loop.call_later(
            CREDENTIAL_REFRESH_CHECK_S, self._check_credential_age,
        )

    def _check_credential_age(self) -> None:
        """Check if credentials are old enough to warrant proactive refresh."""
        if self._shutdown:
            return

        if self._credential_obtained_ts > 0:
            age = time.monotonic() - self._credential_obtained_ts
            if age >= CREDENTIAL_MAX_AGE_S:
                _LOGGER.debug(
                    "Credentials for %s are %.0fh old - proactive refresh",
                    self.device_sn, age / 3600,
                )
                self._log_event("credential_proactive_refresh", f"age={age / 3600:.0f}h")
                self.hass.async_create_task(self._proactive_credential_refresh())
            else:
                _LOGGER.debug(
                    "Credentials for %s are %.0fh old - still fresh",
                    self.device_sn, age / 3600,
                )

        # Re-schedule
        if not self._shutdown:
            self._credential_refresh_unsub = self.hass.loop.call_later(
                CREDENTIAL_REFRESH_CHECK_S, self._check_credential_age,
            )

    async def _proactive_credential_refresh(self) -> None:
        """Proactively refresh credentials before they expire."""
        if self._mqtt_client is None:
            return

        auth_method = self._auth_method
        old_account = self._mqtt_client.cert_account

        if auth_method == AUTH_METHOD_APP:
            session = async_get_clientsession(self.hass)
            from ..ecoflow.app_api import AppApiClient

            email = self._entry.data.get(CONF_EMAIL, "")
            password = self._entry.data.get(CONF_PASSWORD, "")
            if not email or not password:
                return

            app_api = AppApiClient(session, email, password)
            if not await app_api.login():
                _LOGGER.debug("Proactive credential refresh: login failed for %s", self.device_sn)
                self._log_event("credential_proactive_fail", "login failed")
                return

            creds = await app_api.get_mqtt_credentials()
            if creds is not None:
                cert_account = creds.get("certificateAccount") or creds.get("userName", "")
                cert_password = creds.get("certificatePassword") or creds.get("password", "")
                self._mqtt_client.update_credentials(cert_account, cert_password)
                self._credential_obtained_ts = time.monotonic()
                self._log_event("credential_proactive_ok", "app-auth")
                if cert_account != old_account:
                    _LOGGER.debug("Proactive refresh: credentials changed for %s - force reconnect", self.device_sn)
                    self.hass.async_add_executor_job(self._mqtt_client.force_reconnect)
            else:
                self._log_event("credential_proactive_fail", "no credentials")
        else:
            if self._iot_api is None:
                return
            creds = await self._iot_api.refresh_credentials()
            if creds is not None:
                cert_account = creds.get("certificateAccount", "")
                cert_password = creds.get("certificatePassword", "")
                self._mqtt_client.update_credentials(cert_account, cert_password)
                self._credential_obtained_ts = time.monotonic()
                self._log_event("credential_proactive_ok", "developer-auth")
                if cert_account != old_account:
                    _LOGGER.debug("Proactive refresh: credentials changed for %s - force reconnect", self.device_sn)
                    self.hass.async_add_executor_job(self._mqtt_client.force_reconnect)
            else:
                self._log_event("credential_proactive_fail", "api failed")

