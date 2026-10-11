"""Tests for Enhanced Mode AES-CFB credential decryption."""

import base64
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

try:
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:  # cryptography < 43.0
    from cryptography.hazmat.primitives.ciphers.modes import CFB

from ecoflow_energy.ecoflow.enhanced_auth import (
    _AES_IV,
    EnhancedAuthUnreachable,
    _decrypt_certification,
    enhanced_login,
    get_enhanced_credentials,
)


def _encrypt_test_data(token: str, data: dict) -> str:
    """Encrypt test data using the same algorithm as EcoFlow Portal."""
    plaintext = json.dumps(data).encode()
    # PKCS7 padding
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len] * pad_len)
    key = hashlib.sha256(token.encode()).digest()
    cipher = Cipher(algorithms.AES(key), CFB(_AES_IV))
    encryptor = cipher.encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


class TestDecryptCertification:
    """Tests for _decrypt_certification."""

    def test_roundtrip(self):
        """Encrypt → decrypt must return the original data."""
        token = "test_jwt_payload"
        data = {
            "certificateAccount": "open-abc123",
            "certificatePassword": "secret-xyz",
            "url": "mqtt-e.ecoflow.com",
            "port": "8883",
        }
        encrypted = _encrypt_test_data(token, data)
        result = _decrypt_certification(token, encrypted)

        assert result is not None
        assert result["certificateAccount"] == "open-abc123"
        assert result["certificatePassword"] == "secret-xyz"
        assert result["url"] == "mqtt-e.ecoflow.com"

    def test_different_tokens_fail(self):
        """Decryption with a different token must fail."""
        data = {"certificateAccount": "test"}
        encrypted = _encrypt_test_data("token_A", data)
        result = _decrypt_certification("token_B", encrypted)
        # Different key → garbage → JSON parse fails → None
        assert result is None

    def test_invalid_base64(self):
        """Invalid base64 must return None, not crash."""
        result = _decrypt_certification("any_token", "not-valid-base64!!!")
        assert result is None

    def test_aes_iv_is_correct(self):
        """The IV constant must match the EcoFlow Portal JS bundle."""
        assert _AES_IV == b"ojsajkqjwk1w2dfg"
        assert len(_AES_IV) == 16


class TestUnreachableVersusRefused:
    """A server that never answers is not a refused password (#531)."""

    @staticmethod
    def _session(post):
        session = MagicMock()
        session.post = post
        return session

    @pytest.mark.asyncio
    async def test_all_endpoints_timing_out_raises_with_type_in_reason(self):
        session = self._session(MagicMock(side_effect=TimeoutError()))

        with pytest.raises(EnhancedAuthUnreachable, match="TimeoutError"):
            await enhanced_login(
                session, "test@example.com", "test_password", raise_if_unreachable=True
            )

    @pytest.mark.asyncio
    async def test_unreachable_without_flag_returns_none(self):
        session = self._session(MagicMock(side_effect=aiohttp.ClientError("down")))

        result = await enhanced_login(session, "test@example.com", "test_password")

        assert result is None

    @pytest.mark.asyncio
    async def test_refused_login_returns_none_even_with_flag(self):
        resp = MagicMock()
        resp.json = AsyncMock(return_value={"code": "2026", "message": "bad password"})
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session = self._session(MagicMock(return_value=ctx))

        result = await enhanced_login(
            session, "test@example.com", "test_password", raise_if_unreachable=True
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_credential_fetch_timeout_raises_with_flag(self):
        session = MagicMock()
        session.get = MagicMock(side_effect=TimeoutError())

        with pytest.raises(EnhancedAuthUnreachable):
            await get_enhanced_credentials(
                session, "test_token", raise_if_unreachable=True
            )
