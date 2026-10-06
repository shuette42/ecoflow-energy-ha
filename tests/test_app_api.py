"""Tests for AppApiClient - login, device discovery, MQTT credentials."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from ecoflow_energy.ecoflow import app_api
from ecoflow_energy.ecoflow.app_api import (
    AppApiClient,
    HistoryLoginError,
    _parse_device_response,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(email="test@example.com", password="test_pw"):
    session = MagicMock()
    return AppApiClient(session=session, email=email, password=password), session


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
# Login
# ===========================================================================


class TestLogin:
    @pytest.mark.asyncio
    async def test_login_success(self):
        client, session = _make_client()

        with patch(
            "ecoflow_energy.ecoflow.app_api.enhanced_login",
            new_callable=AsyncMock,
            return_value={"token": "test_jwt_token_123", "user_id": "uid_456"},
        ):
            result = await client.login()

        assert result is True
        assert client.token == "test_jwt_token_123"
        assert client.user_id == "uid_456"

    @pytest.mark.asyncio
    async def test_login_failure(self):
        client, session = _make_client()

        with patch(
            "ecoflow_energy.ecoflow.app_api.enhanced_login",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await client.login()

        assert result is False
        assert client.token is None
        assert client.user_id is None

    @pytest.mark.asyncio
    async def test_login_clears_previous_token_on_failure(self):
        client, session = _make_client()
        # Simulate a previously successful login
        client._token = "old_token"
        client._user_id = "old_uid"

        with patch(
            "ecoflow_energy.ecoflow.app_api.enhanced_login",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await client.login()

        assert result is False
        assert client.token is None
        assert client.user_id is None


# ===========================================================================
# Device List
# ===========================================================================


class TestGetDeviceList:
    @pytest.mark.asyncio
    async def test_parses_bound_and_share(self):
        client, session = _make_client()
        client._token = "valid_token"

        api_response = {
            "code": "0",
            "data": {
                "bound": {
                    "HJ3100001": {
                        "deviceName": "My PowerOcean",
                        "productName": "PowerOcean",
                        "online": 1,
                    },
                },
                "share": {
                    "R3510002": {
                        "deviceName": "Shared Delta",
                        "productName": "Delta 2 Max",
                        "online": 0,
                    },
                },
            },
        }
        session.get = MagicMock(return_value=_mock_response(api_response))

        result = await client.get_device_list()

        assert len(result) == 2
        sns = {d["sn"] for d in result}
        assert "HJ3100001" in sns
        assert "R3510002" in sns

        po = next(d for d in result if d["sn"] == "HJ3100001")
        assert po["product_name"] == "PowerOcean"
        assert po["online"] == 1
        assert po["device_type"] == "powerocean"

        delta = next(d for d in result if d["sn"] == "R3510002")
        assert delta["product_name"] == "Delta 2 Max"
        assert delta["online"] == 0
        assert delta["device_type"] == "delta"

    @pytest.mark.asyncio
    async def test_empty_response_returns_empty_list(self):
        client, session = _make_client()
        client._token = "valid_token"

        api_response = {"code": "0", "data": {}}
        session.get = MagicMock(return_value=_mock_response(api_response))

        result = await client.get_device_list()
        assert result == []

    @pytest.mark.asyncio
    async def test_no_token_returns_empty_list(self):
        client, session = _make_client()
        # No login, token is None

        result = await client.get_device_list()
        assert result == []
        # Should not call session.get at all
        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_error_code_returns_empty_list(self):
        client, session = _make_client()
        client._token = "valid_token"

        api_response = {"code": "401", "message": "Unauthorized", "data": None}
        session.get = MagicMock(return_value=_mock_response(api_response))

        result = await client.get_device_list()
        assert result == []

    @pytest.mark.asyncio
    async def test_network_error_returns_empty_list(self):
        import aiohttp

        client, session = _make_client()
        client._token = "valid_token"

        session.get = MagicMock(side_effect=aiohttp.ClientError("connection lost"))

        result = await client.get_device_list()
        assert result == []

    @pytest.mark.asyncio
    async def test_deduplicates_devices(self):
        """A device appearing in both bound and share should appear only once."""
        client, session = _make_client()
        client._token = "valid_token"

        api_response = {
            "code": "0",
            "data": {
                "bound": {
                    "HJ3100001": {
                        "productName": "PowerOcean",
                        "online": 1,
                    },
                },
                "share": {
                    "HJ3100001": {
                        "productName": "PowerOcean",
                        "online": 1,
                    },
                },
            },
        }
        session.get = MagicMock(return_value=_mock_response(api_response))

        result = await client.get_device_list()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_list_format_devices(self):
        """Handle group format where value is a list of devices."""
        client, session = _make_client()
        client._token = "valid_token"

        api_response = {
            "code": "0",
            "data": {
                "bound": {
                    "group1": [
                        {"sn": "HW5200001", "productName": "Smart Plug", "online": 1},
                        {"sn": "HW5200002", "productName": "Smart Plug", "online": 0},
                    ],
                },
                "share": {},
            },
        }
        session.get = MagicMock(return_value=_mock_response(api_response))

        result = await client.get_device_list()
        assert len(result) == 2
        assert all(d["device_type"] == "smartplug" for d in result)


# ===========================================================================
# Normalized Field Names
# ===========================================================================


class TestNormalizedFieldNames:
    def test_output_matches_iot_api_format(self):
        """Normalized output must have sn, product_name, online, device_type."""
        data = {
            "bound": {
                "HJ3100001": {
                    "deviceName": "My Device",
                    "productName": "PowerOcean",
                    "online": 1,
                },
            },
        }
        result = _parse_device_response(data)
        assert len(result) == 1
        dev = result[0]
        assert set(dev.keys()) == {"sn", "product_name", "online", "device_type"}

    def test_sn_from_key_when_missing(self):
        """When 'sn' is not in device info, the dict key is used as SN."""
        data = {
            "bound": {
                "R3510001": {
                    "productName": "Delta 2 Max",
                    "online": 1,
                },
            },
        }
        result = _parse_device_response(data)
        assert result[0]["sn"] == "R3510001"

    def test_fallback_to_name_field(self):
        """When productName is missing, fall back to name field."""
        data = {
            "bound": {
                "HJ3100001": {
                    "name": "Power Ocean DC Fit",
                    "online": 1,
                },
            },
        }
        result = _parse_device_response(data)
        assert result[0]["product_name"] == "Power Ocean DC Fit"
        assert result[0]["device_type"] == "powerocean"


# ===========================================================================
# MQTT Credentials
# ===========================================================================


class TestGetMqttCredentials:
    @pytest.mark.asyncio
    async def test_delegates_to_enhanced_auth(self):
        client, session = _make_client()
        client._token = "valid_token"

        expected_creds = {
            "certificateAccount": "mqtt_user",
            "certificatePassword": "mqtt_pass",
            "url": "mqtt-e.ecoflow.com",
            "port": "8084",
            "protocol": "mqtts",
        }

        with patch(
            "ecoflow_energy.ecoflow.app_api.get_enhanced_credentials",
            new_callable=AsyncMock,
            return_value=expected_creds,
        ) as mock_get:
            result = await client.get_mqtt_credentials()

        assert result == expected_creds
        mock_get.assert_called_once_with(
            session, "valid_token", base_url=client._base_url
        )

    @pytest.mark.asyncio
    async def test_no_token_returns_none(self):
        client, session = _make_client()
        # No login, token is None

        result = await client.get_mqtt_credentials()
        assert result is None

    @pytest.mark.asyncio
    async def test_enhanced_auth_failure_returns_none(self):
        client, session = _make_client()
        client._token = "valid_token"

        with patch(
            "ecoflow_energy.ecoflow.app_api.get_enhanced_credentials",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await client.get_mqtt_credentials()

        assert result is None


# ===========================================================================
# Region Routing
# ===========================================================================


class TestRegionRouting:
    @pytest.mark.asyncio
    async def test_login_records_winning_base_url(self):
        client, session = _make_client()

        with patch(
            "ecoflow_energy.ecoflow.app_api.enhanced_login",
            new_callable=AsyncMock,
            return_value={
                "token": "test_jwt_token_123",
                "user_id": "uid_456",
                "base_url": "https://api.ecoflow.com",
            },
        ):
            assert await client.login() is True

        assert client._base_url == "https://api.ecoflow.com"

    @pytest.mark.asyncio
    async def test_device_list_uses_winning_base_url(self):
        """After a login that succeeded on the global host, the device list
        request must go to the global host, not the EU default."""
        client, session = _make_client()

        with patch(
            "ecoflow_energy.ecoflow.app_api.enhanced_login",
            new_callable=AsyncMock,
            return_value={
                "token": "test_jwt_token_123",
                "user_id": "uid_456",
                "base_url": "https://api.ecoflow.com",
            },
        ):
            await client.login()

        session.get = MagicMock(return_value=_mock_response({"code": "0", "data": {}}))
        await client.get_device_list()

        url = session.get.call_args[0][0]
        assert url.startswith("https://api.ecoflow.com/")

    @pytest.mark.asyncio
    async def test_mqtt_credentials_use_winning_base_url(self):
        client, session = _make_client()

        with patch(
            "ecoflow_energy.ecoflow.app_api.enhanced_login",
            new_callable=AsyncMock,
            return_value={
                "token": "test_jwt_token_123",
                "user_id": "uid_456",
                "base_url": "https://api.ecoflow.com",
            },
        ):
            await client.login()

        with patch(
            "ecoflow_energy.ecoflow.app_api.get_enhanced_credentials",
            new_callable=AsyncMock,
            return_value={"certificateAccount": "acct"},
        ) as mock_get:
            await client.get_mqtt_credentials()

        mock_get.assert_called_once_with(
            session, "test_jwt_token_123", base_url="https://api.ecoflow.com"
        )

    @pytest.mark.asyncio
    async def test_login_without_base_url_falls_back_to_default(self):
        """A login result without base_url (legacy contract) keeps the EU default."""
        from ecoflow_energy.ecoflow.const import IOT_API_BASE

        client, session = _make_client()

        with patch(
            "ecoflow_energy.ecoflow.app_api.enhanced_login",
            new_callable=AsyncMock,
            return_value={"token": "test_jwt_token_123", "user_id": "uid_456"},
        ):
            await client.login()

        assert client._base_url == IOT_API_BASE


# ===========================================================================
# Properties
# ===========================================================================


class TestProperties:
    def test_token_none_before_login(self):
        client, _ = _make_client()
        assert client.token is None

    def test_user_id_none_before_login(self):
        client, _ = _make_client()
        assert client.user_id is None

    def test_properties_are_read_only(self):
        client, _ = _make_client()
        with pytest.raises(AttributeError):
            client.token = "hack"
        with pytest.raises(AttributeError):
            client.user_id = "hack"


# ===========================================================================
# Charging history: a rejected token must not outlive the rejection
# ===========================================================================

_HISTORY_SERIAL = "C371TEST0001"
_EMPTY_PAGE = '{"code": "0", "data": {"content": [], "hasNext": false, "total": 0}}'


def _history_response(status, text, served_type="application/json"):
    """An aiohttp-like response: json() enforces the content type unless told not to."""
    request_info = MagicMock()
    resp = MagicMock()
    resp.status = status

    def raise_for_status():
        if status >= 400:
            raise aiohttp.ClientResponseError(request_info, (), status=status)

    async def read_json(*, content_type="application/json"):
        if content_type is not None and served_type != content_type:
            raise aiohttp.ContentTypeError(request_info, ())
        return json.loads(text)

    resp.raise_for_status = raise_for_status
    resp.json = read_json
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _sign_in_with(client, *tokens):
    """Replace login() with one that hands out the given tokens in order."""
    queue = iter(tokens)

    async def login():
        client._token = next(queue)
        return True

    client.login = AsyncMock(side_effect=login)


class TestHistoryTokenRejection:
    async def test_token_invalidated_on_401_json(self):
        client, session = _make_client()
        client._token = "dead"
        _sign_in_with(client, "fresh", "second")
        rejected = '{"code": "401", "message": "token expired"}'
        session.get = MagicMock(
            side_effect=[
                _history_response(401, rejected),
                _history_response(401, rejected),
            ]
        )
        with pytest.raises(HistoryLoginError):
            await client._get_powerpulse_orders(_HISTORY_SERIAL)
        assert client.token is None
        assert client.login.await_count == 1

        session.get = MagicMock(return_value=_history_response(200, _EMPTY_PAGE))
        assert await client._get_powerpulse_orders(_HISTORY_SERIAL) == []
        assert client.login.await_count == 2
        sent = session.get.call_args.kwargs["headers"]["Authorization"]
        assert sent == "Bearer second"

    @pytest.mark.parametrize(
        ("status", "text"),
        [
            (401, "Unauthorized"),
            (403, "Forbidden"),
            (200, '{"code": "401"}'),
        ],
    )
    async def test_token_invalidated_on_401_text_plain(self, status, text):
        client, session = _make_client()
        client._token = "dead"
        _sign_in_with(client, "fresh")
        session.get = MagicMock(
            side_effect=[
                _history_response(status, text, "text/plain"),
                _history_response(status, text, "text/plain"),
            ]
        )
        with pytest.raises(HistoryLoginError):
            await client._get_powerpulse_orders(_HISTORY_SERIAL)
        assert client.token is None
        assert session.get.call_count == 2

    async def test_token_invalidated_on_rejection_code(self):
        client, session = _make_client()
        client._token = "dead"
        _sign_in_with(client, "fresh")
        session.get = MagicMock(
            return_value=_history_response(200, '{"code": "8513", "data": null}')
        )
        with pytest.raises(ValueError, match="rejected"):
            await client._get_powerpulse_orders(_HISTORY_SERIAL)
        assert client.token is None
        client.login.assert_not_awaited()

    async def test_server_error_keeps_a_working_token(self):
        """A 5xx says nothing about the token; re-login would only spend the budget."""
        client, session = _make_client()
        client._token = "good"
        session.get = MagicMock(return_value=_history_response(502, "Bad Gateway"))
        with pytest.raises(aiohttp.ClientResponseError):
            await client._get_powerpulse_orders(_HISTORY_SERIAL)
        assert client.token == "good"


class TestHistoryFetchTimeout:
    async def test_timeout_is_per_fetch_not_per_entry(self, monkeypatch):
        """Each charger gets its own budget once it holds the entry-wide lock.

        A's fetch hangs and is cut off at the timeout. B queued behind A for
        that whole time and then needs 0.25 s of its own, so A's wait plus B's
        fetch exceeds the 0.3 s budget; B only succeeds when the budget starts
        at the fetch, not at the queue.
        """
        monkeypatch.setattr(app_api, "HISTORY_FETCH_TIMEOUT_S", 0.3)
        client, session = _make_client()
        client._token = "tok"

        def get(url, *, params, headers, timeout):
            serial = params["sn"]
            resp = MagicMock()
            resp.status = 200
            resp.raise_for_status = MagicMock()
            resp.json = AsyncMock(return_value=json.loads(_EMPTY_PAGE))

            async def enter():
                if serial == "HANG":
                    await asyncio.Event().wait()
                await asyncio.sleep(0.25)
                return resp

            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(side_effect=enter)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        session.get = MagicMock(side_effect=get)
        hung, slow = await asyncio.wait_for(
            asyncio.gather(
                client.get_powerpulse_orders("HANG"),
                client.get_powerpulse_orders("SLOW"),
                return_exceptions=True,
            ),
            timeout=5,
        )
        assert isinstance(hung, TimeoutError)
        assert slow == []
        assert session.get.call_count == 2
