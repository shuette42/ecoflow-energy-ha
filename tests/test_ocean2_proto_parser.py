"""Tests for the Ocean 2 (RE11) protobuf telemetry parser.

The frames are assembled here rather than replayed from a capture. A real
Ocean 2 frame carries the serial number in its header, and the readings used
below are the ones that were confirmed against the EcoFlow app, so building
the wire format from those numbers keeps the evidence and drops the identity.
"""

from __future__ import annotations

import struct

import pytest
from ecoflow_energy.ecoflow.const import (
    DEVICE_TYPE_OCEAN2,
    DEVICE_TYPE_POWEROCEAN,
    get_device_name,
    get_device_type,
)
from ecoflow_energy.ecoflow.parsers.ocean2_proto import (
    parse_ocean2_proto_message,
)


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _f32(field: int, value: float) -> bytes:
    """A 32-bit float field - how the device sends every power reading."""
    return _varint((field << 3) | 5) + struct.pack("<f", value)


def _vint(field: int, value: int) -> bytes:
    return _varint((field << 3) | 0) + _varint(value)


def _msg(field: int, body: bytes) -> bytes:
    """A length-delimited sub-message."""
    return _varint((field << 3) | 2) + _varint(len(body)) + body


def _frame(pdata: bytes, cmd_func: int = 254, cmd_id: int = 39) -> bytes:
    """Wrap a payload in the EcoFlow outer frame."""
    header = _msg(1, pdata) + _vint(8, cmd_func) + _vint(9, cmd_id)
    return _msg(1, header)


def _pv_string(index: int, watts: float) -> bytes:
    return _msg(1, _f32(1, float(index)) + _f32(4, watts))


def _telemetry(
    *,
    summary: bytes = b"",
    flow: dict[int, bytes] | None = None,
    inverter: bytes = b"",
) -> bytes:
    body = b""
    if summary:
        body += _msg(65, summary)
    for block, content in (flow or {}).items():
        body += _msg(block, content)
    if inverter:
        body += _msg(4, inverter)
    return _frame(body)


class TestDeviceClassification:
    def test_serial_prefix_routes_to_ocean2(self) -> None:
        assert get_device_type("", "RE11TEST00000001") == DEVICE_TYPE_OCEAN2

    def test_the_12kw_variant_shares_the_read_path(self) -> None:
        # RE11 and RE17 differ in power rating and in nothing this integration
        # reads: same message families, same payload shape.
        assert get_device_type("", "RE17TEST00000001") == DEVICE_TYPE_OCEAN2

    def test_the_prefix_beats_a_powerocean_product_name(self) -> None:
        # "PowerOcean" as a product name would otherwise claim the unit and
        # hand it to a parser that decodes none of its frames. The prefix is
        # exact evidence and is checked first.
        assert get_device_type("PowerOcean", "RE11TEST00000001") == DEVICE_TYPE_OCEAN2

    def test_powerocean_is_unaffected(self) -> None:
        assert get_device_type("PowerOcean", "HJ31000000000000") == (
            DEVICE_TYPE_POWEROCEAN
        )

    def test_display_name_falls_back_to_the_prefix(self) -> None:
        # The app API reports an empty product name for this device.
        assert get_device_name("", "RE11TEST00001234") == "Ocean 2 (1234)"
        assert get_device_name("", "RE17TEST00001234") == "Ocean 2 (1234)"


class TestTelemetryFrame:
    def test_reads_the_system_summary(self) -> None:
        # PV 3210 W, SoC 81.5 %, 4110 Wh left in the pack.
        parsed = parse_ocean2_proto_message(
            _telemetry(summary=_f32(4, 3210.0) + _f32(15, 4110.0) + _f32(17, 81.5))
        )
        assert parsed is not None
        assert parsed["solar_w"] == pytest.approx(3210.0)
        assert parsed["soc_pct"] == pytest.approx(81.5)
        assert parsed["batt_remaining_wh"] == pytest.approx(4110.0)

    def test_reads_the_balanced_energy_flow_block(self) -> None:
        # PV 3210 - battery 1500 - grid (-1847) = home 560, and it adds up.
        flow = _f32(1, 560.0) + _f32(2, -1847.0) + _f32(3, 3210.0) + _f32(4, 1500.0)
        parsed = parse_ocean2_proto_message(_telemetry(flow={87: flow}))
        assert parsed is not None
        assert parsed["home_w"] == pytest.approx(560.0)
        assert parsed["solar_w"] == pytest.approx(3210.0)
        assert parsed["batt_w"] == pytest.approx(1500.0)

    def test_block_87_wins_over_block_7(self) -> None:
        # Observed on hardware: block 7 trails block 87 by about one
        # measurement cycle, and the app shows what block 87 reports.
        parsed = parse_ocean2_proto_message(
            _telemetry(flow={7: _f32(1, 550.0), 87: _f32(1, 560.0)})
        )
        assert parsed is not None
        assert parsed["home_w"] == pytest.approx(560.0)

    def test_block_87_wins_over_block_7_for_solar(self) -> None:
        # Same precedence, checked on the field that used to take the
        # opposite block (7 before 87) before the source-precedence fix.
        parsed = parse_ocean2_proto_message(
            _telemetry(flow={7: _f32(3, 3100.0), 87: _f32(3, 3210.0)})
        )
        assert parsed is not None
        assert parsed["solar_w"] == pytest.approx(3210.0)

    def test_a_missing_field_does_not_overwrite_a_good_value(self) -> None:
        # Block 87 carries only the home load here. Block 7's grid reading has
        # to survive, because telemetry is partial by design.
        parsed = parse_ocean2_proto_message(
            _telemetry(flow={7: _f32(1, 550.0) + _f32(2, -1200.0), 87: _f32(1, 560.0)})
        )
        assert parsed is not None
        assert parsed["home_w"] == pytest.approx(560.0)
        assert parsed["grid_w"] == pytest.approx(-1200.0)

    def test_the_flow_block_wins_over_inverter_field_13(self) -> None:
        # Both read the grid from their own instant; the flow block is the
        # one that balances with the other three readings of its instant.
        parsed = parse_ocean2_proto_message(
            _telemetry(flow={87: _f32(2, -1800.0)}, inverter=_f32(13, 1719.0))
        )
        assert parsed is not None
        assert parsed["grid_w"] == pytest.approx(-1800.0)

    def test_inverter_field_13_is_a_fallback_for_a_frame_without_the_flow_block(
        self,
    ) -> None:
        parsed = parse_ocean2_proto_message(_telemetry(inverter=_f32(13, 1719.0)))
        assert parsed is not None
        assert parsed["grid_w"] == pytest.approx(1719.0)

    def test_the_flow_block_wins_over_the_summary_for_solar(self) -> None:
        # 65.4 and 87.3 read the same quantity from different instants; the
        # flow block is the one that balances with home/grid/battery.
        parsed = parse_ocean2_proto_message(
            _telemetry(summary=_f32(4, 3193.0), flow={87: _f32(3, 3210.0)})
        )
        assert parsed is not None
        assert parsed["solar_w"] == pytest.approx(3210.0)

    def test_reads_per_string_pv_power(self) -> None:
        strings = _msg(14, _pv_string(1, 1780.0) + _pv_string(2, 1430.0))
        parsed = parse_ocean2_proto_message(_telemetry(inverter=strings))
        assert parsed is not None
        assert parsed["pv1_w"] == pytest.approx(1780.0)
        assert parsed["pv2_w"] == pytest.approx(1430.0)

    def test_reports_a_resting_battery_as_zero(self) -> None:
        parsed = parse_ocean2_proto_message(_telemetry(summary=_f32(20, 0.0)))
        assert parsed is not None
        assert parsed["batt_w"] == pytest.approx(0.0)

    @pytest.mark.parametrize(
        ("summary_value", "expected"),
        [(-871.0, 871.0), (1859.0, -1859.0)],
    )
    def test_the_summary_battery_field_is_signed_the_other_way(
        self, summary_value: float, expected: float
    ) -> None:
        # Measured on an RE11 while charging: 65.20 runs negative where block
        # 87.4 runs positive, so the sign is inverted rather than absent.
        parsed = parse_ocean2_proto_message(_telemetry(summary=_f32(20, summary_value)))
        assert parsed is not None
        assert parsed["batt_w"] == pytest.approx(expected)

    def test_the_flow_block_wins_over_the_summary(self) -> None:
        # Both present: the flow block is the one that balances with the other
        # three readings of its own instant.
        parsed = parse_ocean2_proto_message(
            _telemetry(summary=_f32(20, -871.0), flow={87: _f32(4, 880.0)})
        )
        assert parsed is not None
        assert parsed["batt_w"] == pytest.approx(880.0)


class TestDirectionalSplits:
    @pytest.mark.parametrize(
        ("grid_w", "expected_import", "expected_export"),
        [(1719.0, 1719.0, 0.0), (-1847.0, 0.0, 1847.0), (0.0, 0.0, 0.0)],
    )
    def test_splits_grid_power(
        self, grid_w: float, expected_import: float, expected_export: float
    ) -> None:
        parsed = parse_ocean2_proto_message(_telemetry(inverter=_f32(13, grid_w)))
        assert parsed is not None
        assert parsed["grid_import_power_w"] == pytest.approx(expected_import)
        assert parsed["grid_export_power_w"] == pytest.approx(expected_export)

    @pytest.mark.parametrize(
        ("batt_w", "expected_charge", "expected_discharge"),
        [(1500.0, 1500.0, 0.0), (-900.0, 0.0, 900.0)],
    )
    def test_splits_battery_power(
        self, batt_w: float, expected_charge: float, expected_discharge: float
    ) -> None:
        parsed = parse_ocean2_proto_message(_telemetry(flow={87: _f32(4, batt_w)}))
        assert parsed is not None
        assert parsed["batt_charge_power_w"] == pytest.approx(expected_charge)
        assert parsed["batt_discharge_power_w"] == pytest.approx(expected_discharge)

    def test_writes_both_halves_so_neither_sensor_freezes(self) -> None:
        # A charge that stops has to push the discharge side to 0 as well;
        # otherwise its energy counter keeps integrating a flow that ended.
        parsed = parse_ocean2_proto_message(_telemetry(flow={87: _f32(4, 0.0)}))
        assert parsed is not None
        assert parsed["batt_charge_power_w"] == pytest.approx(0.0)
        assert parsed["batt_discharge_power_w"] == pytest.approx(0.0)


class TestRobustness:
    def test_drops_a_non_finite_reading(self) -> None:
        # A NaN reaching a sensor raises inside Home Assistant's rounding and
        # aborts the rest of that update.
        parsed = parse_ocean2_proto_message(_telemetry(inverter=_f32(13, float("nan"))))
        assert parsed is None or "grid_w" not in parsed

    def test_ignores_frames_from_other_command_ids(self) -> None:
        # cmd_id 46 is the per-module battery frame, which this parser does
        # not map yet. It must not be decoded through the telemetry layout.
        payload = _frame(_msg(65, _f32(4, 3210.0)), cmd_id=46)
        assert parse_ocean2_proto_message(payload) is None

    @pytest.mark.parametrize("payload", [b"", b"\x00", b"\x0a\xff", b"not protobuf"])
    def test_survives_malformed_input(self, payload: bytes) -> None:
        assert parse_ocean2_proto_message(payload) is None

    def test_returns_none_when_no_mapped_field_is_present(self) -> None:
        assert parse_ocean2_proto_message(_frame(_vint(999, 1))) is None

    def test_decodes_a_xor_masked_payload(self) -> None:
        pdata = _msg(65, _f32(17, 81.5))
        masked = bytes(b ^ 0x2A for b in pdata)
        header = (
            _msg(1, masked)
            + _vint(6, 1)
            + _vint(8, 254)
            + _vint(9, 39)
            + _vint(14, 0x2A)
        )
        parsed = parse_ocean2_proto_message(_msg(1, header))
        assert parsed is not None
        assert parsed["soc_pct"] == pytest.approx(81.5)
