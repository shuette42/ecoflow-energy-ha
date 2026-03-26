"""Tests for EcoFlow ClientID generator."""

import hashlib
import re

from ecoflow_energy.ecoflow.clientid import BT, generate_client_id


def test_generate_client_id_format():
    """ClientID must match WEB_{uuid}_{userId}_{appKey}_{ts}_{hash}."""
    cid = generate_client_id("123456789")
    parts = cid.split("_")
    # WEB, uuid (5 parts with hyphens joined = 1 part here split further)
    assert cid.startswith("WEB_")
    # Must contain the user_id
    assert "_123456789_" in cid
    # Total length > 80 (uuid + appkey + ts + hash)
    assert len(cid) > 80


def test_generate_client_id_unique():
    """Two calls must produce different ClientIDs (new uuid + timestamp)."""
    a = generate_client_id("user1")
    b = generate_client_id("user1")
    assert a != b


def test_generate_client_id_hash_verification():
    """Verify the MD5 hash is computed correctly (matches the algorithm)."""
    user_id = "1798085972312506370"
    cid = generate_client_id(user_id)

    # Parse components: WEB_{uuid}_{userId}_{appKey}_{timestamp}_{hash}
    # uuid has 4 hyphens → 5 sub-parts
    assert cid.startswith("WEB_")
    rest = cid[4:]  # after "WEB_"

    # Find the user_id to split around it
    idx = rest.index(f"_{user_id}_")
    uuid_part = rest[:idx]
    after_user = rest[idx + len(f"_{user_id}_"):]

    # after_user = appKey_timestamp_hash
    app_key = after_user[:32]
    ts_and_hash = after_user[33:]  # skip "_"
    timestamp = ts_and_hash.split("_")[0]
    verify_hash = ts_and_hash.split("_")[1]

    # Find the secret for this appKey
    secret = None
    for ak, s in BT:
        if ak == app_key:
            secret = s
            break
    assert secret is not None, f"appKey {app_key} not in BT table"

    # Recompute hash
    base = f"WEB_{uuid_part}_{user_id}"
    hash_input = f"{secret}{base}{timestamp}"
    expected = hashlib.md5(hash_input.encode()).hexdigest().upper()
    assert verify_hash == expected


def test_bt_table_has_32_entries():
    """BT lookup table must have exactly 32 entries."""
    assert len(BT) == 32
    for app_key, secret in BT:
        assert len(app_key) == 32
        assert len(secret) == 32
