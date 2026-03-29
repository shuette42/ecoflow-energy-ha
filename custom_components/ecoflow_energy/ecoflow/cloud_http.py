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

logger = logging.getLogger(__name__)


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
        now = time.time()
        if now - self._last_call < self._min_interval:
            logger.debug("HTTP: rate-limited (%.1fs since last call)", now - self._last_call)
            return False
        self._last_call = now
        return True

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

            except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
                logger.warning("HTTP %s: error (attempt %d/%d): %s", method, attempt, HTTP_RETRIES, exc)

            if attempt < HTTP_RETRIES:
                await asyncio.sleep(HTTP_RETRY_BACKOFF_S)

        logger.error("HTTP: all %d attempts failed for %s", HTTP_RETRIES, self._device_sn)
        return None

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> dict | None:
        """Parse and validate an API response."""
        data = await resp.json()
        code = str(data.get("code"))

        if resp.ok and code == "0":
            logger.debug("HTTP: quota OK for %s", self._device_sn)
            return data.get("data") or {}
        else:
            logger.warning("HTTP: quota code=%s msg=%s (sn=%s)", code, data.get("message"), self._device_sn)
            return None

