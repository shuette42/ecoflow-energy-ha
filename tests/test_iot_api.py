"""Tests for IoTApiClient — signature, caching, rate-limit, device list."""

import hashlib
import hmac
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ecoflow_energy.ecoflow.iot_api import IoTApiClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(access_key="test_ak", secret_key="test_sk", base_url=None):
    session = MagicMock()
    kwargs = dict(session=session, access_key=access_key, secret_key=secret_key)
    if base_url:
        kwargs["base_url"] = base_url
    return IoTApiClient(**kwargs), session


def _mock_response(json_data, status=200):
    """Create an async context-manager mock for aiohttp response."""
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ===========================================================================
# Signature
# ===========================================================================


class TestSign:
    def test_sign_deterministic(self):
        sig = IoTApiClient.sign({"a": "1", "b": "2"}, "secret")
        expected = hmac.new(b"secret", b"a=1&b=2", hashlib.sha256).hexdigest()
        assert sig == expected

    def test_sign_sorted_keys(self):
        """Parameters must be sorted alphabetically before signing."""
        sig1 = IoTApiClient.sign({"z": "1", "a": "2"}, "key")
        sig2 = IoTApiClient.sign({"a": "2", "z": "1"}, "key")
        assert sig1 == sig2

    def test_sign_different_secret_differs(self):
        sig1 = IoTApiClient.sign({"a": "1"}, "key1")
        sig2 = IoTApiClient.sign({"a": "1"}, "key2")
        assert sig1 != sig2

    def test_sign_empty_params(self):
        sig = IoTApiClient.sign({}, "secret")
        expected = hmac.new(b"secret", b"", hashlib.sha256).hexdigest()
        assert sig == expected


# ===========================================================================
# Signed Headers
# ===========================================================================


class TestSignedHeaders:
    def test_headers_contain_required_fields(self):
        client, _ = _make_client()
        headers = client._make_signed_headers()
        assert "accessKey" in headers
        assert "nonce" in headers
        assert "timestamp" in headers
        assert "sign" in headers

    def test_access_key_in_headers(self):
        client, _ = _make_client(access_key="my_key")
        headers = client._make_signed_headers()
        assert headers["accessKey"] == "my_key"

    def test_nonce_length(self):
        client, _ = _make_client()
        headers = client._make_signed_headers()
        assert len(headers["nonce"]) == 6

    def test_timestamp_is_millis(self):
        client, _ = _make_client()
        headers = client._make_signed_headers()
        ts = int(headers["timestamp"])
        # Should be in milliseconds (13+ digits)
        assert ts > 1_000_000_000_000


# ===========================================================================
# Base URL
# ===========================================================================


class TestBaseURL:
    def test_default_base_url(self):
        from ecoflow_energy.ecoflow.const import IOT_API_BASE

        client, _ = _make_client()
        assert client._base_url == IOT_API_BASE

    def test_trailing_slash_stripped(self):
        client, _ = _make_client(base_url="https://example.com/")
        assert client._base_url == "https://example.com"


# ===========================================================================
# Credential Caching
# ===========================================================================


class TestCredentialCaching:
    @pytest.mark.asyncio
    async def test_get_returns_cached(self):
        client, session = _make_client()
        creds = {"certificateAccount": "user", "certificatePassword": "pass"}
        session.get = MagicMock(return_value=_mock_response({"data": creds}))

        result1 = await client.get_mqtt_credentials()
        assert result1 == creds

        # Second call should return cache, not hit API again
        result2 = await client.get_mqtt_credentials()
        assert result2 == creds
        assert session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_refresh_clears_cache(self):
        client, session = _make_client()
        creds1 = {"certificateAccount": "user1"}
        creds2 = {"certificateAccount": "user2"}

        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_response({"data": creds1})
            return _mock_response({"data": creds2})

        session.get = MagicMock(side_effect=_side_effect)

        result1 = await client.get_mqtt_credentials()
        assert result1 == creds1

        # Force refresh — but rate-limit will block (< 60s)
        # Reset _last_fetch_ts to bypass rate limit
        client._last_fetch_ts = 0.0
        result2 = await client.refresh_credentials()
        assert result2 == creds2


# ===========================================================================
# Rate Limit
# ===========================================================================


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_rate_limit_blocks_rapid_refetch(self):
        client, session = _make_client()
        creds = {"certificateAccount": "user"}
        session.get = MagicMock(return_value=_mock_response({"data": creds}))

        await client.get_mqtt_credentials()

        # Clear cache but don't reset timestamp
        client._cached = None
        result = await client._fetch()
        # Should return None (cached was cleared, rate-limit blocks new fetch)
        assert result is None
        assert session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_rate_limit_allows_after_interval(self):
        client, session = _make_client()
        creds = {"certificateAccount": "user"}
        session.get = MagicMock(return_value=_mock_response({"data": creds}))

        await client.get_mqtt_credentials()
        client._cached = None

        # Pretend 61 seconds have passed
        client._last_fetch_ts = time.monotonic() - 61
        result = await client._fetch()
        assert result == creds
        assert session.get.call_count == 2


# ===========================================================================
# Empty / Missing Keys
# ===========================================================================


class TestEmptyKeyGuard:
    @pytest.mark.asyncio
    async def test_empty_access_key_returns_none(self):
        client, session = _make_client(access_key="", secret_key="sk")
        result = await client._fetch()
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_secret_key_returns_none(self):
        client, session = _make_client(access_key="ak", secret_key="")
        result = await client._fetch()
        assert result is None


# ===========================================================================
# Error Handling
# ===========================================================================


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_empty_api_response_returns_none(self):
        client, session = _make_client()
        session.get = MagicMock(return_value=_mock_response({"code": "1234", "data": None}))
        result = await client.get_mqtt_credentials()
        assert result is None

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self):
        import aiohttp

        client, session = _make_client()
        session.get = MagicMock(side_effect=aiohttp.ClientError("connection lost"))
        result = await client.get_mqtt_credentials()
        assert result is None


# ===========================================================================
# Device List
# ===========================================================================


class TestDeviceList:
    @pytest.mark.asyncio
    async def test_device_list_returns_data(self):
        client, session = _make_client()
        devices = [{"sn": "SN001", "productName": "Delta 2 Max"}]
        session.get = MagicMock(return_value=_mock_response({"data": devices}))
        result = await client.get_device_list()
        assert result == devices

    @pytest.mark.asyncio
    async def test_device_list_empty_returns_none(self):
        client, session = _make_client()
        session.get = MagicMock(return_value=_mock_response({"data": None, "code": "0"}))
        result = await client.get_device_list()
        assert result is None

    @pytest.mark.asyncio
    async def test_device_list_network_error_returns_none(self):
        import aiohttp

        client, session = _make_client()
        session.get = MagicMock(side_effect=aiohttp.ClientError("timeout"))
        result = await client.get_device_list()
        assert result is None
