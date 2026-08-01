"""Raw frame capture for diagnostics.

A device variant can only be diagnosed against the bytes it actually
sends. Two callers need that: the coordinator, for a device that is
routed but decodes wrongly, and the unrouted-device probe, for a device
with no parser at all.

Both store the same shape, so a diagnostics reader sees one format
regardless of which path captured the frame.

``TypedFrameBuffer`` adds the sampling policy the probe needs for long
recordings - see its docstring for why a plain ring buffer is not enough.

No Home Assistant dependencies - stdlib only.
"""

from __future__ import annotations

import json
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


# A discriminator read out of a payload is device-controlled input. Keys end up
# in a diagnostics dump and in a dict that is capped by key count, so the value
# is length-limited before it becomes part of a key.
_KEY_VALUE_MAX_CHARS = 32

# JSON discriminators, in the order they are tried. Every device family that
# pushes JSON marks its message type with one of these: Delta 2 uses
# ``typeCode``, Delta 3 and the Smart Plug use ``cmdFunc``/``cmdId``, and SET
# echoes carry ``operateType``/``moduleType``.
_JSON_TYPE_FIELDS = ("typeCode", "operateType", "moduleType")


def _key_value(value: Any) -> str:
    """Return a discriminator value that is safe to use inside a key."""
    return str(value)[:_KEY_VALUE_MAX_CHARS]


def frame_key(entry: dict[str, Any], payload: bytes, secrets: list[str]) -> str:
    """Return the message-type key a frame is bucketed under.

    The key is the topic class plus the message type, because the same
    message type means something different on ``get_reply`` (a full state
    dump, answered on request) than on ``property`` (an incremental push),
    and the full dump is the single most useful frame for building a
    parser. Giving it its own bucket keeps a frequent push from crowding
    it out.

    For a protobuf frame the message type is the **first** ``(cmd_func,
    cmd_id)`` header. A frame can bundle several commands, and the first
    one is used because a bundle's composition is stable per message type
    in every capture observed so far - keying on the whole list would
    split one message type across buckets whenever a device appends an
    optional command. The remaining headers stay visible in the entry's
    ``cmds`` field, so nothing is lost for the reader.

    For a JSON frame the message type is the payload's own type
    discriminator. A payload that carries none falls back to a single
    JSON bucket, which is the honest answer: without a discriminator
    there is nothing to tell its messages apart by.

    ``secrets`` is required rather than optional because that JSON
    discriminator is a string copied out of the payload, and the key it
    becomes is exported to diagnostics - which users are asked to attach
    to public issue reports. The payload is masked first, exactly as the
    stored frame is, so a device that puts an identifier where a type
    marker belongs cannot smuggle it past the export as a dict key.
    """
    topic = str(entry.get("topic", "?"))
    if entry.get("format") == "json":
        return f"{topic}:{_json_key(sanitize_frame(payload, secrets))}"
    cmds = entry.get("cmds") or []
    if cmds and isinstance(cmds[0], dict):
        head = cmds[0]
        return f"{topic}:proto/{head.get('cmd_func')}.{head.get('cmd_id')}"
    return f"{topic}:proto/undecoded"


def _json_key(payload: bytes) -> str:
    """Return the type discriminator of a JSON push, or a shared fallback."""
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001
        return "json/unparsed"
    if not isinstance(data, dict):
        return "json"
    cmd_func = data.get("cmdFunc")
    cmd_id = data.get("cmdId")
    if cmd_func is not None or cmd_id is not None:
        return f"json/{_key_value(cmd_func)}.{_key_value(cmd_id)}"
    for field in _JSON_TYPE_FIELDS:
        value = data.get(field)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return f"json/{field}={_key_value(value)}"
    return "json"


class _Bucket:
    """The frames kept for one message type.

    Split in two on purpose. ``samples`` is the thinned history, one
    frame per time slot across the whole recording. ``latest`` always
    holds the newest frame of this type, so the end of the capture is
    never a slot width stale - a reporter who downloads diagnostics right
    after doing something on the device has to see that frame.
    """

    __slots__ = ("first_ts", "latest", "samples", "seen", "slot_s")

    def __init__(self) -> None:
        self.samples: list[tuple[int, dict[str, Any]]] = []
        self.latest: tuple[int, dict[str, Any]] | None = None
        self.first_ts = 0.0
        # Width of one sampling slot in seconds. Zero means "keep every
        # frame" and holds until the budget is first exceeded.
        self.slot_s = 0.0
        self.seen = 0

    def items(self) -> list[tuple[int, dict[str, Any]]]:
        """Return every frame kept for this type."""
        return self.samples if self.latest is None else [*self.samples, self.latest]


class TypedFrameBuffer:
    """Frame store that keeps a sample of every message type.

    A single ring buffer is the wrong shape for this job, and a six-hour
    recording proved it: a Stream Micro sent one message type every two
    seconds, so the 24 slots held its last 199 seconds and nothing else.
    The frames a parser actually needed - the battery report, the frames
    carrying state of charge - had been pushed out by the most frequent
    one. The recording was fine; the buffer threw it away.

    Two rules fix that:

    **One bucket per message type.** A rare type competes only with
    itself, so a report that arrives once an hour can never be displaced
    by one that arrives every two seconds. The number of buckets is
    capped so a chatty device cannot grow the capture without bound;
    frames beyond that cap are counted, not silently dropped.

    **One frame per time slot; when the budget is full, double the slot
    width and re-thin.** Slots are counted from the first frame of the
    type, so slot zero - the start of the recording - is kept for as long
    as the capture runs, and every later slot in which the device said
    anything is represented by exactly one frame. Nothing in the middle
    can be lost while the ends survive, because the rule never
    distinguishes between old and new: doubling the width merges
    neighbouring slots pairwise across the whole span at once. That is
    the property a plain "drop every second frame" ring does not have -
    there the freed slots refill at the arrival rate and the middle
    empties out within minutes, which is how the first attempt at this
    fix failed its own test.

    Slots are measured on frame timestamps, which are wall clock. A clock
    step backwards can only make a frame land in an earlier slot, which
    at worst drops it; the ``latest`` slot keeps the capture current
    either way.

    Not thread-safe by itself, and ``add`` is not atomic the way appending
    to a deque is. Both callers write from the Paho thread and read on the
    event loop, so each holds a lock of its own around every call: the
    probe its capture lock, the coordinator its raw-frame lock. A third
    caller has to bring one too.
    """

    def __init__(self, keys_max: int, per_key_max: int) -> None:
        self._keys_max = keys_max
        self._per_key_max = per_key_max
        # One slot of every type's budget is reserved for the newest frame.
        self._samples_max = max(per_key_max - 1, 1)
        # Insertion-ordered: the first message types a device sends are the
        # ones that get a bucket.
        self._buckets: dict[str, _Bucket] = {}
        self._dropped_frames = 0
        self._seq = 0

    def add(self, key: str, entry: dict[str, Any]) -> None:
        """Store one frame under its message-type key."""
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._keys_max:
                # No budget left for a new type. Counted rather than ignored,
                # so a reader can tell a saturated capture from a complete one.
                self._dropped_frames += 1
                return
            bucket = self._buckets[key] = _Bucket()
            bucket.first_ts = _entry_ts(entry)
        bucket.seen += 1
        self._seq += 1
        if bucket.latest is not None:
            # The frame that was newest until now competes for a sampling
            # slot; the incoming one takes over the reserved newest slot.
            self._sample(bucket, bucket.latest)
        bucket.latest = (self._seq, entry)

    def _sample(self, bucket: _Bucket, item: tuple[int, dict[str, Any]]) -> None:
        """Offer one frame to the thinned history of its type."""
        if bucket.samples and _slot(item, bucket) == _slot(bucket.samples[-1], bucket):
            return
        bucket.samples.append(item)
        # A loop, not a single doubling: an arrival burst can overshoot the
        # budget by more than one factor of two, and the invariant
        # len(samples) <= samples_max has to hold on the way out regardless.
        while len(bucket.samples) > self._samples_max:
            span = _entry_ts(bucket.samples[-1][1]) - bucket.first_ts
            bucket.slot_s = (
                bucket.slot_s * 2
                if bucket.slot_s > 0
                else max(span / self._samples_max, _MIN_SLOT_S)
            )
            bucket.samples = _thin_to_slots(bucket.samples, bucket)

    def frames(self) -> list[dict[str, Any]]:
        """Return every kept frame, oldest first.

        Sorted by the timestamp the reader sees, with arrival order as the
        tie-break so frames captured within the same clock tick keep the
        sequence they arrived in.
        """
        merged = [item for bucket in self._buckets.values() for item in bucket.items()]
        merged.sort(key=lambda item: (_entry_ts(item[1]), item[0]))
        return [entry for _, entry in merged]

    def stats(self) -> dict[str, Any]:
        """Return what was heard versus what was kept.

        Without this a reader cannot tell a quiet device from a saturated
        capture: both show a short frame list. ``span_s`` covers the whole
        capture window rather than one type, because the first and the
        newest frame of every type are always kept.
        """
        kept = self.frames()
        timestamps = [ts for ts in map(_entry_ts, kept) if ts > 0]
        return {
            "frames_seen": (
                sum(bucket.seen for bucket in self._buckets.values())
                + self._dropped_frames
            ),
            "frames_kept": len(kept),
            "span_s": (
                round(timestamps[-1] - timestamps[0], 1)
                if len(timestamps) > 1
                else None
            ),
            "keys_tracked": len(self._buckets),
            "keys_max": self._keys_max,
            "per_key_max": self._per_key_max,
            "frames_dropped_key_budget": self._dropped_frames,
            "per_key": {
                key: {"seen": bucket.seen, "kept": len(bucket.items())}
                for key, bucket in self._buckets.items()
            },
        }


# Floor for the sampling slot, used when a type's first frames all carry the
# same timestamp and the observed span is therefore zero.
_MIN_SLOT_S = 0.001


def _slot(item: tuple[int, dict[str, Any]], bucket: _Bucket) -> int:
    """Return which sampling slot a frame falls into."""
    if bucket.slot_s <= 0:
        # Every frame gets its own slot until the budget is first exceeded.
        return item[0]
    return int((_entry_ts(item[1]) - bucket.first_ts) // bucket.slot_s)


def _thin_to_slots(
    items: list[tuple[int, dict[str, Any]]], bucket: _Bucket
) -> list[tuple[int, dict[str, Any]]]:
    """Keep the earliest frame of each slot, discarding the rest."""
    thinned: list[tuple[int, dict[str, Any]]] = []
    last_slot: int | None = None
    for item in items:
        slot = _slot(item, bucket)
        if slot != last_slot:
            thinned.append(item)
            last_slot = slot
    return thinned


def _entry_ts(entry: dict[str, Any]) -> float:
    """Return the entry timestamp, tolerating a missing or odd value."""
    ts = entry.get("ts")
    return float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0.0
