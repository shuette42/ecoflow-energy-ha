"""Tests for protobuf decoder and runtime decoder."""

import importlib.abc
import importlib.machinery
import logging
import sys

from ecoflow_energy.ecoflow.energy_stream import (
    _encode_field_bytes,
    _encode_field_varint,
    build_energy_stream_activate_payload,
    build_energy_stream_deactivate_payload,
)
from ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
    JTS1EmsChangeReport,
    JTS1EmsHeartbeat,
    JTS1EnergyStreamReport,
)
from ecoflow_energy.ecoflow.proto.runtime import (
    _build_cmd_registry,
    decode_proto_runtime_frame,
)


def _build_frame(cmd_func: int, cmd_id: int, inner: bytes) -> bytes:
    """Build a minimal HeaderMessage frame for testing."""
    header = bytearray()
    header.extend(_encode_field_bytes(1, inner))       # pdata
    header.extend(_encode_field_varint(8, cmd_func))   # cmd_func
    header.extend(_encode_field_varint(9, cmd_id))     # cmd_id
    return _encode_field_bytes(1, bytes(header))


class TestProtobufDecoder:
    """Tests for the low-level protobuf header decoder."""

    def test_decode_empty(self):
        headers, payload = decode_header_message(b"")
        assert headers == []
        assert payload is None

    def test_decode_simple_header(self):
        """Build a frame with one header containing cmd_func=96, cmd_id=33."""
        inner = _encode_field_varint(1, 1)  # dummy pdata
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
        """Proto3 omits 0.0 — runtime decoder must zero-fill power fields."""
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
