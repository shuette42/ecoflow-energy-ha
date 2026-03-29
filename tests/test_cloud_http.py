"""Tests for EcoFlowHTTPQuota — signature, rate limiting, dead code removal."""

import hashlib
import hmac
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent

from ecoflow_energy.ecoflow.cloud_http import EcoFlowHTTPQuota


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


class TestDeadCodeRemoved:
    def test_no_powerocean_quota_keys(self):
        """POWEROCEAN_QUOTA_KEYS was dead code and must be removed."""
        source = (REPO_ROOT / "custom_components/ecoflow_energy/ecoflow/cloud_http.py").read_text()
        assert "POWEROCEAN_QUOTA_KEYS" not in source

    def test_no_get_powerocean_quota(self):
        """get_powerocean_quota was dead code and must be removed."""
        source = (REPO_ROOT / "custom_components/ecoflow_energy/ecoflow/cloud_http.py").read_text()
        assert "get_powerocean_quota" not in source

    def test_no_iot_quota_path_import(self):
        """IOT_QUOTA_PATH import was only used by dead code."""
        source = (REPO_ROOT / "custom_components/ecoflow_energy/ecoflow/cloud_http.py").read_text()
        assert "IOT_QUOTA_PATH" not in source
