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
        """Field map should cover pd, inv, bms_bmsStatus, bms_emsStatus, mppt, bms_slave."""
        prefixes = {k.split(".")[0] for k in DELTA2MAX_HTTP_FIELD_MAP}
        assert "pd" in prefixes
        assert "inv" in prefixes
        assert "bms_bmsStatus" in prefixes
        assert "bms_emsStatus" in prefixes
        assert "mppt" in prefixes

    def test_slave_modules_covered(self):
        """Field map should include slave battery pack mappings.

        The prefix is checked against what a device sends rather than
        against whatever the map happens to contain. The old spelling,
        `bms_slave.1.`, matched nothing on real hardware and this test
        passed anyway for a year, because it only ever compared the map
        with itself (#287).
        """
        keys = list(DELTA2MAX_HTTP_FIELD_MAP.keys())
        slave1_keys = [k for k in keys if k.startswith("bms_slave_bmsSlaveStatus_1.")]
        slave2_keys = [k for k in keys if k.startswith("bms_slave_bmsSlaveStatus_2.")]
        assert len(slave1_keys) == 16, f"Expected 16 slave1 keys, got {len(slave1_keys)}"
        assert len(slave2_keys) == 16, f"Expected 16 slave2 keys, got {len(slave2_keys)}"


# ===========================================================================
# New Fields: Beeper, AC Auto On, Screen, Backup Reserve, Car Standby
# ===========================================================================


class TestNewPdFields:
    def test_beep_mode_inverted_normal(self):
        """beepMode=0 (normal mode) → beep_enabled=1 (beeper ON)."""
        result = parse_delta_http_quota({"pd.beepMode": 0})
        assert result["beep_enabled"] == 1
        assert "beep_mode_raw" not in result

    def test_beep_mode_inverted_quiet(self):
        """beepMode=1 (quiet mode) → beep_enabled=0 (beeper OFF)."""
        result = parse_delta_http_quota({"pd.beepMode": 1})
        assert result["beep_enabled"] == 0

    def test_ac_auto_on(self):
        result = parse_delta_http_quota({"pd.newAcAutoOnCfg": 1})
        assert result["ac_auto_on"] == 1.0

    def test_ac_auto_on_disabled(self):
        result = parse_delta_http_quota({"pd.newAcAutoOnCfg": 0})
        assert result["ac_auto_on"] == 0.0

    def test_ac_auto_on_legacy(self):
        result = parse_delta_http_quota({"pd.acAutoOutConfig": 1})
        assert result["ac_auto_on"] == 1.0

    def test_modern_key_wins_over_legacy(self):
        """When both modern and legacy keys are present, modern value wins."""
        result = parse_delta_http_quota({
            "pd.newAcAutoOnCfg": 1,
            "pd.acAutoOutConfig": 0,
        })
        assert result["ac_auto_on"] == 1.0

    def test_screen_brightness(self):
        result = parse_delta_http_quota({"pd.brightLevel": 80})
        assert result["screen_brightness"] == 80.0

    def test_screen_timeout(self):
        result = parse_delta_http_quota({"pd.lcdOffSec": 300})
        assert result["screen_timeout_sec"] == 300.0

    def test_screen_timeout_never(self):
        result = parse_delta_http_quota({"pd.lcdOffSec": 0})
        assert result["screen_timeout_sec"] == 0.0

    def test_backup_reserve_soc(self):
        result = parse_delta_http_quota({"pd.bpPowerSoc": 50})
        assert result["backup_reserve_soc"] == 50.0

    def test_backup_reserve_enabled(self):
        result = parse_delta_http_quota({"pd.watchIsConfig": 1})
        assert result["backup_reserve_enabled"] == 1.0

    def test_backup_reserve_disabled(self):
        result = parse_delta_http_quota({"pd.watchIsConfig": 0})
        assert result["backup_reserve_enabled"] == 0.0


class TestNewMpptFields:
    def test_car_standby_min(self):
        result = parse_delta_http_quota({"mppt.carStandbyMin": 120})
        assert result["car_standby_min"] == 120.0

    def test_car_standby_min_zero(self):
        result = parse_delta_http_quota({"mppt.carStandbyMin": 0})
        assert result["car_standby_min"] == 0.0

    def test_legacy_mppt_ac_enabled_fallback(self):
        result = parse_delta_http_quota({"mppt.cfgAcEnabled": 1})
        assert result["ac_enabled"] == 1.0

    def test_legacy_mppt_xboost_fallback(self):
        result = parse_delta_http_quota({"mppt.cfgAcXboost": 1})
        assert result["ac_xboost"] == 1.0

    def test_legacy_mppt_beeper_inverted(self):
        result = parse_delta_http_quota({"mppt.beepState": 1})
        assert result["beep_enabled"] == 0


# ===========================================================================
# Slave Battery Pack Support
# ===========================================================================


class TestSlavePackMapping:
    """Test slave battery pack field mapping and unit conversions."""

    def test_slave1_soc(self):
        result = parse_delta_http_quota({"bms_slave_bmsSlaveStatus_1.soc": 85})
        assert result["slave1_soc"] == 85.0

    def test_slave2_soc(self):
        result = parse_delta_http_quota({"bms_slave_bmsSlaveStatus_2.soc": 72})
        assert result["slave2_soc"] == 72.0

    def test_slave1_voltage_mv_to_v(self):
        """Slave battery voltage in mV must be converted to V."""
        result = parse_delta_http_quota({"bms_slave_bmsSlaveStatus_1.vol": 52000})
        assert result["slave1_voltage_v"] == 52.0
        assert "slave1_voltage_mv" not in result

    def test_slave2_voltage_mv_to_v(self):
        result = parse_delta_http_quota({"bms_slave_bmsSlaveStatus_2.vol": 48500})
        assert result["slave2_voltage_v"] == 48.5

    def test_slave1_current_ma_to_a(self):
        """Slave battery current in mA must be converted to A."""
        result = parse_delta_http_quota({"bms_slave_bmsSlaveStatus_1.amp": 1500})
        assert result["slave1_current_a"] == 1.5
        assert "slave1_current_ma" not in result

    def test_slave1_current_negative(self):
        """Negative current (discharging) must be preserved."""
        result = parse_delta_http_quota({"bms_slave_bmsSlaveStatus_1.amp": -2000})
        assert result["slave1_current_a"] == -2.0

    def test_slave2_current_ma_to_a(self):
        result = parse_delta_http_quota({"bms_slave_bmsSlaveStatus_2.amp": 3200})
        assert result["slave2_current_a"] == 3.2

    def test_slave1_temp_offset(self):
        """Slave BMS temp has +15 offset like main battery."""
        result = parse_delta_http_quota({"bms_slave_bmsSlaveStatus_1.temp": 40})
        assert result["slave1_temp_c"] == 25.0
        assert "slave1_temp_raw" not in result

    def test_slave2_temp_offset(self):
        result = parse_delta_http_quota({"bms_slave_bmsSlaveStatus_2.temp": 15})
        assert result["slave2_temp_c"] == 0.0

    def test_slave1_soh(self):
        result = parse_delta_http_quota({"bms_slave_bmsSlaveStatus_1.soh": 98})
        assert result["slave1_soh"] == 98.0

    def test_slave1_cycles(self):
        result = parse_delta_http_quota({"bms_slave_bmsSlaveStatus_1.cycles": 42})
        assert result["slave1_cycles"] == 42.0

    def test_slave1_power_direct(self):
        """Slave input/output watts are direct values (no scaling)."""
        result = parse_delta_http_quota({
            "bms_slave_bmsSlaveStatus_1.inputWatts": 200,
            "bms_slave_bmsSlaveStatus_1.outputWatts": 150,
        })
        assert result["slave1_in_w"] == 200.0
        assert result["slave1_out_w"] == 150.0

    def test_slave1_capacity(self):
        result = parse_delta_http_quota({
            "bms_slave_bmsSlaveStatus_1.remainCap": 38000,
            "bms_slave_bmsSlaveStatus_1.fullCap": 40000,
        })
        assert result["slave1_remain_cap_mah"] == 38000.0
        assert result["slave1_full_cap_mah"] == 40000.0

    def test_slave1_cell_voltages_stay_mv(self):
        """Cell-level voltages stay in mV (not converted to V)."""
        result = parse_delta_http_quota({
            "bms_slave_bmsSlaveStatus_1.maxCellVol": 3450,
            "bms_slave_bmsSlaveStatus_1.minCellVol": 3380,
        })
        assert result["slave1_max_cell_vol_mv"] == 3450.0
        assert result["slave1_min_cell_vol_mv"] == 3380.0

    def test_slave1_cell_temps_direct(self):
        """Cell temps are direct Celsius (no offset)."""
        result = parse_delta_http_quota({
            "bms_slave_bmsSlaveStatus_1.maxCellTemp": 35,
            "bms_slave_bmsSlaveStatus_1.minCellTemp": 28,
        })
        assert result["slave1_max_cell_temp_c"] == 35.0
        assert result["slave1_min_cell_temp_c"] == 28.0

    def test_slave1_mos_temp_direct(self):
        result = parse_delta_http_quota({"bms_slave_bmsSlaveStatus_1.maxMosTemp": 42})
        assert result["slave1_max_mos_temp_c"] == 42.0

    def test_slave1_err_code(self):
        result = parse_delta_http_quota({"bms_slave_bmsSlaveStatus_1.errCode": 0})
        assert result["slave1_err_code"] == 0.0

    def test_slave_full_payload(self):
        """Parse a complete slave battery pack payload."""
        data = {
            "bms_slave_bmsSlaveStatus_1.soc": 85,
            "bms_slave_bmsSlaveStatus_1.vol": 52000,
            "bms_slave_bmsSlaveStatus_1.amp": -1200,
            "bms_slave_bmsSlaveStatus_1.temp": 40,
            "bms_slave_bmsSlaveStatus_1.soh": 97,
            "bms_slave_bmsSlaveStatus_1.cycles": 150,
            "bms_slave_bmsSlaveStatus_1.inputWatts": 0,
            "bms_slave_bmsSlaveStatus_1.outputWatts": 62,
            "bms_slave_bmsSlaveStatus_1.remainCap": 38000,
            "bms_slave_bmsSlaveStatus_1.fullCap": 40000,
            "bms_slave_bmsSlaveStatus_1.maxCellVol": 3450,
            "bms_slave_bmsSlaveStatus_1.minCellVol": 3380,
            "bms_slave_bmsSlaveStatus_1.maxCellTemp": 35,
            "bms_slave_bmsSlaveStatus_1.minCellTemp": 28,
            "bms_slave_bmsSlaveStatus_1.maxMosTemp": 42,
            "bms_slave_bmsSlaveStatus_1.errCode": 0,
        }
        result = parse_delta_http_quota(data)
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

    def test_both_slaves_independent(self):
        """Both slave packs can be parsed independently in same payload."""
        data = {
            "bms_slave_bmsSlaveStatus_1.soc": 85,
            "bms_slave_bmsSlaveStatus_2.soc": 72,
            "bms_slave_bmsSlaveStatus_1.vol": 52000,
            "bms_slave_bmsSlaveStatus_2.vol": 48000,
        }
        result = parse_delta_http_quota(data)
        assert result["slave1_soc"] == 85.0
        assert result["slave2_soc"] == 72.0
        assert result["slave1_voltage_v"] == 52.0
        assert result["slave2_voltage_v"] == 48.0


class TestDeltaHttpUnknownEnumDropped:
    """Unknown enum ints from newer firmware must not reach HA enum sensors."""

    def test_unknown_chg_dsg_state_dropped(self):
        result = parse_delta_http_quota({"pd.chgDsgState": 9, "pd.soc": 50})
        assert "chg_dsg_state" not in result
        assert result["soc"] == 50.0

    def test_unknown_mppt_chg_state_dropped(self):
        result = parse_delta_http_quota({"mppt.chgState": 7})
        assert "mppt_chg_state" not in result

    def test_known_ems_chg_state_mapped(self):
        result = parse_delta_http_quota({"bms_emsStatus.chgState": 1})
        assert result["ems_chg_state"] == "discharging"


class TestCfgAcOutVolParity:
    """HTTP inv.cfgAcOutVol must land identically to the MQTT path."""

    def test_http_scaling_matches_mqtt(self):
        from ecoflow_energy.ecoflow.parsers.delta import parse_delta_report

        http = parse_delta_http_quota({"inv.cfgAcOutVol": 230000})
        mqtt = parse_delta_report(
            {"typeCode": "invStatus", "params": {"cfgAcOutVol": 230000}}
        )
        assert http["ac_cfg_out_vol_v"] == pytest.approx(230.0)
        assert http["ac_cfg_out_vol_v"] == mqtt["ac_cfg_out_vol_v"]
        assert "ac_cfg_out_vol_mv" not in http


class TestSlavePackAgainstRealHardware:
    """The expansion battery block as a Delta 2 Max actually sends it.

    Every key here is copied from the whole-state answer of a reporter's
    unit with one pack attached (#287), rather than from this repo's own
    field map. That distinction is the point: the previous spelling was
    invented, matched nothing, left all 32 entities empty for a year, and
    the tests never noticed because they asked the map about itself.
    """

    # Verbatim from the recording, values included.
    _PACK_1 = {
        "bms_slave_bmsSlaveStatus_1.soc": 60,
        "bms_slave_bmsSlaveStatus_1.vol": 53336,
        "bms_slave_bmsSlaveStatus_1.amp": -55,
        "bms_slave_bmsSlaveStatus_1.soh": 100,
        "bms_slave_bmsSlaveStatus_1.cycles": 22,
        "bms_slave_bmsSlaveStatus_1.inputWatts": 0,
        "bms_slave_bmsSlaveStatus_1.outputWatts": 0,
        "bms_slave_bmsSlaveStatus_1.remainCap": 23487,
        "bms_slave_bmsSlaveStatus_1.fullCap": 39280,
        "bms_slave_bmsSlaveStatus_1.maxCellVol": 3305,
        "bms_slave_bmsSlaveStatus_1.minCellVol": 3303,
        "bms_slave_bmsSlaveStatus_1.maxCellTemp": 29,
        "bms_slave_bmsSlaveStatus_1.minCellTemp": 27,
        "bms_slave_bmsSlaveStatus_1.maxMosTemp": 27,
        "bms_slave_bmsSlaveStatus_1.errCode": 23,
    }

    def test_every_reading_fills(self):
        result = parse_delta_http_quota(self._PACK_1)

        assert result["slave1_soc"] == 60.0
        assert result["slave1_voltage_v"] == 53.336
        assert result["slave1_current_a"] == -0.055
        assert result["slave1_soh"] == 100.0
        assert result["slave1_cycles"] == 22.0
        assert result["slave1_max_cell_vol_mv"] == 3305.0
        assert result["slave1_min_cell_temp_c"] == 27.0

    def test_nothing_is_left_unread(self):
        """A key the device sends and the map ignores is the whole defect."""
        result = parse_delta_http_quota(self._PACK_1)

        filled = {key for key in result if key.startswith("slave1_")}
        assert len(filled) == len(self._PACK_1)

    def test_the_old_spelling_no_longer_matches(self):
        """It never matched hardware; it must not linger as a second path."""
        assert parse_delta_http_quota({"bms_slave.1.soc": 85}) == {}
