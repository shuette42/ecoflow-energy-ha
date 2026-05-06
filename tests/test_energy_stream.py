"""Tests for EcoFlow Protobuf encoder — EnergyStreamSwitch and SoC limit SET."""

import pytest

from ecoflow_energy.ecoflow.energy_stream import (
    build_backup_event_set_payload,
    build_energy_stream_activate_payload,
    build_energy_stream_deactivate_payload,
    build_feed_mode_set_payload,
    build_feed_power_set_payload,
    build_powerocean_soc_set_payload,
    build_soc_limit_set_payload,
    build_work_mode_set_payload,
)
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
    encode_varint,
)


# ===========================================================================
# Varint / field primitives
# ===========================================================================


class TestVarintEncoding:
    def test_single_byte(self):
        assert encode_varint(0) == b"\x00"
        assert encode_varint(1) == b"\x01"
        assert encode_varint(127) == b"\x7f"

    def test_two_bytes(self):
        assert encode_varint(128) == b"\x80\x01"
        assert encode_varint(300) == b"\xac\x02"

    def testencode_field_varint(self):
        # field 1, value 1 → tag=0x08, value=0x01
        result = encode_field_varint(1, 1)
        assert result == b"\x08\x01"

    def testencode_field_bytes(self):
        # field 1, 2-byte payload → tag=0x0a, len=2, data
        result = encode_field_bytes(1, b"\x08\x01")
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
        inner_field1 = encode_field_varint(1, 95)
        # field 2 = 10 (discharge lower)
        inner_field2 = encode_field_varint(2, 10)
        # Both must appear consecutively in the payload
        assert inner_field1 + inner_field2 in payload

    def test_payload_with_zero_discharge(self):
        """Value 0 for min_discharge_soc is correctly encoded."""
        payload = build_soc_limit_set_payload(100, 0, seq=1)
        # field 2 = 0 must still be on the wire
        inner_field2 = encode_field_varint(2, 0)
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


# ===========================================================================
# PowerOcean 3-field SoC SET payload (cmd_id=112, app-replay format)
# ===========================================================================


class TestPowerOceanSocSetPayload:
    """3-field SoC SET that replicates the official EcoFlow app payload."""

    def test_payload_structure(self):
        payload = build_powerocean_soc_set_payload(60, 100, seq=99999, device_sn="HJ31TEST")
        assert isinstance(payload, bytes)
        assert payload[0] == 0x0A

    def test_cmd_id_112_present(self):
        payload = build_powerocean_soc_set_payload(60, 100, seq=1)
        assert b"\x48\x70" in payload  # cmd_id=112

    def test_pdata_contains_all_three_fields(self):
        # field 1=100 (0x64), field 2=60 (0x3c), field 4=100 (0x64)
        payload = build_powerocean_soc_set_payload(60, 100, seq=1)
        # Inner pdata should contain: 0x08 0x64 (f1=100), 0x10 0x3c (f2=60), 0x20 0x64 (f4=100)
        assert b"\x08\x64\x10\x3c\x20\x64" in payload

    def test_check_type_field7_present(self):
        # New envelope adds field 7 (check_type) = 3
        # field 7 tag = (7<<3)|0 = 56 = 0x38, value 3 = 0x03
        payload = build_powerocean_soc_set_payload(0, 100, seq=1)
        assert b"\x38\x03" in payload

    def test_from_ios_field23_present(self):
        # field 23 = "ios" string. tag for length-delimited = (23<<3)|2
        # = 186 = 0xBA 0x01 (multi-byte varint), then len 3, then "ios"
        payload = build_powerocean_soc_set_payload(0, 100, seq=1)
        assert b"\xba\x01\x03ios" in payload

    def test_device_sn_field25_present_when_provided(self):
        # field 25 = SN string. tag = (25<<3)|2 = 202 = 0xCA 0x01
        sn = "HJ31TEST"
        payload = build_powerocean_soc_set_payload(0, 100, seq=1, device_sn=sn)
        assert b"\xca\x01" + bytes([len(sn)]) + sn.encode() in payload

    def test_device_sn_omitted_when_empty(self):
        sn_marker = b"\xca\x01"
        payload_with_sn = build_powerocean_soc_set_payload(0, 100, seq=1, device_sn="X")
        payload_without = build_powerocean_soc_set_payload(0, 100, seq=1, device_sn="")
        assert sn_marker in payload_with_sn
        assert sn_marker not in payload_without

    def test_rejects_backup_above_solar(self):
        with pytest.raises(ValueError):
            build_powerocean_soc_set_payload(70, 60)

    def test_rejects_backup_below_zero(self):
        with pytest.raises(ValueError):
            build_powerocean_soc_set_payload(-1, 100)

    def test_rejects_solar_above_100(self):
        with pytest.raises(ValueError):
            build_powerocean_soc_set_payload(50, 101)

    def test_equal_values_allowed(self):
        # backup_reserve == solar_surplus is allowed (boundary)
        payload = build_powerocean_soc_set_payload(50, 50, seq=1)
        assert isinstance(payload, bytes)

    def test_zero_zero_allowed(self):
        payload = build_powerocean_soc_set_payload(0, 0, seq=1)
        assert isinstance(payload, bytes)

    def test_deterministic_with_fixed_seq(self):
        p1 = build_powerocean_soc_set_payload(60, 100, seq=42, device_sn="X")
        p2 = build_powerocean_soc_set_payload(60, 100, seq=42, device_sn="X")
        assert p1 == p2

    def test_different_values_produce_different_payload(self):
        p1 = build_powerocean_soc_set_payload(0, 100, seq=42)
        p2 = build_powerocean_soc_set_payload(60, 100, seq=42)
        assert p1 != p2

    def test_max_charge_field1_always_100(self):
        # field 1 should always be 100 regardless of inputs
        payload = build_powerocean_soc_set_payload(0, 0, seq=1)
        # field 1 tag = 0x08, value 100 = 0x64
        assert b"\x08\x64" in payload


# ===========================================================================
# Work Mode SET payload (SysWorkModeSet, cmd_id=98)
# ===========================================================================


class TestWorkModeSetPayload:
    def test_payload_structure(self):
        payload = build_work_mode_set_payload(0, seq=99999)
        assert isinstance(payload, bytes)
        assert payload[0] == 0x0A

    def test_cmd_id_98_present(self):
        """cmd_id=98 (field 9, varint) must be encoded in the header."""
        payload = build_work_mode_set_payload(0, seq=1)
        # field 9 tag = 0x48, value 98 = 0x62
        assert b"\x48\x62" in payload

    def test_cmd_func_96_present(self):
        payload = build_work_mode_set_payload(0, seq=1)
        assert b"\x40\x60" in payload

    def test_payload_contains_work_mode_value(self):
        # Inner pdata = field 1 varint with mode value
        payload = build_work_mode_set_payload(2, seq=1)  # BACKUP
        assert b"\x08\x02" in payload

    def test_self_use_zero_encoded(self):
        # WorkMode SELFUSE=0 must still be present on the wire
        payload = build_work_mode_set_payload(0, seq=1)
        assert b"\x08\x00" in payload

    def test_ai_schedule_value(self):
        # WorkMode AI_SCHEDULE=12
        payload = build_work_mode_set_payload(12, seq=1)
        assert b"\x08\x0c" in payload

    def test_deterministic_with_fixed_seq(self):
        p1 = build_work_mode_set_payload(1, seq=42)
        p2 = build_work_mode_set_payload(1, seq=42)
        assert p1 == p2

    def test_different_modes_produce_different_payload(self):
        p1 = build_work_mode_set_payload(0, seq=42)
        p2 = build_work_mode_set_payload(2, seq=42)
        assert p1 != p2

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            build_work_mode_set_payload(-1)

    def test_rejects_above_13(self):
        with pytest.raises(ValueError):
            build_work_mode_set_payload(14)

    def test_default_seq(self):
        payload = build_work_mode_set_payload(0)
        assert isinstance(payload, bytes)


# ===========================================================================
# Feed Mode SET payload (SysFeedPowerSet field 2, cmd_id=115)
# ===========================================================================


class TestFeedModeSetPayload:
    def test_payload_structure(self):
        payload = build_feed_mode_set_payload(0, seq=99999)
        assert isinstance(payload, bytes)
        assert payload[0] == 0x0A

    def test_cmd_id_115_present(self):
        """cmd_id=115 (field 9, varint) must be encoded in the header."""
        payload = build_feed_mode_set_payload(0, seq=1)
        # field 9 tag = 0x48, value 115 = 0x73
        assert b"\x48\x73" in payload

    def test_payload_contains_feed_mode_field2(self):
        # Inner pdata = field 2 varint
        # field 2 varint tag = (2<<3)|0 = 0x10
        payload = build_feed_mode_set_payload(3, seq=1)  # limit mode
        assert b"\x10\x03" in payload

    def test_off_mode_zero_encoded(self):
        payload = build_feed_mode_set_payload(0, seq=1)
        assert b"\x10\x00" in payload

    def test_zero_feed_mode(self):
        # mode=2 (zero-feed for RegEnergie 0%)
        payload = build_feed_mode_set_payload(2, seq=1)
        assert b"\x10\x02" in payload

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            build_feed_mode_set_payload(-1)

    def test_rejects_above_3(self):
        with pytest.raises(ValueError):
            build_feed_mode_set_payload(4)


# ===========================================================================
# Feed Power Limit SET payload (SysFeedPowerSet field 4, cmd_id=115)
# ===========================================================================


class TestFeedPowerSetPayload:
    def test_payload_structure(self):
        payload = build_feed_power_set_payload(800, seq=99999)
        assert isinstance(payload, bytes)
        assert payload[0] == 0x0A

    def test_cmd_id_115_present(self):
        payload = build_feed_power_set_payload(800, seq=1)
        # field 9 tag = 0x48, value 115 = 0x73
        assert b"\x48\x73" in payload

    def test_payload_contains_feed_power_field4(self):
        # field 4 varint tag = (4<<3)|0 = 0x20
        # 800W = varint 0xA0 0x06
        payload = build_feed_power_set_payload(800, seq=1)
        assert b"\x20\xa0\x06" in payload

    def test_zero_watts_encoded(self):
        payload = build_feed_power_set_payload(0, seq=1)
        assert b"\x20\x00" in payload

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            build_feed_power_set_payload(-1)

    def test_accepts_high_value(self):
        # 10kW typical inverter ceiling
        payload = build_feed_power_set_payload(10000, seq=1)
        assert isinstance(payload, bytes)


# ===========================================================================
# Backup Event SET payload (SysBackupEventSet, cmd_id=99)
# ===========================================================================


class TestBackupEventSetPayload:
    def test_payload_structure(self):
        payload = build_backup_event_set_payload(True, 1700000000, 1700003600, seq=1)
        assert isinstance(payload, bytes)
        assert payload[0] == 0x0A

    def test_cmd_id_99_present(self):
        payload = build_backup_event_set_payload(True, 1700000000, 1700003600, seq=1)
        # field 9 tag = 0x48, value 99 = 0x63
        assert b"\x48\x63" in payload

    def test_enable_field2_set(self):
        # field 2 varint tag = 0x10, enable=true (1)
        payload = build_backup_event_set_payload(True, 1700000000, 1700003600, seq=1)
        assert b"\x10\x01" in payload

    def test_disable_field2_zero(self):
        # disable=false (0). start/end_ts can be 0 when disabling.
        payload = build_backup_event_set_payload(False, 0, 0, seq=1)
        assert b"\x10\x00" in payload

    def test_rejects_inverted_window(self):
        # When enabling, start must be < end
        with pytest.raises(ValueError):
            build_backup_event_set_payload(True, 1700003600, 1700000000)

    def test_rejects_negative_timestamp(self):
        with pytest.raises(ValueError):
            build_backup_event_set_payload(True, -1, 1700000000)

    def test_disable_with_zero_window_allowed(self):
        # Disabling doesn't require valid window
        payload = build_backup_event_set_payload(False, 0, 0, seq=1)
        assert isinstance(payload, bytes)
