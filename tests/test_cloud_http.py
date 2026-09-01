"""Tests for EcoFlowHTTPQuota - signature, rate limiting, dead code removal."""

import hashlib
import hmac
import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from ecoflow_energy.ecoflow.cloud_http import (
    _HTTP_FAILURE_SUMMARY_INTERVAL_S,
    EcoFlowHTTPQuota,
)


class TestHTTPClientInit:
    def test_default_base_url(self):
        from ecoflow_energy.ecoflow.const import IOT_API_BASE
        from unittest.mock import MagicMock

        client = EcoFlowHTTPQuota(
            session=MagicMock(),
            access_key="ak",
            secret_key="sk",
            device_sn="SN123",
        )
        assert client._base_url == IOT_API_BASE

    def test_custom_base_url_trailing_slash(self):
        from unittest.mock import MagicMock

        client = EcoFlowHTTPQuota(
            session=MagicMock(),
            access_key="ak",
            secret_key="sk",
            device_sn="SN123",
            base_url="https://example.com/",
        )
        assert not client._base_url.endswith("/")


class TestSignature:
    def _make_client(self):
        from unittest.mock import MagicMock

        return EcoFlowHTTPQuota(
            session=MagicMock(),
            access_key="test_ak",
            secret_key="test_sk",
            device_sn="SN123",
        )

    def test_sign_headers_has_required_fields(self):
        client = self._make_client()
        headers = client._sign_headers({"sn": "SN123"})
        assert "accessKey" in headers
        assert "nonce" in headers
        assert "timestamp" in headers
        assert "sign" in headers

    def test_sign_headers_access_key_matches(self):
        client = self._make_client()
        headers = client._sign_headers({})
        assert headers["accessKey"] == "test_ak"

    def test_nonce_is_6_digit_numeric(self):
        client = self._make_client()
        headers = client._sign_headers({"sn": "SN123"})
        nonce = headers["nonce"]
        assert len(nonce) == 6, f"Nonce must be 6 digits, got {len(nonce)}"
        assert re.match(r"^\d{6}$", nonce), f"Nonce must be 6-digit numeric, got '{nonce}'"

    def test_sign_is_hex(self):
        client = self._make_client()
        headers = client._sign_headers({"foo": "bar"})
        assert re.match(r"^[0-9a-f]{64}$", headers["sign"]), "Sign must be 64-char hex (SHA256)"

    def test_flatten_nested(self):
        client = self._make_client()
        result = client._flatten({"a": {"b": "c"}, "d": "e"})
        result_dict = dict(result)
        assert result_dict == {"a.b": "c", "d": "e"}

    def test_flatten_list(self):
        client = self._make_client()
        result = client._flatten({"items": [1, 2]})
        result_dict = dict(result)
        assert result_dict == {"items[0]": "1", "items[1]": "2"}

    def test_flatten_booleans_use_json_spelling(self):
        """Booleans must sign as "true"/"false", never Python's "True"/"False".

        The server recomputes the signature from the JSON body it receives. A
        capitalised boolean produces a different string on our side and the
        request comes back as 8521 "signature is wrong". Delta 3 control
        payloads are all booleans, so this is on the hot path.
        """
        client = self._make_client()
        result = dict(client._flatten({"cfgAcOutOpen": True, "cfgBeepEn": False}))
        assert result == {"cfgAcOutOpen": "true", "cfgBeepEn": "false"}

    def test_flatten_nested_boolean(self):
        """The energy-backup command nests its boolean one level down."""
        client = self._make_client()
        result = dict(client._flatten({"cfgEnergyBackup": {"energyBackupEn": True}}))
        assert result == {"cfgEnergyBackup.energyBackupEn": "true"}

    def test_flatten_none_uses_json_null(self):
        """None must sign as "null", never Python's "None".

        Same reasoning as booleans: the server recomputes the signature from
        the JSON body, and json.dumps emits null for None values.
        """
        client = self._make_client()
        result = dict(client._flatten({"cfgValue": None}))
        assert result == {"cfgValue": "null"}

    def test_flatten_keeps_integers_unquoted(self):
        """Guard against the boolean branch swallowing ints (bool subclasses int)."""
        client = self._make_client()
        result = dict(client._flatten({"cfgMaxChgSoc": 90, "cfgMinDsgSoc": 0}))
        assert result == {"cfgMaxChgSoc": "90", "cfgMinDsgSoc": "0"}

    def test_sign_payload_sorted_then_auth_tail(self):
        """Payload params sorted, then auth tail (accessKey, nonce, timestamp) appended.

        EcoFlow API expects: sorted payload params first, then unsorted auth tail.
        """
        client = self._make_client()
        fixed_nonce = 345164
        fixed_ts = "1700000000000"

        with patch("ecoflow_energy.ecoflow.cloud_http.time") as mock_time, \
             patch("ecoflow_energy.ecoflow.cloud_http.random") as mock_random:
            mock_time.time.return_value = 1700000000.0
            mock_random.randint.return_value = fixed_nonce

            headers = client._sign_headers({"sn": "HW52ZZ"})

        # Expected: payload sorted, then auth tail appended
        expected_sign_string = (
            f"sn=HW52ZZ&accessKey=test_ak&nonce={fixed_nonce}&timestamp={fixed_ts}"
        )
        expected_sig = hmac.new(
            b"test_sk", expected_sign_string.encode(), hashlib.sha256
        ).hexdigest()

        assert headers["sign"] == expected_sig

    def test_sign_empty_params_only_auth(self):
        """With no payload params, signature must contain only sorted auth params."""
        client = self._make_client()
        fixed_nonce = 537642
        fixed_ts = "1700000000000"

        with patch("ecoflow_energy.ecoflow.cloud_http.time") as mock_time, \
             patch("ecoflow_energy.ecoflow.cloud_http.random") as mock_random:
            mock_time.time.return_value = 1700000000.0
            mock_random.randint.return_value = fixed_nonce

            headers = client._sign_headers({})

        expected_sign_string = (
            f"accessKey=test_ak&nonce={fixed_nonce}&timestamp={fixed_ts}"
        )
        expected_sig = hmac.new(
            b"test_sk", expected_sign_string.encode(), hashlib.sha256
        ).hexdigest()

        assert headers["sign"] == expected_sig

    def test_sign_matches_official_api_example(self):
        """Verify signature against the official EcoFlow API documentation example.

        From: https://developer-eu.ecoflow.com General Information > Step 8
        """
        client = EcoFlowHTTPQuota(
            session=MagicMock(),
            access_key="Fp4SvIprYSDPXtYJidEtUAd1o",
            secret_key="WIbFEKre0s6sLnh4ei7SPUeYnptHG6V",
            device_sn="unused",
        )
        fixed_nonce = 345164
        fixed_ts = "1671171709428"

        with patch("ecoflow_energy.ecoflow.cloud_http.time") as mock_time, \
             patch("ecoflow_energy.ecoflow.cloud_http.random") as mock_random:
            mock_time.time.return_value = 1671171709.428
            mock_random.randint.return_value = fixed_nonce

            # JSON body from the official example
            headers = client._sign_headers({
                "sn": "123456789",
                "params": {
                    "cmdSet": 11,
                    "id": 24,
                    "eps": 0,
                },
            })

        assert headers["sign"] == "07c13b65e037faf3b153d51613638fa80003c4c38d2407379a7f52851af1473e"


class TestRateLimit:
    def test_first_request_allowed(self):
        client = self._make_client()
        assert client._check_rate_limit() is True

    def test_second_request_blocked(self):
        client = self._make_client()
        client._check_rate_limit()
        assert client._check_rate_limit() is False

    def _make_client(self):
        from unittest.mock import MagicMock

        return EcoFlowHTTPQuota(
            session=MagicMock(),
            access_key="ak",
            secret_key="sk",
            device_sn="SN123",
            min_interval=60.0,
        )


class TestError8521Retry:
    """Error 8521 is a transient EcoFlow server error that should be retried (#2)."""

    @pytest.mark.asyncio
    async def test_8521_retries_and_succeeds(self):
        """Error 8521 on first attempt, success on second → returns data."""
        mock_resp_fail = AsyncMock()
        mock_resp_fail.ok = True
        mock_resp_fail.json = AsyncMock(return_value={"code": "8521", "message": "server error"})

        mock_resp_ok = AsyncMock()
        mock_resp_ok.ok = True
        mock_resp_ok.json = AsyncMock(return_value={"code": "0", "data": {"soc": 85}})

        mock_session = MagicMock()
        # Context manager returns fail then success
        mock_session.get = MagicMock(
            side_effect=[
                AsyncContextManager(mock_resp_fail),
                AsyncContextManager(mock_resp_ok),
            ]
        )

        client = EcoFlowHTTPQuota(
            session=mock_session,
            access_key="ak",
            secret_key="sk",
            device_sn="SN123",
            min_interval=0,
        )

        result = await client.get_quota_all()
        assert result == {"soc": 85}

    @pytest.mark.asyncio
    async def test_8521_all_retries_exhausted(self):
        """Error 8521 on all attempts → returns None."""
        mock_resp = AsyncMock()
        mock_resp.ok = True
        mock_resp.json = AsyncMock(return_value={"code": "8521", "message": "server error"})

        mock_session = MagicMock()
        mock_session.get = MagicMock(
            side_effect=[AsyncContextManager(mock_resp) for _ in range(5)]
        )

        client = EcoFlowHTTPQuota(
            session=mock_session,
            access_key="ak",
            secret_key="sk",
            device_sn="SN123",
            min_interval=0,
        )

        result = await client.get_quota_all()
        assert result is None

    @pytest.mark.asyncio
    async def test_six_exhausted_8521_polls_have_only_terminal_summary(
        self, caplog
    ):
        mock_resp = AsyncMock()
        mock_resp.ok = True
        mock_resp.json = AsyncMock(
            return_value={"code": "8521", "message": "server error"}
        )
        mock_session = MagicMock()
        mock_session.get = MagicMock(
            return_value=AsyncContextManager(mock_resp)
        )
        client = EcoFlowHTTPQuota(
            session=mock_session,
            access_key="ak",
            secret_key="sk",
            device_sn="SN123",
            min_interval=0,
        )

        with (
            patch.object(client, "_check_rate_limit", return_value=True),
            patch(
                "ecoflow_energy.ecoflow.cloud_http.time.monotonic",
                side_effect=[0.0, 50.0, 100.0, 150.0, 299.0, 300.0],
            ),
            patch(
                "ecoflow_energy.ecoflow.cloud_http.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            caplog.at_level(
                logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"
            ),
        ):
            for _ in range(6):
                assert await client.get_quota_all() is None

        records = [
            record
            for record in caplog.records
            if record.name == "ecoflow_energy.ecoflow.cloud_http"
        ]
        assert mock_session.get.call_count == 18
        assert len([r for r in records if r.levelno == logging.WARNING]) == 1
        assert not [r for r in records if r.levelno >= logging.ERROR]
        assert len(records) == 2
        assert not [r for r in records if "8521" in r.getMessage()]
        assert [
            r.getMessage() for r in records if "failure persists" in r.getMessage()
        ] == [
            "HTTP poll failure persists for SN12...: "
            "kind=transport repeated=5"
        ]

    @pytest.mark.asyncio
    async def test_non_8521_error_not_retried(self):
        """Other API errors (e.g. code=1) are NOT retried."""
        mock_resp = AsyncMock()
        mock_resp.ok = True
        mock_resp.json = AsyncMock(return_value={"code": "1", "message": "invalid param"})

        mock_session = MagicMock()
        mock_session.get = MagicMock(
            return_value=AsyncContextManager(mock_resp)
        )

        client = EcoFlowHTTPQuota(
            session=mock_session,
            access_key="ak",
            secret_key="sk",
            device_sn="SN123",
            min_interval=0,
        )

        result = await client.get_quota_all()
        assert result is None
        # Only called once - no retry for non-8521 errors
        assert mock_session.get.call_count == 1


class AsyncContextManager:
    """Helper to mock async context managers (async with session.get(...))."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        pass


class TestOutageLogHygiene:
    @pytest.fixture(autouse=True)
    def _no_retry_delay(self):
        with patch(
            "ecoflow_energy.ecoflow.cloud_http.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            yield

    def _client(self, session):
        return EcoFlowHTTPQuota(
            session=session,
            access_key="ak",
            secret_key="sk",
            device_sn="HW51TEST00000001",
            min_interval=0,
        )

    @staticmethod
    def _success():
        response = AsyncMock()
        response.ok = True
        response.json = AsyncMock(
            return_value={"code": "0", "data": {"soc": 85}}
        )
        return response

    @staticmethod
    def _generic_failure(code: str = "1"):
        response = AsyncMock()
        response.ok = True
        response.json = AsyncMock(
            return_value={"code": code, "message": "temporary failure"}
        )
        return response

    @pytest.mark.asyncio
    async def test_transport_outage_warns_once_recovers_once_and_can_reopen(
        self, caplog
    ) -> None:
        session = MagicMock()
        session.get = MagicMock(side_effect=aiohttp.ClientError("offline"))
        client = self._client(session)

        with caplog.at_level(
            logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"
        ):
            await client.get_quota_all()
            await client.get_quota_all()

            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            errors = [r for r in caplog.records if r.levelno == logging.ERROR]
            assert len(warnings) == 1
            assert errors == []
            assert len([
                record
                for record in caplog.records
                if record.name == "ecoflow_energy.ecoflow.cloud_http"
            ]) == 1

            session.get = MagicMock(
                return_value=AsyncContextManager(self._success())
            )
            await client.get_quota_all()
            recoveries = [
                r
                for r in caplog.records
                if r.levelno == logging.INFO and "recovered" in r.message
            ]
            assert len(recoveries) == 1

            session.get = MagicMock(side_effect=aiohttp.ClientError("offline"))
            await client.get_quota_all()
            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert len(warnings) == 2

    @pytest.mark.asyncio
    async def test_generic_api_outage_is_transition_logged(self, caplog) -> None:
        session = MagicMock()
        session.get = MagicMock(
            return_value=AsyncContextManager(self._generic_failure())
        )
        client = self._client(session)

        with caplog.at_level(
            logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"
        ):
            await client.get_quota_all()
            await client.get_quota_all()

        assert len(
            [r for r in caplog.records if r.levelno == logging.WARNING]
        ) == 1
        assert client.last_error_code == "1"

    @pytest.mark.asyncio
    async def test_failure_kind_and_api_code_changes_are_transitions(
        self, caplog
    ) -> None:
        session = MagicMock()
        session.get = MagicMock(side_effect=aiohttp.ClientError("offline"))
        client = self._client(session)

        with caplog.at_level(
            logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"
        ):
            await client.get_quota_all()
            session.get = MagicMock(
                return_value=AsyncContextManager(self._generic_failure("41"))
            )
            await client.get_quota_all()
            await client.get_quota_all()
            session.get = MagicMock(
                return_value=AsyncContextManager(self._generic_failure("42"))
            )
            await client.get_quota_all()

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 3
        assert "transport failure" in warnings[0]
        assert "code=41" in warnings[1]
        assert "code=42" in warnings[2]

    def test_repeated_failure_summary_is_monotonic_and_rate_limited(
        self, caplog
    ) -> None:
        client = self._client(MagicMock())
        start = 1000.0

        with (
            patch(
                "ecoflow_energy.ecoflow.cloud_http.time.monotonic",
                side_effect=[
                    start,
                    start + _HTTP_FAILURE_SUMMARY_INTERVAL_S - 1,
                    start + _HTTP_FAILURE_SUMMARY_INTERVAL_S,
                ],
            ),
            caplog.at_level(
                logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"
            ),
        ):
            client._note_failure("transport", "first failure", "poll")
            client._note_failure("transport", "same failure", "poll")
            client._note_failure("transport", "same failure", "poll")

        messages = [record.getMessage() for record in caplog.records]
        assert messages == [
            "first failure",
            "HTTP poll failure persists for HW51...: "
            "kind=transport repeated=2",
        ]

    @pytest.mark.asyncio
    async def test_diagnostic_transport_failure_is_debug_only(self, caplog) -> None:
        session = MagicMock()
        session.get = MagicMock(side_effect=aiohttp.ClientError("offline"))
        client = self._client(session)

        with caplog.at_level(
            logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"
        ):
            await client.get_quota_all(diagnostic=True)

        assert not [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]

    @pytest.mark.asyncio
    async def test_explicit_set_transport_failure_is_debug_only(
        self, caplog
    ) -> None:
        session = MagicMock()
        session.put = MagicMock(side_effect=aiohttp.ClientError("offline"))
        client = self._client(session)

        with caplog.at_level(
            logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"
        ):
            await client.set_quota({"params": {"enabled": 1}})

        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "purpose=action kind=transport" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.DEBUG
        )

    @pytest.mark.asyncio
    async def test_successful_action_does_not_recover_poll_outage(
        self, caplog
    ) -> None:
        session = MagicMock()
        session.get = MagicMock(side_effect=aiohttp.ClientError("offline"))
        session.put = MagicMock(
            return_value=AsyncContextManager(self._success())
        )
        client = self._client(session)

        with caplog.at_level(
            logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"
        ):
            await client.get_quota_all()
            await client.set_quota({"params": {"enabled": 1}})
            await client.get_quota_all()

        assert len(
            [r for r in caplog.records if r.levelno == logging.WARNING]
        ) == 1
        assert not [
            r for r in caplog.records
            if r.levelno == logging.INFO and "recovered" in r.getMessage()
        ]

    @pytest.mark.asyncio
    async def test_client_response_error_never_leaks_url_or_full_serial(
        self, caplog
    ) -> None:
        full_sn = "HW51FULLSERIAL0001"
        full_url = (
            "https://api-e.ecoflow.com/iot-open/sign/device/quota/all"
            f"?sn={full_sn}"
        )
        request_info = MagicMock()
        request_info.real_url = full_url
        exc = aiohttp.ClientResponseError(
            request_info,
            (),
            status=503,
            message=f"failed request {full_url}",
        )
        session = MagicMock()
        session.get = MagicMock(side_effect=exc)
        client = EcoFlowHTTPQuota(
            session=session,
            access_key="ak",
            secret_key="sk",
            device_sn=full_sn,
            min_interval=0,
        )

        with caplog.at_level(
            logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"
        ):
            await client.get_quota_all()

        rendered = "\n".join(record.getMessage() for record in caplog.records)
        assert full_sn not in rendered
        assert full_url not in rendered
        assert "?sn=" not in rendered
        assert "ClientResponseError" in rendered


class TestError1006Handling:
    """Error 1006 (device not linked to API key) - config issue, not auth (#2)."""

    def _make_1006_response(self):
        resp = AsyncMock()
        resp.ok = True
        resp.json = AsyncMock(return_value={
            "code": "1006",
            "message": "current device is not allowed to get device info",
        })
        return resp

    def _make_success_response(self):
        resp = AsyncMock()
        resp.ok = True
        resp.json = AsyncMock(return_value={"code": "0", "data": {"soc": 85}})
        return resp

    def _make_client(self, session):
        return EcoFlowHTTPQuota(
            session=session,
            access_key="ak",
            secret_key="sk",
            device_sn="SN123",
            min_interval=0,
        )

    @pytest.mark.asyncio
    async def test_1006_sets_last_error_code(self):
        """Error 1006 sets last_error_code to '1006'."""
        mock_session = MagicMock()
        mock_session.get = MagicMock(
            return_value=AsyncContextManager(self._make_1006_response())
        )
        client = self._make_client(mock_session)

        result = await client.get_quota_all()
        assert result is None
        assert client.last_error_code == "1006"

    @pytest.mark.asyncio
    async def test_1006_not_retried(self):
        """Error 1006 is not retried - only one HTTP call."""
        mock_session = MagicMock()
        mock_session.get = MagicMock(
            return_value=AsyncContextManager(self._make_1006_response())
        )
        client = self._make_client(mock_session)

        await client.get_quota_all()
        assert mock_session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_six_poll_1006_failures_are_bounded_by_summary_interval(
        self, caplog
    ):
        mock_session = MagicMock()
        mock_session.get = MagicMock(
            return_value=AsyncContextManager(self._make_1006_response())
        )
        client = self._make_client(mock_session)

        with (
            patch.object(client, "_check_rate_limit", return_value=True),
            patch(
                "ecoflow_energy.ecoflow.cloud_http.time.monotonic",
                side_effect=[0.0, 50.0, 100.0, 150.0, 299.0, 300.0],
            ),
            caplog.at_level(
                logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"
            ),
        ):
            for _ in range(6):
                await client.get_quota_all()

        records = [
            record
            for record in caplog.records
            if record.name == "ecoflow_energy.ecoflow.cloud_http"
        ]
        assert len([r for r in records if r.levelno == logging.WARNING]) == 1
        assert not [r for r in records if r.levelno >= logging.ERROR]
        assert [
            r.getMessage() for r in records if "failure persists" in r.getMessage()
        ] == [
            "HTTP poll failure persists for SN12...: "
            "kind=api:1006 repeated=5"
        ]
        assert len(records) == 2

    @pytest.mark.asyncio
    async def test_poll_success_resets_1006_so_new_outage_warns(self, caplog):
        mock_session = MagicMock()
        mock_session.get = MagicMock(
            return_value=AsyncContextManager(self._make_1006_response())
        )
        client = self._make_client(mock_session)

        with (
            patch.object(client, "_check_rate_limit", return_value=True),
            patch(
                "ecoflow_energy.ecoflow.cloud_http.time.monotonic",
                side_effect=[0.0, 10.0],
            ),
            caplog.at_level(
                logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"
            ),
        ):
            await client.get_quota_all()
            mock_session.get = MagicMock(
                return_value=AsyncContextManager(self._make_success_response())
            )
            await client.get_quota_all()
            mock_session.get = MagicMock(
                return_value=AsyncContextManager(self._make_1006_response())
            )
            await client.get_quota_all()

        assert client.last_error_code == "1006"
        assert len(
            [r for r in caplog.records if r.levelno == logging.WARNING]
        ) == 2
        assert len([
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "recovered" in r.getMessage()
        ]) == 1

    @pytest.mark.asyncio
    async def test_action_success_does_not_reset_poll_1006(self, caplog):
        mock_session = MagicMock()
        mock_session.get = MagicMock(
            return_value=AsyncContextManager(self._make_1006_response())
        )
        mock_session.put = MagicMock(
            return_value=AsyncContextManager(self._make_success_response())
        )
        client = self._make_client(mock_session)

        with (
            patch.object(client, "_check_rate_limit", return_value=True),
            patch(
                "ecoflow_energy.ecoflow.cloud_http.time.monotonic",
                side_effect=[0.0, 10.0],
            ),
            caplog.at_level(
                logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"
            ),
        ):
            await client.get_quota_all()
            await client.set_quota({"params": {"enabled": True}})
            await client.get_quota_all()

        assert len(
            [r for r in caplog.records if r.levelno == logging.WARNING]
        ) == 1
        assert not [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "recovered" in r.getMessage()
        ]

    @pytest.mark.asyncio
    async def test_diagnostic_1006_and_8521_do_not_mutate_poll_latch(
        self, caplog
    ):
        mock_session = MagicMock()
        mock_session.get = MagicMock(
            return_value=AsyncContextManager(self._make_1006_response())
        )
        client = self._make_client(mock_session)

        with (
            patch.object(client, "_check_rate_limit", return_value=True),
            patch(
                "ecoflow_energy.ecoflow.cloud_http.time.monotonic",
                return_value=100.0,
            ),
            patch(
                "ecoflow_energy.ecoflow.cloud_http.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            caplog.at_level(
                logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"
            ),
        ):
            await client.get_quota_all()
            latch = (
                client._outage_active,
                client._outage_kind,
                client._outage_last_summary_at,
                client._outage_suppressed,
            )
            caplog.clear()

            await client.get_quota_all(diagnostic=True)
            response_8521 = AsyncMock()
            response_8521.ok = True
            response_8521.json = AsyncMock(
                return_value={"code": "8521", "message": "server error"}
            )
            mock_session.get = MagicMock(
                return_value=AsyncContextManager(response_8521)
            )
            await client.get_quota_all(diagnostic=True)

        assert latch == (
            client._outage_active,
            client._outage_kind,
            client._outage_last_summary_at,
            client._outage_suppressed,
        )
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


class TestFullSerialNeverLogged:
    """A full device serial must never reach the HA logs (only the prefix).

    Unsupported devices commonly return 1006, and the diagnostics feature
    exists to make users attach their HA log to a GitHub issue - so a full
    serial in the logs would leak. Every error/status log path is checked.
    """

    FULL_SN = "SM3ATEST00000001"
    PREFIX = "SM3A"

    def _make_client(self, session):
        return EcoFlowHTTPQuota(
            session=session,
            access_key="ak",
            secret_key="sk",
            device_sn=self.FULL_SN,
            min_interval=0,
        )

    def _response(self, payload):
        resp = AsyncMock()
        resp.ok = True
        resp.json = AsyncMock(return_value=payload)
        return resp

    def _assert_no_full_sn(self, caplog):
        for record in caplog.records:
            assert self.FULL_SN not in record.getMessage(), (
                "full serial leaked into logs: " + record.getMessage()
            )

    @pytest.mark.asyncio
    async def test_1006_does_not_log_full_sn(self, caplog):
        import logging

        mock_session = MagicMock()
        mock_session.get = MagicMock(
            return_value=AsyncContextManager(
                self._response({
                    "code": "1006",
                    "message": "current device is not allowed to get device info",
                })
            )
        )
        client = self._make_client(mock_session)

        with caplog.at_level(logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"):
            await client.get_quota_all()

        self._assert_no_full_sn(caplog)
        # The prefix is allowed and expected in the warning.
        assert any(self.PREFIX in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_generic_error_code_does_not_log_full_sn(self, caplog):
        import logging

        mock_session = MagicMock()
        mock_session.get = MagicMock(
            return_value=AsyncContextManager(
                self._response({"code": "1", "message": "invalid param"})
            )
        )
        client = self._make_client(mock_session)

        with caplog.at_level(logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"):
            await client.get_quota_all()

        self._assert_no_full_sn(caplog)

    @pytest.mark.asyncio
    async def test_all_attempts_failed_does_not_log_full_sn(self, caplog):
        import logging

        mock_session = MagicMock()
        # A network error on every attempt drives the "all attempts failed"
        # ERROR line.
        mock_session.get = MagicMock(side_effect=TimeoutError("boom"))
        client = self._make_client(mock_session)

        with (
            caplog.at_level(logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"),
            patch("ecoflow_energy.ecoflow.cloud_http.asyncio.sleep", new=AsyncMock()),
        ):
            result = await client.get_quota_all()

        assert result is None
        self._assert_no_full_sn(caplog)

    @pytest.mark.asyncio
    async def test_success_does_not_log_full_sn(self, caplog):
        import logging

        mock_session = MagicMock()
        mock_session.get = MagicMock(
            return_value=AsyncContextManager(
                self._response({"code": "0", "data": {"soc": 85}})
            )
        )
        client = self._make_client(mock_session)

        with caplog.at_level(logging.DEBUG, logger="ecoflow_energy.ecoflow.cloud_http"):
            await client.get_quota_all()

        self._assert_no_full_sn(caplog)


class TestDeadCodeRemoved:
    def test_no_powerocean_quota_keys(self):
        """POWEROCEAN_QUOTA_KEYS was dead code and must be removed."""
        source = (REPO_ROOT / "custom_components/ecoflow_energy/ecoflow/cloud_http.py").read_text()
        assert "POWEROCEAN_QUOTA_KEYS" not in source

    def test_no_get_powerocean_quota(self):
        """get_powerocean_quota was dead code and must be removed."""
        source = (REPO_ROOT / "custom_components/ecoflow_energy/ecoflow/cloud_http.py").read_text()
        assert "get_powerocean_quota" not in source

    def test_iot_quota_path_is_only_used_for_writes(self):
        """IOT_QUOTA_PATH came back for Delta 3 controls, not for reading.

        It was removed once as dead code. Reads must keep using the /quota/all
        endpoint, so guard that the path is reachable from set_quota only.
        """
        source = (REPO_ROOT / "custom_components/ecoflow_energy/ecoflow/cloud_http.py").read_text()
        assert "IOT_QUOTA_PATH" in source
        set_quota_body = source.split("async def set_quota")[1].split("async def")[0]
        assert "IOT_QUOTA_PATH" in set_quota_body
        get_body = source.split("async def get_quota_all")[1].split("async def")[0]
        assert "IOT_QUOTA_ALL_PATH" in get_body
        assert "IOT_QUOTA_PATH}" not in get_body
