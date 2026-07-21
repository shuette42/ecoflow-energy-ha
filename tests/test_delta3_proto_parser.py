"""Tests for the Delta 3 protobuf telemetry parser (Enhanced Mode).

Covers the two frames the device pushes on the app connection - the main
status frame (cmd_func=254, cmd_id=21) and the battery heartbeat
(cmd_func=32, cmd_id=2) - end to end: encode a realistic frame, run it
through the runtime decoder, parse it, and compare the resulting sensor
keys against the HTTP path.
"""

from __future__ import annotations

from ecoflow_energy.ecoflow.parsers.delta3_http import parse_delta3_http_quota
from ecoflow_energy.ecoflow.parsers.delta3_proto import (
    parse_delta3_cms_heartbeat,
    parse_delta3_display_property,
)
from ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
    Delta3CmsHeartbeat,
    Delta3DisplayProperty,
)
from ecoflow_energy.ecoflow.proto.runtime import decode_proto_runtime_frame
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
