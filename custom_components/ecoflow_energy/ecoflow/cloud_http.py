"""EcoFlow HTTP Quota API client (async).

Fetches device quota data via the EcoFlow IoT Developer HTTP API.
Uses GET /iot-open/sign/device/quota/all?sn=... for all device types.

See: https://developer-eu.ecoflow.com/us/document/generalInfo
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import time
from typing import Any

import aiohttp

from .const import (
    HTTP_RETRIES,
    HTTP_RETRY_BACKOFF_S,
    IOT_API_BASE,
    IOT_QUOTA_ALL_PATH,
    QUOTA_HTTP_MIN_INTERVAL_S,
)

_LOGGER = logging.getLogger(__name__)


class EcoFlowHTTPQuota:
    """Async HTTP Quota API client with rate-limiting and HMAC-SHA256 signing."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_key: str,
        secret_key: str,
        device_sn: str,
        base_url: str = IOT_API_BASE,
        min_interval: float = QUOTA_HTTP_MIN_INTERVAL_S,
    ) -> None:
        self._session = session
        self._access_key = access_key
        self._secret_key = secret_key
        self._device_sn = device_sn
        self._base_url = base_url.rstrip("/")
        self._min_interval = min_interval
        self._last_call: float = 0.0
        self.last_error_code: str | None = None
        self._logged_1006: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_quota_all(self) -> dict | None:
        """Fetch all quotas via GET /iot-open/sign/device/quota/all?sn=...

        No request body — SN is passed as query parameter.
        Response: {"code": "0", "data": {"pd.soc": 83, "inv.outputWatts": 0, ...}}
        """
        if not self._check_rate_limit():
            return None

        url = f"{self._base_url}{IOT_QUOTA_ALL_PATH}"
        query = {"sn": self._device_sn}

        return await self._request_with_retry("GET", url, query=query)

    # ------------------------------------------------------------------
    # Signature
    # ------------------------------------------------------------------

    def _flatten(self, obj: Any, parent: str = "") -> list[tuple[str, str]]:
        """Flatten nested objects for API signature (EcoFlow spec)."""
        items: list[tuple[str, str]] = []
        if isinstance(obj, dict):
            for k in obj.keys():
                new_key = f"{parent}.{k}" if parent else k
                items.extend(self._flatten(obj[k], new_key))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                new_key = f"{parent}[{i}]"
                items.extend(self._flatten(v, new_key))
        else:
            items.append((parent, str(obj)))
        return items

    def _sign_headers(self, params_dict: dict) -> dict:
        """Create HMAC-SHA256 signed headers.

        params_dict is the flattened request parameters (body or query).
        """
        ts = str(int(time.time() * 1000))
        nonce = str(random.randint(100000, 999999))

        flat = self._flatten(params_dict)
        flat.sort(key=lambda kv: kv[0])

        kv_string = "&".join(f"{k}={v}" for k, v in flat)
        tail = f"accessKey={self._access_key}&nonce={nonce}&timestamp={ts}"
        sign_string = (kv_string + "&" if kv_string else "") + tail

        sig = hmac.new(
            self._secret_key.encode("utf-8"),
            sign_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return {
            "accessKey": self._access_key,
            "nonce": nonce,
            "timestamp": ts,
            "sign": sig,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _check_rate_limit(self) -> bool:
        """Check and update rate limit. Returns True if request is allowed."""
        now = time.monotonic()
        if now - self._last_call < self._min_interval:
            _LOGGER.debug("HTTP: rate-limited (%.1fs since last call)", now - self._last_call)
            return False
        self._last_call = now
        return True

    class _RetryableAPIError(Exception):
        """API returned a transient error code that should be retried."""

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        body: dict | None = None,
        query: dict | None = None,
    ) -> dict | None:
        """Execute an HTTP request with retry logic."""
        for attempt in range(1, HTTP_RETRIES + 1):
            try:
                # Sign: for POST use body params, for GET use query params
                sign_params = body if body else query if query else {}
                headers = self._sign_headers(sign_params)
                timeout = aiohttp.ClientTimeout(total=10)

                if method == "POST":
                    headers["Content-Type"] = "application/json;charset=UTF-8"
                    body_json = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
                    async with self._session.post(
                        url, headers=headers, data=body_json.encode("utf-8"), timeout=timeout,
                    ) as resp:
                        return await self._handle_response(resp)
                else:
                    async with self._session.get(
                        url, headers=headers, params=query, timeout=timeout,
                    ) as resp:
                        return await self._handle_response(resp)

            except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError, self._RetryableAPIError) as exc:
                if attempt < HTTP_RETRIES:
                    _LOGGER.debug("HTTP %s: error (attempt %d/%d): %s", method, attempt, HTTP_RETRIES, exc)
                else:
                    _LOGGER.warning("HTTP %s: error (attempt %d/%d): %s", method, attempt, HTTP_RETRIES, exc)

            if attempt < HTTP_RETRIES:
                await asyncio.sleep(HTTP_RETRY_BACKOFF_S)

        self.last_error_code = "network"
        _LOGGER.error("HTTP: all %d attempts failed for %s", HTTP_RETRIES, self._device_sn)
        return None

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> dict | None:
        """Parse and validate an API response."""
        data = await resp.json()
        code = str(data.get("code"))

        if resp.ok and code == "0":
            _LOGGER.debug("HTTP: quota OK for %s", self._device_sn)
            self.last_error_code = None
            self._logged_1006 = False
            return data.get("data") or {}

        # EcoFlow error 8521 is a transient server-side error — retry
        if code == "8521":
            _LOGGER.debug("HTTP: transient error 8521 for %s — will retry", self._device_sn)
            raise self._RetryableAPIError(f"code={code}")

        # Error 1006: device not linked to API key — not an auth failure (#2)
        if code == "1006":
            self.last_error_code = "1006"
            if not self._logged_1006:
                _LOGGER.warning(
                    "HTTP: device %s not linked to API key — "
                    "verify device binding at developer.ecoflow.com (code=1006)",
                    self._device_sn,
                )
                self._logged_1006 = True
            else:
                _LOGGER.debug("HTTP: device %s still returns 1006", self._device_sn)
            return None

        self.last_error_code = code
        _LOGGER.warning("HTTP: quota code=%s msg=%s (sn=%s)", code, data.get("message"), self._device_sn)
        return None

