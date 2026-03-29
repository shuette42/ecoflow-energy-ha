"""Enhanced Mode authentication — login + optional AES-CFB credential decryption.

Primary path: IoT Developer API provides MQTT credentials directly.
Fallback path: Portal login → JWT → AES-encrypted credentials → decrypt.

The login is always needed to obtain the userId for WSS ClientID generation.
Tries multiple API base URLs (EU + global) for resilience.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import IOT_API_BASE

logger = logging.getLogger(__name__)

_AUTH_LOGIN_PATH = "/auth/login"
_ENHANCED_CERT_PATH = "/iot-auth/enterprise-development/user/certification"
# Constant IV extracted from EcoFlow Portal JS bundle (module 78829)
_AES_IV = b"ojsajkqjwk1w2dfg"

# EcoFlow has regional API endpoints — try EU first, then global fallback
_AUTH_BASE_URLS = [
    IOT_API_BASE,                  # https://api-e.ecoflow.com (EU)
    "https://api.ecoflow.com",     # global fallback
]


async def enhanced_login(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
) -> dict[str, Any] | None:
    """Login to EcoFlow and return JWT token + userId.

    Tries multiple API base URLs for resilience.

    Returns:
        dict with ``token`` and ``user_id``, or None on failure.
    """
    payload = {
        "email": email,
        "password": base64.b64encode(password.encode()).decode(),
        "scene": "IOT_APP",
        "userType": "ECOFLOW",
    }

    last_error = ""
    for base_url in _AUTH_BASE_URLS:
        url = f"{base_url.rstrip('/')}{_AUTH_LOGIN_PATH}"
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with session.post(url, json=payload, timeout=timeout) as resp:
                body = await resp.json()
                if str(body.get("code")) != "0":
                    last_error = f"code={body.get('code')} msg={body.get('message')}"
                    logger.debug("Login attempt %s: %s", base_url, last_error)
                    continue
                data = body.get("data", {})
                token = data.get("token", "")
                user = data.get("user", {})
                user_id = str(user.get("userId", ""))
                if not token or not user_id:
                    last_error = "missing token or userId in response"
                    logger.debug("Login attempt %s: %s", base_url, last_error)
                    continue
                logger.debug("Enhanced login OK via %s", base_url)
                return {"token": token, "user_id": user_id}
        except (aiohttp.ClientError, TimeoutError) as exc:
            last_error = str(exc)
            logger.debug("Login attempt %s failed: %s", base_url, exc)
            continue

    logger.warning("Enhanced login failed on all endpoints: %s", last_error)
    return None


async def get_enhanced_credentials(
    session: aiohttp.ClientSession,
    token: str,
    base_url: str = IOT_API_BASE,
) -> dict[str, Any] | None:
    """Fetch and decrypt Enhanced Mode MQTT credentials (Portal path).

    This is a fallback — the primary path uses IoT Developer API credentials.

    Returns:
        dict with ``certificateAccount``, ``certificatePassword``, etc.
    """
    url = f"{base_url.rstrip('/')}{_ENHANCED_CERT_PATH}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            body = await resp.json()
            if str(body.get("code")) != "0":
                logger.warning(
                    "Enhanced certification failed: code=%s msg=%s",
                    body.get("code"),
                    body.get("message"),
                )
                return None
            encrypted_data = body.get("data")
            if not encrypted_data or not isinstance(encrypted_data, str):
                logger.warning("Enhanced certification: empty or invalid response")
                return None
            return _decrypt_certification(token, encrypted_data)
    except (aiohttp.ClientError, TimeoutError) as exc:
        logger.warning("Enhanced certification request failed: %s", exc)
        return None


def _decrypt_certification(
    token: str, encrypted_data: str
) -> dict[str, Any] | None:
    """Decrypt AES-CFB encrypted certification data.

    Algorithm (from EcoFlow Portal JS, module 78829):
      key = SHA256(JWT_token)           -> 32-byte AES key
      iv  = b"ojsajkqjwk1w2dfg"        -> constant 16-byte IV
      cipher = AES-CFB128              (cryptography.modes.CFB = 128-bit segments)
      plaintext = decrypt(base64_decode(encrypted_data))
      remove PKCS7 padding
      parse as JSON
    """
    try:
        key = hashlib.sha256(token.encode()).digest()
        cipher = Cipher(algorithms.AES(key), modes.CFB(_AES_IV))
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(base64.b64decode(encrypted_data)) + decryptor.finalize()

        # Remove PKCS7 padding (validate all padding bytes)
        if plaintext:
            pad_len = plaintext[-1]
            if 0 < pad_len <= 16 and all(b == pad_len for b in plaintext[-pad_len:]):
                plaintext = plaintext[:-pad_len]

        return json.loads(plaintext)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Enhanced certification decryption failed: %s", exc)
        return None
