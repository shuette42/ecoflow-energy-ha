"""Tests for Delta 2 Max HTTP quota API response parser."""

import pytest

from ecoflow_energy.ecoflow.parsers.delta_http import (
    DELTA2MAX_HTTP_FIELD_MAP,
    parse_delta_http_quota,
)


# ===========================================================================
# Basic Mapping
# ===========================================================================


class TestBasicMapping:
    def test_soc_maps_directly(self):
        result = parse_delta_http_quota({"pd.soc": 85})
        assert result["soc"] == 85.0

    def test_multiple_fields(self):
        data = {
            "pd.soc": 75,
            "pd.wattsInSum": 200,
            "pd.wattsOutSum": 109,
            "inv.inputWatts": 50,
        }
        result = parse_delta_http_quota(data)
        assert result["soc"] == 75.0
        assert result["watts_in_sum"] == 200.0
        assert result["watts_out_sum"] == 109.0
        assert result["ac_in_w"] == 50.0

    def test_unknown_key_ignored(self):
        result = parse_delta_http_quota({"pd.soc": 85, "foo.bar": 42})
        assert "foo.bar" not in result
        assert result == {"soc": 85.0}

    def test_empty_input(self):
        result = parse_delta_http_quota({})
        assert result == {}


# ===========================================================================
# Temperature Offset
# ===========================================================================


class TestTemperatureOffset:
    def test_bms_temp_offset_removed(self):
        """bms_bmsStatus.temp has a +15 offset that must be subtracted."""
        result = parse_delta_http_quota({"bms_bmsStatus.temp": 40})
        assert result["batt_temp_c"] == 25.0
        assert "batt_temp_raw" not in result

    def test_bms_temp_zero(self):
        result = parse_delta_http_quota({"bms_bmsStatus.temp": 15})
        assert result["batt_temp_c"] == 0.0


# ===========================================================================
# Voltage Conversions (mV -> V)
# ===========================================================================


class TestVoltageConversion:
    def test_batt_voltage_mv_to_v(self):
        result = parse_delta_http_quota({"bms_bmsStatus.vol": 52000})
        assert result["batt_voltage_v"] == 52.0
        assert "batt_voltage_mv" not in result

    def test_ac_out_vol_mv_to_v(self):
        result = parse_delta_http_quota({"inv.invOutVol": 230000})
        assert result["ac_out_vol_v"] == 230.0
        assert "ac_out_vol_mv" not in result

    def test_ac_in_vol_mv_to_v(self):
        result = parse_delta_http_quota({"inv.acInVol": 115000})
        assert result["ac_in_vol_v"] == 115.0

    def test_dc_in_vol_mv_to_v(self):
        result = parse_delta_http_quota({"inv.dcInVol": 48500})
        assert result["dc_in_vol_v"] == 48.5

    def test_dcdc_12v_vol_dv_to_v(self):
        """dcdc12vVol is amplified 10x (deci-volt), not mV."""
        result = parse_delta_http_quota({"mppt.dcdc12vVol": 126})
        assert result["dcdc_12v_vol_v"] == pytest.approx(12.6)


# ===========================================================================
# Voltage Conversions (dV -> V)
# ===========================================================================


class TestDeciVoltConversion:
    def test_solar_in_vol_dv_to_v(self):
        result = parse_delta_http_quota({"mppt.inVol": 450})
        assert result["solar_in_vol_v"] == 45.0
        assert "solar_in_vol_dv" not in result

    def test_solar2_in_vol_dv_to_v(self):
        result = parse_delta_http_quota({"mppt.pv2InVol": 380})
        assert result["solar2_in_vol_v"] == 38.0


# ===========================================================================
# Current Conversions (mA -> A)
# ===========================================================================


class TestCurrentConversion:
    def test_batt_current_ma_to_a(self):
        result = parse_delta_http_quota({"bms_bmsStatus.amp": 1500})
        assert result["batt_current_a"] == 1.5
        assert "batt_current_ma" not in result

    def test_ac_out_amp_ma_to_a(self):
        result = parse_delta_http_quota({"inv.invOutAmp": 2300})
        assert result["ac_out_amp_a"] == 2.3

    def test_solar_in_amp_ma_to_a(self):
        result = parse_delta_http_quota({"mppt.inAmp": 8500})
        assert result["solar_in_amp_a"] == 8.5

    def test_dc_in_amp_ma_to_a(self):
        result = parse_delta_http_quota({"inv.dcInAmp": 3200})
        assert result["dc_in_amp_a"] == 3.2

    def test_solar2_in_amp_ca_to_a(self):
        """pv2InAmp is amplified 100x (centi-amp), not mA."""
        result = parse_delta_http_quota({"mppt.pv2InAmp": 850})
        assert result["solar2_in_amp_a"] == pytest.approx(8.5)
        assert "solar2_in_amp_ca" not in result


# ===========================================================================
# MPPT Power/Temp Scaling (amplified values)
# ===========================================================================


class TestMpptScaling:
    def test_mppt_out_watts_amplified_10x(self):
        """outWatts is amplified 10x."""
        result = parse_delta_http_quota({"mppt.outWatts": 3500})
        assert result["mppt_out_w"] == pytest.approx(350.0)

    def test_car_out_watts_amplified_10x(self):
        """carOutWatts is amplified 10x."""
        result = parse_delta_http_quota({"mppt.carOutWatts": 1200})
        assert result["car_12v_out_w"] == pytest.approx(120.0)

    def test_solar2_in_watts_amplified_10x(self):
        """pv2InWatts is amplified 10x."""
        result = parse_delta_http_quota({"mppt.pv2InWatts": 2000})
        assert result["solar2_in_w"] == pytest.approx(200.0)

    def test_dcdc_12v_watts_amplified_100x(self):
        """dcdc12vWatts is amplified 100x."""
        result = parse_delta_http_quota({"mppt.dcdc12vWatts": 5000})
        assert result["dcdc_12v_w"] == pytest.approx(50.0)

    def test_solar2_mppt_temp_amplified_10x(self):
        """pv2MpptTemp is amplified 10x."""
        result = parse_delta_http_quota({"mppt.pv2MpptTemp": 350})
        assert result["solar2_mppt_temp_c"] == pytest.approx(35.0)


# ===========================================================================
# Non-numeric / Edge Cases
# ===========================================================================


class TestEdgeCases:
    def test_non_numeric_value_skipped(self):
        result = parse_delta_http_quota({"pd.soc": "not_a_number"})
        assert "soc" not in result

    def test_none_value_skipped(self):
        result = parse_delta_http_quota({"pd.soc": None})
        assert "soc" not in result

    def test_zero_value_included(self):
        result = parse_delta_http_quota({"pd.wattsOutSum": 0})
        assert result["watts_out_sum"] == 0.0

    def test_negative_value_included(self):
        result = parse_delta_http_quota({"bms_bmsStatus.amp": -500})
        assert result["batt_current_a"] == -0.5

    def test_float_string_parsed(self):
        """Numeric strings should be parsed via _safe_float."""
        result = parse_delta_http_quota({"pd.soc": "85.5"})
        assert result["soc"] == 85.5


# ===========================================================================
# Field Map Integrity
# ===========================================================================


class TestFieldMapIntegrity:
    def test_all_http_keys_unique(self):
        keys = list(DELTA2MAX_HTTP_FIELD_MAP.keys())
        assert len(keys) == len(set(keys))

    def test_all_sensor_keys_unique(self):
        vals = list(DELTA2MAX_HTTP_FIELD_MAP.values())
        assert len(vals) == len(set(vals))

    def test_field_map_not_empty(self):
        assert len(DELTA2MAX_HTTP_FIELD_MAP) > 50

    def test_all_modules_covered(self):
        """Field map should cover pd, inv, bms_bmsStatus, bms_emsStatus, mppt."""
        prefixes = {k.split(".")[0] for k in DELTA2MAX_HTTP_FIELD_MAP}
        assert "pd" in prefixes
        assert "inv" in prefixes
        assert "bms_bmsStatus" in prefixes
        assert "bms_emsStatus" in prefixes
        assert "mppt" in prefixes
