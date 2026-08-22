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
from collections.abc import Mapping
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


def frame_budget(
    cmds: list[Any], message_max: int, bundle_max: int, bundle_cap: int
) -> int:
    """Return how many bytes of a frame to keep.

    The two kinds of frame a device sends differ by an order of magnitude,
    so one number for both is the wrong shape. A change is pushed as a
    single message - 41 to 587 B across every capture in the corpus - while
    the answer to a get bundles the whole state into one frame: 1434 to
    1465 B in six messages on a STREAM AC 5000, 1009 B in two on a
    PowerOcean. A budget that fits the pushes cuts every bundle, and the
    bundle is the frame a field layout is read from.

    Deciding per frame rather than raising one constant keeps the cost
    where the bytes are: a device that never bundles is bounded by
    ``message_max``, and only a frame that demonstrably carries several
    messages may claim more.

    How much more follows from the frame as well. A fixed bundle budget was
    too small twice, most recently against an Ocean 2 whose battery report
    bundles 12 to 14 messages at 4956 to 5899 B - every bundle in a 16 hour
    capture cut, and the count moving between frames on the same unit. A
    bundle of n messages may therefore claim n message budgets, so the
    budget grows with the width the frame itself declares rather than with
    the next device that surprises us. ``bundle_max`` stays as the floor,
    because a narrow bundle of wide messages was already covered by it and
    must not come out worse, and ``bundle_cap`` bounds the claim.

    A frame whose headers did not decode counts as one message. Nothing
    proves it carries more, and the entry says it was cut if it was.
    """
    if len(cmds) <= 1:
        return message_max
    return min(bundle_cap, max(bundle_max, len(cmds) * message_max))


def _topic_class(topic: str) -> str:
    """Return the bucket a topic belongs to.

    Four classes rather than two. `frame_key` puts the class in front of
    the message type, so a write gets its own bucket and cannot be crowded
    out of the capture by telemetry - which outnumbers it by four orders of
    magnitude on a live device.

    `set_reply` is not a write and does not share its bucket. It is the
    device answering one, which is the half that says whether a write was
    accepted rather than merely sent, and it is worth its own place for the
    same reason the write is.
    """
    if "get_reply" in topic:
        return "get_reply"
    if topic.endswith("/set_reply"):
        return "set_reply"
    if topic.endswith("/set"):
        return "set"
    return "property"


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
    `size` reports the original length.

    A cut frame carries ``truncated``. `size` alone technically said the
    same thing - it disagrees with the length of the stored hex - but
    nobody read it that way: three STREAM AC 5000 downloads went through
    analysis with their full-state frames cut to a third, and the missing
    fields were reported as fields the device does not send. A flag that
    has to be derived is a flag that gets missed, so this one is written
    down. It is absent rather than false on a whole frame, which also
    dates the entry: a capture whose frames never carry the key predates
    the marker and has to be checked by hand.
    """
    sanitized = sanitize_frame(payload, secrets)
    entry: dict[str, Any] = {
        "ts": time.time(),
        "topic": _topic_class(topic),
        "size": len(payload),
        "hex": sanitized[:max_bytes].hex(),
    }
    if len(sanitized) > max_bytes:
        entry["truncated"] = True
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

    __slots__ = ("first_ts", "keys_seen", "latest", "novel", "samples", "seen", "slot_s")

    def __init__(self) -> None:
        self.samples: list[tuple[int, dict[str, Any]]] = []
        self.latest: tuple[int, dict[str, Any]] | None = None
        self.first_ts = 0.0
        # Width of one sampling slot in seconds. Zero means "keep every
        # frame" and holds until the budget is first exceeded.
        self.slot_s = 0.0
        self.seen = 0
        # What this message type has said before: the last value of every
        # reading, and how many frames in a row it has held it. Both are
        # needed - see TypedFrameBuffer for why a first appearance alone
        # does not catch the case this exists for.
        self.keys_seen: dict[str, tuple[Any, int]] = {}
        self.novel: list[tuple[int, dict[str, Any]]] = []

    def items(self) -> list[tuple[int, dict[str, Any]]]:
        """Return every frame kept for this type, without repeating one.

        A novel frame is also offered to the thinned history, so the two
        lists can hold the same frame. They are merged on the arrival
        sequence, which is unique per frame.
        """
        merged = dict(self.novel)
        merged.update(self.samples)
        if self.latest is not None:
            merged[self.latest[0]] = self.latest[1]
        return sorted(merged.items())


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

    Slots are measured on frame timestamps, which are wall clock, so an
    NTP correction can hand a frame a timestamp older than the bucket's
    first one. Such a frame shares slot 0 with the oldest frame instead of
    getting a slot of its own below zero. That is not cosmetic: widening
    merges neighbouring slots, and a below-zero slot sitting between two
    zero slots is a pattern no widening can merge, so the loop that widens
    until the budget holds would never reach it.

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
        # Novel frames are kept on top of that budget rather than out of it:
        # taking their slots from the thinned history would trade the shape
        # of the recording for its contents, and the sampling exists so that
        # a six-hour capture still has a middle.
        self._novel_max = _NOVEL_PER_KEY_MAX
        # Insertion-ordered: the first message types a device sends are the
        # ones that get a bucket.
        self._buckets: dict[str, _Bucket] = {}
        self._dropped_frames = 0
        self._seq = 0

    def add(
        self,
        key: str,
        entry: dict[str, Any],
        readings: Mapping[str, Any] | None = None,
    ) -> None:
        """Store one frame under its message-type key.

        ``readings`` are what the parser took out of this frame, names and
        values. They decide whether the frame said something its message
        type has not said before, which is the one thing the thinned
        history cannot preserve on its own.
        """
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
        if readings is not None:
            self._note_novelty(bucket, (self._seq, entry), readings)

    def _note_novelty(
        self,
        bucket: _Bucket,
        item: tuple[int, dict[str, Any]],
        readings: Mapping[str, Any],
    ) -> None:
        """Keep a frame that said something its message type had not said.

        A configuration readback is the case this exists for. It rides the
        same message type as the telemetry around it and arrives once, so
        the one frame a recording was opened for competes with a push that
        repeats every two seconds, and loses.

        Two things count as new, because the first alone is not enough. A
        reading the type has never carried is the obvious one. A reading
        whose value changed after holding still is the one that matters for
        a setting somebody just moved in the vendor app, and the first
        appearance of that key will usually have been its **old** value.

        Holding still is what separates a setting from telemetry, and it is
        why the second rule does not simply keep every change. Grid power
        moves on every frame and would claim a slot on every frame, which
        is the crowding the sampling exists to prevent. A value that has
        been the same for `_STABLE_RUN` frames and then moves is a setting
        on any device this integration talks to.

        Novelty is judged per message type rather than per device. The same
        reading can arrive both in an answer to a get and in an incremental
        push, and those are different buckets on purpose: that a full dump
        once carried a field says nothing about whether the device ever
        pushes it on its own, which is the question a control needs
        answered.

        The keys a type has carried are bounded by the parser's key space,
        so the record cannot grow without bound. Values are kept only to be
        compared with the next one, never exported. The frames kept are
        capped separately, keeping the most recent: the first frames of a
        recording introduce every key a device reports, and those are
        already kept as the start of the thinned history.
        """
        novel = False
        for name, value in readings.items():
            key = str(name)
            previous = bucket.keys_seen.get(key)
            if previous is None:
                novel = True
                bucket.keys_seen[key] = (value, 1)
                continue
            last, run = previous
            if _same_reading(last, value):
                bucket.keys_seen[key] = (last, run + 1)
                continue
            if run >= _STABLE_RUN:
                novel = True
            bucket.keys_seen[key] = (value, 1)
        if not novel:
            return
        bucket.novel.append(item)
        if len(bucket.novel) > self._novel_max:
            del bucket.novel[0]

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
            before = len(bucket.samples)
            widest = max(
                _entry_ts(item[1]) - bucket.first_ts for item in bucket.samples
            )
            bucket.samples = _thin_to_slots(bucket.samples, bucket)
            if len(bucket.samples) >= before and bucket.slot_s > max(
                widest, _MIN_SLOT_S
            ):
                # Unreachable while slots stay non-negative and monotonic in
                # the timestamp: once a slot is wider than the whole span,
                # every sample falls into slot 0 and thinning leaves one. It
                # stays as a backstop because the alternative is a loop that
                # never ends, on the Paho thread and under the caller's lock,
                # which stops ingest outright and blocks the next diagnostics
                # read behind the same lock.
                del bucket.samples[0]

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
                key: {
                    "seen": bucket.seen,
                    "kept": len(bucket.items()),
                    # Frames held for carrying a reading this type had not
                    # carried before. A zero here on a type that clearly
                    # changed during the recording says the parser never saw
                    # the change, which is a different fault from a thinned
                    # capture and needs a different answer.
                    "novel": len(bucket.novel),
                }
                for key, bucket in self._buckets.items()
            },
        }


# Floor for the sampling slot, used when a type's first frames all carry the
# same timestamp and the observed span is therefore zero.
_MIN_SLOT_S = 0.001

# How many frames a message type may keep for having said something new.
# Four rather than one because a single change in the vendor app can move
# several settings at once, and rather than many because these sit on top of
# the per-type budget instead of inside it. Worst case for a whole capture is
# therefore keys_max * (per_key_max + 4) frames rather than
# keys_max * per_key_max: 280 rather than 200 at the coordinator's settings.
_NOVEL_PER_KEY_MAX = 4

# How many frames a reading must hold one value before a change to it is
# worth a slot. Telemetry moves on nearly every frame and never reaches this;
# a setting holds for as long as nobody touches it. Five is deliberately low:
# the cost of a false positive is one kept frame, the cost of a false negative
# is the frame the recording was opened for.
_STABLE_RUN = 5


def _same_reading(previous: Any, current: Any) -> bool:
    """Return whether a reading is unchanged, tolerating unhashable values.

    Values come out of a parser and are mostly numbers, but a list or a dict
    reaches here too. Equality is compared rather than identity, and anything
    that refuses to compare counts as changed - which costs at most one kept
    frame and never hides one.
    """
    try:
        return bool(previous == current)
    except Exception:  # noqa: BLE001
        return False


def _slot(item: tuple[int, dict[str, Any]], bucket: _Bucket) -> int:
    """Return which sampling slot a frame falls into."""
    if bucket.slot_s <= 0:
        # Every frame gets its own slot until the budget is first exceeded.
        return item[0]
    # Clamped at zero: a frame older than the bucket's first one would other-
    # wise take a negative slot, and _thin_to_slots compares each frame only
    # against its predecessor, so [0, -1, 0] survives every doubling.
    offset = _entry_ts(item[1]) - bucket.first_ts
    return int(offset // bucket.slot_s) if offset > 0 else 0


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
