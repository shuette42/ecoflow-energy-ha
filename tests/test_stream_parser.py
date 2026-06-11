"""Tests for the reverse-engineered Stream protobuf parser."""

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
        inner.extend(_encode_fixed32_field(515, 1351.0))
        inner.extend(_encode_fixed32_field(516, 309.5))
        inner.extend(_encode_fixed32_field(517, 0.0))
        inner.extend(encode_field_varint(461, 20))
        inner.extend(_encode_fixed32_field(518, 1043.4))
        inner.extend(_encode_fixed32_field(613, 228.4))
        inner.extend(_encode_fixed32_field(615, 49.98))

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert result is not None
        assert result["soc_pct"] == 21
        assert result["max_charge_soc_pct"] == 95
        assert result["min_discharge_soc_pct"] == 15
        assert result["grid_w"] == pytest.approx(1351.0, rel=1e-5)
        assert result["home_w"] == pytest.approx(309.5, rel=1e-5)
        assert result["solar_w"] == 0.0
        assert result["backup_reserve_pct"] == 20
        assert result["batt_w"] == pytest.approx(1043.4, rel=1e-5)
        assert result["batt_charge_power_w"] == pytest.approx(1043.4, rel=1e-5)
        assert result["batt_discharge_power_w"] == 0.0
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

    def test_parse_cumulative_totals_and_battery_details(self) -> None:
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
        aux.extend(encode_field_varint(79, 110))
        aux.extend(encode_field_varint(80, 296))

        result = parse_stream_proto_message(_build_frame(32, 50, bytes(aux)))

        assert result is not None
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
        assert result["batt_charge_energy_wh"] == 110
        assert result["batt_discharge_energy_wh"] == 296
        assert result["batt_charge_capacity_ah"] == pytest.approx(5.503, rel=1e-5)
        assert result["batt_discharge_capacity_ah"] == pytest.approx(15.27, rel=1e-5)

    def test_parse_negated_power_fallback(self) -> None:
        inner = _encode_fixed32_field(992, 304.15)

        result = parse_stream_proto_message(_build_frame(254, 21, inner))

        assert result is not None
        assert result["batt_w"] == pytest.approx(-304.15, rel=1e-5)
        assert result["batt_charge_power_w"] == 0.0
        assert result["batt_discharge_power_w"] == pytest.approx(304.15, rel=1e-5)
