"""Tests for the Smart Plug HTTP Quota, MQTT report, and Protobuf parsers."""

import pytest

from ecoflow_energy.ecoflow.parsers.smartplug import (
    build_plug_brightness_payload,
    build_plug_max_watts_payload,
    build_plug_switch_payload,
    parse_smartplug_http_quota,
    parse_smartplug_proto,
    parse_smartplug_report,
)


class TestCoreMeasurements:
    def test_power_deci_watt_to_watt(self):
        """API returns deci-watts (0.1 W units), parser converts to W."""
        result = parse_smartplug_http_quota({"2_1.watts": 150})
        assert result["power_w"] == pytest.approx(15.0)

    def test_current_ma_to_a(self):
        """API returns mA, parser converts to A."""
        result = parse_smartplug_http_quota({"2_1.current": 430})
        assert result["current_a"] == pytest.approx(0.43)

    def test_voltage(self):
        result = parse_smartplug_http_quota({"2_1.volt": 237})
        assert result["voltage_v"] == 237.0

    def test_frequency(self):
        result = parse_smartplug_http_quota({"2_1.freq": 50})
        assert result["frequency_hz"] == 50.0

    def test_temperature(self):
        result = parse_smartplug_http_quota({"2_1.temp": 39})
        assert result["temperature_c"] == 39.0

    def test_zero_power(self):
        result = parse_smartplug_http_quota({"2_1.watts": 0})
        assert result["power_w"] == 0.0

    def test_standby_power_from_api_example(self):
        """MQTT example: watts=10 -> 1.0W (plausible standby)."""
        result = parse_smartplug_http_quota({"2_1.watts": 10})
        assert result["power_w"] == pytest.approx(1.0)

    def test_all_core_fields(self):
        data = {
            "2_1.watts": 1000,
            "2_1.current": 500,
            "2_1.volt": 230,
            "2_1.freq": 50,
            "2_1.temp": 25,
        }
        result = parse_smartplug_http_quota(data)
        assert result["power_w"] == pytest.approx(100.0)
        assert result["current_a"] == pytest.approx(0.5)
        assert result["voltage_v"] == 230.0
        assert result["frequency_hz"] == 50.0
        assert result["temperature_c"] == 25.0


class TestSwitchState:
    def test_switch_bool_true(self):
        result = parse_smartplug_http_quota({"2_1.switchSta": True})
        assert result["switch_state"] == 1

    def test_switch_bool_false(self):
        result = parse_smartplug_http_quota({"2_1.switchSta": False})
        assert result["switch_state"] == 0

    def test_switch_int_1(self):
        result = parse_smartplug_http_quota({"2_1.switchSta": 1})
        assert result["switch_state"] == 1

    def test_switch_int_0(self):
        result = parse_smartplug_http_quota({"2_1.switchSta": 0})
        assert result["switch_state"] == 0


class TestDiagnostics:
    def test_brightness(self):
        result = parse_smartplug_http_quota({"2_1.brightness": 1023})
        assert result["led_brightness"] == pytest.approx(100.0)  # 1023 -> 100%

    def test_max_watts(self):
        result = parse_smartplug_http_quota({"2_1.maxWatts": 2500})
        assert result["max_power_w"] == 2500.0

    def test_max_current_deci_amp_to_amp(self):
        """API returns deci-amps (0.1 A units), parser converts to A."""
        result = parse_smartplug_http_quota({"2_1.maxCur": 130})
        assert result["max_current_a"] == pytest.approx(13.0)

    def test_max_current_zero(self):
        """MQTT example: maxCur=0 -> 0.0A."""
        result = parse_smartplug_http_quota({"2_1.maxCur": 0})
        assert result["max_current_a"] == 0.0

    def test_error_code(self):
        result = parse_smartplug_http_quota({"2_1.errCode": 0})
        assert result["error_code"] == 0

    def test_error_code_sentinel_65535_maps_to_zero(self):
        """65535 (0xFFFF) is the 'no error' sentinel - must display as 0."""
        result = parse_smartplug_http_quota({"2_1.errCode": 65535})
        assert result["error_code"] == 0

    def test_error_code_real_error_preserved(self):
        result = parse_smartplug_http_quota({"2_1.errCode": 12})
        assert result["error_code"] == 12

    def test_warning_code(self):
        result = parse_smartplug_http_quota({"2_1.warnCode": 0})
        assert result["warning_code"] == 0

    def test_warning_code_sentinel_65535_maps_to_zero(self):
        """65535 (0xFFFF) is the 'no warning' sentinel - must display as 0."""
        result = parse_smartplug_http_quota({"2_1.warnCode": 65535})
        assert result["warning_code"] == 0


class TestEdgeCases:
    def test_empty_input(self):
        result = parse_smartplug_http_quota({})
        assert result == {}

    def test_unknown_keys_ignored(self):
        result = parse_smartplug_http_quota({"2_1.meshId": 12345, "2_2.task1": {}})
        assert result == {}

    def test_none_value_skipped(self):
        result = parse_smartplug_http_quota({"2_1.watts": None})
        assert "power_w" not in result

    def test_non_numeric_skipped(self):
        result = parse_smartplug_http_quota({"2_1.watts": "error"})
        assert "power_w" not in result

    def test_string_number_parsed(self):
        result = parse_smartplug_http_quota({"2_1.watts": "150"})
        assert result["power_w"] == pytest.approx(15.0)


# ===========================================================================
# MQTT Report Parser (parse_smartplug_report)
# ===========================================================================


class TestMQTTReportWithParamsEnvelope:
    """MQTT messages wrapped in {"params": {"2_1.field": value}} envelope."""

    def test_params_envelope_with_prefixed_keys(self):
        """When params contains 2_1.* keys, reuses HTTP parser."""
        data = {
            "params": {
                "2_1.watts": 250,
                "2_1.volt": 230,
                "2_1.current": 1000,
            }
        }
        result = parse_smartplug_report(data)
        assert result["power_w"] == pytest.approx(25.0)
        assert result["voltage_v"] == 230.0
        assert result["current_a"] == pytest.approx(1.0)

    def test_params_envelope_all_fields(self):
        """Full HTTP-style MQTT message."""
        data = {
            "params": {
                "2_1.watts": 1500,
                "2_1.current": 6500,
                "2_1.volt": 230,
                "2_1.freq": 50,
                "2_1.temp": 35,
                "2_1.switchSta": 1,
                "2_1.brightness": 512,
                "2_1.maxWatts": 2500,
                "2_1.maxCur": 130,
                "2_1.errCode": 0,
                "2_1.warnCode": 0,
            }
        }
        result = parse_smartplug_report(data)
        assert result["power_w"] == pytest.approx(150.0)
        assert result["current_a"] == pytest.approx(6.5)
        assert result["voltage_v"] == 230.0
        assert result["frequency_hz"] == 50.0
        assert result["temperature_c"] == 35.0
        assert result["switch_state"] == 1
        assert result["led_brightness"] == round(512 * 100.0 / 1023.0)
        assert result["max_power_w"] == 2500.0
        assert result["max_current_a"] == pytest.approx(13.0)
        assert result["error_code"] == 0
        assert result["warning_code"] == 0


class TestMQTTReportWithDirectFields:
    """MQTT messages with direct field names (cmdId/cmdFunc format)."""

    def test_power_scaling(self):
        """watts field is deci-W, same scaling as HTTP."""
        result = parse_smartplug_report({"watts": 150})
        assert result["power_w"] == pytest.approx(15.0)

    def test_current_scaling(self):
        """current field is mA."""
        result = parse_smartplug_report({"current": 430})
        assert result["current_a"] == pytest.approx(0.43)

    def test_voltage_no_scaling(self):
        result = parse_smartplug_report({"volt": 237})
        assert result["voltage_v"] == 237.0

    def test_frequency(self):
        result = parse_smartplug_report({"freq": 50})
        assert result["frequency_hz"] == 50.0

    def test_temperature(self):
        result = parse_smartplug_report({"temp": 39})
        assert result["temperature_c"] == 39.0

    def test_brightness(self):
        result = parse_smartplug_report({"brightness": 1023})
        assert result["led_brightness"] == pytest.approx(100.0)  # 1023 -> 100%

    def test_max_watts_no_scaling(self):
        """maxWatts has no scaling — consistent with HTTP parser."""
        result = parse_smartplug_report({"maxWatts": 2500})
        assert result["max_power_w"] == 2500.0

    def test_max_current_scaling(self):
        """maxCur is deci-A, /10 to A."""
        result = parse_smartplug_report({"maxCur": 130})
        assert result["max_current_a"] == pytest.approx(13.0)

    def test_error_and_warning_codes(self):
        result = parse_smartplug_report({"errCode": 5, "warnCode": 3})
        assert result["error_code"] == 5
        assert result["warning_code"] == 3

    def test_error_code_sentinel_65535_maps_to_zero(self):
        """MQTT: 65535 sentinel mapped to 0."""
        result = parse_smartplug_report({"errCode": 65535, "warnCode": 65535})
        assert result["error_code"] == 0
        assert result["warning_code"] == 0

    def test_switch_state_int(self):
        result = parse_smartplug_report({"switchSta": 1})
        assert result["switch_state"] == 1

    def test_switch_state_bool(self):
        result = parse_smartplug_report({"switchSta": True})
        assert result["switch_state"] == 1

    def test_switch_state_off(self):
        result = parse_smartplug_report({"switchSta": 0})
        assert result["switch_state"] == 0

    def test_all_direct_fields(self):
        """Full direct-field MQTT message."""
        data = {
            "watts": 1000,
            "current": 500,
            "volt": 230,
            "freq": 50,
            "temp": 25,
            "switchSta": 1,
            "brightness": 512,
            "maxWatts": 2500,
            "maxCur": 100,
            "errCode": 0,
            "warnCode": 0,
        }
        result = parse_smartplug_report(data)
        assert result["power_w"] == pytest.approx(100.0)
        assert result["current_a"] == pytest.approx(0.5)
        assert result["voltage_v"] == 230.0
        assert result["frequency_hz"] == 50.0
        assert result["temperature_c"] == 25.0
        assert result["switch_state"] == 1
        assert result["led_brightness"] == round(512 * 100.0 / 1023.0)
        assert result["max_power_w"] == 2500.0
        assert result["max_current_a"] == pytest.approx(10.0)
        assert result["error_code"] == 0
        assert result["warning_code"] == 0


class TestMQTTReportWithParamEnvelope:
    """MQTT messages wrapped in {"param": {direct_fields}} envelope."""

    def test_param_envelope_direct_fields(self):
        data = {"param": {"watts": 200, "volt": 230}}
        result = parse_smartplug_report(data)
        assert result["power_w"] == pytest.approx(20.0)
        assert result["voltage_v"] == 230.0


class TestMQTTReportEdgeCases:
    def test_empty_input(self):
        result = parse_smartplug_report({})
        assert result == {}

    def test_none_value_skipped(self):
        result = parse_smartplug_report({"watts": None})
        assert "power_w" not in result

    def test_non_numeric_skipped(self):
        result = parse_smartplug_report({"watts": "error"})
        assert "power_w" not in result

    def test_unknown_keys_ignored(self):
        result = parse_smartplug_report({"meshId": 12345, "task1": {}})
        assert result == {}

    def test_zero_power(self):
        result = parse_smartplug_report({"watts": 0})
        assert result["power_w"] == 0.0


# =========================================================================
# SmartPlug Protobuf parser (app-auth WSS stream)
# =========================================================================


class TestSmartPlugProto:
    """Tests for parse_smartplug_proto (protobuf from /app/device/property)."""

    def test_real_message_watts(self):
        """Real captured SmartPlug protobuf message returns power_w."""
        # Captured from live MQTT: 145.0W (f10=1450 deciWatt)
        msg = bytes.fromhex(
            "0a4c0a2150aa0b7084079002d6f4ffffffffffffff01"
            "e002e2dab3ce06e802901cf002901c1035182020012801"
            "4002480150215801800103880103ca0110"
            "485735325a4448345346365738353936"
        )
        result = parse_smartplug_proto(msg)
        assert result is not None
        assert result["power_w"] == pytest.approx(145.0)

    def test_time_only_heartbeat_returns_none(self):
        """Heartbeat with only time fields (no sensor data) returns None."""
        # Captured: pdata contains only f19=run_time, f44=utc_time, f45/f46=timezone
        msg = bytes.fromhex(
            "0a3f0a14980188f11fe002e6dab3ce06e802901cf002901c"
            "103518202001280140024801501458018001038801"
            "03ca0110485735325a4448345346365738353936"
        )
        result = parse_smartplug_proto(msg)
        # No sensor fields present -> None (time-only heartbeat is not actionable)
        assert result is None

    def test_non_protobuf_returns_none(self):
        """Non-protobuf payload returns None."""
        result = parse_smartplug_proto(b'{"json": true}')
        assert result is None

    def test_empty_payload_returns_none(self):
        """Empty payload returns None."""
        result = parse_smartplug_proto(b"")
        assert result is None

    def test_truncated_payload_returns_none(self):
        """Truncated protobuf returns None gracefully."""
        result = parse_smartplug_proto(b"\x0a\x03\x0a\x01")
        assert result is None

    def test_synthetic_all_fields(self):
        """Synthetic message with all plug_heartbeat_pack sensor fields."""
        from ecoflow_energy.ecoflow.proto_encoding import (
            encode_field_bytes,
            encode_field_varint,
        )
        # Build a plug_heartbeat_pack with known values
        pdata = bytearray()
        pdata.extend(encode_field_varint(1, 0))        # err_code = 0
        pdata.extend(encode_field_varint(2, 0))        # warn_code = 0
        pdata.extend(encode_field_varint(5, 130))       # max_cur = 130 deciA -> 13.0A
        pdata.extend(encode_field_varint(6, 35))        # temp = 35C
        pdata.extend(encode_field_varint(7, 50))        # freq = 50Hz
        pdata.extend(encode_field_varint(8, 650))       # current = 650mA -> 0.65A
        pdata.extend(encode_field_varint(9, 230))       # volt = 230V
        pdata.extend(encode_field_varint(10, 1500))     # watts = 1500 deciW -> 150.0W
        pdata.extend(encode_field_varint(11, 1))        # switch_sta = on
        pdata.extend(encode_field_varint(12, 512))      # brightness = 512
        pdata.extend(encode_field_varint(13, 2500))     # max_watts = 2500W

        # Wrap in Header envelope (Send_Header_Msg)
        header = bytearray()
        header.extend(encode_field_bytes(1, bytes(pdata)))  # pdata
        header.extend(encode_field_varint(2, 53))           # src (Plug)
        header.extend(encode_field_varint(3, 32))           # dest (App)
        header.extend(encode_field_varint(8, 2))            # cmd_func = 2
        header.extend(encode_field_varint(9, 1))            # cmd_id = 1

        msg = encode_field_bytes(1, bytes(header))

        result = parse_smartplug_proto(msg)
        assert result is not None
        assert result["power_w"] == pytest.approx(150.0)
        assert result["current_a"] == pytest.approx(0.65)
        assert result["voltage_v"] == pytest.approx(230.0)
        assert result["frequency_hz"] == pytest.approx(50.0)
        assert result["temperature_c"] == pytest.approx(35.0)
        assert result["switch_state"] == 1
        assert result["led_brightness"] == round(512 * 100.0 / 1023.0)
        assert result["max_power_w"] == pytest.approx(2500.0)
        assert result["max_current_a"] == pytest.approx(13.0)
        assert result["error_code"] == 0
        assert result["warning_code"] == 0

    def test_proto_error_code_sentinel_65535_maps_to_zero(self):
        """Proto: 65535 sentinel on error/warning codes mapped to 0."""
        from ecoflow_energy.ecoflow.proto_encoding import (
            encode_field_bytes,
            encode_field_varint,
        )
        pdata = (
            encode_field_varint(1, 65535)    # err_code = 65535 (sentinel)
            + encode_field_varint(2, 65535)   # warn_code = 65535 (sentinel)
            + encode_field_varint(10, 1500)   # watts (needed so result is not None)
        )
        header = encode_field_bytes(1, pdata) + encode_field_varint(8, 2) + encode_field_varint(9, 1)
        msg = encode_field_bytes(1, header)

        result = parse_smartplug_proto(msg)
        assert result is not None
        assert result["error_code"] == 0
        assert result["warning_code"] == 0

    def test_switch_off_in_proto(self):
        """Switch state 0 (off) is correctly parsed."""
        from ecoflow_energy.ecoflow.proto_encoding import (
            encode_field_bytes,
            encode_field_varint,
        )
        pdata = encode_field_varint(10, 100) + encode_field_varint(11, 0)
        header = encode_field_bytes(1, pdata) + encode_field_varint(8, 2) + encode_field_varint(9, 1)
        msg = encode_field_bytes(1, header)

        result = parse_smartplug_proto(msg)
        assert result is not None
        assert result["power_w"] == pytest.approx(10.0)
        assert result["switch_state"] == 0


# =========================================================================
# SmartPlug SET command builders
# =========================================================================


class TestSmartPlugSetCommands:
    """Tests for SmartPlug protobuf SET command builders."""

    def test_plug_switch_on_is_valid_proto(self):
        """build_plug_switch_payload(True) produces valid protobuf."""
        payload = build_plug_switch_payload(True, seq=12345)
        assert payload[0:1] == b"\x0a"  # field 1, wire type 2
        assert len(payload) > 10

    def test_plug_switch_off_is_valid_proto(self):
        """build_plug_switch_payload(False) produces valid protobuf."""
        payload = build_plug_switch_payload(False, seq=12345)
        assert payload[0:1] == b"\x0a"

    def test_plug_switch_contains_cmd_id_129(self):
        """Switch payload contains cmd_id=129 (0x81) in header."""
        payload = build_plug_switch_payload(True, seq=1)
        # cmd_id field 9, varint: tag=0x48, value=0x81 0x01
        assert b"\x48\x81\x01" in payload

    def test_plug_switch_contains_cmd_func_2(self):
        """Switch payload contains cmd_func=2 in header."""
        payload = build_plug_switch_payload(True, seq=1)
        # cmd_func field 8, varint: tag=0x40, value=0x02
        assert b"\x40\x02" in payload

    def test_brightness_payload_valid(self):
        """build_plug_brightness_payload produces valid protobuf."""
        payload = build_plug_brightness_payload(512, seq=1)
        assert payload[0:1] == b"\x0a"
        # cmd_id field 9 = 130 (0x82): tag=0x48, value=0x82 0x01
        assert b"\x48\x82\x01" in payload

    def test_brightness_clamped_to_range(self):
        """Brightness is clamped to 0-1023."""
        payload_max = build_plug_brightness_payload(9999, seq=1)
        payload_min = build_plug_brightness_payload(-5, seq=1)
        # Both should produce valid protobuf without errors
        assert payload_max[0:1] == b"\x0a"
        assert payload_min[0:1] == b"\x0a"

    def test_max_watts_payload_valid(self):
        """build_plug_max_watts_payload produces valid protobuf."""
        payload = build_plug_max_watts_payload(2500, seq=1)
        assert payload[0:1] == b"\x0a"
        # cmd_id field 9 = 137 (0x89): tag=0x48, value=0x89 0x01
        assert b"\x48\x89\x01" in payload

    def test_set_payloads_different_per_command(self):
        """Each SET command produces a different payload."""
        p1 = build_plug_switch_payload(True, seq=1)
        p2 = build_plug_brightness_payload(512, seq=1)
        p3 = build_plug_max_watts_payload(2500, seq=1)
        assert p1 != p2
        assert p2 != p3
        assert p1 != p3

    def test_device_sn_included_in_header(self):
        """device_sn is encoded as field 25 (string) in the header."""
        sn = "HW52TEST00000001"
        payload = build_plug_switch_payload(True, device_sn=sn, seq=1)
        # field 25, wire type 2 (length-delimited): tag = (25 << 3) | 2 = 0xCA 0x01
        # length = 16 bytes, then the ASCII SN
        assert sn.encode("utf-8") in payload

    def test_no_device_sn_when_empty(self):
        """Empty device_sn produces no field 25 in the header."""
        payload_with = build_plug_switch_payload(True, device_sn="HW52TEST", seq=1)
        payload_without = build_plug_switch_payload(True, device_sn="", seq=1)
        assert b"HW52TEST" in payload_with
        assert b"HW52TEST" not in payload_without
        assert len(payload_with) > len(payload_without)

    def test_no_dSrc_dDest_isRwCmd_isQueue_fields(self):
        """Header must not contain dSrc/dDest/isRwCmd/isQueue fields."""
        payload = build_plug_switch_payload(True, device_sn="HW52TEST", seq=1)
        # dSrc would be field 4, wire type 0: tag = (4 << 3) | 0 = 0x20
        # dDest would be field 5, wire type 0: tag = (5 << 3) | 0 = 0x28
        # isRwCmd would be field 19, wire type 0: tag = (19 << 3) | 0 = 0x98 0x01
        # isQueue would be field 20, wire type 0: tag = (20 << 3) | 0 = 0xA0 0x01
        # Check these tags don't appear in the header (inner of outer field 1)
        from ecoflow_energy.ecoflow.parsers.smartplug import _decode_varint_fields, _extract_pdata
        from google.protobuf.internal.decoder import _DecodeVarint

        # Parse the outer Send_Header_Msg to get the Header bytes
        _, pos = _DecodeVarint(payload, 0)
        header_len, pos = _DecodeVarint(payload, pos)
        header_bytes = payload[pos:pos + header_len]

        # Decode all varint fields from the header
        fields = _decode_varint_fields(header_bytes)
        assert 4 not in fields, "dSrc (field 4) should not be present"
        assert 5 not in fields, "dDest (field 5) should not be present"
        assert 19 not in fields, "isRwCmd (field 19) should not be present"
        assert 20 not in fields, "isQueue (field 20) should not be present"

    def test_brightness_with_device_sn(self):
        """Brightness payload includes device_sn."""
        sn = "HW52TEST1234"
        payload = build_plug_brightness_payload(512, device_sn=sn, seq=1)
        assert sn.encode("utf-8") in payload

    def test_max_watts_with_device_sn(self):
        """Max watts payload includes device_sn."""
        sn = "HW52TEST5678"
        payload = build_plug_max_watts_payload(2500, device_sn=sn, seq=1)
        assert sn.encode("utf-8") in payload
