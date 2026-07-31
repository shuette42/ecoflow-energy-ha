"""Tests for the shared raw-frame capture helpers."""

from typing import Any

from ecoflow_energy.ecoflow.frame_capture import (
    TypedFrameBuffer,
    build_frame_entry,
    frame_key,
    is_proto_frame,
    sanitize_frame,
)


# Frame timestamps are wall clock. Tests offset from a fixed epoch instead of
# starting at zero, so they exercise the same value range the capture sees.
_T0 = 1_785_000_000.0


def _entry(offset: float, **extra: Any) -> dict[str, Any]:
    """Build a stored frame `offset` seconds into the recording.

    Timestamps are supplied by the test, never read from a clock, so the
    span assertions below mean the same thing on every machine.
    """
    return {"ts": _T0 + offset, "topic": "property", "format": "proto", **extra}


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
