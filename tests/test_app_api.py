"""Tests for AppApiClient - login, device discovery, MQTT credentials."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ecoflow_energy.ecoflow.app_api import AppApiClient, _parse_device_response


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
            return_value={"token": "jwt_token_123", "user_id": "uid_456"},
        ):
            result = await client.login()

        assert result is True
        assert client.token == "jwt_token_123"
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
        mock_get.assert_called_once_with(session, "valid_token")

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
