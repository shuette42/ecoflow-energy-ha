"""Tests for PowerOcean HTTP quota API response parser."""

import json

import pytest

from ecoflow_energy.ecoflow.parsers.powerocean import (
    parse_powerocean_http_quota,
    _extract_battery_pack,
    _extract_energy_stream,
    _extract_all_battery_packs,
    _extract_ems_extended,
    _is_real_battery_pack,
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

    def test_bp_remain_watth_summed_across_packs(self):
        """bp_remain_watth is the sum of all real packs, not just pack 1."""
        data = {
            "bp_addr.PACK1": {
                "bpSoh": 98,
                "bpCycles": 45,
                "bpRemainWatth": 2400,
                "bpVol": 52.1,
            },
            "bp_addr.PACK2": {
                "bpSoh": 97,
                "bpCycles": 40,
                "bpRemainWatth": 2600,
                "bpVol": 51.8,
            },
        }
        result = parse_powerocean_http_quota(data)
        assert result["bp_remain_watth"] == 5000.0

    def test_bp_remain_watth_single_pack(self):
        """Single pack: bp_remain_watth equals that pack's value."""
        data = {
            "bp_addr.PACK1": {
                "bpSoh": 98,
                "bpRemainWatth": 4800,
                "bpVol": 52.1,
            },
        }
        result = parse_powerocean_http_quota(data)
        assert result["bp_remain_watth"] == 4800.0

    def test_bp_remain_watth_phantom_excluded(self):
        """Phantom pack (all zeros) excluded from remain_watth sum."""
        data = {
            "bp_addr.EMS": {},
            "bp_addr.PACK1": {
                "bpSoh": 98,
                "bpRemainWatth": 2400,
                "bpVol": 52.1,
            },
            "bp_addr.PACK2": {
                "bpSoh": 97,
                "bpRemainWatth": 2600,
                "bpVol": 51.8,
            },
        }
        result = parse_powerocean_http_quota(data)
        assert result["bp_remain_watth"] == 5000.0


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
        assert result["ems_feed_mode"] == "time_of_use"

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


class TestPhaseReactivePower:
    def test_flat_key_reactive_power_all_phases(self):
        data = {
            "pcsAPhase.reactPwr": 120.5,
            "pcsBPhase.reactPwr": 95.3,
            "pcsCPhase.reactPwr": 110.7,
        }
        result = parse_powerocean_http_quota(data)
        assert result["grid_phase_a_reactive_power_var"] == pytest.approx(120.5)
        assert result["grid_phase_b_reactive_power_var"] == pytest.approx(95.3)
        assert result["grid_phase_c_reactive_power_var"] == pytest.approx(110.7)

    def test_nested_dict_reactive_power(self):
        data = {
            "pcsAPhase": {"reactPwr": -50.0},
            "pcsBPhase": {"reactPwr": 30.0},
        }
        result = parse_powerocean_http_quota(data)
        assert result["grid_phase_a_reactive_power_var"] == pytest.approx(-50.0)
        assert result["grid_phase_b_reactive_power_var"] == pytest.approx(30.0)

    def test_reactive_power_missing(self):
        data = {"pcsAPhase": {"vol": 230.0}}
        result = parse_powerocean_http_quota(data)
        assert "grid_phase_a_reactive_power_var" not in result


class TestPhaseApparentPower:
    def test_flat_key_apparent_power_all_phases(self):
        data = {
            "pcsAPhase.apparentPwr": 450.0,
            "pcsBPhase.apparentPwr": 380.5,
            "pcsCPhase.apparentPwr": 420.1,
        }
        result = parse_powerocean_http_quota(data)
        assert result["grid_phase_a_apparent_power_va"] == pytest.approx(450.0)
        assert result["grid_phase_b_apparent_power_va"] == pytest.approx(380.5)
        assert result["grid_phase_c_apparent_power_va"] == pytest.approx(420.1)

    def test_nested_dict_apparent_power(self):
        data = {
            "pcsAPhase": {"apparentPwr": 500.0},
            "pcsCPhase": {"apparentPwr": 470.0},
        }
        result = parse_powerocean_http_quota(data)
        assert result["grid_phase_a_apparent_power_va"] == pytest.approx(500.0)
        assert result["grid_phase_c_apparent_power_va"] == pytest.approx(470.0)

    def test_apparent_power_missing(self):
        data = {"pcsAPhase": {"vol": 230.0}}
        result = parse_powerocean_http_quota(data)
        assert "grid_phase_a_apparent_power_va" not in result

    def test_flat_key_priority_over_nested_for_reactive(self):
        """Flat keys take priority over nested dicts for reactive power."""
        data = {
            "pcsAPhase.reactPwr": 100.0,
            "pcsAPhase": {"reactPwr": 999.0},
        }
        result = parse_powerocean_http_quota(data)
        assert result["grid_phase_a_reactive_power_var"] == pytest.approx(100.0)


class TestEMSState:
    def test_ems_work_mode_string(self):
        data = {"ems_change_report.emsWordMode": "WORKMODE_SELFUSE"}
        result = parse_powerocean_http_quota(data)
        assert result["ems_work_mode"] == "self_use"

    def test_pcs_run_state_string(self):
        data = {"ems_change_report.pcsRunSta": "RUNSTA_RUN"}
        result = parse_powerocean_http_quota(data)
        assert result["pcs_run_state"] == "running"

    def test_grid_status_numeric(self):
        data = {"ems_change_report.sysGridSta": 0}
        result = parse_powerocean_http_quota(data)
        assert result["grid_status"] == "disconnected"

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
        assert result["batt_charge_discharge_state"] == "discharging"


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


# ===========================================================================
# Multi-Battery-Pack Extraction (_extract_all_battery_packs)
# ===========================================================================


class TestMultiBatteryPack:
    """Tests for per-pack battery extraction (pack{n}_* keys)."""

    # Real probe data structure (2 packs from HJ31ZDH4ZF7H0170)
    PACK1_DATA = {
        "bpPwr": 2486.4836, "bpSoc": 76, "bpSoh": 100,
        "bpCycles": 464, "bpVol": 54.671, "bpAmp": 45.48085,
        "bpRemainWatth": 3891.2, "bpMaxCellTemp": 23.0,
        "bpMinCellTemp": 21.0, "bpEnvTemp": 33.0,
        "bpCalendarSoh": 100.0, "bpCycleSoh": 100.0,
        "bpMaxMosTemp": 41.0, "bpHvMosTemp": 41.0,
        "bpLvMosTemp": 38.0, "bpBusVol": 809.11285,
        "bpPtcTemp": 22.0, "bpCellMaxVol": 3422.0,
        "bpCellMinVol": 3414.0, "bpDesignCap": 100000,
        "bpFullCap": 100000, "bpErrCode": 0,
        "bpAccuChgEnergy": 2238706, "bpAccuDsgEnergy": 2147108,
    }

    PACK2_DATA = {
        "bpPwr": 2529.1938, "bpSoc": 76, "bpSoh": 100,
        "bpCycles": 464, "bpVol": 54.698, "bpAmp": 46.23924,
        "bpRemainWatth": 3891.2, "bpMaxCellTemp": 24.0,
        "bpMinCellTemp": 21.0, "bpEnvTemp": 34.0,
        "bpCalendarSoh": 100.0, "bpCycleSoh": 100.0,
        "bpMaxMosTemp": 42.0, "bpHvMosTemp": 42.0,
        "bpLvMosTemp": 39.0, "bpBusVol": 809.7283,
        "bpPtcTemp": 22.0, "bpCellMaxVol": 3420.0,
        "bpCellMinVol": 3412.0, "bpDesignCap": 100000,
        "bpFullCap": 100000, "bpErrCode": 0,
        "bpAccuChgEnergy": 2207455, "bpAccuDsgEnergy": 2122737,
    }

    def _two_pack_data(self):
        """Build quota_data with 2 battery packs (dict form)."""
        return {
            "bp_addr.HJ32ZDH5ZG190227": dict(self.PACK1_DATA),
            "bp_addr.HJ32ZDH5ZG190278": dict(self.PACK2_DATA),
        }

    def test_extract_two_packs(self):
        """Two packs produce pack1_* and pack2_* keys."""
        result = _extract_all_battery_packs(self._two_pack_data())
        assert "pack1_soc" in result
        assert "pack2_soc" in result
        assert "pack3_soc" not in result

    def test_pack1_soc_from_probe_data(self):
        """Pack 1 SoC matches real probe value."""
        result = _extract_all_battery_packs(self._two_pack_data())
        assert result["pack1_soc"] == 76.0

    def test_pack2_power_from_probe_data(self):
        """Pack 2 power matches real probe value."""
        result = _extract_all_battery_packs(self._two_pack_data())
        assert result["pack2_power_w"] == pytest.approx(2529.1938)

    def test_accu_energy_wh_to_kwh(self):
        """Accumulated energy converts from Wh to kWh."""
        result = _extract_all_battery_packs(self._two_pack_data())
        assert result["pack1_accu_chg_energy_kwh"] == pytest.approx(2238.706)
        assert result["pack1_accu_dsg_energy_kwh"] == pytest.approx(2147.108)
        assert result["pack2_accu_chg_energy_kwh"] == pytest.approx(2207.455)
        assert result["pack2_accu_dsg_energy_kwh"] == pytest.approx(2122.737)

    def test_pack1_all_core_fields(self):
        """All 7 core fields extracted for pack 1."""
        result = _extract_all_battery_packs(self._two_pack_data())
        assert result["pack1_soc"] == 76.0
        assert result["pack1_power_w"] == pytest.approx(2486.4836)
        assert result["pack1_soh"] == 100.0
        assert result["pack1_cycles"] == 464.0
        assert result["pack1_voltage_v"] == pytest.approx(54.671)
        assert result["pack1_current_a"] == pytest.approx(45.48085)
        assert result["pack1_remain_watth"] == pytest.approx(3891.2)

    def test_pack1_diagnostic_fields(self):
        """Diagnostic fields extracted for pack 1."""
        result = _extract_all_battery_packs(self._two_pack_data())
        assert result["pack1_max_cell_temp_c"] == 23.0
        assert result["pack1_min_cell_temp_c"] == 21.0
        assert result["pack1_env_temp_c"] == 33.0
        assert result["pack1_calendar_soh"] == 100.0
        assert result["pack1_cycle_soh"] == 100.0
        assert result["pack1_max_mos_temp_c"] == 41.0
        assert result["pack1_hv_mos_temp_c"] == 41.0
        assert result["pack1_lv_mos_temp_c"] == 38.0
        assert result["pack1_bus_voltage_v"] == pytest.approx(809.11285)
        assert result["pack1_ptc_temp_c"] == 22.0
        assert result["pack1_cell_max_vol_mv"] == 3422.0
        assert result["pack1_cell_min_vol_mv"] == 3414.0
        assert result["pack1_design_cap_mah"] == 100000.0
        assert result["pack1_full_cap_mah"] == 100000.0
        assert result["pack1_error_code"] == 0.0

    def test_pack_from_json_string(self):
        """Battery pack data as JSON string (API format)."""
        data = {
            "bp_addr.PACK_SN1": json.dumps({"bpSoc": 80, "bpPwr": 1000}),
        }
        result = _extract_all_battery_packs(data)
        assert result["pack1_soc"] == 80.0
        assert result["pack1_power_w"] == 1000.0

    def test_update_time_ignored(self):
        """bp_addr.updateTime is not a battery pack."""
        data = {"bp_addr.updateTime": "2026-03-30 17:46:38"}
        result = _extract_all_battery_packs(data)
        assert len(result) == 0

    def test_max_5_packs(self):
        """Packs beyond 5 are ignored."""
        data = {}
        for i in range(7):
            data[f"bp_addr.PACK_{i}"] = {"bpSoc": 50 + i}
        result = _extract_all_battery_packs(data)
        assert "pack5_soc" in result
        assert "pack6_soc" not in result
        assert "pack7_soc" not in result

    def test_invalid_json_skipped(self):
        """Invalid JSON string is skipped, valid pack still extracted."""
        data = {
            "bp_addr.BAD": "not valid json",
            "bp_addr.GOOD": {"bpSoc": 90},
        }
        result = _extract_all_battery_packs(data)
        assert result["pack1_soc"] == 90.0

    def test_non_dict_value_skipped(self):
        """Non-dict/non-string value is skipped."""
        data = {
            "bp_addr.NUM": 12345,
            "bp_addr.DICT": {"bpSoc": 70},
        }
        result = _extract_all_battery_packs(data)
        assert result["pack1_soc"] == 70.0

    def test_non_numeric_field_skipped(self):
        """Non-numeric field values are skipped."""
        data = {"bp_addr.P1": {"bpSoc": "unknown", "bpVol": 54.0}}
        result = _extract_all_battery_packs(data)
        assert "pack1_soc" not in result
        assert result["pack1_voltage_v"] == 54.0

    def test_no_packs(self):
        """No battery packs returns empty dict."""
        result = _extract_all_battery_packs({"mpptPwr": 100})
        assert result == {}

    def test_existing_bp_sensors_unchanged(self):
        """Multi-pack extraction does not affect existing bp_* sensors."""
        data = self._two_pack_data()
        result = parse_powerocean_http_quota(data)
        # Existing bp_* keys come from first pack via _extract_battery_pack
        assert result["bp_soh_pct"] == 100.0
        assert result["bp_cycles"] == 464.0
        assert result["bp_voltage_v"] == pytest.approx(54.671)
        # Pack-specific keys also present
        assert result["pack1_soc"] == 76.0
        assert result["pack2_soc"] == 76.0

    def test_full_integration_with_parse(self):
        """Full parser produces both bp_* and pack{n}_* keys."""
        data = {
            "mpptPwr": 3000,
            "bpSoc": 76,
            **self._two_pack_data(),
        }
        result = parse_powerocean_http_quota(data)
        assert result["solar_w"] == 3000.0
        assert result["soc_pct"] == 76.0
        assert result["pack1_power_w"] == pytest.approx(2486.4836)
        assert result["pack2_power_w"] == pytest.approx(2529.1938)

    def test_phantom_empty_pack_skipped(self):
        """Phantom/empty pack at position 0 is skipped — real packs start at 1."""
        data = {
            "bp_addr.PHANTOM_EMS": {},  # empty entry (EMS module)
            "bp_addr.HJ32ZDH5ZG190227": dict(self.PACK1_DATA),
            "bp_addr.HJ32ZDH5ZG190278": dict(self.PACK2_DATA),
        }
        result = _extract_all_battery_packs(data)
        # Real packs numbered 1 and 2 (phantom skipped)
        assert result["pack1_soc"] == 76.0
        assert result["pack2_soc"] == 76.0
        assert "pack3_soc" not in result

    def test_phantom_pack_no_battery_fields(self):
        """Pack with non-battery fields only is skipped."""
        data = {
            "bp_addr.EMS_MODULE": {"someOtherField": 42, "anotherField": "abc"},
            "bp_addr.REAL_PACK": dict(self.PACK1_DATA),
        }
        result = _extract_all_battery_packs(data)
        assert result["pack1_soc"] == 76.0
        assert "pack2_soc" not in result

    def test_aggregate_bp_skips_phantom(self):
        """_extract_battery_pack picks first real pack, not phantom."""
        data = {
            "bp_addr.PHANTOM": {},
            "bp_addr.REAL": {"bpSoh": 98, "bpCycles": 42},
        }
        result = {}
        _extract_battery_pack(data, result)
        assert result["bp_soh_pct"] == 98.0
        assert result["bp_cycles"] == 42.0

    def test_aggregate_bp_phantom_only(self):
        """Only phantom packs — no aggregate sensors produced."""
        data = {"bp_addr.PHANTOM": {}}
        result = {}
        _extract_battery_pack(data, result)
        assert "bp_soh_pct" not in result


# ===========================================================================
# Phantom Pack Detection (_is_real_battery_pack)
# ===========================================================================


class TestIsRealBatteryPack:
    """Tests for the phantom pack detection helper."""

    def test_real_pack_with_soc(self):
        assert _is_real_battery_pack({"bpSoc": 76}) is True

    def test_real_pack_with_power(self):
        assert _is_real_battery_pack({"bpPwr": 2500.0}) is True

    def test_real_pack_with_soh(self):
        assert _is_real_battery_pack({"bpSoh": 100}) is True

    def test_real_pack_with_cycles(self):
        assert _is_real_battery_pack({"bpCycles": 464}) is True

    def test_real_pack_with_voltage(self):
        assert _is_real_battery_pack({"bpVol": 54.6}) is True

    def test_empty_dict_is_phantom(self):
        assert _is_real_battery_pack({}) is False

    def test_non_battery_fields_is_phantom(self):
        assert _is_real_battery_pack({"someField": 42, "other": "abc"}) is False

    def test_zero_soc_is_real(self):
        """SoC of 0 (deeply discharged) is still a real pack."""
        assert _is_real_battery_pack({"bpSoc": 0}) is True

    def test_none_values_is_phantom(self):
        """All indicator fields explicitly None is phantom."""
        assert _is_real_battery_pack({"bpSoc": None, "bpPwr": None}) is False


# ===========================================================================
# EMS Extended Fields
# ===========================================================================


class TestEMSExtended:
    """Tests for EMS extended sensor extraction."""

    def test_ems_charge_upper_limit(self):
        data = {"ems_change_report.sysBatChgUpLimit": 100}
        result = parse_powerocean_http_quota(data)
        assert result["ems_charge_upper_limit_pct"] == 100.0

    def test_ems_discharge_lower_limit(self):
        data = {"ems_change_report.sysBatDsgDownLimit": 0}
        result = parse_powerocean_http_quota(data)
        assert result["ems_discharge_lower_limit_pct"] == 0.0

    def test_ems_keep_soc(self):
        data = {"ems_change_report.emsKeepSoc": 0}
        result = parse_powerocean_http_quota(data)
        assert result["ems_keep_soc_pct"] == 0.0

    def test_ems_backup_ratio(self):
        data = {"ems_change_report.sysBatBackupRatio": 0}
        result = parse_powerocean_http_quota(data)
        assert result["ems_backup_ratio_pct"] == 0.0

    def test_mppt_fault_codes(self):
        data = {
            "ems_change_report.mppt1FaultCode": 0,
            "ems_change_report.mppt2FaultCode": 0,
        }
        result = parse_powerocean_http_quota(data)
        assert result["mppt1_fault_code"] == 0.0
        assert result["mppt2_fault_code"] == 0.0

    def test_pcs_error_codes(self):
        data = {
            "ems_change_report.pcsAcErrCode": 0,
            "ems_change_report.pcsDcErrCode": 0,
            "ems_change_report.pcsAcWarningCode": 0,
        }
        result = parse_powerocean_http_quota(data)
        assert result["pcs_ac_error_code"] == 0.0
        assert result["pcs_dc_error_code"] == 0.0
        assert result["pcs_ac_warning_code"] == 0.0

    def test_connectivity_status(self):
        data = {
            "ems_change_report.wifiStaStat": 0,
            "ems_change_report.ethWanStat": 0,
            "ems_change_report.iot4gSta": 2,
        }
        result = parse_powerocean_http_quota(data)
        assert result["wifi_status"] == "disconnected"
        assert result["ethernet_status"] == "disconnected"
        assert result["cellular_status"] == "connected"

    def test_ems_led_brightness(self):
        data = {"ems_change_report.emsCtrlLedBright": 10}
        result = parse_powerocean_http_quota(data)
        assert result["ems_led_brightness"] == 10.0

    def test_ems_work_state(self):
        data = {"ems_change_report.emsWorkState": 0}
        result = parse_powerocean_http_quota(data)
        assert result["ems_work_state"] == "pre_power_on"

    def test_ai_schedule_battery_capacity(self):
        data = {"ems_change_report.poAiSchedule.bpFullCap": 10240.0}
        result = parse_powerocean_http_quota(data)
        assert result["ems_total_battery_capacity_wh"] == 10240.0

    def test_ai_schedule_power_limits(self):
        data = {
            "ems_change_report.poAiSchedule.pcsMaxOutPwr": 9985.556,
            "ems_change_report.poAiSchedule.pcsMaxInPwr": 10000.0,
            "ems_change_report.poAiSchedule.bpChgPwrMax": 5000.0,
            "ems_change_report.poAiSchedule.bpDsgPwrMax": 6600.0,
        }
        result = parse_powerocean_http_quota(data)
        assert result["pcs_max_output_power_w"] == pytest.approx(9985.556)
        assert result["pcs_max_input_power_w"] == 10000.0
        assert result["bp_max_charge_power_w"] == 5000.0
        assert result["bp_max_discharge_power_w"] == 6600.0

    def test_ems_extended_missing_keys(self):
        """Missing keys produce no sensor entries."""
        result = parse_powerocean_http_quota({})
        assert "ems_charge_upper_limit_pct" not in result
        assert "mppt1_fault_code" not in result
        assert "wifi_status" not in result
        assert "ems_total_battery_capacity_wh" not in result

    def test_ems_extended_does_not_conflict_with_existing(self):
        """New EMS sensors do not conflict with existing ems_feed_mode etc."""
        data = {
            "ems_change_report.emsFeedMode": 2,
            "ems_change_report.sysBatChgUpLimit": 100,
            "ems_change_report.emsCtrlLedBright": 10,
        }
        result = parse_powerocean_http_quota(data)
        assert result["ems_feed_mode"] == "backup"
        assert result["ems_charge_upper_limit_pct"] == 100.0
        assert result["ems_led_brightness"] == 10.0
