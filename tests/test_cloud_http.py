"""Tests for EcoFlowHTTPQuota — signature, rate limiting, dead code removal."""

import hashlib
import hmac
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_nonce_is_16_char_alphanumeric(self):
        client = self._make_client()
        headers = client._sign_headers({"sn": "SN123"})
        nonce = headers["nonce"]
        assert len(nonce) == 16, f"Nonce must be 16 chars, got {len(nonce)}"
        assert re.match(r"^[a-zA-Z0-9]{16}$", nonce), f"Nonce must be alphanumeric, got '{nonce}'"

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

    def test_sign_params_sorted_with_auth(self):
        """All parameters (payload + auth) must be sorted alphabetically together.

        Bug fix: previously auth params (accessKey, nonce, timestamp) were appended
        as unsorted tail instead of being merged into the sorted parameter list.
        """
        client = self._make_client()
        fixed_nonce = "abcdef1234567890"
        fixed_ts = "1700000000000"

        with patch("ecoflow_energy.ecoflow.cloud_http.time") as mock_time, \
             patch("ecoflow_energy.ecoflow.cloud_http.random") as mock_random:
            mock_time.time.return_value = 1700000000.0
            mock_random.choices.return_value = list(fixed_nonce)

            headers = client._sign_headers({"sn": "HW52ZZ"})

        # Expected: all params sorted alphabetically
        expected_sign_string = (
            f"accessKey=test_ak&nonce={fixed_nonce}&sn=HW52ZZ&timestamp={fixed_ts}"
        )
        expected_sig = hmac.new(
            b"test_sk", expected_sign_string.encode(), hashlib.sha256
        ).hexdigest()

        assert headers["sign"] == expected_sig, (
            f"Signature mismatch — params must be sorted alphabetically "
            f"including auth params. Expected sign_string: {expected_sign_string}"
        )

    def test_sign_empty_params_only_auth(self):
        """With no payload params, signature must contain only sorted auth params."""
        client = self._make_client()
        fixed_nonce = "zzzzzzzzzzzzzzzz"
        fixed_ts = "1700000000000"

        with patch("ecoflow_energy.ecoflow.cloud_http.time") as mock_time, \
             patch("ecoflow_energy.ecoflow.cloud_http.random") as mock_random:
            mock_time.time.return_value = 1700000000.0
            mock_random.choices.return_value = list(fixed_nonce)

            headers = client._sign_headers({})

        expected_sign_string = (
            f"accessKey=test_ak&nonce={fixed_nonce}&timestamp={fixed_ts}"
        )
        expected_sig = hmac.new(
            b"test_sk", expected_sign_string.encode(), hashlib.sha256
        ).hexdigest()

        assert headers["sign"] == expected_sig


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
        source = Path("custom_components/ecoflow_energy/ecoflow/cloud_http.py").read_text()
        assert "POWEROCEAN_QUOTA_KEYS" not in source

    def test_no_get_powerocean_quota(self):
        """get_powerocean_quota was dead code and must be removed."""
        source = Path("custom_components/ecoflow_energy/ecoflow/cloud_http.py").read_text()
        assert "get_powerocean_quota" not in source

    def test_no_iot_quota_path_import(self):
        """IOT_QUOTA_PATH import was only used by dead code."""
        source = Path("custom_components/ecoflow_energy/ecoflow/cloud_http.py").read_text()
        assert "IOT_QUOTA_PATH" not in source
