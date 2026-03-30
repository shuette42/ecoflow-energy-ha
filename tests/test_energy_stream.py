"""Tests for EcoFlow Protobuf encoder — EnergyStreamSwitch and SoC limit SET."""

from ecoflow_energy.ecoflow.energy_stream import (
    _encode_field_bytes,
    _encode_field_varint,
    _encode_varint,
    build_energy_stream_activate_payload,
    build_energy_stream_deactivate_payload,
    build_soc_limit_set_payload,
)


# ===========================================================================
# Varint / field primitives
# ===========================================================================


class TestVarintEncoding:
    def test_single_byte(self):
        assert _encode_varint(0) == b"\x00"
        assert _encode_varint(1) == b"\x01"
        assert _encode_varint(127) == b"\x7f"

    def test_two_bytes(self):
        assert _encode_varint(128) == b"\x80\x01"
        assert _encode_varint(300) == b"\xac\x02"

    def test_encode_field_varint(self):
        # field 1, value 1 → tag=0x08, value=0x01
        result = _encode_field_varint(1, 1)
        assert result == b"\x08\x01"

    def test_encode_field_bytes(self):
        # field 1, 2-byte payload → tag=0x0a, len=2, data
        result = _encode_field_bytes(1, b"\x08\x01")
        assert result == b"\x0a\x02\x08\x01"


# ===========================================================================
# EnergyStreamSwitch payloads
# ===========================================================================


class TestEnergyStreamPayload:
    def test_activate_payload_structure(self):
        payload = build_energy_stream_activate_payload(seq=12345)
        assert isinstance(payload, bytes)
        assert len(payload) > 0
        # Outer wrapper: field 1 (tag=0x0a) + length-delimited
        assert payload[0] == 0x0A

    def test_deactivate_payload_structure(self):
        payload = build_energy_stream_deactivate_payload(seq=12345)
        assert isinstance(payload, bytes)
        assert payload[0] == 0x0A

    def test_activate_default_seq_generates_timestamp(self):
        p1 = build_energy_stream_activate_payload()
        p2 = build_energy_stream_activate_payload()
        # Both should succeed (not crash)
        assert isinstance(p1, bytes)
        assert isinstance(p2, bytes)


# ===========================================================================
# SoC Limit SET payload (SysBatChgDsgSet, cmd_id=112)
# ===========================================================================


class TestSocLimitSetPayload:
    def test_payload_structure(self):
        """Payload is a valid Send_Header_Msg wrapper."""
        payload = build_soc_limit_set_payload(95, 10, seq=99999)
        assert isinstance(payload, bytes)
        # Outer wrapper: field 1 tag = 0x0a
        assert payload[0] == 0x0A

    def test_deterministic_with_fixed_seq(self):
        """Same inputs produce identical output when seq is fixed."""
        p1 = build_soc_limit_set_payload(100, 0, seq=42)
        p2 = build_soc_limit_set_payload(100, 0, seq=42)
        assert p1 == p2

    def test_different_values_produce_different_payload(self):
        p1 = build_soc_limit_set_payload(100, 0, seq=42)
        p2 = build_soc_limit_set_payload(95, 10, seq=42)
        assert p1 != p2

    def test_cmd_id_112_present(self):
        """cmd_id=112 (field 9, varint) must be encoded in the header."""
        payload = build_soc_limit_set_payload(100, 0, seq=1)
        # cmd_id=112 → field 9 tag = (9<<3)|0 = 72 = 0x48, value 112 = 0x70
        assert b"\x48\x70" in payload

    def test_cmd_func_96_present(self):
        """cmdFunc=96 (field 8, varint) must be encoded in the header."""
        payload = build_soc_limit_set_payload(100, 0, seq=1)
        # cmdFunc=96 → field 8 tag = (8<<3)|0 = 64 = 0x40, value 96 = 0x60
        assert b"\x40\x60" in payload

    def test_payload_contains_soc_values(self):
        """The inner SysBatChgDsgSet message should contain both SoC fields."""
        payload = build_soc_limit_set_payload(95, 10, seq=1)
        # field 1 = 95 (charge upper)
        inner_field1 = _encode_field_varint(1, 95)
        # field 2 = 10 (discharge lower)
        inner_field2 = _encode_field_varint(2, 10)
        # Both must appear consecutively in the payload
        assert inner_field1 + inner_field2 in payload

    def test_payload_with_zero_discharge(self):
        """Value 0 for min_discharge_soc is correctly encoded."""
        payload = build_soc_limit_set_payload(100, 0, seq=1)
        # field 2 = 0 must still be on the wire
        inner_field2 = _encode_field_varint(2, 0)
        assert inner_field2 in payload

    def test_default_seq_generates_timestamp(self):
        """Default seq=0 auto-generates from current timestamp."""
        payload = build_soc_limit_set_payload(100, 0)
        assert isinstance(payload, bytes)
        assert len(payload) > 10

    def test_boundary_values_max(self):
        """Max charge SoC=100 and min discharge SoC=0."""
        payload = build_soc_limit_set_payload(100, 0, seq=1)
        assert isinstance(payload, bytes)

    def test_boundary_values_min(self):
        """Max charge SoC=50 and min discharge SoC=30."""
        payload = build_soc_limit_set_payload(50, 30, seq=1)
        assert isinstance(payload, bytes)

    def test_needack_is_set(self):
        """needAck=1 (field 11, varint) must be present."""
        payload = build_soc_limit_set_payload(100, 0, seq=1)
        # field 11 tag = (11<<3)|0 = 88 = 0x58, value 1 = 0x01
        assert b"\x58\x01" in payload

    def test_is_rw_cmd_3(self):
        """isRwCmd=3 (field 16, varint) must be present."""
        payload = build_soc_limit_set_payload(100, 0, seq=1)
        # field 16 tag = (16<<3)|0 = 128 → varint 0x80 0x01, value 3 = 0x03
        assert b"\x80\x01\x03" in payload
