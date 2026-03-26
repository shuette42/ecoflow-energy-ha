"""Tests for PowerOcean HTTP quota API response parser."""

import json

import pytest

from ecoflow_energy.ecoflow.parsers.powerocean import (
    parse_powerocean_http_quota,
    _extract_battery_pack,
    _extract_energy_stream,
)


# ===========================================================================
# Top-level Power Values
# ===========================================================================


class TestTopLevelPower:
    def test_solar_w(self):
        result = parse_powerocean_http_quota({"mpptPwr": 3500})
        assert result["solar_w"] == 3500.0

    def test_home_w(self):
        result = parse_powerocean_http_quota({"sysLoadPwr": 1200})
        assert result["home_w"] == 1200.0

    def test_grid_w(self):
        result = parse_powerocean_http_quota({"sysGridPwr": -500})
        assert result["grid_w"] == -500.0

    def test_batt_w(self):
        result = parse_powerocean_http_quota({"bpPwr": 800})
        assert result["batt_w"] == 800.0

    def test_soc_pct(self):
        result = parse_powerocean_http_quota({"bpSoc": 72})
        assert result["soc_pct"] == 72.0

    def test_all_top_level(self):
        data = {
            "mpptPwr": 3000,
            "sysLoadPwr": 1500,
            "sysGridPwr": 200,
            "bpPwr": -300,
            "bpSoc": 85,
        }
        result = parse_powerocean_http_quota(data)
        assert result["solar_w"] == 3000.0
        assert result["home_w"] == 1500.0
        assert result["grid_w"] == 200.0
        assert result["batt_w"] == -300.0
        assert result["soc_pct"] == 85.0

    def test_empty_input(self):
        result = parse_powerocean_http_quota({})
        assert result == {}


# ===========================================================================
# Derived Power Values (grid import/export, batt charge/discharge)
# ===========================================================================


class TestDerivedPower:
    def test_grid_import_positive(self):
        """Positive grid_w = importing from grid."""
        result = parse_powerocean_http_quota({"sysGridPwr": 500})
        assert result["grid_import_power_w"] == 500.0
        assert result["grid_export_power_w"] == 0.0

    def test_grid_export_negative(self):
        """Negative grid_w = exporting to grid."""
        result = parse_powerocean_http_quota({"sysGridPwr": -800})
        assert result["grid_import_power_w"] == 0.0
        assert result["grid_export_power_w"] == 800.0

    def test_grid_zero(self):
        result = parse_powerocean_http_quota({"sysGridPwr": 0})
        assert result["grid_import_power_w"] == 0.0
        assert result["grid_export_power_w"] == 0.0

    def test_batt_charging_positive(self):
        """Positive batt_w = charging."""
        result = parse_powerocean_http_quota({"bpPwr": 1000})
        assert result["batt_charge_power_w"] == 1000.0
        assert result["batt_discharge_power_w"] == 0.0

    def test_batt_discharging_negative(self):
        """Negative batt_w = discharging."""
        result = parse_powerocean_http_quota({"bpPwr": -600})
        assert result["batt_charge_power_w"] == 0.0
        assert result["batt_discharge_power_w"] == 600.0

    def test_batt_zero(self):
        result = parse_powerocean_http_quota({"bpPwr": 0})
        assert result["batt_charge_power_w"] == 0.0
        assert result["batt_discharge_power_w"] == 0.0


# ===========================================================================
# EMS Data
# ===========================================================================


class TestEMSData:
    def test_ems_bp_alive_num(self):
        result = parse_powerocean_http_quota({"emsBpAliveNum": 2})
        assert result["ems_bp_alive_num"] == 2.0

    def test_ems_bp_alive_num_missing(self):
        result = parse_powerocean_http_quota({})
        assert "ems_bp_alive_num" not in result


# ===========================================================================
# Battery Pack Extraction
# ===========================================================================


class TestBatteryPack:
    def test_bp_from_dict(self):
        """Battery pack data as dict."""
        data = {
            "bp_addr.HW52ZAB000001": {
                "bpSoh": 98,
                "bpCycles": 45,
                "bpRemainWatth": 4800,
                "bpVol": 52.1,
                "bpAmp": -2.5,
            }
        }
        result = parse_powerocean_http_quota(data)
        assert result["bp_soh_pct"] == 98.0
        assert result["bp_cycles"] == 45.0
        assert result["bp_remain_watth"] == 4800.0
        assert result["bp_voltage_v"] == 52.1
        assert result["bp_current_a"] == -2.5

    def test_bp_from_json_string(self):
        """Battery pack data as JSON string (API sometimes returns this)."""
        bp = {
            "bpSoh": 95,
            "bpCycles": 120,
            "bpRealSoc": 73.5,
        }
        data = {"bp_addr.SN12345": json.dumps(bp)}
        result = parse_powerocean_http_quota(data)
        assert result["bp_soh_pct"] == 95.0
        assert result["bp_cycles"] == 120.0
        assert result["bp_real_soc_pct"] == 73.5

    def test_bp_invalid_json_string_skipped(self):
        data = {"bp_addr.SN12345": "not valid json"}
        result = parse_powerocean_http_quota(data)
        assert "bp_soh_pct" not in result

    def test_bp_update_time_ignored(self):
        """bp_addr.updateTime is not a battery pack."""
        data = {"bp_addr.updateTime": 1700000000}
        result = parse_powerocean_http_quota(data)
        assert "bp_soh_pct" not in result

    def test_bp_no_packs(self):
        result = parse_powerocean_http_quota({"mpptPwr": 100})
        assert "bp_soh_pct" not in result

    def test_bp_all_fields(self):
        bp = {
            "bpSoh": 97,
            "bpCycles": 50,
            "bpRemainWatth": 5000,
            "bpVol": 51.5,
            "bpAmp": 3.2,
            "bpMaxCellTemp": 32,
            "bpMinCellTemp": 28,
            "bpEnvTemp": 25,
            "bpMaxMosTemp": 35,
            "bpCellMaxVol": 3450,
            "bpCellMinVol": 3420,
            "bpRealSoc": 80.5,
            "bpRealSoh": 96.5,
            "bpDownLimitSoc": 10,
            "bpUpLimitSoc": 100,
        }
        data = {"bp_addr.PACK1": bp}
        result = parse_powerocean_http_quota(data)
        assert result["bp_soh_pct"] == 97.0
        assert result["bp_cycles"] == 50.0
        assert result["bp_remain_watth"] == 5000.0
        assert result["bp_voltage_v"] == 51.5
        assert result["bp_current_a"] == 3.2
        assert result["bp_max_cell_temp_c"] == 32.0
        assert result["bp_min_cell_temp_c"] == 28.0
        assert result["bp_env_temp_c"] == 25.0
        assert result["bp_max_mos_temp_c"] == 35.0
        assert result["bp_cell_max_vol_mv"] == 3450.0
        assert result["bp_cell_min_vol_mv"] == 3420.0
        assert result["bp_real_soc_pct"] == 80.5
        assert result["bp_real_soh_pct"] == 96.5
        assert result["bp_down_limit_soc_pct"] == 10.0
        assert result["bp_up_limit_soc_pct"] == 100.0

    def test_bp_non_numeric_skipped(self):
        data = {"bp_addr.PACK1": {"bpSoh": "unknown", "bpCycles": 10}}
        result = parse_powerocean_http_quota(data)
        assert "bp_soh_pct" not in result
        assert result["bp_cycles"] == 10.0


# ===========================================================================
# MPPT Per-String (mpptHeartBeat[0].mpptPv[0|1])
# ===========================================================================


class TestMPPTStrings:
    def test_two_pv_strings(self):
        data = {
            "mpptHeartBeat": [
                {
                    "mpptPv": [
                        {"pwr": 1500, "vol": 38.5, "amp": 8.2},
                        {"pwr": 1200, "vol": 36.0, "amp": 7.1},
                    ]
                }
            ]
        }
        result = parse_powerocean_http_quota(data)
        assert result["mppt_pv1_power_w"] == 1500.0
        assert result["mppt_pv1_voltage_v"] == 38.5
        assert result["mppt_pv1_current_a"] == 8.2
        assert result["mppt_pv2_power_w"] == 1200.0
        assert result["mppt_pv2_voltage_v"] == 36.0
        assert result["mppt_pv2_current_a"] == 7.1

    def test_single_pv_string(self):
        data = {
            "mpptHeartBeat": [
                {
                    "mpptPv": [
                        {"pwr": 2000, "vol": 40.0, "amp": 9.0},
                    ]
                }
            ]
        }
        result = parse_powerocean_http_quota(data)
        assert result["mppt_pv1_power_w"] == 2000.0
        assert "mppt_pv2_power_w" not in result

    def test_mppt_empty_heartbeat(self):
        result = parse_powerocean_http_quota({"mpptHeartBeat": []})
        assert "mppt_pv1_power_w" not in result

    def test_mppt_missing_heartbeat(self):
        result = parse_powerocean_http_quota({})
        assert "mppt_pv1_power_w" not in result

    def test_mppt_non_list_ignored(self):
        result = parse_powerocean_http_quota({"mpptHeartBeat": "invalid"})
        assert "mppt_pv1_power_w" not in result

    def test_mppt_max_two_strings(self):
        """Only first 2 PV strings are extracted."""
        data = {
            "mpptHeartBeat": [
                {
                    "mpptPv": [
                        {"pwr": 100},
                        {"pwr": 200},
                        {"pwr": 300},
                    ]
                }
            ]
        }
        result = parse_powerocean_http_quota(data)
        assert result["mppt_pv1_power_w"] == 100.0
        assert result["mppt_pv2_power_w"] == 200.0
        assert "mppt_pv3_power_w" not in result

    def test_mppt_non_numeric_skipped(self):
        data = {
            "mpptHeartBeat": [
                {
                    "mpptPv": [
                        {"pwr": "err", "vol": 38.5},
                    ]
                }
            ]
        }
        result = parse_powerocean_http_quota(data)
        assert "mppt_pv1_power_w" not in result
        assert result["mppt_pv1_voltage_v"] == 38.5


# ===========================================================================
# Grid Phase Voltages
# ===========================================================================


class TestGridPhaseVoltages:
    def test_three_phases(self):
        data = {
            "pcsAPhase": {"vol": 230.1},
            "pcsBPhase": {"vol": 229.8},
            "pcsCPhase": {"vol": 231.0},
        }
        result = parse_powerocean_http_quota(data)
        assert result["grid_phase_a_voltage_v"] == 230.1
        assert result["grid_phase_b_voltage_v"] == 229.8
        assert result["grid_phase_c_voltage_v"] == 231.0

    def test_single_phase(self):
        data = {"pcsAPhase": {"vol": 230.5}}
        result = parse_powerocean_http_quota(data)
        assert result["grid_phase_a_voltage_v"] == 230.5
        assert "grid_phase_b_voltage_v" not in result

    def test_phase_missing_vol(self):
        data = {"pcsAPhase": {"amp": 10.5}}
        result = parse_powerocean_http_quota(data)
        assert "grid_phase_a_voltage_v" not in result

    def test_phase_non_dict_ignored(self):
        data = {"pcsAPhase": "invalid"}
        result = parse_powerocean_http_quota(data)
        assert "grid_phase_a_voltage_v" not in result

    def test_flat_key_phases(self):
        """GET /quota/all returns flat keys like pcsAPhase.vol."""
        data = {
            "pcsAPhase.vol": 236.2,
            "pcsBPhase.vol": 235.1,
            "pcsCPhase.vol": 235.5,
        }
        result = parse_powerocean_http_quota(data)
        assert result["grid_phase_a_voltage_v"] == pytest.approx(236.2)
        assert result["grid_phase_b_voltage_v"] == pytest.approx(235.1)
        assert result["grid_phase_c_voltage_v"] == pytest.approx(235.5)

    def test_flat_key_priority_over_nested(self):
        """Flat keys take priority over nested dicts."""
        data = {
            "pcsAPhase.vol": 236.0,
            "pcsAPhase": {"vol": 999.0},
        }
        result = parse_powerocean_http_quota(data)
        assert result["grid_phase_a_voltage_v"] == pytest.approx(236.0)


# ===========================================================================
# Energy Stream / EMS Change Report
# ===========================================================================


class TestEnergyStream:
    def test_energy_stream_wh_to_kwh(self):
        """API returns Wh, parser must convert to kWh."""
        data = {
            "energy_stream.solarTotalEnergy": 5000,
            "energy_stream.homeTotalEnergy": 3000,
            "energy_stream.gridInTotalEnergy": 1000,
            "energy_stream.gridOutTotalEnergy": 2000,
            "energy_stream.bpChgTotalEnergy": 4000,
            "energy_stream.bpDsgTotalEnergy": 3500,
        }
        result = parse_powerocean_http_quota(data)
        assert result["solar_energy_kwh"] == 5.0
        assert result["home_energy_kwh"] == 3.0
        assert result["grid_import_energy_kwh"] == 1.0
        assert result["grid_export_energy_kwh"] == 2.0
        assert result["batt_charge_energy_kwh"] == 4.0
        assert result["batt_discharge_energy_kwh"] == 3.5

    def test_top_level_energy_fallback(self):
        """Top-level energy keys used when energy_stream.* not present."""
        data = {
            "mpptTotalEnergy": 8000,
            "sysTotalLoadEnergy": 6000,
        }
        result = parse_powerocean_http_quota(data)
        assert result["solar_energy_kwh"] == 8.0
        assert result["home_energy_kwh"] == 6.0

    def test_energy_stream_takes_priority(self):
        """energy_stream.* takes priority over top-level keys."""
        data = {
            "energy_stream.solarTotalEnergy": 5000,
            "mpptTotalEnergy": 9999,  # should be ignored
        }
        result = parse_powerocean_http_quota(data)
        assert result["solar_energy_kwh"] == 5.0

    def test_ems_change_report_energy_fallback(self):
        """ems_change_report.* energy totals used when energy_stream.* and top-level not present."""
        data = {
            "ems_change_report.bpTotalChgEnergy": 4398306,
            "ems_change_report.bpTotalDsgEnergy": 4217302,
        }
        result = parse_powerocean_http_quota(data)
        assert result["batt_charge_energy_kwh"] == pytest.approx(4398.306)
        assert result["batt_discharge_energy_kwh"] == pytest.approx(4217.302)

    def test_energy_stream_priority_over_ems_change_report(self):
        """energy_stream.* takes priority over ems_change_report.*."""
        data = {
            "energy_stream.bpChgTotalEnergy": 5000,
            "ems_change_report.bpTotalChgEnergy": 9999999,
        }
        result = parse_powerocean_http_quota(data)
        assert result["batt_charge_energy_kwh"] == 5.0

    def test_ems_bp_online_sum(self):
        """ems_change_report.bpOnlineSum → bp_online_sum."""
        data = {"ems_change_report.bpOnlineSum": 2}
        result = parse_powerocean_http_quota(data)
        assert result["bp_online_sum"] == 2

    def test_ems_feed_mode(self):
        data = {"ems_change_report.emsFeedMode": 1}
        result = parse_powerocean_http_quota(data)
        assert result["ems_feed_mode"] == 1

    def test_pcs_grid_freq(self):
        data = {"pcs_change_report.gridFreq": 50.02}
        result = parse_powerocean_http_quota(data)
        assert result["pcs_ac_freq_hz"] == 50.02


# ===========================================================================
# New Sensors: Phase Power/Current, EMS State, PV Inverter
# ===========================================================================


class TestPhaseActivePowerAndCurrent:
    def test_flat_key_phase_power_and_current(self):
        data = {
            "pcsAPhase.actPwr": -287.0,
            "pcsAPhase.amp": 1.8,
            "pcsBPhase.actPwr": -190.0,
            "pcsBPhase.amp": 1.0,
            "pcsCPhase.actPwr": -356.0,
            "pcsCPhase.amp": 1.7,
        }
        result = parse_powerocean_http_quota(data)
        assert result["grid_phase_a_active_power_w"] == pytest.approx(-287.0)
        assert result["grid_phase_a_current_a"] == pytest.approx(1.8)
        assert result["grid_phase_b_active_power_w"] == pytest.approx(-190.0)
        assert result["grid_phase_c_current_a"] == pytest.approx(1.7)


class TestEMSState:
    def test_ems_work_mode_string(self):
        data = {"ems_change_report.emsWordMode": "WORKMODE_SELFUSE"}
        result = parse_powerocean_http_quota(data)
        assert result["ems_work_mode"] == "WORKMODE_SELFUSE"

    def test_pcs_run_state_string(self):
        data = {"ems_change_report.pcsRunSta": "RUNSTA_RUN"}
        result = parse_powerocean_http_quota(data)
        assert result["pcs_run_state"] == "RUNSTA_RUN"

    def test_grid_status_numeric(self):
        data = {"ems_change_report.sysGridSta": 0}
        result = parse_powerocean_http_quota(data)
        assert result["grid_status"] == 0

    def test_power_factor(self):
        data = {"ems_change_report.pcsPfValue": 0.98}
        result = parse_powerocean_http_quota(data)
        assert result["pcs_power_factor"] == pytest.approx(0.98)

    def test_feed_power_limit(self):
        data = {"ems_change_report.emsFeedPwr": 10000}
        result = parse_powerocean_http_quota(data)
        assert result["ems_feed_power_limit_w"] == 10000

    def test_feed_ratio(self):
        data = {"ems_change_report.emsFeedRatio": 100}
        result = parse_powerocean_http_quota(data)
        assert result["ems_feed_ratio_pct"] == 100

    def test_batt_charge_discharge_state(self):
        data = {"ems_change_report.bpChgDsgSta": 1}
        result = parse_powerocean_http_quota(data)
        assert result["batt_charge_discharge_state"] == 1


class TestPVInverterPower:
    def test_pv_inverter_power(self):
        data = {"pvInvPwr": 3200.0}
        result = parse_powerocean_http_quota(data)
        assert result["pv_inverter_power_w"] == 3200.0

    def test_pv_inverter_power_zero(self):
        data = {"pvInvPwr": 0.0}
        result = parse_powerocean_http_quota(data)
        assert result["pv_inverter_power_w"] == 0.0


# ===========================================================================
# Edge Cases / Safe Float
# ===========================================================================


class TestSafeFloat:
    def test_none_value_skipped(self):
        result = parse_powerocean_http_quota({"mpptPwr": None})
        assert "solar_w" not in result

    def test_string_number_parsed(self):
        result = parse_powerocean_http_quota({"mpptPwr": "3500.5"})
        assert result["solar_w"] == 3500.5

    def test_non_numeric_string_skipped(self):
        result = parse_powerocean_http_quota({"mpptPwr": "error"})
        assert "solar_w" not in result

    def test_zero_included(self):
        result = parse_powerocean_http_quota({"mpptPwr": 0})
        assert result["solar_w"] == 0.0
