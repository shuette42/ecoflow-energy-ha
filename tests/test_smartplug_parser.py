"""Tests for the Smart Plug HTTP Quota parser."""

import pytest

from ecoflow_energy.ecoflow.parsers.smartplug import parse_smartplug_http_quota


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
