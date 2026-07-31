"""Raw frame capture for diagnostics.

A device variant can only be diagnosed against the bytes it actually
sends. Two callers need that: the coordinator, for a device that is
routed but decodes wrongly, and the unrouted-device probe, for a device
with no parser at all.

Both store the same shape, so a diagnostics reader sees one format
regardless of which path captured the frame.

No Home Assistant dependencies - stdlib only.
"""

from __future__ import annotations

import re
import time
from typing import Any

# Filler byte for masked identifiers. Replacing a secret with a run of the
# same length keeps every byte offset in the frame intact, which is what a
# field-layout analysis depends on.
_MASK_BYTE = b"X"

# EcoFlow serials are 16 alphanumeric upper-case characters and appear in
# frames as plain ASCII. The caller can only name the identifiers it knows,
# which is the device serial and the account id - a frame also carries the
# serial of every battery pack and of any attached accessory, and those are
# nobody's to publish either. This catches them by shape.
_SERIAL_RUN = re.compile(rb"[A-Z0-9]{15,}")


def sanitize_frame(payload: bytes, secrets: list[str]) -> bytes:
    """Mask identifying strings inside a raw frame.

    Protobuf frames carry the device serial and, on some commands, the
    account user id as plain ASCII. Each is replaced by a filler of equal
    length, in every case variant the payload might use.

    Named identifiers are masked first, then anything else shaped like a
    serial. The second pass matters because a frame also carries the serial
    of every battery pack and of any attached accessory, and the caller
    cannot name what it has not discovered yet. Masking preserves length, so
    byte offsets survive both passes and a field-layout analysis still works.
    """
    sanitized = payload
    for secret in secrets:
        if not secret:
            continue
        for variant in {secret, secret.upper(), secret.lower()}:
            raw = variant.encode("ascii", "ignore")
            if raw and raw in sanitized:
                sanitized = sanitized.replace(raw, _MASK_BYTE * len(raw))
    return _SERIAL_RUN.sub(lambda m: _MASK_BYTE * len(m.group()), sanitized)


def is_proto_frame(payload: bytes) -> bool:
    """Return whether a payload looks like a protobuf frame.

    Mirrors the check the ingest path uses to decide between the JSON and
    the protobuf branch, so capture and parsing never disagree about what
    a frame is.
    """
    return b"\x0a" in payload[:4]


def build_frame_entry(
    topic: str,
    payload: bytes,
    secrets: list[str],
    max_bytes: int,
    parsed_keys: int | None = None,
) -> dict[str, Any]:
    """Build one diagnostics entry for a raw frame.

    The frame is masked first and truncated second, so truncation can
    never cut a mask in half and leave a fragment of a serial behind.
    `size` reports the original length, which is what tells a reader that
    a frame was longer than the stored hex.
    """
    entry: dict[str, Any] = {
        "ts": time.time(),
        "topic": "get_reply" if "get_reply" in topic else "property",
        "size": len(payload),
        "hex": sanitize_frame(payload, secrets)[:max_bytes].hex(),
    }
    if parsed_keys is not None:
        entry["parsed_keys"] = parsed_keys
    return entry


def decode_cmd_headers(payload: bytes) -> list[dict[str, Any]]:
    """Return the (cmd_func, cmd_id) pairs a frame carries.

    Used for diagnostics readability only: knowing which commands a device
    sends is the first thing a field-layout analysis needs. A frame that
    does not decode yields an empty list rather than raising, because a
    capture path must never affect ingest.
    """
    from .proto.decoder import decode_header_message

    try:
        headers, _ = decode_header_message(payload)
    except Exception:  # noqa: BLE001
        return []
    return [
        {"cmd_func": header.get("cmd_func"), "cmd_id": header.get("cmd_id")}
        for header in (headers or [])
        if isinstance(header, dict)
    ]
