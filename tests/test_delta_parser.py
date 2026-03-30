"""Tests for the Delta 2 Max JSON report parser."""

from ecoflow_energy.ecoflow.parsers.delta import (
    DELTA2MAX_FIELD_MAP,
    parse_delta_report,
)


class TestDeltaParser:
    """Tests for parse_delta_report."""

    def test_pd_status_soc(self):
        report = {"typeCode": "pdStatus", "params": {"soc": 85}}
        result = parse_delta_report(report)
        assert result == {"soc": 85.0}

    def test_pd_status_multiple_params(self):
        report = {
            "typeCode": "pdStatus",
            "params": {"soc": 75, "wattsInSum": 200, "wattsOutSum": 109},
        }
        result = parse_delta_report(report)
        assert result["soc"] == 75.0
        assert result["watts_in_sum"] == 200.0
        assert result["watts_out_sum"] == 109.0

    def test_inv_status(self):
        report = {
            "typeCode": "invStatus",
            "params": {"outputWatts": 1500, "inputWatts": 200},
        }
        result = parse_delta_report(report)
        assert result["ac_out_w"] == 1500.0
        assert result["ac_in_w"] == 200.0

    def test_bms_temp_offset(self):
        """bmsStatus.temp has a +15 offset that must be removed."""
        report = {"typeCode": "bmsStatus", "params": {"temp": 40}}
        result = parse_delta_report(report)
        assert result["batt_temp_c"] == 25.0  # 40 - 15

    def test_voltage_mv_to_v_conversion(self):
        """Battery voltage in mV must be converted to V."""
        report = {"typeCode": "bmsStatus", "params": {"vol": 52000}}
        result = parse_delta_report(report)
        assert result["batt_voltage_v"] == 52.0

    def test_current_ma_to_a_conversion(self):
        """Battery current in mA must be converted to A."""
        report = {"typeCode": "bmsStatus", "params": {"amp": 1500}}
        result = parse_delta_report(report)
        assert result["batt_current_a"] == 1.5

    def test_unknown_type_code(self):
        report = {"typeCode": "unknownModule", "params": {"foo": 42}}
        result = parse_delta_report(report)
        assert result == {}

    def test_non_numeric_param_ignored(self):
        report = {"typeCode": "pdStatus", "params": {"soc": 85, "name": "test"}}
        result = parse_delta_report(report)
        assert result == {"soc": 85.0}

    def test_empty_params(self):
        result = parse_delta_report({"typeCode": "pdStatus", "params": {}})
        assert result == {}

    def test_missing_params(self):
        result = parse_delta_report({"typeCode": "pdStatus"})
        assert result == {}

    def test_missing_type_code(self):
        result = parse_delta_report({"params": {"soc": 50}})
        assert result == {}

    def test_custom_field_map(self):
        custom = {"pdStatus.soc": "my_soc"}
        report = {"typeCode": "pdStatus", "params": {"soc": 90}}
        result = parse_delta_report(report, field_map=custom)
        assert result == {"my_soc": 90.0}

    def test_mppt_status(self):
        report = {"typeCode": "mpptStatus", "params": {"inWatts": 350}}
        result = parse_delta_report(report)
        assert result["solar_in_w"] == 350.0

    def test_mppt_out_watts_amplified_10x(self):
        """outWatts is amplified 10x, must be divided."""
        report = {"typeCode": "mpptStatus", "params": {"outWatts": 3500}}
        result = parse_delta_report(report)
        assert result["mppt_out_w"] == 350.0

    def test_car_out_watts_amplified_10x(self):
        report = {"typeCode": "mpptStatus", "params": {"carOutWatts": 1200}}
        result = parse_delta_report(report)
        assert result["car_12v_out_w"] == 120.0

    def test_dcdc_12v_watts_amplified_100x(self):
        report = {"typeCode": "mpptStatus", "params": {"dcdc12vWatts": 5000}}
        result = parse_delta_report(report)
        assert result["dcdc_12v_w"] == 50.0

    def test_dcdc_12v_vol_amplified_10x(self):
        """dcdc12vVol is amplified 10x (deci-volt), not mV."""
        report = {"typeCode": "mpptStatus", "params": {"dcdc12vVol": 126}}
        result = parse_delta_report(report)
        assert result["dcdc_12v_vol_v"] == 12.6

    def test_solar2_in_watts_amplified_10x(self):
        report = {"typeCode": "mpptStatus", "params": {"pv2InWatts": 2000}}
        result = parse_delta_report(report)
        assert result["solar2_in_w"] == 200.0

    def test_solar2_in_amp_amplified_100x(self):
        """pv2InAmp is amplified 100x (centi-amp), not mA."""
        report = {"typeCode": "mpptStatus", "params": {"pv2InAmp": 850}}
        result = parse_delta_report(report)
        assert result["solar2_in_amp_a"] == 8.5

    def test_solar2_mppt_temp_amplified_10x(self):
        report = {"typeCode": "mpptStatus", "params": {"pv2MpptTemp": 350}}
        result = parse_delta_report(report)
        assert result["solar2_mppt_temp_c"] == 35.0

    def test_field_map_coverage(self):
        """All field_map entries must have unique destination keys."""
        dest_keys = list(DELTA2MAX_FIELD_MAP.values())
        # Raw keys are never in the output (replaced by processed keys)
        raw_keys = ("batt_temp_raw", "beep_mode_raw", "slave1_temp_raw", "slave2_temp_raw")
        dest_keys_without_raw = [k for k in dest_keys if k not in raw_keys]
        assert len(dest_keys_without_raw) == len(set(dest_keys_without_raw))

    # --- New fields: beeper, ac auto on, screen, backup reserve, car standby ---

    def test_beeper_mode_inverted(self):
        """beepMode=0 (normal mode) → beep_enabled=1 (beeper ON)."""
        report = {"typeCode": "pdStatus", "params": {"beepMode": 0}}
        result = parse_delta_report(report)
        assert result["beep_enabled"] == 1

    def test_beeper_quiet_mode_inverted(self):
        """beepMode=1 (quiet mode) → beep_enabled=0 (beeper OFF)."""
        report = {"typeCode": "pdStatus", "params": {"beepMode": 1}}
        result = parse_delta_report(report)
        assert result["beep_enabled"] == 0

    def test_ac_auto_on(self):
        report = {"typeCode": "pdStatus", "params": {"newAcAutoOnCfg": 1}}
        result = parse_delta_report(report)
        assert result["ac_auto_on"] == 1.0

    def test_screen_brightness(self):
        report = {"typeCode": "pdStatus", "params": {"brightLevel": 80}}
        result = parse_delta_report(report)
        assert result["screen_brightness"] == 80.0

    def test_screen_timeout(self):
        report = {"typeCode": "pdStatus", "params": {"lcdOffSec": 300}}
        result = parse_delta_report(report)
        assert result["screen_timeout_sec"] == 300.0

    def test_backup_reserve_soc(self):
        report = {"typeCode": "pdStatus", "params": {"bpPowerSoc": 50}}
        result = parse_delta_report(report)
        assert result["backup_reserve_soc"] == 50.0

    def test_backup_reserve_enabled(self):
        report = {"typeCode": "pdStatus", "params": {"watchIsConfig": 1}}
        result = parse_delta_report(report)
        assert result["backup_reserve_enabled"] == 1.0

    def test_car_standby_min(self):
        report = {"typeCode": "mpptStatus", "params": {"carStandbyMin": 120}}
        result = parse_delta_report(report)
        assert result["car_standby_min"] == 120.0

    # --- Slave Battery Pack Support ---

    def test_slave1_soc(self):
        report = {"typeCode": "bmsSlaveStatus_1", "params": {"soc": 85}}
        result = parse_delta_report(report)
        assert result["slave1_soc"] == 85.0

    def test_slave2_soc(self):
        report = {"typeCode": "bmsSlaveStatus_2", "params": {"soc": 72}}
        result = parse_delta_report(report)
        assert result["slave2_soc"] == 72.0

    def test_slave1_voltage_mv_to_v(self):
        """Slave battery voltage in mV must be converted to V."""
        report = {"typeCode": "bmsSlaveStatus_1", "params": {"vol": 52000}}
        result = parse_delta_report(report)
        assert result["slave1_voltage_v"] == 52.0

    def test_slave1_current_ma_to_a(self):
        """Slave battery current in mA must be converted to A."""
        report = {"typeCode": "bmsSlaveStatus_1", "params": {"amp": -1500}}
        result = parse_delta_report(report)
        assert result["slave1_current_a"] == -1.5

    def test_slave1_temp_offset(self):
        """Slave BMS temp has +15 offset like main battery."""
        report = {"typeCode": "bmsSlaveStatus_1", "params": {"temp": 40}}
        result = parse_delta_report(report)
        assert result["slave1_temp_c"] == 25.0

    def test_slave2_temp_offset(self):
        report = {"typeCode": "bmsSlaveStatus_2", "params": {"temp": 15}}
        result = parse_delta_report(report)
        assert result["slave2_temp_c"] == 0.0

    def test_slave1_full_report(self):
        """Parse a complete slave battery pack MQTT report."""
        report = {
            "typeCode": "bmsSlaveStatus_1",
            "params": {
                "soc": 85,
                "vol": 52000,
                "amp": -1200,
                "temp": 40,
                "soh": 97,
                "cycles": 150,
                "inputWatts": 0,
                "outputWatts": 62,
                "remainCap": 38000,
                "fullCap": 40000,
                "maxCellVol": 3450,
                "minCellVol": 3380,
                "maxCellTemp": 35,
                "minCellTemp": 28,
                "maxMosTemp": 42,
                "errCode": 0,
            },
        }
        result = parse_delta_report(report)
        assert result["slave1_soc"] == 85.0
        assert result["slave1_voltage_v"] == 52.0
        assert result["slave1_current_a"] == -1.2
        assert result["slave1_temp_c"] == 25.0
        assert result["slave1_soh"] == 97.0
        assert result["slave1_cycles"] == 150.0
        assert result["slave1_in_w"] == 0.0
        assert result["slave1_out_w"] == 62.0
        assert result["slave1_remain_cap_mah"] == 38000.0
        assert result["slave1_full_cap_mah"] == 40000.0
        assert result["slave1_max_cell_vol_mv"] == 3450.0
        assert result["slave1_min_cell_vol_mv"] == 3380.0
        assert result["slave1_max_cell_temp_c"] == 35.0
        assert result["slave1_min_cell_temp_c"] == 28.0
        assert result["slave1_max_mos_temp_c"] == 42.0
        assert result["slave1_err_code"] == 0.0

    def test_slave2_voltage_and_current(self):
        """Verify slave 2 uses independent conversion paths."""
        report = {
            "typeCode": "bmsSlaveStatus_2",
            "params": {"vol": 48000, "amp": 2500},
        }
        result = parse_delta_report(report)
        assert result["slave2_voltage_v"] == 48.0
        assert result["slave2_current_a"] == 2.5
