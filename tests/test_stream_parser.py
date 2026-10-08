"""Tests for the Stream protobuf telemetry parser."""

from __future__ import annotations

import json
import struct
from collections.abc import Callable
from pathlib import Path

import pytest
from ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import (
    _decode_scalar as _decode_scalar_ac5000,
)
from ecoflow_energy.ecoflow.parsers.stream_proto import (
    _STREAM_FIELD_MAP,
    _TYPE_FLOAT,
    _TYPE_INT,
    SOC_FALLBACK_KEY,
    _decode_mapped_fields,
    _decode_scalar,
    parse_stream_proto_message,
)
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


def _build_masked_frame(cmd_func: int, cmd_id: int, inner: bytes, seq: int) -> bytes:
    """Build a frame whose payload is masked the way a real BK01 masks it.

    ``enc_type = 1`` plus the low byte of the header's own sequence number as
    the XOR key, which is what every frame of the tracked capture carries.
    """
    masked = bytes(value ^ (seq & 0xFF) for value in inner)
    header = bytearray()
    header.extend(encode_field_bytes(1, masked))
    header.extend(encode_field_varint(6, 1))  # enc_type
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    header.extend(encode_field_varint(14, seq))
    return encode_field_bytes(1, bytes(header))


class TestStreamProtoParser:
    @pytest.mark.parametrize(
        ("pv2", "pv3", "pv4", "solar"),
        [
            # Two telemetry frames from a four-input Stream with strings 2-4
            # wired (#139): the three string powers add up to the solar total.
            (111.9, 111.92, 113.53, 335.0),
            (235.06, 259.86, 269.44, 763.0),
        ],
    )
    def test_strings_3_and_4_sum_to_the_solar_total(
        self, pv2: float, pv3: float, pv4: float, solar: float
    ) -> None:
        inner = bytearray()
        inner.extend(_encode_fixed32_field(70, pv2))
        inner.extend(_encode_fixed32_field(996, pv3))
        inner.extend(_encode_fixed32_field(997, pv4))
        inner.extend(_encode_fixed32_field(517, solar))

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert result["pv3_w"] == pytest.approx(pv3, rel=1e-5)
        assert result["pv4_w"] == pytest.approx(pv4, rel=1e-5)
        string_sum = result["pv2_w"] + result["pv3_w"] + result["pv4_w"]
        assert string_sum == pytest.approx(result["solar_w"], rel=0.01)

    def test_frame_without_strings_3_and_4_creates_no_keys_for_them(self) -> None:
        """A frame without 996/997 must not gain keys for them."""
        inner = _encode_fixed32_field(70, 19.0) + _encode_fixed32_field(361, 20.0)

        result = parse_stream_proto_message(_build_frame(254, 21, inner))

        assert "pv3_w" not in result
        assert "pv4_w" not in result

    def test_system_soc_feeds_battery_soc_and_unit_soc_stays_apart(self) -> None:
        """Field 262 is the linked system's SoC, field 242 this unit's own.

        Two Stream units on a parallel cable report both, and they differ
        whenever the units are unbalanced. The battery sensor is the system
        figure, which is what the app shows for the pair; the unit's own
        reading gets its own key (#323).
        """
        inner = bytearray()
        inner.extend(_encode_fixed32_field(242, 40.0))
        inner.extend(_encode_fixed32_field(262, 55.0))

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert result["soc_pct"] == 55
        assert result["unit_soc_pct"] == 40

    def test_system_soc_wins_regardless_of_field_order(self) -> None:
        """Whichever of 242/262 the frame carries last must not decide."""
        inner = bytearray()
        inner.extend(_encode_fixed32_field(262, 55.0))
        inner.extend(_encode_fixed32_field(242, 40.0))

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert result["soc_pct"] == 55
        assert result["unit_soc_pct"] == 40

    def test_unit_soc_alone_is_offered_as_stand_in_not_promoted(self) -> None:
        """A frame with only field 242 offers the unit figure for `soc_pct`.

        The parser must not promote it itself: it sees one frame at a time,
        and promoting here would let a BMS-only frame overwrite the system
        figure of the frame before. The coordinator decides (#323).
        """
        inner = bytearray()
        inner.extend(_encode_fixed32_field(242, 40.0))

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert "soc_pct" not in result
        assert result["unit_soc_pct"] == 40
        assert result[SOC_FALLBACK_KEY] == 40

    def test_stand_in_prefers_the_unit_figure_over_the_precise_one(self) -> None:
        """Field 242 outranks the auxiliary frame's precise BMS figure, and
        the stand-in is a whole percent either way."""
        aux = _build_frame(32, 50, _encode_fixed32_field(25, 40.2))
        main = _build_frame(254, 21, _encode_fixed32_field(242, 41.0))

        result = parse_stream_proto_message(aux + main)

        assert result[SOC_FALLBACK_KEY] == 41
        assert isinstance(result[SOC_FALLBACK_KEY], int)
        assert result["soc_precise_pct"] == pytest.approx(40.2, rel=1e-5)

    def test_system_figure_leaves_no_stand_in(self) -> None:
        inner = bytearray()
        inner.extend(_encode_fixed32_field(242, 40.0))
        inner.extend(_encode_fixed32_field(262, 55.0))

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert SOC_FALLBACK_KEY not in result

    def test_parse_main_status_frame(self) -> None:
        inner = bytearray()
        inner.extend(encode_field_varint(242, 21))
        inner.extend(encode_field_varint(270, 95))
        inner.extend(encode_field_varint(271, 15))
        # Outlet state comes from the relay fields only. 380/381 are the PV
        # string 1 voltage and current on this frame.
        inner.extend(encode_field_varint(980, 1))
        inner.extend(encode_field_varint(982, 0))
        inner.extend(_encode_fixed32_field(380, 28.43))
        inner.extend(_encode_fixed32_field(381, 0.669))
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
        assert result["unit_soc_pct"] == 21
        assert result["max_charge_soc_pct"] == 95
        assert result["min_discharge_soc_pct"] == 15
        assert result["ac_outlet_1_enabled"] == 1
        assert result["ac_outlet_2_enabled"] == 0
        assert result["pv_voltage_v"] == pytest.approx(28.43, rel=1e-5)
        assert result["pv_current_a"] == pytest.approx(0.669, rel=1e-5)
        assert result["grid_w"] == pytest.approx(1351.0, rel=1e-5)
        assert result["home_w"] == pytest.approx(309.5, rel=1e-5)
        assert result["solar_w"] == 0.0
        assert result["backup_reserve_pct"] == 20
        # Field 518 is a group aggregate that one unit of a group publishes;
        # it must never become this unit's battery power (#486). The per-unit
        # value comes from the BMS frame, see TestStreamUnitBatteryPower.
        assert "batt_w" not in result
        assert "batt_charge_power_w" not in result
        assert "batt_discharge_power_w" not in result
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
        # The precise BMS figure is offered as a whole-percent stand-in for
        # the battery sensor, never written to `soc_pct` by the parser.
        assert "soc_pct" not in result
        assert result[SOC_FALLBACK_KEY] == 22
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

    def test_zero_capacity_totals_are_not_published(self) -> None:
        """A factory-new or reset BMS reports 0 on both lifetime counters.

        The protocol declares fields 50/51 with explicit presence, so the
        zero arrives on the wire instead of being omitted. Both sensors are
        total_increasing, and Home Assistant reads a 0 there as a meter
        reset, booking the standing total a second time - the key has to be
        absent, not 0.0 and not None.
        """
        aux = bytearray()
        aux.extend(_encode_fixed32_field(25, 21.2))
        aux.extend(encode_field_varint(32, 0))
        aux.extend(encode_field_varint(50, 0))
        aux.extend(encode_field_varint(51, 0))

        result = parse_stream_proto_message(_build_frame(32, 50, bytes(aux)))

        assert result is not None
        assert result["soc_precise_pct"] == pytest.approx(21.2, rel=1e-5)
        assert "batt_charge_capacity_ah" not in result
        assert "batt_discharge_capacity_ah" not in result

    def test_nonzero_capacity_totals_still_publish_scaled(self) -> None:
        """The zero guard must not eat a real reading."""
        aux = bytearray()
        aux.extend(encode_field_varint(50, 5503))
        aux.extend(encode_field_varint(51, 15270))

        result = parse_stream_proto_message(_build_frame(32, 50, bytes(aux)))

        assert result is not None
        assert result["batt_charge_capacity_ah"] == pytest.approx(5.503, rel=1e-5)
        assert result["batt_discharge_capacity_ah"] == pytest.approx(15.27, rel=1e-5)

    def test_zero_precise_totals_do_not_fall_back_to_a_zero_rounded_value(
        self,
    ) -> None:
        """Field 32 is the fallback source and reports 0 in the same state."""
        aux = bytearray()
        aux.extend(_encode_fixed32_field(25, 21.2))
        aux.extend(encode_field_varint(32, 0))

        result = parse_stream_proto_message(_build_frame(32, 50, bytes(aux)))

        assert result is not None
        assert "batt_charge_capacity_ah" not in result

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
        inner = bytearray()
        inner.extend(encode_field_varint(26, 0))
        inner.extend(encode_field_varint(27, 0))

        result = parse_stream_proto_message(_build_frame(32, 50, bytes(inner)))

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

    def test_field_602_is_wifi_rssi_and_never_battery_power(self):
        """602 carries the WiFi signal strength, not a battery power path.

        Promoting it to batt_w fed a permanent phantom discharge into the
        total_increasing battery energy counter, which no later correction
        can undo.
        """
        inner = _encode_fixed32_field(602, -68.0)

        result = parse_stream_proto_message(_build_frame(254, 21, inner))

        assert result is not None
        assert result["wifi_rssi_dbm"] == pytest.approx(-68.0, rel=1e-5)
        assert "batt_w" not in result
        assert "batt_charge_power_w" not in result
        assert "batt_discharge_power_w" not in result

    def test_field_602_beside_518_gives_wifi_and_no_battery_power(self):
        """602 and 518 in one frame: the WiFi reading lands, no battery key does.

        518 is the group aggregate and is not a per-unit battery reading (#486),
        and 602 is a radio reading, so neither may reach a battery key.
        """
        inner = bytearray()
        inner.extend(_encode_fixed32_field(518, -300.0))
        inner.extend(_encode_fixed32_field(602, -66.0))

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert result is not None
        assert result["wifi_rssi_dbm"] == pytest.approx(-66.0, rel=1e-5)
        assert "batt_w" not in result
        assert "batt_charge_power_w" not in result
        assert "batt_discharge_power_w" not in result

    def test_outlet_state_comes_from_relay_fields_only(self):
        """980/982 are the sole source for the outlet flags.

        380/381 sit in the same frame as floats (PV string 1 voltage and
        current) and must not be able to flip an outlet.
        """
        inner = bytearray()
        inner.extend(_encode_fixed32_field(380, 28.43))
        inner.extend(encode_field_varint(980, 1))
        inner.extend(_encode_fixed32_field(381, 0.669))
        inner.extend(encode_field_varint(982, 0))

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert result is not None
        assert result["ac_outlet_1_enabled"] == 1
        assert result["ac_outlet_2_enabled"] == 0
        assert result["pv_voltage_v"] == pytest.approx(28.43, rel=1e-5)
        assert result["pv_current_a"] == pytest.approx(0.669, rel=1e-5)

    def test_config_write_reply_does_not_carry_outlet_state(self):
        """The config-write reply echoes the requested target, not the live
        relay state, so its outlet fields stay unmapped (as with LED 384)."""
        inner = bytearray()
        inner.extend(encode_field_varint(102, 80))
        inner.extend(encode_field_varint(380, 1))
        inner.extend(encode_field_varint(381, 1))

        result = parse_stream_proto_message(_build_frame(254, 18, bytes(inner)))

        assert result is not None
        assert result["backup_reserve_pct"] == 80
        assert "ac_outlet_1_enabled" not in result
        assert "ac_outlet_2_enabled" not in result
        assert "pv_voltage_v" not in result

    def test_grid_connection_state_maps_to_enum_label(self):
        inner = encode_field_varint(619, 3)

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert result is not None
        assert result["grid_connection_state"] == "feed_grid"

    def test_unknown_grid_connection_state_is_none_not_a_raw_int(self):
        """An unmapped enum value must reach the sensor as None. A raw int
        is outside the options list and makes HA raise on every write."""
        inner = encode_field_varint(619, 99)

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert result is not None
        assert result["grid_connection_state"] is None
        assert "_grid_connection_state_raw" not in result


BK11_GROUP_CAPTURE = (
    Path(__file__).parent / "fixtures" / "stream" / "bk11_group_battery_power.json"
)


class TestStreamGroupCaptureReplay:
    """Real frames from three Stream Ultra (BK11) in one group, issue #486."""

    @staticmethod
    def _parsed(label: str) -> dict:
        frames = json.loads(BK11_GROUP_CAPTURE.read_text())["frames"]
        frame = next(f for f in frames if f["label"] == label)
        result = parse_stream_proto_message(bytes.fromhex(frame["hex"]))
        assert result is not None
        return result

    @pytest.mark.parametrize(
        ("label", "charge", "discharge"),
        [
            ("follower_bms_discharge", 0.0, 843.0),
            ("follower_bms_charge", 532.0, 0.0),
            ("leader_bms_idle", 0.0, 2.0),
        ],
    )
    def test_bms_frames_carry_both_directions(
        self, label: str, charge: float, discharge: float
    ) -> None:
        result = self._parsed(label)

        assert result["batt_charge_power_w"] == charge
        assert result["batt_discharge_power_w"] == discharge
        assert result["batt_w"] == charge - discharge
        # The unit's own direction for the per-unit `f54` entries.
        assert result["_bms_batt_w"] == charge - discharge

    @pytest.mark.parametrize(
        ("label", "cycles"),
        [
            ("follower_bms_discharge", 59),
            ("follower_bms_charge", 158),
            ("leader_bms_idle", 67),
        ],
    )
    def test_cycles_follow_the_lifetime_counters(self, label: str, cycles: int) -> None:
        """Field 14 is the BMS cycle count, not a field read by position.

        It sits within one cycle of the lifetime throughput over the pack
        capacity, which is what a cycle count is: 5,877 Ah out of a 100 Ah
        pack against 59.
        """
        result = self._parsed(label)

        assert result["bms_cycles"] == cycles
        throughput_ah = min(
            result["batt_charge_capacity_ah"], result["batt_discharge_capacity_ah"]
        )
        full_ah = result["batt_full_cap_mah"] / 1000
        assert abs(throughput_ah / full_ah - cycles) < 1


BK01_CAPTURE = (
    Path(__file__).parent / "fixtures" / "stream" / "bk01_capture_masked.json"
)

# Battery keys the Stream Micro must never produce. Any of these appearing in
# a parsed capture frame means a non-battery field leaked into a battery path.
_BATTERY_KEYS = (
    "batt_w",
    "batt_charge_power_w",
    "batt_discharge_power_w",
    "soc_pct",
    "soc_precise_pct",
    "bms_soh_pct",
    "backup_reserve_pct",
)


def _load_bk01_frames() -> list[dict]:
    return json.loads(BK01_CAPTURE.read_text())["frames"]


class TestStreamMicroCaptureReplay:
    """Replay of a real BK01 (Stream Micro) capture from issue #141.

    The frames go through the production entry point, so the test covers the
    header decode, the encrypted-payload handling and the field map together.
    A test-local decoder would prove none of that.
    """

    def test_every_captured_frame_parses(self) -> None:
        frames = _load_bk01_frames()
        assert len(frames) == 24

        parsed = [
            parse_stream_proto_message(bytes.fromhex(frame["hex"])) for frame in frames
        ]
        assert all(result is not None for result in parsed)

    def test_full_upload_values_match_the_device(self) -> None:
        """The periodic full upload, decoded field by field."""
        frames = _load_bk01_frames()
        # Frame 13 is one of the two 195-byte full uploads in the capture.
        result = parse_stream_proto_message(bytes.fromhex(frames[13]["hex"]))

        assert result is not None
        assert result["ac_voltage_v"] == pytest.approx(234.3, rel=1e-5)
        assert result["ac_frequency_hz"] == pytest.approx(50.01, rel=1e-5)
        assert result["ac_current_a"] == pytest.approx(0.16, rel=1e-3)
        assert result["wifi_rssi_dbm"] == pytest.approx(-68.0, rel=1e-5)
        assert result["pv_voltage_v"] == pytest.approx(28.43, rel=1e-4)
        assert result["pv_current_a"] == pytest.approx(0.669, rel=1e-3)
        assert result["pv2_voltage_v"] == pytest.approx(28.6, rel=1e-4)
        assert result["pv2_current_a"] == pytest.approx(0.688, rel=1e-3)
        assert result["pv1_w"] == pytest.approx(19.02, rel=1e-3)
        assert result["pv2_w"] == pytest.approx(19.679, rel=1e-3)
        assert result["grid_connection_power_w"] == pytest.approx(38.7, rel=1e-3)
        assert result["feed_grid_power_limit_w"] == 800
        assert result["grid_connection_state"] == "feed_grid"

    def test_pv_voltage_times_current_is_pv_power(self) -> None:
        """The two corrected keys multiply out to the PV power in the same
        frame. That is what proves 380/381 are a voltage and a current, and
        not the outlet flags they used to be mapped to."""
        frames = _load_bk01_frames()

        for index in (13, 21):  # the two full uploads
            result = parse_stream_proto_message(bytes.fromhex(frames[index]["hex"]))
            assert result is not None
            assert result["pv_voltage_v"] * result["pv_current_a"] == pytest.approx(
                result["pv1_w"], rel=1e-2
            )
            assert result["pv2_voltage_v"] * result["pv2_current_a"] == pytest.approx(
                result["pv2_w"], rel=1e-2
            )

    def test_capture_produces_no_battery_or_outlet_keys(self) -> None:
        """Six hours of a battery-less inverter must not yield one battery
        reading. Field 602 (WiFi RSSI) used to become a phantom discharge."""
        for frame in _load_bk01_frames():
            result = parse_stream_proto_message(bytes.fromhex(frame["hex"]))
            assert result is not None
            for key in _BATTERY_KEYS:
                assert key not in result, f"{key} leaked from frame at {frame['ts']}"
            assert "ac_outlet_1_enabled" not in result
            assert "ac_outlet_2_enabled" not in result

    def test_wifi_rssi_is_reported_as_its_own_reading(self) -> None:
        frames = _load_bk01_frames()
        values = []
        for frame in frames:
            result = parse_stream_proto_message(bytes.fromhex(frame["hex"]))
            assert result is not None
            if "wifi_rssi_dbm" in result:
                values.append(result["wifi_rssi_dbm"])

        assert values, "capture carries field 602 and it must be parsed"
        assert all(-100.0 <= value <= 0.0 for value in values)


class TestMaskedPayloadIsNeverRetriedRaw:
    """A masked payload is decoded once, with its own key, and never again.

    Most of what a Stream Micro sends carries only fields this map does not
    have, so an empty decode is the normal outcome of a correct unmasking and
    not a failure. Retrying the still-masked bytes after an empty result turned
    those frames into random readings: measured against the (254, 21) map,
    arbitrary bytes decode to a plausible-looking value in about two of every
    thousand attempts, and nothing downstream can tell such a value from a real
    one.
    """

    # Field 133 (the device's UTC offset) is not mapped, so the correctly
    # unmasked payload yields nothing. Masked with the low byte of its own
    # sequence number the same bytes decode to a state of charge of 3280 % on
    # a device that has no battery at all.
    _UNMAPPED_INNER = encode_field_varint(133, 200)
    _SEQ = 4632  # low byte 24

    # Second pairing, a different field and a different key: an unmapped grid
    # code revision that decodes to a WiFi signal of +8985 dBm when masked.
    _UNMAPPED_INNER_2 = encode_field_varint(731, 10001)
    _SEQ_2 = 4616  # low byte 8

    def test_a_frame_of_unmapped_fields_publishes_nothing(self) -> None:
        frame = _build_masked_frame(254, 21, self._UNMAPPED_INNER, self._SEQ)

        assert parse_stream_proto_message(frame) is None

    def test_a_second_unmapped_frame_publishes_nothing_either(self) -> None:
        frame = _build_masked_frame(254, 21, self._UNMAPPED_INNER_2, self._SEQ_2)

        assert parse_stream_proto_message(frame) is None

    def test_those_frames_really_are_false_accept_candidates(self) -> None:
        """Guard for the two tests above.

        They only prove anything while the masked bytes still decode to a
        value. If the field map ever changes so that they do not, the tests
        would keep passing for the wrong reason and stop protecting anything.
        """
        field_map = _STREAM_FIELD_MAP[(254, 21)]

        for inner, seq, expected in (
            (self._UNMAPPED_INNER, self._SEQ, {"soc_pct": 3280}),
            (self._UNMAPPED_INNER_2, self._SEQ_2, {"wifi_rssi_dbm": 8985.0}),
        ):
            masked = bytes(value ^ (seq & 0xFF) for value in inner)
            assert _decode_mapped_fields(inner, field_map) == {}
            assert _decode_mapped_fields(masked, field_map) == expected

    def test_a_masked_frame_with_a_mapped_field_still_parses(self) -> None:
        """The masking path itself is untouched by the fallback removal."""
        inner = _encode_fixed32_field(615, 50.01)
        frame = _build_masked_frame(254, 21, inner, seq=4632)

        result = parse_stream_proto_message(frame)

        assert result is not None
        assert result["ac_frequency_hz"] == pytest.approx(50.01, rel=1e-5)

    def test_a_truncated_header_does_not_discard_the_rest_of_a_bundle(self) -> None:
        """One unreadable message in a bundle costs only that message.

        Decode errors used to escape to the frame-level guard and drop every
        header of the bundle; they are now caught per payload, so the intact
        messages still contribute.
        """
        truncated = _build_frame(254, 21, encode_varint((615 << 3) | 0) + b"\x80")
        intact = _build_frame(32, 50, _encode_fixed32_field(25, 21.6))

        result = parse_stream_proto_message(truncated + intact)

        assert result is not None
        assert result["soc_precise_pct"] == pytest.approx(21.6, rel=1e-5)
        assert "ac_frequency_hz" not in result


class TestStreamSignedVarint:
    """Negative int32/int64 varints must decode to negative numbers."""

    def test_negative_varint_batt_temp(self) -> None:
        # (32, 50) field 9 = batt_temp_c, encoded as 64-bit two's complement
        inner = encode_field_varint(9, (1 << 64) - 5)  # -5 C
        result = parse_stream_proto_message(_build_frame(32, 50, bytes(inner)))
        assert result is not None
        assert result["batt_temp_c"] == -5

    def test_negative_varint_float_target(self) -> None:
        # (254, 21) field 992 = sys_grid_connection_power_w (float target) as
        # negative varint
        inner = encode_field_varint(992, (1 << 64) - 300)  # -300 W
        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))
        assert result is not None
        assert result["sys_grid_connection_power_w"] == pytest.approx(-300.0)
        assert result["ac_grid_connection_power_w"] == pytest.approx(-300.0)

    def test_oversized_varint_returns_none(self) -> None:
        # Field 9 tag followed by an 11-byte (>64-bit) varint
        inner = encode_varint((9 << 3) | 0) + b"\xff" * 11
        assert parse_stream_proto_message(_build_frame(32, 50, inner)) is None

    def test_truncated_inner_returns_none(self) -> None:
        inner = encode_varint((9 << 3) | 0) + b"\x80"
        assert parse_stream_proto_message(_build_frame(32, 50, inner)) is None


_BATTERY_POWER_KEYS = ("batt_w", "batt_charge_power_w", "batt_discharge_power_w")


def _bms_frame(
    voltage_mv: int,
    current_ma: int | None,
    charge_w: int | None,
    discharge_w: int | None,
) -> bytes:
    """Build a cmd 32/50 BMS frame; ``None`` leaves a field out entirely."""
    inner = bytearray(encode_field_varint(7, voltage_mv))
    if current_ma is not None:
        # Signed pack current as two's complement, the way the device sends it.
        inner.extend(encode_field_varint(8, current_ma & ((1 << 64) - 1)))
    if charge_w is not None:
        inner.extend(encode_field_varint(26, charge_w))
    if discharge_w is not None:
        inner.extend(encode_field_varint(27, discharge_w))
    return _build_frame(32, 50, bytes(inner))


class TestStreamUnitBatteryPower:
    """Per-unit battery power comes from the BMS frame, not from field 518 (#486).

    Field 518 of cmd 254/21 is a group aggregate that only one unit of a
    multi-unit group publishes. Every other unit sends 0.0, so its battery
    power stayed at 0 W while it charged or discharged. Cmd 32/50 carries the
    unit's own pack power in fields 26 (charge) and 27 (discharge).
    """

    def test_discharge_frame_gives_negative_battery_power(self) -> None:
        result = parse_stream_proto_message(_bms_frame(20343, -43523, 0, 885))

        assert result is not None
        assert result["batt_w"] == -885.0
        assert result["batt_charge_power_w"] == 0.0
        assert result["batt_discharge_power_w"] == 885.0
        # The private staging keys never reach the published data.
        assert "_batt_charge_power_w" not in result
        assert "_batt_discharge_power_w" not in result

    def test_charge_frame_gives_positive_battery_power(self) -> None:
        result = parse_stream_proto_message(_bms_frame(20017, 17467, 349, 0))

        assert result is not None
        assert result["batt_w"] == 349.0
        assert result["batt_charge_power_w"] == 349.0
        assert result["batt_discharge_power_w"] == 0.0

    def test_idle_frame_keeps_the_small_discharge_reading(self) -> None:
        result = parse_stream_proto_message(_bms_frame(20100, None, 0, 2))

        assert result is not None
        assert result["batt_w"] == -2.0
        assert result["batt_charge_power_w"] == 0.0
        assert result["batt_discharge_power_w"] == 2.0

    @pytest.mark.parametrize(
        ("charge_w", "discharge_w"),
        [(349, None), (None, 885)],
        ids=["only-charge", "only-discharge"],
    )
    def test_one_direction_alone_sets_no_battery_power(
        self, charge_w: int | None, discharge_w: int | None
    ) -> None:
        result = parse_stream_proto_message(
            _bms_frame(20017, 17467, charge_w, discharge_w)
        )

        assert result is not None
        for key in _BATTERY_POWER_KEYS:
            assert key not in result
        assert "_batt_charge_power_w" not in result
        assert "_batt_discharge_power_w" not in result
        # The rest of the BMS frame still lands.
        assert result["batt_voltage_v"] == pytest.approx(20.017)

    def test_follower_telemetry_frame_publishes_no_battery_power(self) -> None:
        """A follower sends 518 = 0.0 while the group moves 791 W (#486)."""
        inner = bytearray()
        inner.extend(_encode_fixed32_field(518, 0.0))
        inner.extend(_encode_fixed32_field(616, 791.04))
        inner.extend(_encode_fixed32_field(1210, 44.71))

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert result is not None
        for key in _BATTERY_POWER_KEYS:
            assert key not in result
        assert result["ac_grid_connection_power_w"] == pytest.approx(791.04, rel=1e-5)
        assert result["ac_outlet_1_w"] == pytest.approx(44.71, rel=1e-5)

    def test_leader_aggregate_518_does_not_become_battery_power(self) -> None:
        """The one unit that does publish 518 gets no battery power from it."""
        inner = bytearray()
        inner.extend(_encode_fixed32_field(518, -600.0))
        inner.extend(_encode_fixed32_field(616, 600.0))

        result = parse_stream_proto_message(_build_frame(254, 21, bytes(inner)))

        assert result is not None
        for key in _BATTERY_POWER_KEYS:
            assert key not in result
        assert result["ac_grid_connection_power_w"] == pytest.approx(600.0, rel=1e-5)


_SYSTEM_KEYS = (
    "grid_w",
    "home_w",
    "solar_w",
    "sys_grid_connection_power_w",
    "home_from_batt_w",
    "home_from_grid_w",
)


def _status_frame(fields: dict[int, float]) -> bytes:
    """Build a cmd 254/21 frame of float fields, in the given order."""
    inner = bytearray()
    for field_number, value in fields.items():
        inner.extend(_encode_fixed32_field(field_number, value))
    return _build_frame(254, 21, bytes(inner))


# Values of the 14:11:28 frames in the first #486 diagnostics download, both
# from the same second. FOLLOWER is the unit the reporter calls 1724 while it
# discharged; LEADER is the unit that fills the system block.
_FOLLOWER_FIELDS: dict[int, float] = {
    262: 0.0, 242: 99.0, 515: 0.0, 516: 0.0, 517: 0.0, 518: 0.0,
    616: 791.0, 992: 0.0, 361: 0.0, 70: 0.0, 1003: 0.0, 1004: 0.0,
    1210: 44.7,
}  # fmt: skip
_LEADER_FIELDS: dict[int, float] = {
    262: 99.0, 242: 99.0, 515: 172.0, 516: 1004.9, 517: 579.0, 518: -253.9,
    616: -73.7, 992: 705.9, 1003: 253.9, 1004: 172.0, 1210: 51.4,
}  # fmt: skip


class TestStreamFollowerSystemBlock:
    """Only one unit of a group fills the system block of 254/21 (#486).

    The other units send 515-518, 262, 992, 1003 and 1004 as 0.0 in every
    frame, in the #486 downloads (3 x BK11) as in the #323 pair (BK61 + BK31).
    Those zeros are not readings: house load, solar and grid of the system
    are not 0 W while the leader reports 1005 W, 579 W and 172 W.
    """

    def test_follower_frame_clears_the_system_readings(self) -> None:
        result = parse_stream_proto_message(_status_frame(_FOLLOWER_FIELDS))

        assert result is not None
        for key in _SYSTEM_KEYS:
            assert key in result
            assert result[key] is None, key

    def test_follower_grid_connection_is_its_own(self) -> None:
        """992 = 0.0 used to win over 616 = 791 W on the full frame."""
        result = parse_stream_proto_message(_status_frame(_FOLLOWER_FIELDS))

        assert result is not None
        assert result["ac_grid_connection_power_w"] == pytest.approx(791.0)
        assert result["grid_connection_power_w"] == pytest.approx(791.0)

    def test_follower_keeps_its_unit_readings(self) -> None:
        result = parse_stream_proto_message(_status_frame(_FOLLOWER_FIELDS))

        assert result is not None
        assert result["unit_soc_pct"] == 99
        assert result["ac_outlet_1_w"] == pytest.approx(44.7, rel=1e-5)
        assert result["pv1_w"] == 0.0
        # The system figure stays for the coordinator to resolve (#336).
        assert result["soc_pct"] == 0

    def test_leader_frame_keeps_the_system_readings(self) -> None:
        result = parse_stream_proto_message(_status_frame(_LEADER_FIELDS))

        assert result is not None
        assert result["grid_w"] == pytest.approx(172.0)
        assert result["home_w"] == pytest.approx(1004.9, rel=1e-5)
        assert result["solar_w"] == pytest.approx(579.0)
        assert result["home_from_batt_w"] == pytest.approx(253.9, rel=1e-5)
        assert result["home_from_grid_w"] == pytest.approx(172.0)
        assert result["ac_grid_connection_power_w"] == pytest.approx(705.9, rel=1e-5)

    def test_empty_battery_keeps_its_zeros(self) -> None:
        """Both figures at zero: nothing says the zeros are placeholders."""
        fields = {**_FOLLOWER_FIELDS, 242: 0.0}

        result = parse_stream_proto_message(_status_frame(fields))

        assert result is not None
        assert result["home_w"] == 0.0
        assert result["grid_w"] == 0.0
        assert result["ac_grid_connection_power_w"] == 0.0

    def test_frame_without_a_system_figure_is_untouched(self) -> None:
        """An incremental frame without 262 gives no basis to clear anything."""
        fields = {k: v for k, v in _FOLLOWER_FIELDS.items() if k != 262}

        result = parse_stream_proto_message(_status_frame(fields))

        assert result is not None
        assert result["home_w"] == 0.0
        assert result["sys_grid_connection_power_w"] == 0.0

    def test_zeros_without_a_unit_figure_in_the_frame_stay(self) -> None:
        """The parser sees one frame; `_resolve_soc` also sees earlier ones.

        A frame with 262 = 0 but no 242 gives no unit figure to compare with,
        so the zeros are published as 0 W. No captured frame has this shape.
        """
        fields = {k: v for k, v in _FOLLOWER_FIELDS.items() if k != 242}

        result = parse_stream_proto_message(_status_frame(fields))

        assert result is not None
        assert result["home_w"] == 0.0
        assert result["grid_w"] == 0.0
        assert result["sys_grid_connection_power_w"] == 0.0

    def test_follower_frame_whose_system_figure_mirrors_its_own_stays(self) -> None:
        """The 12:26:50 frame of a follower in the #486 downloads: 262 = 242 = 97.

        Both figures read 97 while 515-518, 992, 1003 and 1004 stay 0.0.
        Nothing in one frame tells this from an idle leader (262 = 99 and
        515 = 0.0 at 15:10:37 of the same download), so it is not cleared and
        that follower reads 0 W for the one update.
        """
        fields = {**_FOLLOWER_FIELDS, 262: 97.0, 242: 97.0}

        result = parse_stream_proto_message(_status_frame(fields))

        assert result is not None
        assert result["home_w"] == 0.0
        assert result["grid_w"] == 0.0


@pytest.mark.parametrize("decode_scalar", [_decode_scalar, _decode_scalar_ac5000])
@pytest.mark.parametrize("wire_type, fmt", [(5, "<f"), (1, "<d")])
@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_a_non_finite_float_has_no_integer_reading(
    decode_scalar: Callable[[int, bytes, str], float | int | None],
    wire_type: int,
    fmt: str,
    bad: float,
) -> None:
    """Both scalar decoders skip the field instead of raising on `int()`."""
    raw = struct.pack(fmt, bad)
    assert decode_scalar(wire_type, raw, _TYPE_INT) is None
    # A float target keeps the value; the caller applies its own finite guard.
    assert repr(decode_scalar(wire_type, raw, _TYPE_FLOAT)) == repr(bad)
    # A finite float still rounds to its integer.
    assert decode_scalar(wire_type, struct.pack(fmt, 41.6), _TYPE_INT) == 42
