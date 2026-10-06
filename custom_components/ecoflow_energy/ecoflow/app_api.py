"""EcoFlow App API client (async) - token-based authentication.

Provides login via email/password, device discovery, and MQTT credential
retrieval using the EcoFlow Portal API. No IoT Developer API keys required.
Uses aiohttp for async HTTP - HA provides the ClientSession.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Protocol

import aiohttp

from .charging_history import identity, is_identity
from .const import IOT_API_BASE, get_device_type
from .enhanced_auth import enhanced_login, get_enhanced_credentials

_LOGGER = logging.getLogger(__name__)

_DEVICE_LIST_PATH = "/iot-service/user/device"

# One charger's whole history read (login and every page), not the wait for
# the entry-wide lock: a slow charger must not spend the next one's budget.
HISTORY_FETCH_TIMEOUT_S = 60


class HistoryStateStore(Protocol):
    """Persistence supplied by the host, without a Home Assistant dependency."""

    async def async_load(self) -> dict[str, Any] | None: ...
    async def async_save(self, data: dict[str, Any]) -> None: ...


class HistoryDeferred(ValueError):
    """A persisted deadline prevents another request yet."""

    def __init__(self, delay: float, authentication: bool) -> None:
        super().__init__("Charging history request deferred")
        self.delay = delay
        self.authentication = authentication


class HistoryStorageError(OSError):
    """History persistence could not be verified; do not spend the read budget."""


class HistoryLoginError(ValueError):
    """History sign-in failed; callers must back off instead of retrying per charger."""


class AppApiClient:
    """EcoFlow App API - token-authenticated async client.

    Uses email/password login to obtain a JWT token, then fetches
    device lists and MQTT credentials without Developer API keys.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        history_store: HistoryStateStore | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._token: str | None = None
        self._user_id: str | None = None
        self._base_url: str = IOT_API_BASE
        self._history_lock = asyncio.Lock()
        self._history_retry_after = 0.0
        self._history_store = history_store

    @property
    def token(self) -> str | None:
        """Return the current JWT token, or None if not logged in."""
        return self._token

    @property
    def user_id(self) -> str | None:
        """Return the current user ID, or None if not logged in."""
        return self._user_id

    async def login(self) -> bool:
        """Login via email/password and store JWT token + user ID.

        Delegates to enhanced_auth.enhanced_login which handles
        multi-region fallback (EU + global).

        Returns True on success, False on failure.
        """
        result = await enhanced_login(self._session, self._email, self._password)
        if result is None:
            self._token = None
            self._user_id = None
            return False

        self._token = result["token"]
        self._user_id = result["user_id"]
        # Remember the host that accepted the login so subsequent calls
        # (device list, MQTT credentials) hit the same region.
        self._base_url = result.get("base_url", IOT_API_BASE)
        _LOGGER.debug(
            "App login OK (user_id=%s, base_url=%s)", self._user_id, self._base_url
        )
        return True

    async def get_device_list(self) -> list[dict[str, Any]]:
        """Fetch the list of bound and shared devices.

        Returns a normalized list of device dicts compatible with
        IoTApiClient format:
            [
                {"sn": "...", "product_name": "...", "online": 1, "device_type": "..."},
                ...,
            ]

        Returns an empty list on failure.
        """
        if not self._token:
            _LOGGER.debug("App API: no token, cannot fetch device list")
            return []

        url = f"{self._base_url}{_DEVICE_LIST_PATH}"
        headers = {"Authorization": f"Bearer {self._token}"}

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with self._session.get(url, headers=headers, timeout=timeout) as resp:
                resp.raise_for_status()
                body = await resp.json()

                if str(body.get("code")) != "0":
                    _LOGGER.warning(
                        "App API device list: code=%s msg=%s",
                        body.get("code"),
                        body.get("message"),
                    )
                    return []

                data = body.get("data", {})
                if not isinstance(data, dict):
                    _LOGGER.debug("App API device list: unexpected data format")
                    return []

                return _parse_device_response(data)

        except (aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.warning("App API device list failed: %s", exc)
            return []

    async def get_powerpulse_orders(self, serial: str) -> list[dict[str, Any]]:
        """Serialize entry-wide reads and back off failed sign-in for an hour."""
        async with self._history_lock:
            now = time.time()
            state: dict[str, Any] = {"last_reads": {}, "retry_after": 0.0}
            if self._history_store is not None:
                saved = await self._history_store.async_load()
                if saved is not None:
                    if not self._valid_history_limits(saved):
                        state = {"last_reads": {}, "retry_after": now + 3600}
                    else:
                        state = {
                            "last_reads": {
                                key: min(stamp, now)
                                for key, stamp in saved["last_reads"].items()
                            },
                            "retry_after": min(saved["retry_after"], now + 3600),
                        }
                    if state != saved:
                        await self._save_history_limits(state)
                self._history_retry_after = min(
                    max(self._history_retry_after, state["retry_after"]), now + 3600
                )
            if now < self._history_retry_after:
                if self._history_store is not None:
                    raise HistoryDeferred(self._history_retry_after - now, True)
                raise HistoryLoginError("Charging history sign-in is backed off")
            key = identity(serial)
            if self._history_store is not None:
                deadline = state["last_reads"].get(key, 0) + 300
                if now < deadline:
                    raise HistoryDeferred(deadline - now, False)
                # Verify the write before any network activity: HA may swallow errors.
                state["last_reads"][key] = now
                await self._save_history_limits(state)
            try:
                async with asyncio.timeout(HISTORY_FETCH_TIMEOUT_S):
                    return await self._get_powerpulse_orders(serial)
            except HistoryLoginError:
                self._history_retry_after = time.time() + 3600
                if self._history_store is not None:
                    state["retry_after"] = self._history_retry_after
                    await self._save_history_limits(state)
                raise

    async def _save_history_limits(self, state: dict[str, Any]) -> None:
        """HA Store may log a failed write and return normally."""
        assert self._history_store is not None
        await self._history_store.async_save(state)
        if await self._history_store.async_load() != state:
            raise HistoryStorageError("Could not persist charging history limits")

    @staticmethod
    def _valid_history_limits(saved: dict[str, Any]) -> bool:
        """Reject damaged limits instead of silently bypassing the read budget."""

        def timestamp(value: Any) -> bool:
            return (
                type(value) in (int, float)
                and value >= 0
                and (isinstance(value, int) or math.isfinite(value))
            )

        return (
            isinstance(saved, dict)
            and isinstance(saved.get("last_reads"), dict)
            and timestamp(saved.get("retry_after"))
            and all(
                is_identity(key) and timestamp(value)
                for key, value in saved["last_reads"].items()
            )
        )

    async def _get_powerpulse_orders(self, serial: str) -> list[dict[str, Any]]:
        """Read every completed-order page, or fail without partial accounting.

        The portal uses this read-only endpoint. C371 live reads confirm one-
        based ``page`` and ``size`` pagination, and that no product header is
        required. A refusal gets one login retry; other failures are not empty
        history. Never log a response, request URL, serial or vehicle name.
        """
        if not self._token and not await self.login():
            raise HistoryLoginError("Charging history login failed")
        rows: list[dict[str, Any]] = []
        seen_pages: set[tuple[str, ...]] = set()
        expected_total: int | None = None
        for page in range(1, 101):
            for attempt in range(2):
                async with self._session.get(
                    f"{self._base_url}/provider-admin/development/device/powerPulse/orders",
                    params={"sn": serial, "page": page, "size": 100},
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    # A 401/403 can carry a text/plain body: decide on the
                    # status before parsing, and parse without a content-type
                    # check, so a dead token is never hidden by a decode error.
                    body: Any = None
                    refused = response.status in (401, 403)
                    if not refused:
                        response.raise_for_status()
                        body = await response.json(content_type=None)
                        refused = (
                            isinstance(body, dict) and str(body.get("code")) == "401"
                        )
                    if refused and attempt == 0:
                        if not await self.login():
                            raise HistoryLoginError("Charging history login failed")
                        continue
                    if refused:
                        self._token = None
                        raise HistoryLoginError("Charging history session rejected")
                break
            if not isinstance(body, dict) or str(body.get("code")) != "0":
                # A rejection code on a 2xx answer may be a dead session: sign
                # in again on the next read instead of reusing this token.
                self._token = None
                raise ValueError("Charging history request rejected")
            data = body.get("data")
            if not isinstance(data, dict):
                raise ValueError("Missing charging history")
            content, has_next, total = (
                data.get("content"),
                data.get("hasNext"),
                data.get("total"),
            )
            if (
                not isinstance(content, list)
                or not all(isinstance(row, dict) for row in content)
                or type(has_next) is not bool
                or type(total) is not int
                or total < 0
            ):
                raise ValueError("Invalid charging history page")
            if expected_total is not None and total != expected_total:
                raise ValueError("Charging history changed during pagination")
            expected_total = total
            signature = tuple(str(row.get("orderId")) for row in content)
            if has_next and (not content or signature in seen_pages):
                raise ValueError("Charging history pagination did not advance")
            seen_pages.add(signature)
            rows.extend(content)
            if not has_next:
                if len({str(row.get("orderId")) for row in rows}) != total:
                    raise ValueError("Incomplete charging history")
                return rows
        raise ValueError("Charging history exceeds pagination limit")

    async def get_mqtt_credentials(self) -> dict[str, Any] | None:
        """Fetch Enhanced Mode MQTT credentials using the stored token.

        Delegates to enhanced_auth.get_enhanced_credentials for
        AES-CFB decryption of the Portal certification endpoint.

        Returns the MQTT credentials dict or None on failure.
        """
        if not self._token:
            _LOGGER.debug("App API: no token, cannot fetch MQTT credentials")
            return None

        return await get_enhanced_credentials(
            self._session, self._token, base_url=self._base_url
        )


def _parse_device_response(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse Portal API device response into normalized device list.

    The Portal API returns devices grouped by ownership:
        {"bound": {SN: {deviceInfo}}, "share": {SN: {deviceInfo}}, ...}

    Each group can also contain list-based formats:
        {"bound": {groupKey: [deviceInfo, ...]}}

    We normalize all formats into a flat list with consistent field names.
    """
    devices: list[dict[str, Any]] = []
    seen_sns: set[str] = set()

    for category in ("bound", "share"):
        group = data.get(category, {})
        if not isinstance(group, dict):
            continue

        for key, value in group.items():
            if isinstance(value, list):
                # Format: {groupKey: [device, device, ...]}
                for dev in value:
                    _add_device(dev, devices, seen_sns)
            elif isinstance(value, dict):
                # Format: {SN: {deviceInfo}} - key is the serial number
                dev = value
                if "sn" not in dev:
                    dev = {**dev, "sn": key}
                _add_device(dev, devices, seen_sns)

    return devices


def _add_device(
    dev: dict[str, Any],
    devices: list[dict[str, Any]],
    seen_sns: set[str],
) -> None:
    """Normalize a single device dict and append to the list.

    Deduplicates by serial number (a device can appear in both bound and share).
    """
    sn = dev.get("sn", "")
    if not sn or sn in seen_sns:
        return

    seen_sns.add(sn)
    product_name = dev.get("productName", dev.get("name", ""))

    devices.append(
        {
            "sn": sn,
            "product_name": product_name,
            "online": dev.get("online", 0),
            "device_type": get_device_type(product_name, sn),
        }
    )
