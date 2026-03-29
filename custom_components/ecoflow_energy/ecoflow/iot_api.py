"""EcoFlow IoT Developer API client (async).

Provides MQTT credentials via /iot-open/sign/certification
and device listing via /iot-open/sign/device/list.
Uses aiohttp for async HTTP — HA provides the ClientSession.
"""

import hashlib
import hmac
import logging
import random
import time
from typing import Any, Dict, Optional

import aiohttp

from .const import IOT_API_BASE, IOT_CERT_PATH, IOT_DEVICE_LIST_PATH, IOT_MIN_FETCH_INTERVAL_S

logger = logging.getLogger(__name__)

_Credentials = Dict[str, Any]


class IoTApiClient:
    """IoT Developer API — HMAC-SHA256 signed async client.

    Uses /iot-open/sign/certification to fetch MQTT credentials.
    Memory cache with rate-limit guard (max 1 fetch / 60 s).
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_key: str,
        secret_key: str,
        base_url: str = IOT_API_BASE,
    ) -> None:
        self._session = session
        self._access_key = access_key
        self._secret_key = secret_key
        self._base_url = base_url.rstrip("/")

        self._cached: Optional[_Credentials] = None
        self._last_fetch_ts: float = 0.0

    async def get_mqtt_credentials(self) -> Optional[_Credentials]:
        """Return cached credentials or fetch on-demand.

        Returns:
            dict with certificateAccount, certificatePassword,
            url, port, protocol — or None on error.
        """
        if self._cached is not None:
            return self._cached
        return await self._fetch()

    async def refresh_credentials(self) -> Optional[_Credentials]:
        """Force a new fetch (e.g. after AUTH error rc=5).

        Respects the rate-limit guard (60 s).
        """
        self._cached = None
        return await self._fetch()

    async def get_device_list(self) -> Optional[list]:
        """Fetch the list of bound devices.

        Returns:
            list of device dicts or None on error.
        """
        url = f"{self._base_url}{IOT_DEVICE_LIST_PATH}"
        headers = self._make_signed_headers()
        try:
            async with self._session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                body = await resp.json()
                data = body.get("data")
                if not data:
                    logger.warning("IoT API device list: empty response — code=%s", body.get("code"))
                    return None
                return data
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.warning("IoT API device list failed — %s", exc)
            return None

    @staticmethod
    def sign(params: Dict[str, str], secret_key: str) -> str:
        """HMAC-SHA256 signature over alphabetically sorted parameters."""
        sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return hmac.new(
            secret_key.encode(),
            sorted_params.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _make_signed_headers(self) -> Dict[str, str]:
        """Build signed request headers."""
        nonce = str(random.randint(100000, 999999))
        timestamp = str(int(time.time() * 1000))
        params = {
            "accessKey": self._access_key,
            "nonce": nonce,
            "timestamp": timestamp,
        }
        sig = self.sign(params, self._secret_key)
        return {
            "accessKey": self._access_key,
            "nonce": nonce,
            "timestamp": timestamp,
            "sign": sig,
        }

    async def _fetch(self) -> Optional[_Credentials]:
        """Fetch credentials from the API (with rate-limit guard)."""
        now = time.time()
        if (now - self._last_fetch_ts) < IOT_MIN_FETCH_INTERVAL_S:
            logger.debug(
                "IoT API: rate-limited — next fetch in %.0f s",
                IOT_MIN_FETCH_INTERVAL_S - (now - self._last_fetch_ts),
            )
            return self._cached

        if not self._access_key or not self._secret_key:
            logger.warning("IoT API: access_key / secret_key not set")
            return None

        self._last_fetch_ts = now
        headers = self._make_signed_headers()
        url = f"{self._base_url}{IOT_CERT_PATH}"

        try:
            async with self._session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                body = await resp.json()
                data = body.get("data")
                if not data:
                    logger.warning("IoT API: empty response — code=%s", body.get("code"))
                    return None
                self._cached = data
                logger.debug(
                    "IoT API: credentials obtained (account=%s..., keys=%s)",
                    str(data.get("certificateAccount", ""))[:6],
                    sorted(data.keys()) if isinstance(data, dict) else "n/a",
                )
                return data
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.warning("IoT API: fetch failed — %s", exc)
            return None
