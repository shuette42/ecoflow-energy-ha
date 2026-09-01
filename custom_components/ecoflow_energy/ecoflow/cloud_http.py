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
    IOT_QUOTA_PATH,
    QUOTA_HTTP_MIN_INTERVAL_S,
)

_LOGGER = logging.getLogger(__name__)
_HTTP_FAILURE_SUMMARY_INTERVAL_S = 300.0


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
        self._outage_active: bool = False
        self._outage_kind: str | None = None
        self._outage_last_summary_at: float = 0.0
        self._outage_suppressed: int = 0

    @property
    def _sn_display(self) -> str:
        """SN prefix only for logs - never leak a full serial to HA logs."""
        return self._device_sn[:4] + "..."

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_quota_all(self, *, diagnostic: bool = False) -> dict | None:
        """Fetch all quotas via GET /iot-open/sign/device/quota/all?sn=...

        No request body - SN is passed as query parameter.
        Response: {"code": "0", "data": {"pd.soc": 83, "inv.outputWatts": 0, ...}}
        """
        if not self._check_rate_limit():
            return None

        url = f"{self._base_url}{IOT_QUOTA_ALL_PATH}"
        query = {"sn": self._device_sn}

        return await self._request_with_retry(
            "GET", url, query=query,
            purpose="diagnostic" if diagnostic else "poll",
        )

    async def set_quota(self, command: dict) -> dict | None:
        """Apply a device setting via PUT /iot-open/sign/device/quota.

        `command` is the device-specific body without the serial, which is
        added here. Returns the decoded response, or None when the request
        could not be delivered.
        """
        url = f"{self._base_url}{IOT_QUOTA_PATH}"
        body = {"sn": self._device_sn, **command}
        return await self._request_with_retry(
            "PUT", url, body=body, purpose="action"
        )

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
        elif isinstance(obj, bool):
            # Must be signed the way JSON spells it. str(True) yields "True",
            # which the server never reconstructs from the body, so the request
            # would fail with 8521 "signature is wrong".
            items.append((parent, "true" if obj else "false"))
        elif obj is None:
            # Same reasoning as booleans: json.dumps emits null, str(None)
            # would sign "None" and the server would reject the signature.
            items.append((parent, "null"))
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
        purpose: str = "poll",
    ) -> dict | None:
        """Execute an HTTP request with retry logic."""
        failure_reason = "request_error"
        for attempt in range(1, HTTP_RETRIES + 1):
            try:
                # Sign: for POST use body params, for GET use query params
                sign_params = body if body else query if query else {}
                headers = self._sign_headers(sign_params)
                timeout = aiohttp.ClientTimeout(total=10)

                if method in ("POST", "PUT"):
                    # Content-Type belongs on body requests only. Setting it on
                    # a GET makes the server validate the signature as if a
                    # body were present and reject it with 8521.
                    headers["Content-Type"] = "application/json;charset=UTF-8"
                    body_json = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
                    request = self._session.post if method == "POST" else self._session.put
                    async with request(
                        url, headers=headers, data=body_json.encode("utf-8"), timeout=timeout,
                    ) as resp:
                        return await self._handle_response(resp, purpose=purpose)
                else:
                    async with self._session.get(
                        url, headers=headers, params=query, timeout=timeout,
                    ) as resp:
                        return await self._handle_response(resp, purpose=purpose)

            except (
                aiohttp.ClientError,
                TimeoutError,
                asyncio.TimeoutError,
                self._RetryableAPIError,
            ) as exc:
                # aiohttp exceptions can embed RequestInfo, including the
                # signed URL and full serial query. Keep only the exception
                # class; retries are coalesced into one terminal outcome.
                failure_reason = type(exc).__name__

            if attempt < HTTP_RETRIES:
                await asyncio.sleep(HTTP_RETRY_BACKOFF_S)

        self.last_error_code = "network"
        self._note_failure(
            "transport",
            f"HTTP {method} transport failure for {self._sn_display} "
            f"after {HTTP_RETRIES} attempts ({failure_reason})",
            purpose,
        )
        return None

    def _note_failure(self, kind: str, message: str, purpose: str) -> None:
        """Log an outage transition once, while keeping repeats quiet."""
        if purpose != "poll":
            _LOGGER.debug(
                "HTTP request failed purpose=%s kind=%s device=%s",
                purpose,
                kind,
                self._sn_display,
            )
            return

        now = time.monotonic()
        if not self._outage_active or self._outage_kind != kind:
            self._outage_active = True
            self._outage_kind = kind
            self._outage_last_summary_at = now
            self._outage_suppressed = 0
            _LOGGER.warning("%s", message)
            return

        self._outage_suppressed += 1
        if now - self._outage_last_summary_at < _HTTP_FAILURE_SUMMARY_INTERVAL_S:
            return
        _LOGGER.debug(
            "HTTP poll failure persists for %s: kind=%s repeated=%d",
            self._sn_display,
            kind,
            self._outage_suppressed,
        )
        self._outage_last_summary_at = now
        self._outage_suppressed = 0

    def _note_success(self, purpose: str) -> None:
        """Log exactly one recovery when a polled outage ends."""
        if purpose == "poll" and self._outage_active:
            _LOGGER.info(
                "HTTP quota recovered for %s after %s failure",
                self._sn_display,
                self._outage_kind or "request",
            )
            self._outage_active = False
            self._outage_kind = None
            self._outage_last_summary_at = 0.0
            self._outage_suppressed = 0

    async def _handle_response(
        self, resp: aiohttp.ClientResponse, *, purpose: str = "poll"
    ) -> dict | None:
        """Parse and validate an API response."""
        data = await resp.json()
        code = str(data.get("code"))

        if resp.ok and code == "0":
            _LOGGER.debug("HTTP: quota OK for %s", self._sn_display)
            self._note_success(purpose)
            self.last_error_code = None
            return data.get("data") or {}

        # EcoFlow error 8521 is a transient server-side error - retry
        if code == "8521":
            raise self._RetryableAPIError

        # Error 1006: device not linked to API key - not an auth failure (#2)
        if code == "1006":
            self.last_error_code = "1006"
            self._note_failure(
                "api:1006",
                f"HTTP: device {self._sn_display} not linked to API key - "
                "verify device binding at developer.ecoflow.com (code=1006)",
                purpose,
            )
            return None

        self.last_error_code = code
        safe_code = code if code.isdecimal() and len(code) <= 10 else "unexpected"
        self._note_failure(
            f"api:{safe_code}",
            f"HTTP quota API failure code={safe_code} for {self._sn_display}",
            purpose,
        )
        return None
