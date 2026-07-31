"""Tests for the shared raw-frame capture helpers."""

from ecoflow_energy.ecoflow.frame_capture import (
    build_frame_entry,
    is_proto_frame,
    sanitize_frame,
)


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
