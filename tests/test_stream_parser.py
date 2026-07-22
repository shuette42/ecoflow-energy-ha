"""Tests for the Stream protobuf telemetry parser."""

from __future__ import annotations

import struct

import pytest

from ecoflow_energy.ecoflow.parsers.stream_proto import parse_stream_proto_message
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
    encode_varint,
)


def _encode_fixed32_field(field_number: int, value: float) -> bytes:
    """Encode one protobuf fixed32 field."""
    tag = (field_number << 3) | 5
    return encode_varint(tag) + struct.pack("<f", value)


def _build_frame(cmd_func: int, cmd_id: int, inner: bytes) -> bytes:
    """Build a minimal EcoFlow header frame for tests."""
    header = bytearray()
    header.extend(encode_field_bytes(1, inner))
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    return encode_field_bytes(1, bytes(header))


class TestStreamProtoParser:
    def test_parse_main_status_frame(self) -> None:
        inner = bytearray()
        inner.extend(encode_field_varint(242, 21))
        inner.extend(encode_field_varint(270, 95))
        inner.extend(encode_field_varint(271, 15))
        inner.extend(encode_field_varint(380, 1))
        inner.extend(encode_field_varint(381, 0))
        inner.extend(_encode_fixed32_field(515, 1351.0))
        inner.extend(_encode_fixed32_field(516, 309.5))
        inner.extend(_encode_fixed32_field(517, 0.0))
        inner.extend(encode_field_varint(461, 20))
        inner.extend(_encode_fixed32_field(518, 1043.4))
        inner.extend(_encode_fixed32_field(616, -967.2))
        inner.extend(_encode_fixed32_field(992, -2020.0))
        inner.extend(_encode_fixed32_field(1003, 0.0))
        inner.extend(_encode_fixed32_field(1004, 309.5))
        inner.extend(_encode_fixed32_field(1210, 0.0))
        inner.extend(_encode_fixed32_field(1211, -0.0))
        inner.extend(_encode_fixed32_field(613, 228.4))
        inner.extend(_encode_fixed32_field(615, 49.98))

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert result is not None
        assert result["soc_pct"] == 21
        assert result["max_charge_soc_pct"] == 95
        assert result["min_discharge_soc_pct"] == 15
        assert result["ac_outlet_1_enabled"] == 1
        assert result["ac_outlet_2_enabled"] == 0
        assert result["grid_w"] == pytest.approx(1351.0, rel=1e-5)
        assert result["home_w"] == pytest.approx(309.5, rel=1e-5)
        assert result["solar_w"] == 0.0
        assert result["backup_reserve_pct"] == 20
        assert result["batt_w"] == pytest.approx(1043.4, rel=1e-5)
        assert result["batt_charge_power_w"] == pytest.approx(1043.4, rel=1e-5)
        assert result["batt_discharge_power_w"] == 0.0
        # batt_charge_discharge_state is derived by the coordinator (#50),
        # not by the parser, so it must not appear in parser output.
        assert "batt_charge_discharge_state" not in result
        assert result["ac_grid_connection_power_w"] == pytest.approx(-2020.0, rel=1e-5)
        assert result["grid_connection_power_w"] == pytest.approx(-967.2, rel=1e-5)
        assert result["sys_grid_connection_power_w"] == pytest.approx(-2020.0, rel=1e-5)
        assert result["home_from_batt_w"] == 0.0
        assert result["home_from_grid_w"] == pytest.approx(309.5, rel=1e-5)
        assert result["ac_outlet_1_w"] == 0.0
        assert result["ac_outlet_2_w"] == 0.0
        assert result["ac_voltage_v"] == pytest.approx(228.4, rel=1e-5)
        assert result["ac_frequency_hz"] == pytest.approx(49.98, rel=1e-5)

    def test_parse_precise_soc_and_backup_ack(self) -> None:
        precise = _build_frame(32, 50, _encode_fixed32_field(25, 21.6))
        ack = _build_frame(254, 18, encode_field_varint(102, 80))

        result = parse_stream_proto_message(precise + ack)

        assert result is not None
        assert result["soc_precise_pct"] == pytest.approx(21.6, rel=1e-5)
        assert result["soc_pct"] == pytest.approx(21.6, rel=1e-5)
        assert result["backup_reserve_pct"] == 80

    def test_parse_led_brightness_live_only(self) -> None:
        """Live brightness (994) is mapped; the set/ack slider target (384)
        is intentionally ignored so it cannot overwrite the live value."""
        live = _build_frame(254, 21, encode_field_varint(994, 50))
        ack = _build_frame(254, 18, encode_field_varint(384, 60))

        result = parse_stream_proto_message(live + ack)

        assert result is not None
        assert result["led_brightness"] == 50

    def test_parse_led_brightness_ack_field_is_ignored(self) -> None:
        """A SET acknowledgement frame's slider-target field 384 must not
        produce a led_brightness value (only the live field 994 does)."""
        inner = bytearray()
        inner.extend(encode_field_varint(102, 80))  # backup_reserve ack
        inner.extend(encode_field_varint(384, 60))  # led brightness target
        ack = _build_frame(254, 18, bytes(inner))

        result = parse_stream_proto_message(ack)

        assert result is not None
        assert result["backup_reserve_pct"] == 80
        assert "led_brightness" not in result

    def test_parse_cumulative_totals_battery_details_and_outlet_mirrors(self) -> None:
        aux = bytearray()
        aux.extend(encode_field_varint(7, 20161))
        aux.extend(encode_field_varint(9, 35))
        aux.extend(encode_field_varint(11, 100000))
        aux.extend(encode_field_varint(12, 46317))
        aux.extend(encode_field_varint(13, 100000))
        aux.extend(encode_field_varint(15, 100))
        aux.extend(encode_field_varint(16, 3362))
        aux.extend(encode_field_varint(17, 3357))
        aux.extend(encode_field_varint(18, 35))
        aux.extend(encode_field_varint(19, 33))
        aux.extend(encode_field_varint(20, 47))
        aux.extend(_encode_fixed32_field(25, 21.2))
        aux.extend(encode_field_varint(32, 6))
        aux.extend(encode_field_varint(50, 5503))
        aux.extend(encode_field_varint(51, 15270))

        mirror = bytearray()
        mirror.extend(encode_field_varint(980, 1))
        mirror.extend(encode_field_varint(982, 0))

        result = parse_stream_proto_message(
            _build_frame(32, 50, bytes(aux)) + _build_frame(254, 21, bytes(mirror))
        )

        assert result is not None
        assert result["ac_outlet_1_enabled"] == 1
        assert result["ac_outlet_2_enabled"] == 0
        assert result["batt_voltage_v"] == pytest.approx(20.161, rel=1e-5)
        assert result["batt_temp_c"] == 35
        assert result["batt_design_cap_mah"] == 100000
        assert result["batt_remain_cap_mah"] == 46317
        assert result["batt_full_cap_mah"] == 100000
        assert result["bms_soh_pct"] == 100
        assert result["batt_max_cell_vol_mv"] == 3362
        assert result["batt_min_cell_vol_mv"] == 3357
        assert result["batt_max_cell_temp_c"] == 35
        assert result["batt_min_cell_temp_c"] == 33
        assert result["batt_max_mos_temp_c"] == 47
        # Raw Wh battery-energy fields (79/80) are no longer parsed: the
        # entities were replaced by the kWh charge/discharge energy sensors.
        assert "batt_charge_energy_wh" not in result
        assert "batt_discharge_energy_wh" not in result
        assert result["batt_charge_capacity_ah"] == pytest.approx(5.503, rel=1e-5)
        assert result["batt_discharge_capacity_ah"] == pytest.approx(15.27, rel=1e-5)

    def test_parse_grid_connection_without_battery_power(self) -> None:
        inner = _encode_fixed32_field(992, 304.15)

        result = parse_stream_proto_message(_build_frame(254, 21, inner))

        assert result is not None
        assert "batt_w" not in result
        assert "batt_charge_power_w" not in result
        assert "batt_discharge_power_w" not in result
        assert "batt_charge_discharge_state" not in result
        assert result["ac_grid_connection_power_w"] == pytest.approx(304.15, rel=1e-5)
        assert result["sys_grid_connection_power_w"] == pytest.approx(304.15, rel=1e-5)

    def test_parse_zero_battery_power_splits_to_zero(self):
        """At zero battery power both charge and discharge splits are 0.
        State derivation is the coordinator's job, so the parser emits no
        batt_charge_discharge_state."""
        inner = _encode_fixed32_field(518, 0.0)

        result = parse_stream_proto_message(_build_frame(254, 21, inner))

        assert result is not None
        assert result["batt_w"] == 0.0
        assert result["batt_charge_power_w"] == 0.0
        assert result["batt_discharge_power_w"] == 0.0
        assert "batt_charge_discharge_state" not in result

    def test_parse_tiny_negative_float_is_normalized_to_zero(self):
        inner = bytearray()
        inner.extend(_encode_fixed32_field(992, -0.0))
        inner.extend(_encode_fixed32_field(1003, -0.0))
        inner.extend(_encode_fixed32_field(1211, -0.0))

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert result is not None
        assert result["sys_grid_connection_power_w"] == 0.0
        assert result["home_from_batt_w"] == 0.0
        assert result["ac_outlet_2_w"] == 0.0
        assert result["ac_grid_connection_power_w"] == 0.0
        assert "batt_w" not in result
        assert "batt_charge_discharge_state" not in result

    def test_parse_confirmed_ac_outlet_power_fields(self):
        inner = bytearray()
        inner.extend(_encode_fixed32_field(1210, 201.47))
        inner.extend(_encode_fixed32_field(1211, 228.18))

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert result is not None
        assert result["ac_outlet_1_w"] == pytest.approx(201.47, rel=1e-5)
        assert result["ac_outlet_2_w"] == pytest.approx(228.18, rel=1e-5)

    def test_parse_batt_w_fallback_field_602(self):
        """When the primary signed battery power (518) is absent, field 602
        provides the fallback value (positive sign preserved)."""
        inner = _encode_fixed32_field(602, 512.5)

        result = parse_stream_proto_message(_build_frame(254, 21, inner))

        assert result is not None
        assert result["batt_w"] == pytest.approx(512.5, rel=1e-5)
        assert result["batt_charge_power_w"] == pytest.approx(512.5, rel=1e-5)
        assert result["batt_discharge_power_w"] == 0.0

    def test_parse_primary_batt_w_wins_over_fallback(self):
        """If both 518 and 602 are present, the primary signed value (518)
        takes precedence over the 602 fallback."""
        inner = bytearray()
        inner.extend(_encode_fixed32_field(518, -300.0))
        inner.extend(_encode_fixed32_field(602, 999.0))

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert result is not None
        assert result["batt_w"] == pytest.approx(-300.0, rel=1e-5)
        assert result["batt_discharge_power_w"] == pytest.approx(300.0, rel=1e-5)

    def test_parse_outlet_enable_primary_and_mirror_same_frame(self):
        """Fields 380/381 and their mirrors 980/982 may co-occur in one
        frame. Both encode the same boolean state, so the result is stable
        regardless of wire order."""
        inner = bytearray()
        inner.extend(encode_field_varint(380, 1))
        inner.extend(encode_field_varint(980, 1))
        inner.extend(encode_field_varint(381, 0))
        inner.extend(encode_field_varint(982, 0))

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert result is not None
        assert result["ac_outlet_1_enabled"] == 1
        assert result["ac_outlet_2_enabled"] == 0


class TestStreamSignedVarint:
    """Negative int32/int64 varints must decode to negative numbers."""

    def test_negative_varint_batt_temp(self) -> None:
        # (32, 50) field 9 = batt_temp_c, encoded as 64-bit two's complement
        inner = encode_field_varint(9, (1 << 64) - 5)  # -5 C
        result = parse_stream_proto_message(_build_frame(32, 50, bytes(inner)))
        assert result is not None
        assert result["batt_temp_c"] == -5

    def test_negative_varint_float_target(self) -> None:
        # (254, 21) field 518 = batt_w (float target) as negative varint
        inner = encode_field_varint(518, (1 << 64) - 300)  # -300 W
        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))
        assert result is not None
        assert result["batt_w"] == pytest.approx(-300.0)
        assert result["batt_discharge_power_w"] == pytest.approx(300.0)

    def test_oversized_varint_returns_none(self) -> None:
        # Field 9 tag followed by an 11-byte (>64-bit) varint
        inner = encode_varint((9 << 3) | 0) + b"\xff" * 11
        assert parse_stream_proto_message(_build_frame(32, 50, inner)) is None

    def test_truncated_inner_returns_none(self) -> None:
        inner = encode_varint((9 << 3) | 0) + b"\x80"
        assert parse_stream_proto_message(_build_frame(32, 50, inner)) is None
