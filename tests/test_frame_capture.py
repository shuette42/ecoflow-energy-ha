"""Tests for the shared raw-frame capture helpers."""

import signal
from contextlib import contextmanager
from typing import Any, Iterator

from ecoflow_energy.const import (
    RAW_FRAME_BUNDLE_HARD_CAP,
    RAW_FRAME_BUNDLE_MAX_BYTES,
    RAW_FRAME_MAX_BYTES,
)
from ecoflow_energy.ecoflow.frame_capture import (
    _slot,
    TypedFrameBuffer,
    build_frame_entry,
    decode_cmd_headers,
    frame_budget,
    frame_key,
    is_proto_frame,
    sanitize_frame,
)
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)


# Frame timestamps are wall clock. Tests offset from a fixed epoch instead of
# starting at zero, so they exercise the same value range the capture sees.
_T0 = 1_785_000_000.0

# The six commands a STREAM AC 5000 bundles into one get_reply, in the order
# the reporter captures show them.
_ES22_BUNDLE = ((32, 50), (254, 39), (254, 40), (53, 77), (50, 2), (32, 2))


def _header(cmd_func: int, cmd_id: int, pdata: bytes) -> bytes:
    """Build one repeated-header entry of a protobuf frame."""
    header = bytearray()
    header.extend(encode_field_bytes(1, pdata))
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    return encode_field_bytes(1, bytes(header))


def _frame_of(total: int, *commands: tuple[int, int]) -> bytes:
    """Build a decodable frame of exactly `total` bytes.

    Padding goes into the last message's payload rather than after the last
    header, because a trailing run of filler is not a valid tag and the whole
    frame would stop decoding - which would test the wrong thing.
    """
    prefix = b"".join(_header(func, ident, bytes(160)) for func, ident in commands[:-1])
    func, ident = commands[-1]
    for pad in range(total):
        frame = prefix + _header(func, ident, bytes(pad))
        if len(frame) == total:
            return frame
    raise AssertionError(f"no frame of exactly {total} B for {commands}")


def _entry(offset: float, **extra: Any) -> dict[str, Any]:
    """Build a stored frame `offset` seconds into the recording.

    Timestamps are supplied by the test, never read from a clock, so the
    span assertions below mean the same thing on every machine.
    """
    return {"ts": _T0 + offset, "topic": "property", "format": "proto", **extra}


@contextmanager
def _must_finish_within(seconds: int) -> Iterator[None]:
    """Fail instead of hanging forever.

    The defect these guard against is a loop that never terminates, so an
    assertion cannot express it: without a deadline the test run itself
    would hang and CI would time out with no clue which test did it.
    """

    def _expired(signum: int, frame: Any) -> None:
        raise AssertionError(f"did not finish within {seconds}s")

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


class TestSanitizeFrame:
    def test_serial_is_replaced_by_equal_length_filler(self) -> None:
        """Byte offsets must survive masking, a field analysis depends on them."""
        sn = "RE11TEST00000001"
        payload = b"\x0a\x10" + sn.encode() + b"\x12\x04data"

        result = sanitize_frame(payload, [sn])

        assert sn.encode() not in result
        assert len(result) == len(payload)
        assert b"X" * len(sn) in result

    def test_case_variants_are_caught(self) -> None:
        sn = "Re11Test00000001"
        payload = b"\x0a" + sn.lower().encode() + sn.upper().encode()

        result = sanitize_frame(payload, [sn])

        assert sn.lower().encode() not in result
        assert sn.upper().encode() not in result

    def test_user_id_is_masked_too(self) -> None:
        payload = b"\x0a user12345 payload"

        result = sanitize_frame(payload, ["", "user12345"])

        assert b"user12345" not in result

    def test_empty_secret_is_ignored(self) -> None:
        payload = b"\x0a\x01\x02"

        assert sanitize_frame(payload, ["", ""]) == payload

    def test_unnamed_serials_are_masked_by_shape(self) -> None:
        """A frame carries serials the caller never knew about.

        Battery packs and attached accessories bring their own serials, and
        the coordinator can only name the device serial and the account id.
        Publishing a dump would otherwise expose them.
        """
        device_sn = "HJ31TEST00000001"
        pack_sn = "BP5000TEST000001"
        payload = (
            b"\x0a\x10" + device_sn.encode() + b"\x12\x10" + pack_sn.encode()
        )

        result = sanitize_frame(payload, [device_sn])

        assert device_sn.encode() not in result
        assert pack_sn.encode() not in result
        assert len(result) == len(payload)

    def test_masking_preserves_byte_offsets(self) -> None:
        """Both passes replace by length, so the frame layout is unchanged."""
        payload = b"\x0a\x10" + b"HJ31TEST00000001" + b"\x18\x2a"

        result = sanitize_frame(payload, ["HJ31TEST00000001"])

        assert result.startswith(b"\x0a\x10")
        assert result.endswith(b"\x18\x2a")
        assert len(result) == len(payload)

    def test_short_alphanumeric_runs_survive(self) -> None:
        """Over-masking would destroy the payload this capture exists for."""
        payload = b"\x0a\x04ABC1\x12\x02OK"

        assert sanitize_frame(payload, []) == payload


class TestIsProtoFrame:
    def test_protobuf_header(self) -> None:
        assert is_proto_frame(b"\x0a\x10abc") is True

    def test_json_payload(self) -> None:
        assert is_proto_frame(b'{"soc": 50}') is False

    def test_empty_payload(self) -> None:
        assert is_proto_frame(b"") is False


class TestBuildFrameEntry:
    def test_masking_happens_before_truncation(self) -> None:
        """Truncating first could leave half a serial in the stored hex."""
        sn = "RE11TEST00000001"
        payload = b"\x0a" + sn.encode() + b"\xff" * 100

        entry = build_frame_entry("/app/device/property/x", payload, [sn], max_bytes=12)

        stored = bytes.fromhex(entry["hex"])
        assert len(stored) == 12
        assert b"RE11" not in stored

    def test_size_reports_the_original_length(self) -> None:
        payload = b"\x0a" + b"\x00" * 999

        entry = build_frame_entry("/topic", payload, [], max_bytes=16)

        assert entry["size"] == 1000
        assert len(bytes.fromhex(entry["hex"])) == 16

    def test_topic_is_classified(self) -> None:
        assert build_frame_entry("/a/get_reply", b"\x0a", [], 8)["topic"] == "get_reply"
        assert build_frame_entry("/a/property/x", b"\x0a", [], 8)["topic"] == "property"

    def test_parsed_keys_optional(self) -> None:
        assert "parsed_keys" not in build_frame_entry("/t", b"\x0a", [], 8)
        assert build_frame_entry("/t", b"\x0a", [], 8, parsed_keys=3)["parsed_keys"] == 3

    def test_a_cut_frame_says_so(self) -> None:
        """The mismatch was always derivable and nobody derived it."""
        entry = build_frame_entry("/t", b"\x0a" + b"\x00" * 999, [], 16)

        assert entry["truncated"] is True

    def test_a_whole_frame_carries_no_marker(self) -> None:
        """Absent, not False: an entry says nothing unless it was cut."""
        entry = build_frame_entry("/t", b"\x0a" + b"\x00" * 10, [], 512)

        assert "truncated" not in entry

    def test_a_serial_past_the_old_cap_is_masked(self) -> None:
        """Masking runs over the whole frame, not over the first 512 B.

        The larger budget stores bytes that used to be discarded, so a serial
        sitting a kilobyte into a bundle is now published unless the mask
        reaches it. This frame stays under its budget, so it proves the mask
        has no length assumption; the ordering against truncation is pinned
        by the straddle case below.
        """
        sn = "ES22TEST00000001"
        payload = b"\x0a" + b"\x00" * 700 + sn.encode() + b"\x00" * 700

        entry = build_frame_entry("/t", payload, [sn], RAW_FRAME_BUNDLE_MAX_BYTES)

        stored = bytes.fromhex(entry["hex"])
        assert len(stored) == len(payload)
        assert sn.encode() not in stored
        assert b"X" * len(sn) in stored

    def test_a_serial_cut_by_the_budget_leaves_no_fragment(self) -> None:
        """Masked first, truncated second - and this is the case that proves it.

        A serial straddling the cut is the only frame that tells the two
        orderings apart. Masked first, the cut lands inside a run of filler
        and the stored frame ends in mask bytes. Truncated first, the stored
        frame ends in the front half of a real serial - a fragment too short
        for the shape-based pass to ever catch, in a download users attach
        to public issues.
        """
        sn = "ES22TEST00000001"
        budget = 64
        # The serial starts 8 bytes before the cut, so exactly half of it
        # falls inside the stored window.
        payload = b"\x0a" + b"\x00" * (budget - 1 - 8) + sn.encode() + b"\x00" * 40

        entry = build_frame_entry("/t", payload, [sn], budget)

        stored = bytes.fromhex(entry["hex"])
        assert entry["truncated"] is True
        assert len(stored) == budget
        assert sn[:8].encode() not in stored
        assert stored.endswith(b"X" * 8)


class TestFrameBudget:
    """How much of a frame is kept follows from what the frame carries."""

    def test_a_single_message_gets_the_message_budget(self) -> None:
        cmds = [{"cmd_func": 254, "cmd_id": 39}]

        assert frame_budget(cmds, 1024, 2048, 8192) == 1024

    def test_an_undecoded_frame_gets_the_message_budget(self) -> None:
        """Nothing proves it carries more than one message."""
        assert frame_budget([], 1024, 2048, 8192) == 1024

    def test_a_bundle_gets_the_bundle_budget(self) -> None:
        cmds = [{"cmd_func": 32, "cmd_id": 50}, {"cmd_func": 254, "cmd_id": 39}]

        assert frame_budget(cmds, 1024, 2048, 8192) == 2048

    def test_a_wide_bundle_claims_one_message_budget_per_message(self) -> None:
        """What a fixed bundle number cannot do: follow the frame's own width."""
        cmds = [{"cmd_func": 254, "cmd_id": 46}] * 14

        assert frame_budget(cmds, 1024, 4096, 65536) == 14 * 1024

    def test_the_cap_bounds_the_claim(self) -> None:
        cmds = [{"cmd_func": 254, "cmd_id": 46}] * 40

        assert frame_budget(cmds, 1024, 4096, 16384) == 16384

    def test_a_narrow_bundle_keeps_the_floor(self) -> None:
        """Two wide messages were covered before this and stay covered.

        Their own width says nothing here - only the count is known at this
        point - so a budget of count alone would have cut a two-message frame
        to half of what it kept before.
        """
        cmds = [{"cmd_func": 96, "cmd_id": 1}, {"cmd_func": 96, "cmd_id": 8}]

        assert frame_budget(cmds, 1024, 4096, 16384) == 4096

    def test_the_ocean2_battery_report_survives_whole(self) -> None:
        """The frame this change exists for.

        The first RE11 capture (#145) holds nine of these across 16 hours, 12
        to 14 messages at 4956 to 5899 B, and the 4096 B bundle budget cut
        every single one. This is the widest of them.
        """
        payload = _frame_of(5899, *(((254, 46),) * 14))
        cmds = decode_cmd_headers(payload)

        entry = build_frame_entry(
            "/app/device/property/x",
            payload,
            [],
            frame_budget(
                cmds,
                RAW_FRAME_MAX_BYTES,
                RAW_FRAME_BUNDLE_MAX_BYTES,
                RAW_FRAME_BUNDLE_HARD_CAP,
            ),
        )

        assert len(cmds) == 14
        assert entry["size"] == 5899
        assert len(bytes.fromhex(entry["hex"])) == 5899
        assert "truncated" not in entry

    def test_the_stream_ac5000_get_reply_survives_whole(self) -> None:
        """The frame this whole split exists for.

        Reporter captures put it at 1434 to 1448 B in six messages, and the
        flat 512 B cap stored a third of it - the last three messages and the
        config block of the full-state message were simply gone, in a download
        that said nothing about it.
        """
        payload = _frame_of(1440, *_ES22_BUNDLE)
        cmds = decode_cmd_headers(payload)

        entry = build_frame_entry(
            "/app/device/property/x",
            payload,
            [],
            frame_budget(
                cmds,
                RAW_FRAME_MAX_BYTES,
                RAW_FRAME_BUNDLE_MAX_BYTES,
                RAW_FRAME_BUNDLE_HARD_CAP,
            ),
        )

        assert len(cmds) == 6
        assert entry["size"] == 1440
        assert len(bytes.fromhex(entry["hex"])) == 1440
        assert "truncated" not in entry

    def test_a_single_message_frame_above_the_budget_is_cut_and_marked(self) -> None:
        """The ceiling still exists; it just says when it applies."""
        payload = _frame_of(1100, (254, 39))
        cmds = decode_cmd_headers(payload)

        entry = build_frame_entry(
            "/app/device/property/x",
            payload,
            [],
            frame_budget(
                cmds,
                RAW_FRAME_MAX_BYTES,
                RAW_FRAME_BUNDLE_MAX_BYTES,
                RAW_FRAME_BUNDLE_HARD_CAP,
            ),
        )

        assert len(cmds) == 1
        assert entry["size"] == 1100
        assert len(bytes.fromhex(entry["hex"])) == RAW_FRAME_MAX_BYTES
        assert entry["truncated"] is True


class TestFrameKey:
    def test_proto_is_keyed_by_its_first_command(self) -> None:
        entry = {
            "format": "proto",
            "topic": "property",
            "cmds": [{"cmd_func": 254, "cmd_id": 21}],
        }

        assert frame_key(entry, b"\x0a\x01", []) == "property:proto/254.21"

    def test_a_bundle_keys_on_the_head_command(self) -> None:
        """An appended optional command must not split one message type."""
        head = {"cmd_func": 32, "cmd_id": 50}
        one = {"format": "proto", "topic": "property", "cmds": [head]}
        two = {
            "format": "proto",
            "topic": "property",
            "cmds": [head, {"cmd_func": 254, "cmd_id": 21}],
        }

        assert frame_key(one, b"\x0a\x01", []) == frame_key(two, b"\x0a\x01", [])

    def test_undecodable_proto_gets_its_own_bucket(self) -> None:
        entry = {"format": "proto", "topic": "property", "cmds": []}

        assert frame_key(entry, b"\x0a\x01", []) == "property:proto/undecoded"

    def test_get_reply_never_shares_a_bucket_with_a_push(self) -> None:
        """The full state dump is the richest frame; it keeps its own budget."""
        cmds = [{"cmd_func": 254, "cmd_id": 21}]
        push = {"format": "proto", "topic": "property", "cmds": cmds}
        reply = {"format": "proto", "topic": "get_reply", "cmds": cmds}

        assert frame_key(push, b"\x0a", []) != frame_key(reply, b"\x0a", [])

    def test_json_is_keyed_by_command_when_present(self) -> None:
        entry = {"format": "json", "topic": "property"}

        key = frame_key(entry, b'{"cmdFunc": 254, "cmdId": 21, "param": {}}', [])

        assert key == "property:json/254.21"

    def test_json_falls_back_to_the_type_code(self) -> None:
        entry = {"format": "json", "topic": "property"}

        key = frame_key(entry, b'{"typeCode": "pdStatus", "params": {}}', [])

        assert key == "property:json/typeCode=pdStatus"

    def test_json_without_a_discriminator_shares_one_bucket(self) -> None:
        entry = {"format": "json", "topic": "property"}

        assert frame_key(entry, b'{"soc": 50}', []) == "property:json"
        assert frame_key(entry, b'{"soc": 60}', []) == "property:json"

    def test_unparsable_json_is_kept_apart(self) -> None:
        entry = {"format": "json", "topic": "property"}

        assert frame_key(entry, b"not json at all", []) == "property:json/unparsed"

    def test_a_long_discriminator_cannot_grow_the_key(self) -> None:
        """The value is device-controlled and ends up in a diagnostics dump."""
        entry = {"format": "json", "topic": "property"}

        key = frame_key(entry, b'{"typeCode": "' + b"A" * 500 + b'"}', [])

        assert len(key) < 80

    def test_a_serial_in_a_discriminator_never_reaches_the_key(self) -> None:
        """Keys are exported, and diagnostics get attached to public issues.

        The discriminator is a string lifted straight out of the payload, so
        it goes through the same masking as the stored frame.
        """
        entry = {"format": "json", "topic": "property"}
        sn = "RE11TEST00000001"

        key = frame_key(entry, b'{"typeCode": "' + sn.encode() + b'"}', [sn])

        assert sn not in key

    def test_an_unnamed_serial_in_a_discriminator_is_masked_too(self) -> None:
        """Pack and accessory serials are never known to the caller."""
        entry = {"format": "json", "topic": "property"}

        key = frame_key(entry, b'{"typeCode": "BP5000TEST000001"}', [])

        assert "BP5000TEST000001" not in key


class TestTypedFrameBuffer:
    """The buffer that a six-hour Stream Micro capture proved was needed."""

    def test_a_rare_type_survives_a_flood_of_a_frequent_one(self) -> None:
        """The reported failure: 24 slots, all of one message type.

        The battery report and the frames carrying state of charge had been
        evicted by a push arriving every two seconds.
        """
        buffer = TypedFrameBuffer(keys_max=4, per_key_max=4)

        for i in range(1000):
            buffer.add("property:proto/254.21", _entry(float(i)))
            if i % 300 == 0:
                buffer.add("property:proto/32.50", _entry(float(i), rare=True))

        kept = buffer.frames()
        rare = [frame["ts"] - _T0 for frame in kept if frame.get("rare")]
        assert rare == [0.0, 300.0, 600.0, 900.0]

    def test_a_long_recording_keeps_its_span(self) -> None:
        """Six hours must not collapse into the last few minutes."""
        buffer = TypedFrameBuffer(keys_max=4, per_key_max=10)

        for i in range(10_800):  # 6 h of one message type every 2 s
            buffer.add("property:proto/254.21", _entry(i * 2.0))

        kept = buffer.frames()
        assert len(kept) <= 10
        assert kept[0]["ts"] == _T0  # the first sighting always survives
        assert kept[-1]["ts"] == _T0 + 21_598.0  # so does the newest
        # The reported capture spanned 199 s out of six recorded hours.
        assert kept[-1]["ts"] - kept[0]["ts"] > 6 * 3600 - 5

    def test_a_long_recording_is_covered_end_to_end(self) -> None:
        """Every part of the window must be represented, not just the ends.

        This is the assertion the first attempt at this fix failed. Dropping
        every second frame keeps the span intact - first frame and newest
        frame both survive - while the middle quietly empties out, because
        the freed slots refill at the arrival rate.
        """
        buffer = TypedFrameBuffer(keys_max=4, per_key_max=10)
        window = 6 * 3600.0

        for i in range(10_800):
            buffer.add("property:proto/254.21", _entry(i * 2.0))

        offsets = [frame["ts"] - _T0 for frame in buffer.frames()]
        sixths = [
            [off for off in offsets if window * n / 6 <= off < window * (n + 1) / 6]
            for n in range(6)
        ]
        assert all(sixths), f"gap in coverage: {[len(part) for part in sixths]}"

    def test_a_recording_that_outruns_the_budget_many_times_over(self) -> None:
        """A 24 h capture is the documented maximum; coverage must hold there."""
        buffer = TypedFrameBuffer(keys_max=4, per_key_max=10)
        window = 24 * 3600.0

        for i in range(86_400):  # 24 h, one frame per second
            buffer.add("property:proto/254.21", _entry(float(i)))

        offsets = [frame["ts"] - _T0 for frame in buffer.frames()]
        quarters = [
            [off for off in offsets if window * n / 4 <= off < window * (n + 1) / 4]
            for n in range(4)
        ]
        assert all(quarters), f"gap in coverage: {[len(part) for part in quarters]}"
        assert len(offsets) <= 10

    def test_a_burst_after_a_quiet_stretch_cannot_overshoot(self) -> None:
        """Thousands of frames in one instant still leave the caps intact."""
        buffer = TypedFrameBuffer(keys_max=4, per_key_max=10)

        buffer.add("k", _entry(0.0))
        for _ in range(5_000):
            buffer.add("k", _entry(3600.0))

        assert len(buffer.frames()) <= 10

    def test_key_budget_is_capped(self) -> None:
        buffer = TypedFrameBuffer(keys_max=3, per_key_max=4)

        for key in range(50):
            buffer.add(f"key{key}", _entry(1.0))

        stats = buffer.stats()
        assert stats["keys_tracked"] == 3
        assert stats["frames_dropped_key_budget"] == 47

    def test_worst_case_stays_inside_both_caps(self) -> None:
        keys_max, per_key_max = 3, 4
        buffer = TypedFrameBuffer(keys_max, per_key_max)

        for key in range(50):
            for i in range(200):
                buffer.add(f"key{key}", _entry(float(i)))

        stats = buffer.stats()
        assert len(buffer.frames()) <= keys_max * per_key_max
        assert stats["keys_tracked"] == keys_max
        assert all(
            entry["kept"] <= per_key_max for entry in stats["per_key"].values()
        )

    def test_frames_are_ordered_by_timestamp_across_buckets(self) -> None:
        buffer = TypedFrameBuffer(keys_max=4, per_key_max=4)

        buffer.add("b", _entry(30.0))
        buffer.add("a", _entry(10.0))
        buffer.add("b", _entry(20.0))

        assert [frame["ts"] - _T0 for frame in buffer.frames()] == [10.0, 20.0, 30.0]

    def test_same_timestamp_keeps_arrival_order(self) -> None:
        """Frames captured within one clock tick must not shuffle."""
        buffer = TypedFrameBuffer(keys_max=4, per_key_max=4)

        buffer.add("a", _entry(5.0, n=1))
        buffer.add("b", _entry(5.0, n=2))
        buffer.add("a", _entry(5.0, n=3))

        assert [frame["n"] for frame in buffer.frames()] == [1, 2, 3]

    def test_stats_tell_a_quiet_device_from_a_thinned_capture(self) -> None:
        buffer = TypedFrameBuffer(keys_max=4, per_key_max=4)

        for i in range(100):
            buffer.add("property:proto/254.21", _entry(float(i)))
        buffer.add("property:proto/32.50", _entry(100.0))

        stats = buffer.stats()
        assert stats["frames_seen"] == 101
        assert stats["frames_kept"] == len(buffer.frames())
        assert stats["frames_kept"] < stats["frames_seen"]
        assert stats["per_key"]["property:proto/254.21"]["seen"] == 100
        assert stats["per_key"]["property:proto/32.50"] == {"seen": 1, "kept": 1}

    def test_span_covers_the_capture_window_not_the_last_bucket(self) -> None:
        """The span is what the reader uses to judge how long it ran."""
        buffer = TypedFrameBuffer(keys_max=4, per_key_max=4)

        for i in range(10_800):
            buffer.add("property:proto/254.21", _entry(i * 2.0))

        assert buffer.stats()["span_s"] == 21_598.0

    def test_a_clock_step_backwards_does_not_hang(self) -> None:
        """A single NTP correction backwards used to hang the Paho thread.

        A frame older than the bucket's first one took a negative slot, and
        _thin_to_slots only compares each frame against its predecessor, so
        the pattern [0, -1, 0] survived every doubling of the slot width and
        the loop in _sample widened forever. These are the coordinator's own
        buffer settings, where the budget leaves room for two samples.
        """
        buffer = TypedFrameBuffer(keys_max=20, per_key_max=3)

        with _must_finish_within(5):
            for offset in (0.0, -1.0, 0.5, 1.0, 1.5):
                buffer.add("property:proto/254.21", _entry(offset))

        assert len(buffer.frames()) <= 3

    def test_an_oscillating_clock_does_not_hang(self) -> None:
        """A guard, not a regression test - and labelled as one deliberately.

        The proven regression detector is the single backwards step above,
        which hangs on the unfixed code. This one does not: no oscillating
        sequence tried against the pre-fix implementation reproduced the hang,
        at either the coordinator's budget or the probe's. It stays because a
        clock moving both ways repeatedly is what a machine without a real
        time clock actually does, and a future change to the slot geometry
        could break it where the single step still passes.

        The offsets deliberately start above their own minimum: the first
        frame sets ``first_ts``, so a sequence beginning at its lowest value
        never produces a frame before the start of the recording at all.
        """
        buffer = TypedFrameBuffer(keys_max=20, per_key_max=3)
        offsets = [0.0] + [(-1.0 if i % 2 else float(i)) for i in range(1, 40)]
        assert min(offsets) < offsets[0], "frames must fall before first_ts"

        with _must_finish_within(5):
            for offset in offsets:
                buffer.add("property:proto/254.21", _entry(offset))

        assert len(buffer.frames()) <= 3

    def test_a_frame_older_than_the_first_shares_the_oldest_slot(self) -> None:
        """The clamp itself, not just the absence of a hang.

        Asserting only ordering would pass without the clamp: the point is
        that a frame before the start of the recording lands in slot 0
        alongside the oldest one instead of taking a slot of its own below
        zero, which is the shape no widening can merge.
        """
        key = "property:proto/254.21"
        buffer = TypedFrameBuffer(keys_max=4, per_key_max=3)
        # Enough frames that the budget was exceeded at least once, so the
        # bucket has a real slot width. Before that every frame gets its own
        # slot by arrival order and the clamp has nothing to act on.
        for offset in (0.0, 10.0, 20.0, 30.0, 40.0, 50.0):
            buffer.add(key, _entry(offset))
        bucket = buffer._buckets[key]
        assert bucket.slot_s > 0, "precondition: slot geometry must be active"

        assert _slot((998, _entry(0.0)), bucket) == 0
        assert _slot((999, _entry(-5.0)), bucket) == 0

    def test_an_empty_capture_reports_no_span(self) -> None:
        assert TypedFrameBuffer(keys_max=4, per_key_max=4).stats() == {
            "frames_seen": 0,
            "frames_kept": 0,
            "span_s": None,
            "keys_tracked": 0,
            "keys_max": 4,
            "per_key_max": 4,
            "frames_dropped_key_budget": 0,
            "per_key": {},
        }


class TestPowerOceanGetAllSurvivesTheBudget:
    """The size that raised the bundle cap, pinned against the real path.

    A PowerOcean answers a full-state request with one bundled frame. The
    measured one (#225) is 2906 B, against a cap that stood at 2048, and the
    eight message types in its tail reached the diagnostics download as names
    with no bytes behind them.

    The frame itself cannot become a fixture - the serial sits in the envelope
    of a live unit. What is reproduced here is its shape and its size, driven
    through the same two functions the ingest path calls.
    """

    _OBSERVED_GET_ALL_BYTES = 2906

    # Offset of the second serial, chosen to sit above the budget this
    # commit replaced. Bytes past 2048 are the ones a PowerOcean download
    # never contained before, so they are the ones whose masking was never
    # witnessed by any test.
    _TAIL_SERIAL_AT = 2500

    def _bundle(self, size: int, sn: str) -> bytes:
        """A multi-command frame of exactly `size` bytes, serial included.

        The serial appears twice: once in the head, where every budget ever
        reached, and once in the tail that only the raised budget exports.
        """
        head = b"\x0a" + b"\x00" * 40 + sn.encode()
        body = bytearray(b"\x2d" + b"\x00" * (size - len(head) - 1))
        at = self._TAIL_SERIAL_AT - len(head)
        body[at : at + len(sn)] = sn.encode()
        return head + bytes(body)

    def test_the_measured_get_all_is_stored_whole(self) -> None:
        sn = "HJ31TEST00000001"
        payload = self._bundle(self._OBSERVED_GET_ALL_BYTES, sn)
        budget = frame_budget(
            [{"cmd_func": 96, "cmd_id": 1}, {"cmd_func": 96, "cmd_id": 8}],
            RAW_FRAME_MAX_BYTES,
            RAW_FRAME_BUNDLE_MAX_BYTES,
            RAW_FRAME_BUNDLE_HARD_CAP,
        )

        entry = build_frame_entry("/t", payload, [sn], budget)

        assert "truncated" not in entry
        assert entry["size"] == self._OBSERVED_GET_ALL_BYTES
        assert len(bytes.fromhex(entry["hex"])) == self._OBSERVED_GET_ALL_BYTES

    def test_the_serial_is_still_masked_in_the_newly_exported_tail(self) -> None:
        """The bytes this commit newly exports are the untested ones.

        Masking runs over the whole payload before truncation, so a serial in
        the head is masked under any budget - an assertion about it would have
        passed before this change and proves nothing about it. The serial that
        matters sits past 2048, in the region a PowerOcean download never
        carried until the budget was raised. This repo has shipped two serial
        leaks into downloads users attach to public issues; both were in a
        region nobody was asserting over.
        """
        sn = "HJ31TEST00000001"
        payload = self._bundle(self._OBSERVED_GET_ALL_BYTES, sn)
        assert payload.count(sn.encode()) == 2
        assert payload.index(sn.encode(), 2048) > 2048

        entry = build_frame_entry("/t", payload, [sn], RAW_FRAME_BUNDLE_MAX_BYTES)

        stored = bytes.fromhex(entry["hex"])
        assert len(stored) == self._OBSERVED_GET_ALL_BYTES
        assert sn.encode() not in stored

    def test_a_single_push_is_not_given_the_bundle_budget(self) -> None:
        """The split still holds: one command, one message budget."""
        budget = frame_budget(
            [{"cmd_func": 96, "cmd_id": 8}],
            RAW_FRAME_MAX_BYTES,
            RAW_FRAME_BUNDLE_MAX_BYTES,
            RAW_FRAME_BUNDLE_HARD_CAP,
        )

        assert budget == RAW_FRAME_MAX_BYTES
