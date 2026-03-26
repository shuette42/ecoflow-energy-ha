"""Tests for Enhanced Mode AES-CFB credential decryption."""

import base64
import hashlib
import json

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ecoflow_energy.ecoflow.enhanced_auth import _AES_IV, _decrypt_certification


def _encrypt_test_data(token: str, data: dict) -> str:
    """Encrypt test data using the same algorithm as EcoFlow Portal."""
    plaintext = json.dumps(data).encode()
    # PKCS7 padding
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len] * pad_len)
    key = hashlib.sha256(token.encode()).digest()
    cipher = Cipher(algorithms.AES(key), modes.CFB(_AES_IV))
    encryptor = cipher.encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


class TestDecryptCertification:
    """Tests for _decrypt_certification."""

    def test_roundtrip(self):
        """Encrypt → decrypt must return the original data."""
        token = "eyJhbGciOiJIUzI1NiJ9.test_payload.sig"
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
