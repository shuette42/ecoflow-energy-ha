"""Tests for protobuf decoder and runtime decoder."""

import importlib.abc
import importlib.machinery
import logging
import sys

from ecoflow_energy.ecoflow.energy_stream import (
    build_energy_stream_activate_payload,
    build_energy_stream_deactivate_payload,
)
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)
from ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
    JTS1EmsChangeReport,
    JTS1EmsParamChangeReport,
    JTS1EmsHeartbeat,
    JTS1EnergyStreamReport,
)
from ecoflow_energy.ecoflow.proto.runtime import (
    _UNKNOWN_FIELDS_MAX,
    _build_cmd_registry,
    decode_proto_runtime_frame,
)


def _build_frame(cmd_func: int, cmd_id: int, inner: bytes) -> bytes:
    """Build a minimal HeaderMessage frame for testing."""
    header = bytearray()
    header.extend(encode_field_bytes(1, inner))       # pdata
    header.extend(encode_field_varint(8, cmd_func))   # cmd_func
    header.extend(encode_field_varint(9, cmd_id))     # cmd_id
    return encode_field_bytes(1, bytes(header))


class TestProtobufDecoder:
    """Tests for the low-level protobuf header decoder."""

    def test_decode_empty(self):
        headers, payload = decode_header_message(b"")
        assert headers == []
        assert payload is None

    def test_decode_simple_header(self):
        """Build a frame with one header containing cmd_func=96, cmd_id=33."""
        inner = encode_field_varint(1, 1)  # dummy pdata
        frame = _build_frame(96, 33, inner)
        headers, payload = decode_header_message(frame)
        assert len(headers) == 1
        assert headers[0]["cmd_func"] == 96
        assert headers[0]["cmd_id"] == 33


class TestRuntimeDecoder:
    """Tests for the typed runtime protobuf decoder."""

    def test_energy_stream_report(self):
        """Decode a JTS1EnergyStreamReport (cmd_id=33)."""
        msg = JTS1EnergyStreamReport()
        msg.mppt_pwr = 3500.0
        msg.sys_load_pwr = 1200.0
        msg.bp_pwr = -800.0
        msg.sys_grid_pwr = 500.0
        msg.bp_soc = 75
        inner = msg.SerializeToString()

        frame = _build_frame(96, 33, inner)
        result = decode_proto_runtime_frame(frame)

        assert result.parse_path == "typed_runtime:energy_stream_report"
        assert result.mapped["solar"] == 3500.0
        assert result.mapped["home_direct"] == 1200.0
        assert result.mapped["batt_pb"] == -800.0
        assert result.mapped["grid_raw_f2"] == 500.0
        assert result.mapped["soc"] == 75.0
        assert result.mapped["_is_energy_stream"] is True
        assert result.mapped["_is_full_power_frame"] is True

    def test_energy_stream_zero_fill(self):
        """Proto3 omits 0.0 - runtime decoder must zero-fill power fields."""
        msg = JTS1EnergyStreamReport()
        msg.bp_soc = 50
        # All power fields are 0.0 (proto3 omits them)
        inner = msg.SerializeToString()
        frame = _build_frame(96, 33, inner)

        result = decode_proto_runtime_frame(frame)
        assert result.mapped["solar"] == 0.0
        assert result.mapped["home_direct"] == 0.0
        assert result.mapped["batt_pb"] == 0.0
        assert result.mapped["grid_raw_f2"] == 0.0

    def test_unknown_field_numbers_reported(self):
        """A field the binding does not declare is reported by its number.

        This is the only signal that a device sends something we have no
        mapping for - MessageToDict returns declared fields only, so without
        it an undeclared field is indistinguishable from one the device never
        sent.
        """
        msg = JTS1EnergyStreamReport()
        msg.bp_soc = 75
        inner = msg.SerializeToString()
        # 5064 is the DisplayPropertyUpload field number for the AC charge
        # power cap - the concrete field this reporting was built for.
        inner += encode_field_varint(5064, 1000)

        result = decode_proto_runtime_frame(_build_frame(96, 33, inner))

        assert result.mapped["_unknown_fields"] == {5064: 1000}
        assert result.mapped["_cmd_key"] == "96/33"
        # Declared fields keep working alongside it.
        assert result.mapped["soc"] == 75.0

    def test_unknown_length_delimited_field_reports_length_only(self):
        """A length-delimited field is reported by size, never by content.

        Its bytes may be a nested message, but they may equally be a serial
        number, and this summary is built to be pasted into a public issue.
        """
        msg = JTS1EnergyStreamReport()
        msg.bp_soc = 50
        inner = msg.SerializeToString() + encode_field_bytes(4242, b"HJ32TESTSERIAL01")

        result = decode_proto_runtime_frame(_build_frame(96, 33, inner))

        assert result.mapped["_unknown_fields"] == {4242: "16 bytes"}
        assert "HJ32TESTSERIAL01" not in str(result.mapped)

    def test_unknown_fields_capped(self):
        """The summary is bounded - a device cannot grow it without limit."""
        msg = JTS1EnergyStreamReport()
        msg.bp_soc = 50
        inner = msg.SerializeToString()
        for number in range(3000, 3100):
            inner += encode_field_varint(number, 1)

        result = decode_proto_runtime_frame(_build_frame(96, 33, inner))

        assert len(result.mapped["_unknown_fields"]) == _UNKNOWN_FIELDS_MAX

    def test_unknown_fields_skipped_without_declared_fields(self):
        """A payload that decodes to unknown fields only is not reported.

        Protobuf accepts arbitrary bytes as unknown fields instead of raising,
        so a frame decoded with the wrong XOR key produces a message that is
        entirely unknown fields. Those numbers are noise from a candidate the
        caller discards.
        """
        inner = encode_field_varint(5064, 1000)

        result = decode_proto_runtime_frame(_build_frame(96, 33, inner))

        assert "_unknown_fields" not in result.mapped

    def test_ems_change_report_rename(self):
        """cmd_id=8 renames ems_word_mode → ems_work_mode."""
        msg = JTS1EmsChangeReport()
        msg.ems_word_mode = 3
        msg.bp_soc = 80
        inner = msg.SerializeToString()
        frame = _build_frame(96, 8, inner)

        result = decode_proto_runtime_frame(frame)
        assert result.parse_path == "typed_runtime:ems_change"
        assert result.mapped.get("ems_work_mode") == 3
        assert "ems_word_mode" not in result.mapped

    def test_unknown_cmd_id(self):
        """Unknown cmd_id should return no_match."""
        inner = b"\x08\x01"  # random varint
        frame = _build_frame(96, 999, inner)
        result = decode_proto_runtime_frame(frame)
        assert result.parse_path == "typed_runtime:no_match"
        assert result.mapped["_is_energy_stream"] is False

    def test_ems_param_change_report_dev_soc_renamed(self):
        """cmd_id=13 (EmsParamChangeReport) maps dev_soc -> ems_app_surplus_pct.

        This is the device-side mirror of the surplus value the EcoFlow app
        writes via cmd_id=112 wire field 4. The coordinator uses this field
        to detect cloud-only app changes that bypass the EMS-side
        sys_bat_backup_ratio.
        """
        from custom_components.ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
            JTS1EmsParamChangeReport,
        )
        msg = JTS1EmsParamChangeReport()
        msg.dev_soc = 47
        inner = msg.SerializeToString()
        frame = _build_frame(96, 13, inner)

        result = decode_proto_runtime_frame(frame)
        assert result.parse_path == "typed_runtime:ems_param_change"
        assert result.mapped.get("ems_app_surplus_pct") == 47
        assert "dev_soc" not in result.mapped


class TestEnergyStreamPayload:
    """Tests for the EnergyStreamSwitch payload builder."""

    def test_activate_payload_size(self):
        payload = build_energy_stream_activate_payload(seq=12345)
        # Payload is a Send_Header_Msg wrapping a Header
        assert len(payload) > 20
        # Must be valid protobuf (starts with field 1, wire type 2)
        assert payload[0] == 0x0A  # field 1, wire type 2

    def test_deactivate_payload_size(self):
        payload = build_energy_stream_deactivate_payload(seq=12345)
        assert len(payload) > 20
        assert payload[0] == 0x0A

    def test_activate_vs_deactivate_differ(self):
        a = build_energy_stream_activate_payload(seq=1)
        d = build_energy_stream_deactivate_payload(seq=1)
        assert a != d


class TestProtobufImportFailure:
    """Tests for protobuf import failure handling."""

    def test_build_cmd_registry_logs_warning_on_import_failure(self, caplog):
        """When protobuf pb2 module cannot be imported, a warning must be logged."""
        pb2_key = "ecoflow_energy.ecoflow.proto.ecocharge_pb2"
        proto_pkg_key = "ecoflow_energy.ecoflow.proto"

        # Save and remove the cached pb2 module from sys.modules
        saved_module = sys.modules.pop(pb2_key, None)

        # Remove the attribute from the parent package so Python cannot
        # short-circuit the import via the package namespace.
        proto_pkg = sys.modules.get(proto_pkg_key)
        had_attr = hasattr(proto_pkg, "ecocharge_pb2")
        if had_attr:
            saved_attr = getattr(proto_pkg, "ecocharge_pb2")
            delattr(proto_pkg, "ecocharge_pb2")

        # Install a blocking meta path finder (modern find_spec API) that
        # raises ImportError before file-system finders locate the .py on disk.

        class _BlockPb2Finder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                if fullname == pb2_key:
                    raise ImportError("mocked: protobuf module not installed")
                return None

        blocker = _BlockPb2Finder()
        sys.meta_path.insert(0, blocker)

        try:
            with caplog.at_level(
                logging.WARNING,
                logger="ecoflow_energy.ecoflow.proto.runtime",
            ):
                result = _build_cmd_registry()

            assert result == {}
            assert "Failed to import protobuf module" in caplog.text
            assert "Enhanced Mode will not work" in caplog.text
        finally:
            sys.meta_path.remove(blocker)
            if saved_module is not None:
                sys.modules[pb2_key] = saved_module
            if had_attr:
                setattr(proto_pkg, "ecocharge_pb2", saved_attr)


class TestDecoderMalformedInput:
    """Truncated or oversized frames must never raise."""

    def test_lone_continuation_byte(self):
        headers, payload = decode_header_message(b"\x80")
        assert headers == []
        assert payload is None

    def test_truncated_varint_value(self):
        # Header tag then a length varint that never terminates
        headers, payload = decode_header_message(b"\x0a\x80")
        assert headers == []
        assert payload is None

    def test_length_exceeds_remaining(self):
        # Field 1, declared length 5, only 1 byte of content: slice clamps
        decode_header_message(b"\x0a\x05\x08")

    def test_truncated_fixed32_in_header(self):
        # Header containing field with wt=5 but only 1 remaining byte
        decode_header_message(b"\x0a\x02\x2d\x00")

    def test_truncated_fixed64_in_header(self):
        decode_header_message(b"\x0a\x02\x31\x00")

    def test_oversized_varint(self):
        # >64-bit varint stops decoding instead of looping/raising
        headers, payload = decode_header_message(b"\x08" + b"\xff" * 11)
        assert headers == []
        assert payload is None

    def test_fuzz_prefixes_of_valid_frame(self):
        inner = encode_field_varint(1, 1)
        frame = _build_frame(96, 33, inner)
        for cut in range(len(frame)):
            decode_header_message(frame[:cut])


class TestPowerOceanCorpusFields:
    """Fields a PowerOcean sends that the binding used to leave undeclared.

    Both payloads below are the inner bytes of real frames, taken from
    listen-only recordings. They carry no serial - that lives in the envelope,
    which is not part of what is asserted here. Constructing the payloads with
    the same binding under test would prove only that protobuf round-trips.
    """

    # cmd_func 96 / cmd_id 8 from a single-phase inverter enrolled in a grid
    # operator's control scheme. The entire message, six bytes: field 612
    # varint 1, field 613 varint 1. Before these were declared it parsed to a
    # valid, empty message - and this is the message that unit sends most.
    _IEEE_PAYLOAD = bytes.fromhex("a02601a82601")

    # cmd_func 96 / cmd_id 33 from a two-string PowerOcean at midday.
    _ENERGY_STREAM_PAYLOAD = bytes.fromhex(
        "0d00c0a844150020dac51d0028024628643500c05f453d00209445"
    )

    def test_grid_control_status_decodes(self) -> None:
        report = JTS1EmsChangeReport()
        report.ParseFromString(self._IEEE_PAYLOAD)

        assert report.ieee20305_connect_stat == 1
        assert report.ieee20305_ctrl_stat == 1
        assert {f.name for f, _ in report.ListFields()} == {
            "ieee20305_connect_stat",
            "ieee20305_ctrl_stat",
        }

    def test_per_string_pv_power_sums_to_the_total(self) -> None:
        """The physical relation, not the schema, is what this checks.

        pv1 and pv2 are separate strings feeding one total, so a field number
        pointing at something that is not a string power fails the sum.

        What the sum cannot catch is 6 and 7 swapped with each other - it is
        symmetric in the two. The single-string case below is what pins their
        order, because that payload carries field 6 alone: under a swap it
        would report pv2 present and pv1 absent. Read the two together.
        """
        report = JTS1EnergyStreamReport()
        report.ParseFromString(self._ENERGY_STREAM_PAYLOAD)

        assert report.HasField("pv1_pwr")
        assert report.HasField("pv2_pwr")
        assert report.pv1_pwr > 0
        assert report.pv2_pwr > 0
        assert abs(report.pv1_pwr + report.pv2_pwr - report.mppt_pwr) <= 10.0

    # The same command from a single-string unit. It omits pv2 rather than
    # sending a zero, which is why pv1 and pv2 are declared with explicit
    # presence: without it, "no second string" and "second string at 0 W"
    # would be the same message.
    _SINGLE_STRING_PAYLOAD = bytes.fromhex(
        "0d0000c3431500a01bc51d0000344528643500003445"
    )

    def test_the_pv_inverter_field_stays_undeclared(self) -> None:
        """Declaring field 8 here would be a third source for one value.

        The command family lists pv_inv_pwr at 8, but cmd_id=39 already
        carries that quantity under a rename to pv_inverter_power_w, and the
        heartbeat's ems_pv_inv_pwr maps to the same key. A third path with no
        rename would land beside them under the raw name. Fields 9 and 10
        appear in no recorded frame at all.
        """
        declared = {f.number for f in JTS1EnergyStreamReport.DESCRIPTOR.fields}

        assert declared == {1, 2, 3, 4, 5, 6, 7}

    def test_no_field_name_means_two_numbers_across_commands(self) -> None:
        """One name, one field number - device data is a single flat dict.

        Every command's output merges into one dict per device, so a name
        reused at a different number in another message silently overwrites.
        bp_soc is the one pre-existing case and is excluded deliberately:
        both really are the battery charge level.
        """
        from ecoflow_energy.ecoflow.proto.runtime import _build_cmd_registry

        seen: dict[str, set[int]] = {}
        for config in _build_cmd_registry().values():
            for field in config.msg_class.DESCRIPTOR.fields:
                seen.setdefault(field.name, set()).add(field.number)

        clashes = {n: v for n, v in seen.items() if len(v) > 1 and n != "bp_soc"}

        assert not clashes, f"one name at two field numbers: {clashes}"

    def test_an_absent_string_is_not_a_zero(self) -> None:
        report = JTS1EnergyStreamReport()
        report.ParseFromString(self._SINGLE_STRING_PAYLOAD)

        assert report.HasField("pv1_pwr")
        assert not report.HasField("pv2_pwr")
        assert report.pv1_pwr == report.mppt_pwr


class TestPowerOceanParamChangeReport:
    """cmd_func 96 / cmd_id 13, the parameter change report.

    The payload is the inner bytes of a real frame from a listen-only
    recording. It carries no identifier - 56 bytes of varints, one float and
    one nested block, verified free of ASCII fragments before it was copied
    here.

    What triggers this message is not established. Three recordings of the
    same system hold it twice and once not at all, both times byte for byte
    identical, at 16.5 s of 600 and 114 s of 150 - so neither on connect nor
    on a parameter moving, since nothing moved. Treat it as rare and
    unpredictable rather than as change-driven.
    """

    _PAYLOAD = bytes.fromhex(
        "08001000180020002800300038234001506458006d0000000072"
        "1b080015000000001d0000000025000000002d0000000035000000007a00"
    )

    def _report(self) -> JTS1EmsParamChangeReport:
        report = JTS1EmsParamChangeReport()
        report.ParseFromString(self._PAYLOAD)
        return report

    def test_the_breaker_rating_is_read(self) -> None:
        """The one value in this message that is neither zero nor a default.

        35 A here against 63 A on a second unit is what shows these are per
        installation rather than one constant decoded twice.
        """
        report = self._report()

        assert report.breaker_capacity_max == 35
        assert report.breaker_enable_state is True

    def test_the_peak_shaving_block_decodes_to_six_values(self) -> None:
        """A 27 byte block that used to be reported as a bare field number."""
        peak = self._report().ems_peak_shaving_report

        assert peak.peak_shaving_status == 0
        assert peak.peak_shaving_max_power == 0.0
        assert peak.peak_shaving_energy == 0.0
        assert peak.peak_shaving_soc == 0.0
        assert peak.peak_shaving_times == 0.0
        assert peak.peak_shaving_control_energy == 0.0

    def test_switched_off_is_not_the_same_as_absent(self) -> None:
        """Every value here is zero, so presence is the only signal left.

        Without explicit presence a system with peak shaving off would be
        indistinguishable from one that never reported the block at all, and
        the zeros above would prove nothing.
        """
        report = self._report()

        assert report.HasField("ems_peak_shaving_report")
        assert report.HasField("smart_ctrl")
        assert report.smart_ctrl is False
        # The other half of the claim: the two declared fields this frame
        # does not carry read as absent, not as another zero. Without both
        # directions the test name promises a contrast it never shows.
        assert not report.HasField("kraken_register_status")
        assert not report.HasField("kraken_switch")

    def test_the_unverified_fields_stay_undeclared(self) -> None:
        """Nested shapes are not guessed from an empty or absent field.

        9 and 22 are the scheduled tasks and appear in no recording. 15 and
        17 were seen only as empty submessages. Protobuf fills what matches a
        declared shape and hides the rest, so a wrong guess here would decode
        convincingly rather than fail.
        """
        declared = {f.number for f in JTS1EmsParamChangeReport.DESCRIPTOR.fields}

        assert declared == {1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 13, 14, 18, 19}
        assert not declared & {9, 12, 15, 16, 17, 20, 21, 22}
