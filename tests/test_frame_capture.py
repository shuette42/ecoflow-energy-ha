"""Tests for the shared raw-frame capture helpers."""

import signal

import pytest
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
    WRITE_CLASS_RESERVE,
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


class TestDelimitedIdentifierMasking:
    """The third pass: identifiers too short for the shape pass to risk.

    A run of 12 upper-case alphanumerics turns up in ordinary binary, so a
    free-running pattern that short would corrupt frames instead of cleaning
    them. Protobuf supplies the boundary: a length-delimited field says how
    many bytes it holds, so the whole value can be tested rather than a run
    inside it.

    The vectors below are the real thing. A STREAM AC 5000 carries a
    12-character identifier this way in every frame it sends, and it reached
    a public issue attachment because the serial pass never matched it.
    """

    # The exact bytes the device sends, identifier replaced: tag for field 2
    # with wire type 2, length 12, then the value.
    REAL_SHAPE = b"\x08\x03\x12\x0cAABBCCDDEEFF\x1d\xfa\x7e\xe2\x41"

    def test_the_identifier_the_serial_pass_missed_is_masked(self) -> None:
        result = sanitize_frame(self.REAL_SHAPE, [])

        assert b"AABBCCDDEEFF" not in result
        assert result == b"\x08\x03\x12\x0cXXXXXXXXXXXX\x1d\xfa\x7e\xe2\x41"

    def test_the_serial_pass_alone_would_have_missed_it(self) -> None:
        """The control for the line above: without this pass, nothing fires.

        Twelve characters sit below the 15 the shape pattern requires, so a
        test asserting the identifier is gone proves nothing unless the old
        behaviour is pinned as well.
        """
        from ecoflow_energy.ecoflow.frame_capture import _SERIAL_RUN

        assert _SERIAL_RUN.search(b"AABBCCDDEEFF") is None

    def test_the_bytes_around_it_are_untouched(self) -> None:
        result = sanitize_frame(self.REAL_SHAPE, [])

        assert result.startswith(b"\x08\x03\x12\x0c")
        assert result.endswith(b"\x1d\xfa\x7e\xe2\x41")
        assert len(result) == len(self.REAL_SHAPE)

    def test_a_field_below_the_floor_survives(self) -> None:
        """Under 12 characters a field stops being an identifier.

        Model names and status words live there, and they are what a capture
        is read for.
        """
        payload = b"\x12\x0bSHORTID1234"

        assert sanitize_frame(payload, []) == payload

    def test_a_value_that_is_not_all_identifier_characters_survives(self) -> None:
        """One lower-case letter is enough to say this is not an identifier."""
        payload = b"\x12\x0cMODEL name12"

        assert sanitize_frame(payload, []) == payload

    def test_a_length_running_past_the_frame_is_ignored(self) -> None:
        """A truncated frame must not be read past its end.

        Frames are cut to a byte budget before they are stored, so a field
        announcing more than remains is the normal case at the cut, not a
        malformed device.
        """
        payload = b"\x12\x20AABBCCDDEEFF"

        assert sanitize_frame(payload, []) == payload

    def test_a_varint_field_is_not_read_as_a_length(self) -> None:
        """Only wire type 2 announces a length. The others must be skipped."""
        payload = b"\x08\x0cAABBCCDDEEFF"

        assert sanitize_frame(payload, []) == payload

    def test_two_identifiers_in_one_frame_both_go(self) -> None:
        payload = b"\x12\x0cAABBCCDDEEFF\x1a\x0c112233445566"

        result = sanitize_frame(payload, [])

        assert b"AABBCCDDEEFF" not in result
        assert b"112233445566" not in result
        assert len(result) == len(payload)

    def test_a_serial_sized_value_is_still_masked_once(self) -> None:
        """The passes overlap on a 16-character serial. That is fine as long
        as the result is a run of filler and not a run of filler masked
        again into something shorter.
        """
        payload = b"\x12\x10HJ31TEST00000001"

        result = sanitize_frame(payload, [])

        assert result == b"\x12\x10" + b"X" * 16


class TestTimeZoneMasking:
    """The city half of a time zone goes, the region stays.

    A Delta 3 reports the owner's time zone as an IANA name, and it reached a
    diagnostics file attached to a public issue while the serial beside it was
    masked. It is not an identifier, so neither of the passes before this one
    could see it: lower case and a slash sit outside the identifier alphabet
    on purpose.

    Dropping it whole would cost something real, though. The region says which
    side of the world a device is on, and that is what a schedule behaving
    oddly is read against. So the region is kept.
    """

    def test_the_city_is_masked_and_the_region_survives(self) -> None:
        payload = b"\xb2\x08\x0fEurope/Budapest\xb8\x08"

        result = sanitize_frame(payload, [])

        assert result == b"\xb2\x08\x0fEurope/XXXXXXXX\xb8\x08"

    def test_neither_earlier_pass_would_have_caught_it(self) -> None:
        """Control for the test above, so it cannot pass for a wrong reason."""
        from ecoflow_energy.ecoflow.frame_capture import (
            _SERIAL_RUN,
            _mask_delimited_identifiers,
        )

        zone = b"Europe/Budapest"

        assert _SERIAL_RUN.search(zone) is None
        assert _mask_delimited_identifiers(b"\x12\x0f" + zone) == b"\x12\x0f" + zone

    @pytest.mark.parametrize(
        ("zone", "expected"),
        [
            (b"America/Denver", b"America/XXXXXX"),
            (b"Asia/Kolkata", b"Asia/XXXXXXX"),
            (b"Australia/Sydney", b"Australia/XXXXXX"),
            # Three-part zones exist and the whole tail goes, not just the
            # first component: the country is the part being removed.
            (b"America/Argentina/Salta", b"America/XXXXXXXXXXXXXXX"),
        ],
    )
    def test_other_regions_are_handled_the_same(
        self, zone: bytes, expected: bytes
    ) -> None:
        assert sanitize_frame(zone, []) == expected

    @pytest.mark.parametrize(
        "payload", [b"MODEL/NAME", b"AC/DC", b"\x12\x03H/P", b"V1.0.1"]
    )
    def test_a_slash_is_not_enough_to_look_like_a_place(
        self, payload: bytes
    ) -> None:
        """The region is matched against the real area names, not by shape.

        A pattern that took any capitalised word before a slash would start
        masking model names, which is what a capture is read for.
        """
        assert sanitize_frame(payload, []) == payload

    def test_masking_preserves_length(self) -> None:
        payload = b"\xb2\x08\x0fEurope/Budapest\xb8\x08"

        assert len(sanitize_frame(payload, [])) == len(payload)


class TestMaskingDoesNotCorruptRealFrames:
    """The guard the history of this mask asks for.

    `diagnostics.py` records an over-broad mask that came back with 25 of 25
    captured frames destroyed. A rule written against a synthetic payload
    cannot see that, because a synthetic payload has no readings to lose. So
    the check is run over every recorded frame this repo holds: mask it, parse
    both versions, and require every number to survive.
    """

    @staticmethod
    def _captures() -> list[tuple[str, bytes]]:
        import json
        from pathlib import Path

        found: list[tuple[str, bytes]] = []
        root = Path(__file__).parent / "fixtures"
        for path in sorted(root.rglob("*.json")):
            try:
                data = json.loads(path.read_text())
            except ValueError:  # pragma: no cover - not a frame fixture
                continue
            if not isinstance(data, dict):
                continue
            for frame in data.get("frames") or []:
                raw = frame.get("hex")
                if raw:
                    found.append((path.name, bytes.fromhex(raw)))
        return found

    def test_there_are_frames_to_check(self) -> None:
        """Positive control. A rglob that matched nothing would let the test
        below pass while checking no frame at all.
        """
        assert len(self._captures()) >= 50

    def test_every_recorded_frame_keeps_its_length(self) -> None:
        for name, raw in self._captures():
            assert len(sanitize_frame(raw, [])) == len(raw), name

    def test_no_reading_changes_under_masking(self) -> None:
        from ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import (
            parse_stream_ac5000_message,
        )

        checked = 0
        for name, raw in self._captures():
            before = parse_stream_ac5000_message(raw) or {}
            if not before:
                continue
            after = parse_stream_ac5000_message(sanitize_frame(raw, [])) or {}
            numeric_before = {
                key: value
                for key, value in before.items()
                if isinstance(value, (int, float))
            }
            numeric_after = {
                key: value
                for key, value in after.items()
                if isinstance(value, (int, float))
            }
            assert numeric_before == numeric_after, name
            checked += 1

        # Second positive control: the loop above is only evidence while it
        # has frames this parser understands.
        assert checked >= 20


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

    def test_dropped_types_are_named_not_only_counted(self) -> None:
        """A saturated capture must say which types it turned away.

        #247: a reporter's capture hit the ceiling and discarded 48 frames.
        The question that mattered was whether one particular message was
        among them, and the count alone could not answer it either way.
        """
        buffer = TypedFrameBuffer(keys_max=2, per_key_max=4)

        buffer.add("kept-a", _entry(1.0))
        buffer.add("kept-b", _entry(1.0))
        buffer.add("turned-away", _entry(1.0))
        buffer.add("turned-away", _entry(2.0))
        buffer.add("also-turned-away", _entry(3.0))

        stats = buffer.stats()
        assert stats["frames_dropped_key_budget"] == 3
        assert stats["dropped_per_key"] == {
            "turned-away": 2,
            "also-turned-away": 1,
        }
        assert stats["dropped_keys_untracked"] == 0

    def test_the_dropped_name_list_is_bounded_too(self) -> None:
        """The names come off the wire, so the list cannot grow freely.

        Past the cap the count of further distinct names is kept, which is
        what tells a reader the list is partial rather than complete.
        """
        buffer = TypedFrameBuffer(keys_max=2, per_key_max=4)

        for key in range(50):
            buffer.add(f"key{key}", _entry(1.0))

        stats = buffer.stats()
        assert len(stats["dropped_per_key"]) == 2
        assert stats["dropped_keys_untracked"] == 46
        assert stats["frames_dropped_key_budget"] == 48

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
        assert stats["per_key"]["property:proto/32.50"] == {
            "seen": 1,
            "kept": 1,
            "novel": 0,
        }

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
            "write_slots_reserved": 8,
            "per_key_max": 4,
            "frames_dropped_key_budget": 0,
            "dropped_per_key": {},
            "dropped_keys_untracked": 0,
            "write_keys_evicted": {},
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


class TestNovelReadingsSurviveThinning:
    """The frame a capture was opened for must not be thinned away.

    Shaped after the recording on #284, and deliberately not claiming what
    that recording shows. An owner changed a power limit in the vendor app
    while the raw capture ran. In his file the configuration block appears
    only in the device's answers to a get - five of them, all kept, all
    before the change - and never once on the incremental push bucket, whose
    118 frames were thinned to 8. So the file does not show a frame carrying
    the new value being dropped. What it shows is that the value never
    reached the recording at all, and either route to that is what these
    tests cover: a first appearance on a bucket, and a change to a value the
    bucket had been carrying unchanged.
    """

    # From the capture: frames and span of the incremental push bucket.
    _FRAMES = 118
    _SPAN_S = 213.0
    _CHANGE_AT = 127.0

    # Timestamps are wall clock, so a stored offset does not come back out
    # of `ts - _T0` bit for bit. Frames are seconds apart and the tolerance
    # is microseconds, so it cannot make a neighbouring frame match.
    _TS_EPSILON = 1e-6

    def _kept_at(self, buffer: TypedFrameBuffer, offset: float) -> bool:
        """Return whether the frame recorded at `offset` survived."""
        step = self._SPAN_S / self._FRAMES
        return any(
            offset - self._TS_EPSILON <= (entry["ts"] - _T0) < offset + step
            for entry in buffer.frames()
        )

    def _telemetry(self, i: int) -> dict[str, float]:
        """Readings that move on every frame, as live power does."""
        return {"grid_w": 100.0 + i, "soc_pct": 50.0 + (i % 7)}

    def _run(self, buffer: TypedFrameBuffer, *, config_from: int = 0) -> None:
        """Replay the capture with a limit that changes partway through.

        `config_from` is the frame at which the configuration block starts
        riding the push, so the same replay covers both the case where it
        was there all along and the case where it appears mid-recording.
        """
        step = self._SPAN_S / self._FRAMES
        for i in range(self._FRAMES):
            at = i * step
            readings = self._telemetry(i)
            if i >= config_from:
                readings["max_grid_input_power_w"] = (
                    2500 if at >= self._CHANGE_AT else 1200
                )
            buffer.add("property:proto/254.39", _entry(at), readings=readings)

    def test_a_changed_setting_is_kept(self) -> None:
        """The #284 question: the limit moves, on a key already being carried.

        This is the case a first-appearance rule does not catch, and the one
        that matters - the first appearance of that key carries the *old*
        value.
        """
        buffer = TypedFrameBuffer(keys_max=20, per_key_max=10)

        self._run(buffer)

        step = self._SPAN_S / self._FRAMES
        # The first frame at or after the change, which is the one that
        # carried the new value.
        changed_at = -(-self._CHANGE_AT // step) * step
        assert self._kept_at(buffer, changed_at)

    def test_a_setting_appearing_mid_recording_is_kept(self) -> None:
        """The other route to the same miss: the block starts arriving late."""
        buffer = TypedFrameBuffer(keys_max=20, per_key_max=10)
        appears_at = 60

        self._run(buffer, config_from=appears_at)

        assert self._kept_at(buffer, appears_at * self._SPAN_S / self._FRAMES)

    def test_live_telemetry_claims_no_slots(self) -> None:
        """Power moves on every frame and must never count as new.

        Otherwise every frame is novel, the cap keeps only the last four,
        and the mechanism becomes a second ring buffer.
        """
        buffer = TypedFrameBuffer(keys_max=20, per_key_max=10)

        for i in range(self._FRAMES):
            buffer.add("property:proto/254.39", _entry(float(i)), readings=self._telemetry(i))

        # One: the first frame, where every key appears for the first time.
        assert buffer.stats()["per_key"]["property:proto/254.39"]["novel"] == 1

    def test_the_recording_keeps_its_shape(self) -> None:
        """A novelty slot must not buy its frame out of the thinned history.

        The sampling exists so a long capture still has a middle. Novel
        frames are kept on top of the per-type budget rather than out of it,
        and there are at most a handful of them.
        """
        buffer = TypedFrameBuffer(keys_max=20, per_key_max=10)

        self._run(buffer)

        stats = buffer.stats()["per_key"]["property:proto/254.39"]
        assert stats["seen"] == self._FRAMES
        assert stats["kept"] <= 10 + 4
        kept = sorted(entry["ts"] - _T0 for entry in buffer.frames())
        assert kept[0] == 0.0
        assert kept[-1] > self._SPAN_S * 0.9

    def test_a_repeated_reading_is_not_novel(self) -> None:
        """Novelty is a change, not every appearance."""
        buffer = TypedFrameBuffer(keys_max=20, per_key_max=10)

        for i in range(50):
            buffer.add(
                "property:proto/254.39", _entry(float(i)), readings={"soc_pct": 50.0}
            )

        assert buffer.stats()["per_key"]["property:proto/254.39"]["novel"] == 1

    def test_a_change_before_the_value_settled_is_not_novel(self) -> None:
        """A reading has to hold still first, or telemetry qualifies too.

        Four frames of one value then a change is inside the run length and
        must not claim a slot; the boundary is worth pinning because it is
        what separates a setting from a fast-moving reading.
        """
        buffer = TypedFrameBuffer(keys_max=20, per_key_max=10)

        for i in range(4):
            buffer.add(
                "property:proto/254.39", _entry(float(i)), readings={"x": 1.0}
            )
        buffer.add("property:proto/254.39", _entry(4.0), readings={"x": 2.0})

        assert buffer.stats()["per_key"]["property:proto/254.39"]["novel"] == 1

    def test_novelty_is_judged_per_message_type(self) -> None:
        """A field in a full dump says nothing about whether it is pushed.

        That a get answered with a configuration block is not evidence the
        device ever reports it on its own, and the second is the question a
        control needs answered. So the buckets do not share what they have
        seen.
        """
        buffer = TypedFrameBuffer(keys_max=20, per_key_max=10)

        buffer.add(
            "get_reply:proto/254.39", _entry(0.0), readings={"max_grid_input_power_w": 1200}
        )
        buffer.add(
            "property:proto/254.39", _entry(1.0), readings={"max_grid_input_power_w": 1200}
        )

        per_key = buffer.stats()["per_key"]
        assert per_key["get_reply:proto/254.39"]["novel"] == 1
        assert per_key["property:proto/254.39"]["novel"] == 1

    def test_a_frame_with_no_readings_is_sampled_as_before(self) -> None:
        """A write or a reply carries no parsed keys and claims no slot."""
        buffer = TypedFrameBuffer(keys_max=20, per_key_max=10)

        for i in range(50):
            buffer.add("set:proto/254.38", _entry(float(i)))

        assert buffer.stats()["per_key"]["set:proto/254.38"]["novel"] == 0

    def test_an_unhashable_reading_does_not_break_the_capture(self) -> None:
        """Parsers emit lists too, and capture must never affect ingest."""
        buffer = TypedFrameBuffer(keys_max=20, per_key_max=10)

        for i in range(10):
            buffer.add(
                "property:proto/254.39", _entry(float(i)), readings={"packs": [1, 2]}
            )

        assert buffer.stats()["per_key"]["property:proto/254.39"]["seen"] == 10


class TestWriteSlotReserve:
    """Writes keep a place of their own, whatever telemetry arrived first.

    Rebuilt from the 2026-08-27 PowerOcean capture (#247): 20 of 20 type
    slots taken by telemetry, and every user write of the session evicted
    with nothing kept but a name and a count.
    """

    def test_a_write_gets_in_after_telemetry_filled_the_budget(self) -> None:
        buffer = TypedFrameBuffer(keys_max=3, per_key_max=4, write_reserve=2)

        for i in range(3):
            buffer.add(f"property:proto/96.{i}", _entry(float(i)))
        # The budget is full before the user touches anything.
        assert buffer.stats()["keys_tracked"] == 3

        buffer.add("set:proto/241.102", _entry(10.0, write=True))
        buffer.add("set_reply:proto/241.102", _entry(11.0, write=True))

        stats = buffer.stats()
        assert "set:proto/241.102" in stats["per_key"]
        assert "set_reply:proto/241.102" in stats["per_key"]
        assert stats["dropped_per_key"] == {}
        assert [f["ts"] - _T0 for f in buffer.frames() if f.get("write")] == [10.0, 11.0]

    def test_the_reserve_does_not_come_out_of_the_telemetry_budget(self) -> None:
        """The guarantee this buffer already had must survive the new one.

        Taking the reserve out of `keys_max` would have starved telemetry -
        the first shape of this fix did exactly that and broke eleven tests.
        """
        buffer = TypedFrameBuffer(keys_max=4, per_key_max=4, write_reserve=8)

        for i in range(4):
            buffer.add(f"property:proto/96.{i}", _entry(float(i)))

        assert buffer.stats()["keys_tracked"] == 4
        assert buffer.stats()["dropped_per_key"] == {}

    def test_the_write_reserve_is_itself_bounded(self) -> None:
        buffer = TypedFrameBuffer(keys_max=2, per_key_max=4, write_reserve=1)

        buffer.add("set:proto/241.100", _entry(1.0, write=True))
        buffer.add("set:proto/241.102", _entry(2.0, write=True))

        stats = buffer.stats()
        assert "set:proto/241.100" in stats["per_key"]
        assert stats["dropped_per_key"] == {"set:proto/241.102": 1}

    def test_a_periodic_write_yields_its_slot_to_a_rare_one(self) -> None:
        """Rebuilt from the 2026-08-28 beta.24 capture (#247).

        The reserve was first-come among writes, and the app's own periodic
        write (96/97, 1157 frames in six hours) had taken a slot before the
        reporter touched anything. His mode changes (241/102, two frames)
        and every reply were dropped - the second capture in a row that lost
        exactly what it was opened for.
        """
        buffer = TypedFrameBuffer(keys_max=2, per_key_max=4, write_reserve=4)

        for i in range(200):
            buffer.add("set:proto/96.97", _entry(float(i), write=True))
        # A second periodic write, less busy: the busiest must be the one
        # to go, not the newest eligible one.
        for i in range(30):
            buffer.add("set:proto/96.98", _entry(250.0 + i, write=True))
        buffer.add("set:proto/96.22", _entry(300.0, write=True))
        buffer.add("set_reply:proto/96.22", _entry(301.0, write=True))
        assert buffer.stats()["keys_tracked"] == 4

        buffer.add("set:proto/241.102", _entry(400.0, write=True))
        buffer.add("set:proto/241.102", _entry(401.0, write=True))

        stats = buffer.stats()
        assert "set:proto/241.102" in stats["per_key"]
        assert stats["per_key"]["set:proto/241.102"]["seen"] == 2
        # The busiest periodic one is gone from the buckets but not from the
        # record; the less busy periodic one and the genuine writes stay.
        assert "set:proto/96.97" not in stats["per_key"]
        assert "set:proto/96.98" in stats["per_key"]
        assert "set:proto/96.22" in stats["per_key"]
        assert "set_reply:proto/96.22" in stats["per_key"]
        assert stats["write_keys_evicted"] == {"set:proto/96.97": 200}
        assert stats["dropped_per_key"] == {"set:proto/96.97": 200}
        # Nothing is lost from the count: 200 + 30 + 2 + 2 added.
        assert stats["frames_seen"] == 234
        assert stats["frames_dropped_key_budget"] == 200

    def test_an_evicted_periodic_write_does_not_buy_its_slot_back(self) -> None:
        """The write evicted for repeating is the one certain to come back.

        Eighteen seconds after 96/97 lost its slot its next frame arrived as
        a "new" write type and, in the first shape of this rule, evicted
        the next busiest write to retake the slot - spending two evictions
        per rare newcomer. It stays out and is counted from then on.
        """
        buffer = TypedFrameBuffer(keys_max=2, per_key_max=4, write_reserve=2)

        for i in range(200):
            buffer.add("set:proto/96.97", _entry(float(i), write=True))
        for i in range(30):
            buffer.add("set:proto/96.98", _entry(250.0 + i, write=True))
        buffer.add("set:proto/241.102", _entry(400.0, write=True))
        assert buffer.stats()["write_keys_evicted"] == {"set:proto/96.97": 200}

        for i in range(30):
            buffer.add("set:proto/96.97", _entry(500.0 + i, write=True))

        stats = buffer.stats()
        assert "set:proto/96.97" not in stats["per_key"]
        assert set(stats["per_key"]) >= {"set:proto/96.98", "set:proto/241.102"}
        assert stats["write_keys_evicted"] == {"set:proto/96.97": 200}
        assert stats["dropped_per_key"] == {"set:proto/96.97": 230}
        assert stats["frames_seen"] == 261

    def test_a_genuine_write_is_never_evicted_for_another(self) -> None:
        """Only a write that repeats like telemetry gives way.

        Four presses on a button are a settings session, not a heartbeat;
        with every slot held by such writes the reserve stays bounded the
        way it was, and the newcomer is counted rather than admitted.
        """
        buffer = TypedFrameBuffer(keys_max=2, per_key_max=4, write_reserve=2)

        for i in range(4):
            buffer.add("set:proto/241.100", _entry(float(i), write=True))
        for i in range(4):
            buffer.add("set_reply:proto/241.100", _entry(10.0 + i, write=True))

        buffer.add("set:proto/241.102", _entry(20.0, write=True))

        stats = buffer.stats()
        assert set(stats["per_key"]) == {"set:proto/241.100", "set_reply:proto/241.100"}
        assert stats["dropped_per_key"] == {"set:proto/241.102": 1}
        assert stats["write_keys_evicted"] == {}

    def test_the_download_names_the_reserve(self) -> None:
        """A reader must be able to date a capture by its own stats block."""
        assert TypedFrameBuffer(keys_max=4, per_key_max=4).stats()[
            "write_slots_reserved"
        ] == WRITE_CLASS_RESERVE
