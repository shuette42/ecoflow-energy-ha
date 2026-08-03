"""Tests for the Delta 3 protobuf telemetry parser (Enhanced Mode).

Covers the three frames the device pushes on the app connection - the
main status frame (cmd_func=254, cmd_id=21), the battery heartbeat
(cmd_func=32, cmd_id=2) and the BMS heartbeat (cmd_func=32, cmd_id=50) -
end to end: encode a realistic frame, run it through the runtime decoder,
parse it, and compare the resulting sensor keys against the HTTP path.
The BMS frame is the exception to that last step: it has no HTTP
counterpart at all.
"""

from __future__ import annotations

from ecoflow_energy.ecoflow.parsers.delta3_http import parse_delta3_http_quota
from ecoflow_energy.ecoflow.parsers.delta3_proto import (
    parse_delta3_bms_heartbeat,
    parse_delta3_cms_heartbeat,
    parse_delta3_display_property,
)
from ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
    Delta3BmsHeartbeat,
    Delta3CmsHeartbeat,
    Delta3DisplayProperty,
)
from ecoflow_energy.ecoflow.proto.runtime import (
    decode_proto_runtime_frame,
    decode_proto_runtime_headers,
)
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

# Fictional serial number - never a real device.
DEVICE_SN = "D3M1TEST0001ABCD"


def _build_frame(cmd_func: int, cmd_id: int, inner: bytes) -> bytes:
    """Build a minimal HeaderMessage frame carrying one pdata payload."""
    header = bytearray()
    header.extend(encode_field_bytes(1, inner))
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    header.extend(encode_field_bytes(24, DEVICE_SN.encode()))
    return encode_field_bytes(1, bytes(header))


def _build_display_message() -> Delta3DisplayProperty:
    """A full status frame: charging from grid and solar, AC1 loaded."""
    msg = Delta3DisplayProperty()
    msg.pow_in_sum_w = 812.0
    msg.pow_out_sum_w = 231.0
    msg.pow_get_ac_in = 500.0
    msg.pow_get_pv = 210.0
    msg.pow_get_pv2 = 98.0
    msg.pow_get_12v = 24.0
    msg.pow_get_typec1 = 45.0
    msg.pow_get_typec2 = 0.0
    msg.pow_get_typec3 = 18.0
    msg.pow_get_qcusb1 = 10.0
    msg.pow_get_qcusb2 = 0.0
    msg.pow_get_ac_out_list.pow_get_ac_out_item.extend(
        [-120.0, 0.0, -35.0, 0.0, 0.0]
    )
    msg.pow_get_12v_list.pow_get_12v_item.extend([12.0, 6.0])
    msg.flow_info_ac_out = 14
    msg.flow_info_ac2_out = 4
    msg.flow_info_12v = 12
    msg.cms_batt_soc = 85.6
    msg.cms_chg_dsg_state = 2  # charging
    msg.cms_chg_rem_time = 143
    msg.cms_dsg_rem_time = 12927  # placeholder while charging
    msg.cms_max_chg_soc = 100
    msg.cms_min_dsg_soc = 5
    msg.backup_reverse_soc = 30
    msg.energy_backup_en = True
    msg.en_beep = False
    msg.xboost_en = True
    msg.bypass_out_disable = False
    return msg


# The same device state expressed the way the HTTP quota endpoint returns
# it. Used to prove Standard and Enhanced Mode produce identical keys.
EQUIVALENT_HTTP_QUOTA: dict = {
    "powInSumW": 812.0,
    "powOutSumW": 231.0,
    "powGetAcIn": 500.0,
    "powGetPv": 210.0,
    "powGetPv2": 98.0,
    "powGet12v": 24.0,
    "powGetTypec1": 45.0,
    "powGetTypec2": 0.0,
    "powGetTypec3": 18.0,
    "powGetQcusb1": 10.0,
    "powGetQcusb2": 0.0,
    "powGetAcOutList": {"powGetAcOutItem": [-120.0, 0.0, -35.0, 0.0, 0.0]},
    "powGet12vList": {"powGet12vItem": [12.0, 6.0]},
    "flowInfoAcOut": 14,
    "flowInfoAc2Out": 4,
    "flowInfo12v": 12,
    "cmsBattSoc": 85.6,
    "cmsChgDsgState": 2,
    "cmsChgRemTime": 143,
    "cmsDsgRemTime": 12927,
    "cmsMaxChgSoc": 100,
    "cmsMinDsgSoc": 5,
    "backupReverseSoc": 30,
    "energyBackupEn": True,
    "enBeep": False,
    "xboostEn": True,
    "bypassOutDisable": False,
}


def _decode(frame: bytes) -> dict:
    """Run a frame through the runtime decoder and strip internal flags."""
    result = decode_proto_runtime_frame(frame)
    return {k: v for k, v in result.mapped.items() if not k.startswith("_")}


class TestDisplayPropertyFrame:
    """Main status frame (cmd_func=254, cmd_id=21)."""

    def test_frame_is_routed_to_the_delta3_decoder(self):
        frame = _build_frame(254, 21, _build_display_message().SerializeToString())
        result = decode_proto_runtime_frame(frame)
        assert result.parse_path == "typed_runtime:delta3_display_property"
        assert result.mapped["_is_delta3_display"] is True

    def test_power_values(self):
        frame = _build_frame(254, 21, _build_display_message().SerializeToString())
        parsed = parse_delta3_display_property(_decode(frame))
        assert parsed["pow_in_sum_w"] == 812
        assert parsed["pow_out_sum_w"] == 231
        assert parsed["ac_in_w"] == 500
        assert parsed["pv1_in_w"] == 210
        assert parsed["pv2_in_w"] == 98
        assert parsed["dc_12v_out_w"] == 24
        assert parsed["typec1_w"] == 45
        assert parsed["typec3_w"] == 18
        assert parsed["usb_qc1_w"] == 10

    def test_per_outlet_power_and_anderson_total(self):
        frame = _build_frame(254, 21, _build_display_message().SerializeToString())
        parsed = parse_delta3_display_property(_decode(frame))
        # item[0] = AC1, item[2] = AC2, both reported as magnitudes.
        assert parsed["ac1_out_w"] == 120
        assert parsed["ac2_out_w"] == 35
        assert parsed["anderson_out_w"] == 18

    def test_flow_states(self):
        frame = _build_frame(254, 21, _build_display_message().SerializeToString())
        parsed = parse_delta3_display_property(_decode(frame))
        assert parsed["ac_out_flow"] == 1
        assert parsed["ac2_out_flow"] == 0  # value 4 = inactive
        assert parsed["dc_12v_out_flow"] == 1

    def test_battery_state_and_remaining_time_gating(self):
        frame = _build_frame(254, 21, _build_display_message().SerializeToString())
        parsed = parse_delta3_display_property(_decode(frame))
        assert parsed["cms_batt_soc"] == 86
        assert parsed["chg_dsg_state"] == "charging"
        assert parsed["chg_remain_time_min"] == 143
        # The placeholder for the inactive direction must never reach a sensor.
        assert parsed["dsg_remain_time_min"] is None

    def test_flags_and_soc_limits(self):
        frame = _build_frame(254, 21, _build_display_message().SerializeToString())
        parsed = parse_delta3_display_property(_decode(frame))
        assert parsed["max_charge_soc_pct"] == 100
        assert parsed["min_discharge_soc_pct"] == 5
        assert parsed["backup_reserve_soc_pct"] == 30
        assert parsed["backup_reserve_enabled"] == 1
        assert parsed["beeper_enabled"] == 0
        assert parsed["xboost_enabled"] == 1
        assert parsed["bypass_out_disabled"] == 0

    def test_zero_value_is_not_dropped(self):
        """A real 0 must reach the sensor, not be treated as 'not sent'.

        Grid disconnected means AC input power is exactly 0. Without
        presence tracking in the proto definition, that frame would leave
        the previous value on the sensor.
        """
        msg = Delta3DisplayProperty()
        msg.pow_get_ac_in = 0.0
        frame = _build_frame(254, 21, msg.SerializeToString())
        parsed = parse_delta3_display_property(_decode(frame))
        assert parsed["ac_in_w"] == 0

    def test_idle_state_is_not_dropped(self):
        """`cms_chg_dsg_state = 0` means idle and must reach the sensor.

        Idle is the most frequent state transition there is, and 0 is the
        only uint32 zero this frame carries. Without presence tracking the
        sensor would stay on "charging" after the charger is unplugged.
        """
        msg = Delta3DisplayProperty()
        msg.cms_chg_dsg_state = 0
        msg.cms_chg_rem_time = 12927
        msg.cms_dsg_rem_time = 12927
        frame = _build_frame(254, 21, msg.SerializeToString())
        parsed = parse_delta3_display_property(_decode(frame))
        assert parsed["chg_dsg_state"] == "idle"
        # Neither direction is running, so no runtime may be published.
        assert parsed["chg_remain_time_min"] is None
        assert parsed["dsg_remain_time_min"] is None

    def test_incremental_frame_emits_only_present_fields(self):
        """A 2 s delta frame carries a few fields and must not invent others."""
        msg = Delta3DisplayProperty()
        msg.pow_out_sum_w = 42.0
        frame = _build_frame(254, 21, msg.SerializeToString())
        parsed = parse_delta3_display_property(_decode(frame))
        assert parsed == {"pow_out_sum_w": 42}

    def test_remaining_time_without_state_is_not_emitted(self):
        """Without the direction flag the placeholder cannot be filtered."""
        msg = Delta3DisplayProperty()
        msg.cms_chg_rem_time = 12927
        frame = _build_frame(254, 21, msg.SerializeToString())
        parsed = parse_delta3_display_property(_decode(frame))
        assert "chg_remain_time_min" not in parsed


class TestCmsHeartbeatFrame:
    """Battery heartbeat (cmd_func=32, cmd_id=2)."""

    def _frame(self) -> bytes:
        msg = Delta3CmsHeartbeat()
        msg.v1p0.max_charge_soc = 90
        msg.v1p0.lcd_show_soc = 85
        msg.v1p0.f32_lcd_show_soc = 85.4
        msg.v1p0.chg_remain_time = 143
        msg.v1p0.dsg_remain_time = 12927
        msg.v1p0.min_dsg_soc = 10
        msg.v1p3.sys_chg_dsg_state = 2
        return _build_frame(32, 2, msg.SerializeToString())

    def test_frame_is_routed_to_the_delta3_decoder(self):
        result = decode_proto_runtime_frame(self._frame())
        assert result.parse_path == "typed_runtime:delta3_cms_heartbeat"
        assert result.mapped["_is_delta3_cms_heartbeat"] is True

    def test_only_soc_is_taken_from_the_heartbeat(self):
        parsed = parse_delta3_cms_heartbeat(_decode(self._frame()))
        assert parsed["cms_batt_soc"] == 85

    def test_soc_limits_are_not_taken_from_the_heartbeat(self):
        """Their meaning was only ever observed at the default 100/0.

        Both sources agreed there, but that is the extreme of the value
        range, where a differing semantic looks identical. Forwarding them
        would let a user-writable number flap at the 10 s heartbeat rate;
        the status frame delivers both limits anyway.
        """
        parsed = parse_delta3_cms_heartbeat(_decode(self._frame()))
        assert "max_charge_soc_pct" not in parsed
        assert "min_discharge_soc_pct" not in parsed

    def test_remaining_times_are_not_taken_from_the_heartbeat(self):
        """The direction flag in this frame uses a different enum."""
        parsed = parse_delta3_cms_heartbeat(_decode(self._frame()))
        assert "chg_remain_time_min" not in parsed
        assert "dsg_remain_time_min" not in parsed

    def test_empty_heartbeat_yields_nothing(self):
        frame = _build_frame(32, 2, Delta3CmsHeartbeat().SerializeToString())
        assert parse_delta3_cms_heartbeat(_decode(frame)) == {}


class TestBmsHeartbeat:
    """cmd_func=32, cmd_id=50 - the only source of battery health.

    Field values are taken from a live DELTA 3 Max Plus capture, so the
    scaling assertions below are measurements, not guesses.
    """

    def _message(self) -> Delta3BmsHeartbeat:
        msg = Delta3BmsHeartbeat()
        msg.soc = 99
        msg.vol = 53_070  # mV, 16 cells at ~3.33 V
        msg.amp = -70  # mA, discharging into a 3 W load
        msg.temp = 28
        msg.design_cap = 40_000
        msg.remain_cap = 39_522
        msg.full_cap = 39_995
        msg.cycles = 1
        msg.soh = 100
        msg.max_cell_vol = 3_330
        msg.min_cell_vol = 3_326
        msg.max_vol_diff = 4
        msg.max_cell_temp = 28
        msg.min_cell_temp = 26
        msg.max_mos_temp = 28
        msg.min_mos_temp = 27
        msg.cell_series_num = 16
        msg.all_err_code = 0
        msg.real_soh = 100.0
        msg.calendar_soh = 88.0
        msg.cycle_soh = 100.0
        msg.accu_chg_energy = 2_377  # Wh
        msg.accu_dsg_energy = 622  # Wh
        return msg

    def _frame(self) -> bytes:
        return _build_frame(32, 50, self._message().SerializeToString())

    def test_frame_is_routed_to_the_bms_decoder(self):
        result = decode_proto_runtime_frame(self._frame())
        assert result.parse_path == "typed_runtime:delta3_bms_heartbeat"
        assert result.mapped["_is_delta3_bms_heartbeat"] is True

    def test_health_fields_reach_their_sensors(self):
        parsed = parse_delta3_bms_heartbeat(_decode(self._frame()))
        assert parsed["bms_soh_pct"] == 100.0
        assert parsed["bms_cycles"] == 1.0
        assert parsed["bms_calendar_soh_pct"] == 88.0
        assert parsed["bms_cycle_soh_pct"] == 100.0
        assert parsed["bms_cell_count"] == 16.0

    def test_voltage_and_current_are_scaled_from_milli_units(self):
        """53070 mV over 16 cells is 53.07 V, and -70 mA is -0.07 A.

        The capture pins both: the cells read ~3327 mV each, and the device
        reported 3 W of output at that voltage, which is 57 mA.
        """
        parsed = parse_delta3_bms_heartbeat(_decode(self._frame()))
        assert parsed["bms_voltage_v"] == 53.07
        assert parsed["bms_current_a"] == -0.07

    def test_cell_and_capacity_diagnostics_pass_through_unscaled(self):
        parsed = parse_delta3_bms_heartbeat(_decode(self._frame()))
        assert parsed["bms_max_cell_vol_mv"] == 3330.0
        assert parsed["bms_min_cell_vol_mv"] == 3326.0
        assert parsed["bms_cell_vol_diff_mv"] == 4.0
        assert parsed["bms_remain_cap_mah"] == 39522.0
        assert parsed["bms_full_cap_mah"] == 39995.0
        assert parsed["bms_design_cap_mah"] == 40000.0
        assert parsed["bms_max_cell_temp_c"] == 28.0
        assert parsed["bms_min_cell_temp_c"] == 26.0

    def test_lifetime_counters_are_converted_to_kwh(self):
        """Read from the BMS, not integrated - the one exception here."""
        parsed = parse_delta3_bms_heartbeat(_decode(self._frame()))
        assert parsed["bms_accu_chg_energy_kwh"] == 2.377
        assert parsed["bms_accu_dsg_energy_kwh"] == 0.622

    def test_zero_lifetime_counter_is_dropped(self):
        """A zero on a total_increasing sensor reads as a meter reset."""
        msg = self._message()
        msg.accu_chg_energy = 0
        msg.accu_dsg_energy = 0
        parsed = parse_delta3_bms_heartbeat(_decode(_build_frame(32, 50, msg.SerializeToString())))
        assert "bms_accu_chg_energy_kwh" not in parsed
        assert "bms_accu_dsg_energy_kwh" not in parsed

    def test_state_of_charge_is_not_taken_from_this_frame(self):
        """`cms_batt_soc` owns the SoC; a second writer would fight it.

        The two do not even agree in the capture - 99 here against the
        display value of 98.8 - so they are not interchangeable.
        """
        parsed = parse_delta3_bms_heartbeat(_decode(self._frame()))
        assert "cms_batt_soc" not in parsed
        assert not any(key.endswith("soc") for key in parsed)

    def test_empty_heartbeat_yields_nothing(self):
        frame = _build_frame(32, 50, Delta3BmsHeartbeat().SerializeToString())
        assert parse_delta3_bms_heartbeat(_decode(frame)) == {}


class TestModeParity:
    """Enhanced Mode and Standard Mode must produce the same sensor keys."""

    def test_same_state_yields_identical_output(self):
        frame = _build_frame(254, 21, _build_display_message().SerializeToString())
        enhanced = parse_delta3_display_property(_decode(frame))
        standard = parse_delta3_http_quota(EQUIVALENT_HTTP_QUOTA)

        assert set(enhanced) == set(standard)
        assert enhanced == standard


class TestRegistryKeysRemainStable:
    """The PowerOcean entries must keep working after the key change."""

    def test_powerocean_energy_stream_still_decodes(self):
        from ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
            JTS1EnergyStreamReport,
        )

        msg = JTS1EnergyStreamReport()
        msg.mppt_pwr = 1500.0
        frame = _build_frame(96, 33, msg.SerializeToString())
        result = decode_proto_runtime_frame(frame)
        assert result.parse_path == "typed_runtime:energy_stream_report"
        assert result.mapped["solar"] == 1500.0

    def test_unknown_command_pair_is_ignored(self):
        msg = Delta3DisplayProperty()
        msg.pow_in_sum_w = 100.0
        frame = _build_frame(254, 22, msg.SerializeToString())
        result = decode_proto_runtime_frame(frame)
        assert result.parse_path == "typed_runtime:no_match"


class TestMultiHeaderEnvelope:
    """The Delta 3 shares the decoder with the bundled PowerOcean replies."""

    def test_delta3_multi_header_envelope_decodes_every_header(self):
        """Each known header in one envelope yields its own typed result."""
        display = _build_display_message()
        heartbeat = Delta3CmsHeartbeat()
        heartbeat.v1p0.lcd_show_soc = 85
        heartbeat.v1p3.sys_chg_dsg_state = 2

        frame = (
            _build_frame(241, 36, b"\x08\x01")
            + _build_frame(254, 21, display.SerializeToString())
            + _build_frame(32, 2, heartbeat.SerializeToString())
        )

        decoded = decode_proto_runtime_headers(frame)

        assert [item.parse_path for item in decoded] == [
            "typed_runtime:delta3_display_property",
            "typed_runtime:delta3_cms_heartbeat",
        ]
        assert decoded[0].mapped["pow_in_sum_w"] == display.pow_in_sum_w
        assert decoded[1].mapped["v1p0"]["lcd_show_soc"] == 85

        # The legacy single-result API keeps the whole header list.
        first = decode_proto_runtime_frame(frame)
        assert first.parse_path == "typed_runtime:delta3_display_property"
        assert len(first.headers) == 3

    def test_delta3_multi_header_outer_payload_still_decodes(self):
        """Headers without pdata still decode from the outer payload field."""
        display = _build_display_message()

        header = bytearray()
        header.extend(encode_field_varint(8, 254))
        header.extend(encode_field_varint(9, 21))
        frame = (
            encode_field_bytes(1, bytes(header))
            + _build_frame(241, 36, b"\x08\x01")
            + encode_field_bytes(2, display.SerializeToString())
        )

        result = decode_proto_runtime_frame(frame)

        assert result.parse_path == "typed_runtime:delta3_display_property"
        assert result.parse_reason_code == "typed_source_payload_field"
        assert result.mapped["pow_in_sum_w"] == display.pow_in_sum_w

        parsed = parse_delta3_display_property(
            {k: v for k, v in result.mapped.items() if not k.startswith("_")}
        )
        assert parsed["pow_in_sum_w"] == display.pow_in_sum_w
        assert parsed["ac_in_w"] == display.pow_get_ac_in


class TestAcChargePowerLimit:
    """Field 209 of the status frame, the app's charge speed slider (#181).

    Identified by direct manipulation rather than inference: a reporter moved
    the slider from 1000 W to 1200 W between two diagnostics downloads and
    this field followed exactly, while the rest of the frame stayed put. A
    second reporter's capture from an unrelated device carries it too.
    """

    def test_value_reaches_the_sensor_key(self):
        display = _build_display_message()
        display.ac_in_chg_pow_max = 1200

        parsed = parse_delta3_display_property(
            {"ac_in_chg_pow_max": display.ac_in_chg_pow_max}
        )

        assert parsed["ac_charge_power_limit_w"] == 1200

    def test_absent_field_writes_nothing(self):
        parsed = parse_delta3_display_property({"pow_in_sum_w": 12.0})

        assert "ac_charge_power_limit_w" not in parsed

    def test_not_claimed_as_a_quota_key(self):
        """The polled quota never carries this value, so the shared HTTP
        field map must not list it - that would claim a reach it lacks."""
        from ecoflow_energy.ecoflow.parsers.delta3_http import DELTA3_HTTP_FIELD_MAP

        assert "acInChgPowMax" not in DELTA3_HTTP_FIELD_MAP
        assert "ac_charge_power_limit_w" not in DELTA3_HTTP_FIELD_MAP.values()

    def test_real_capture_from_a_base_delta_3(self):
        """End to end against an untouched reporter frame (#182)."""
        import json
        from pathlib import Path

        fixture = (
            Path(__file__).parent / "fixtures" / "delta3" / "p231_status_frame.json"
        )
        frame = bytes.fromhex(json.loads(fixture.read_text())["frame_hex"])

        result = decode_proto_runtime_frame(frame)
        parsed = parse_delta3_display_property(
            {k: v for k, v in result.mapped.items() if not k.startswith("_")}
        )

        assert result.mapped["_is_delta3_display"] is True
        assert parsed["ac_charge_power_limit_w"] == 1600
        # The same frame carries the ordinary telemetry, which is what makes
        # this device a Delta 3 rather than a new device class.
        assert "cms_batt_soc" in parsed or "soc_pct" in parsed


class TestAcChargeMode:
    """Field 124 of the status frame, the read-back for the charge mode.

    The mode decides whether the charge power above does anything, so the two
    were shipped together. Read off a DELTA 3 Max Plus that reported 0 while
    its app sat in custom mode with an active slider.
    """

    def test_each_mode_reaches_the_sensor_key(self):
        for wire, label in ((0, "self_def_pow"), (1, "bat_optimal_pow"), (2, "silence")):
            display = _build_display_message()
            display.ac_in_chg_mode = wire

            parsed = parse_delta3_display_property(
                {"ac_in_chg_mode": display.ac_in_chg_mode}
            )

            assert parsed["ac_charge_mode"] == label

    def test_zero_survives_the_proto3_omission(self):
        """Custom mode is the zero value, and it is the mode that matters."""
        display = _build_display_message()
        display.ac_in_chg_mode = 0

        decoded = type(display).FromString(display.SerializeToString())

        assert decoded.HasField("ac_in_chg_mode")
        assert parse_delta3_display_property(
            {"ac_in_chg_mode": decoded.ac_in_chg_mode}
        )["ac_charge_mode"] == "self_def_pow"

    def test_unknown_mode_writes_nothing(self):
        """A select showing an option the device did not report is worse than
        one showing nothing."""
        parsed = parse_delta3_display_property({"ac_in_chg_mode": 7})

        assert "ac_charge_mode" not in parsed

    def test_absent_field_writes_nothing(self):
        parsed = parse_delta3_display_property({"pow_in_sum_w": 12.0})

        assert "ac_charge_mode" not in parsed

    def test_not_claimed_as_a_quota_key(self):
        from ecoflow_energy.ecoflow.parsers.delta3_http import DELTA3_HTTP_FIELD_MAP

        assert "acInChgMode" not in DELTA3_HTTP_FIELD_MAP
        assert "ac_charge_mode" not in DELTA3_HTTP_FIELD_MAP.values()
