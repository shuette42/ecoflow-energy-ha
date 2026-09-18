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
    MAX_MODULES,
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

    def test_the_plus_shares_the_read_path(self) -> None:
        # An `RE41` diagnostics download on #145 carries the `RE11`'s field
        # numbers throughout. Single phase changes only the per-phase block,
        # which no entity here reads.
        assert get_device_type("", "RE41TEST00000001") == DEVICE_TYPE_OCEAN2

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
        assert get_device_name("", "RE41TEST00001234") == "Ocean 2 Plus (1234)"


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
        # cmd_id 46 is the per-module battery frame. A telemetry-shaped body
        # sent under it declares no module fields, so it must not be decoded
        # through the telemetry layout either.
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


def _module(index: int, body: bytes = b"") -> bytes:
    """A `254/46` frame: one module per header, module number in 5.15."""
    return _frame(_msg(5, _vint(15, index) + body), cmd_id=46)


class TestModuleFrame:
    def test_reads_one_module(self) -> None:
        # Power 1122.59 W, SoH 100 %, 4 cycles - the readings confirmed
        # against a four-week-old system on 2026-07-28.
        # 39 for the state of health, as a float - field 3 carries the same
        # number as a varint, which is how the two are told apart.
        body = _f32(1, 1122.59) + _f32(39, 100.0) + _vint(17, 4) + _f32(38, 81.5)
        parsed = parse_ocean2_proto_message(_module(1, body))
        assert parsed is not None
        assert parsed["module1_power_w"] == pytest.approx(1122.59, rel=1e-4)
        assert parsed["module1_soh_pct"] == pytest.approx(100.0)
        assert parsed["module1_cycles"] == 4
        assert parsed["module1_soc_pct"] == pytest.approx(81.5)

    def test_reads_the_state_of_health_as_a_float_from_39(self) -> None:
        # Field 3 holds the same number as a varint on real frames. Declaring
        # it as a float made the walker reject it on type and left the entity
        # empty - a mistake the earlier tests hid by building 3 as a float.
        parsed = parse_ocean2_proto_message(_module(1, _vint(3, 100) + _f32(39, 97.5)))
        assert parsed is not None
        assert parsed["module1_soh_pct"] == pytest.approx(97.5)

    def test_keeps_modules_apart(self) -> None:
        # A frame bundles one header per module - two here, fourteen on a
        # large installation. Readings must not bleed between them.
        payload = _module(1, _f32(1, 1122.0)) + _module(2, _f32(1, -880.0))
        parsed = parse_ocean2_proto_message(payload)
        assert parsed is not None
        assert parsed["module1_power_w"] == pytest.approx(1122.0)
        assert parsed["module2_power_w"] == pytest.approx(-880.0)

    def test_publishes_the_hottest_power_electronics_reading(self) -> None:
        # Four board readings, one entity: the hottest is what matters, and
        # four near-identical sensors per module would be noise at fourteen.
        body = _f32(23, 41.0) + _f32(24, 47.5) + _f32(32, 39.0) + _f32(33, 44.0)
        parsed = parse_ocean2_proto_message(_module(1, body))
        assert parsed is not None
        assert parsed["module1_mos_temp_c"] == pytest.approx(47.5)

    def test_keeps_the_cell_temperatures_apart(self) -> None:
        # min <= average <= max holds in every frame; they are separate
        # readings, not one value rounded three ways.
        body = _f32(21, 24.0) + _f32(30, 26.0) + _f32(31, 22.0)
        parsed = parse_ocean2_proto_message(_module(1, body))
        assert parsed is not None
        assert parsed["module1_cell_temp_c"] == pytest.approx(24.0)
        assert parsed["module1_cell_temp_max_c"] == pytest.approx(26.0)
        assert parsed["module1_cell_temp_min_c"] == pytest.approx(22.0)

    def test_drops_a_module_without_a_number(self) -> None:
        # Without 5.15 there is nothing to attach the readings to, and
        # guessing a position would file one module's cells under another.
        payload = _frame(_msg(5, _f32(1, 1122.0)), cmd_id=46)
        assert parse_ocean2_proto_message(payload) is None

    @pytest.mark.parametrize("index", [0, MAX_MODULES + 1, 99])
    def test_ignores_an_out_of_range_module_number(self, index: int) -> None:
        assert parse_ocean2_proto_message(_module(index, _f32(1, 1.0))) is None

    def test_drops_a_nan_module_number_instead_of_raising(self) -> None:
        # `int()` on a NaN raises; the same guard the telemetry frame applies
        # to its own readings must cover the module index too.
        payload = _module(1, _f32(15, float("nan")) + _f32(1, 1122.0))
        assert parse_ocean2_proto_message(payload) is None

    def test_drops_a_non_finite_module_reading(self) -> None:
        # A NaN or an infinity reaching a sensor raises inside Home
        # Assistant's rounding, the same reason the telemetry frame drops
        # them - the module frame decodes through a different function and
        # needs the same guard rather than inheriting it.
        payload = _module(1, _f32(1, float("nan")) + _f32(38, 81.5))
        parsed = parse_ocean2_proto_message(payload)
        assert parsed is not None
        assert "module1_power_w" not in parsed
        assert parsed["module1_soc_pct"] == pytest.approx(81.5)

    def test_a_non_finite_power_electronics_reading_does_not_poison_the_max(
        self,
    ) -> None:
        # max() over a list containing NaN returns NaN in Python - one bad
        # board sensor must not blank out the other three good ones.
        body = _f32(23, 41.0) + _f32(24, float("nan")) + _f32(32, 39.0) + _f32(33, 44.0)
        parsed = parse_ocean2_proto_message(_module(1, body))
        assert parsed is not None
        assert parsed["module1_mos_temp_c"] == pytest.approx(44.0)

    def test_reads_telemetry_and_modules_from_one_payload(self) -> None:
        # Both frame types can arrive bundled; neither may swallow the other.
        payload = _telemetry(summary=_f32(17, 81.5)) + _module(1, _f32(1, 1122.0))
        parsed = parse_ocean2_proto_message(payload)
        assert parsed is not None
        assert parsed["soc_pct"] == pytest.approx(81.5)
        assert parsed["module1_power_w"] == pytest.approx(1122.0)


class TestRealModuleFrames:
    """Two `254/46` payloads captured from an RE11 while the pack rested.

    Serial numbers are replaced length-preservingly - protobuf carries a
    length before every field, so a shorter replacement would make
    everything after it unreadable and the fixture worthless for the purpose
    it was taken for.
    """

    MODULE_1 = (
        "081e2ae7020d00000000106318642a140000e8410000e0410000e0410000e0410000d841"
        "3500d056453d0040554540014d48e1884155f0480bbf5d76be5644650000000068007214"
        "00d0564500d0564500f055450040554500d0564578018201105858585858585858585858"
        "58585858588801239001009d0100002f45a50100c02845ad010000e041b5010000e841bd"
        "010000e041c5010000e041cd0100001042d001e8d1a005d801c9f0a305e001909513e801"
        "d98f13f5010000e841fd010000d84185020000e0418d020000d8419002009802ffff03a0"
        "0221a802cebfafd506b5021c32c742bd020000c842c00220c80205d00264d80201e00200"
        "e80283d001f00201f802ab828408800385808408880302900301980301a00300a803f701"
        "b50391ff9c45bd031c32c742c50300000000cd031c32c742d5031c32c742dd0300000000"
        "e50343a0c742ed0300acc742f00300f803008004008804bead0b9004b3870ba00402ad04"
        "00a05645"
    )
    MODULE_2 = (
        "081e2ae7020d00000000106318642a140000d8410000d8410000d8410000d0410000d041"
        "35007056453d00f0544540014da4708841555d1710bf5d94c35644650000000068007214"
        "00f05445001056450070564500e0554500d0554578028201105858585858585858585858"
        "58585858588801239001009d0100c02845a50100c02845ad010000d841b5010000e041bd"
        "010000d841c5010000d841cd0100000c42d001fcb99e05d8018bb1a105e001909513e801"
        "d98f13f5010000d841fd010000d04185020000d8418d020000d8419002009802ffff03a0"
        "0221a802a3bfafd506b5025b37c742bd020000c842c00220c80205d00264d80201e00200"
        "e80283d001f00201f802ab828408800385808408880302900301980301a00300a803f701"
        "b50391ff9c45bd031c32c742c50300000000cd031c32c742d5031c32c742dd0300000000"
        "e50343a0c742ed0300acc742f00300f803008004008804bead0b9004b3870ba00402ad04"
        "00f05445"
    )

    # A `254/46` record captured while the module was actually discharging
    # (~2.3 kW), not resting - the two frames above hit 0.0 W on field 1 and
    # cannot verify the mapping, its scale or its sign, however real they
    # are. Same 2026-09-07 RE11 recording, serial already masked in the
    # capture on file.
    MODULE_3 = (
        "081e2ae8020d24230dc5105718642a1400000442000004420000004200000042000000"
        "423500404c453d00104c4540014dec518241552b690bc35d86ce454465b8d80ac36800"
        "721400404c4500304c4500204c4500104c4500204c4578018201105858585858585858"
        "58585858585858588801229001009d0100009645a50100c08f45ad010000f041b50100"
        "001c42bd0100002c42c50100003442cd0100003042d001ecb28c05d801b28f8e05e001"
        "909513e801ee8f13f50100000442fd01000000428502000040428d0200003442900200"
        "9802ffff03a00221a802efd6fad406b50265ddae42bd020000c842c00200c80205d002"
        "64d80201e00200e80283d001f00201f802ab8284088003858084088803029003019803"
        "01a0039901a8038402b50328138945bd03689eae42c50300000000cd0365ddae42d503"
        "65ddae42dd035a2c783fe503b0a4c742ed0366aec742f00300f803008004008804d1fe"
        "0a9004bdda0aa00402ad0400304c45"
    )

    def test_decodes_a_real_frame(self) -> None:
        parsed = parse_ocean2_proto_message(
            _frame(bytes.fromhex(self.MODULE_1), cmd_id=46)
        )
        assert parsed is not None
        # State of charge moves, state of health does not - the pair that
        # was mapped the wrong way round before.
        assert parsed["module1_soc_pct"] == pytest.approx(99.598, abs=0.01)
        assert parsed["module1_soh_pct"] == pytest.approx(100.0)
        assert parsed["module1_remaining_wh"] == pytest.approx(5023.9, abs=0.1)
        assert parsed["module1_cycles"] == 35

    def test_the_cell_voltage_is_millivolts(self) -> None:
        # 3437 mV, not 3437 V. Five cells in series at 3.437 V make 17.19 V
        # against the 17.11 V the module reports as its own voltage - which
        # is both the scale check and the proof that 5.6 is a cell.
        parsed = parse_ocean2_proto_message(
            _frame(bytes.fromhex(self.MODULE_1), cmd_id=46)
        )
        assert parsed is not None
        cell_mv = parsed["module1_cell_voltage_mv"]
        pack_v = parsed["module1_voltage_v"]
        assert cell_mv == pytest.approx(3437.0)
        assert 5 * cell_mv / 1000 == pytest.approx(pack_v, abs=0.2)

    def test_reads_a_real_bundle_as_two_modules(self) -> None:
        # The bundle size is not the module count: a frame carries one header
        # per heartbeat, and a capture with bundles of fourteen still held
        # only two distinct module numbers.
        payload = _frame(bytes.fromhex(self.MODULE_1), cmd_id=46) + _frame(
            bytes.fromhex(self.MODULE_2), cmd_id=46
        )
        parsed = parse_ocean2_proto_message(payload)
        assert parsed is not None
        assert parsed["module1_soc_pct"] == pytest.approx(99.598, abs=0.01)
        assert parsed["module2_soc_pct"] == pytest.approx(99.608, abs=0.01)
        assert parsed["module1_voltage_v"] != parsed["module2_voltage_v"]

    def test_the_power_reading_holds_up_while_the_module_is_loaded(self) -> None:
        # MODULE_1 and MODULE_2 are both a resting pack (field 1 is 0.0 in
        # both), which cannot verify a power mapping, its scale, or its sign
        # - MODULE_3 is a real record from the same capture with the module
        # under ~2.3 kW load.
        parsed = parse_ocean2_proto_message(
            _frame(bytes.fromhex(self.MODULE_3), cmd_id=46)
        )
        assert parsed is not None
        power = parsed["module1_power_w"]
        voltage = parsed["module1_voltage_v"]
        current = parsed["module1_current_a"]

        # V x I hits the reported power within 1%.
        assert voltage * current == pytest.approx(power, rel=0.01)

        # Signed like the system reading: positive charges, negative
        # discharges. A negative current here (power flowing out of the
        # module) has to land on a negative power.
        assert current < 0
        assert power < 0
