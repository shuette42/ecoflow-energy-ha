"""Tests for the Smart Plug HTTP Quota and MQTT report parsers."""

import pytest

from ecoflow_energy.ecoflow.parsers.smartplug import (
    parse_smartplug_http_quota,
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
        assert result["led_brightness"] == 1023.0

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

    def test_warning_code(self):
        result = parse_smartplug_http_quota({"2_1.warnCode": 0})
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
        assert result["led_brightness"] == 512.0
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
        assert result["led_brightness"] == 1023.0

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
        assert result["led_brightness"] == 512.0
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
