"""Delta 2 Max IoT-API JSON report parser.

Parses JSON reports from the EcoFlow IoT platform for Delta-series devices.
The IoT API sends individual reports per module:
  {"typeCode": "pdStatus",   "params": {"soc": 85, "wattsInSum": 200, ...}}
  {"typeCode": "invStatus",  "params": {"outputWatts": 109, ...}}
  {"typeCode": "bmsStatus",  "params": {"vol": 52000, "temp": 40, ...}}
  {"typeCode": "mpptStatus", "params": {"inWatts": 300, ...}}
"""

from typing import Dict, Optional

# Field map: "typeCode.paramKey" -> destination key
DELTA2MAX_FIELD_MAP: Dict[str, str] = {
    # --- pdStatus (pd.*) ---
    "pdStatus.soc": "soc",
    "pdStatus.wattsInSum": "watts_in_sum",
    "pdStatus.wattsOutSum": "watts_out_sum",
    "pdStatus.remainTime": "remain_time_min",
    "pdStatus.usb1Watts": "usb1_w",
    "pdStatus.usb2Watts": "usb2_w",
    "pdStatus.qcUsb1Watts": "usb_qc1_w",
    "pdStatus.qcUsb2Watts": "usb_qc2_w",
    "pdStatus.typec1Watts": "typec1_w",
    "pdStatus.typec2Watts": "typec2_w",
    "pdStatus.dcOutState": "dc_out_enabled",
    "pdStatus.carWatts": "car_out_w",
    "pdStatus.chgDsgState": "chg_dsg_state",
    "pdStatus.errCode": "pd_err_code",
    # --- invStatus (inv.*) ---
    "invStatus.inputWatts": "ac_in_w",
    "invStatus.outputWatts": "ac_out_w",
    "invStatus.invOutVol": "ac_out_vol_mv",
    "invStatus.invOutAmp": "ac_out_amp_ma",
    "invStatus.invOutFreq": "ac_out_freq_hz",
    "invStatus.acInVol": "ac_in_vol_mv",
    "invStatus.acInAmp": "ac_in_amp_ma",
    "invStatus.acInFreq": "ac_in_freq_hz",
    "invStatus.cfgAcEnabled": "ac_enabled",
    "invStatus.cfgAcXboost": "ac_xboost",
    "invStatus.outTemp": "inv_out_temp_c",
    "invStatus.dcInVol": "dc_in_vol_mv",
    "invStatus.dcInTemp": "dc_in_temp_c",
    "invStatus.fanState": "inv_fan_state",
    "invStatus.cfgAcOutVol": "ac_cfg_out_vol_mv",
    "invStatus.chargerType": "charger_type",
    "invStatus.acChgRatedPower": "ac_chg_rated_power_w",
    "invStatus.errCode": "inv_err_code",
    # --- bmsStatus (bms.*) ---
    "bmsStatus.vol": "batt_voltage_mv",
    "bmsStatus.amp": "batt_current_ma",
    "bmsStatus.temp": "batt_temp_raw",
    "bmsStatus.soc": "bms_soc",
    "bmsStatus.soh": "bms_soh_pct",
    "bmsStatus.cycles": "bms_cycles",
    "bmsStatus.maxCellTemp": "batt_max_cell_temp_c",
    "bmsStatus.minCellTemp": "batt_min_cell_temp_c",
    "bmsStatus.maxCellVol": "batt_max_cell_vol_mv",
    "bmsStatus.minCellVol": "batt_min_cell_vol_mv",
    "bmsStatus.maxMosTemp": "batt_max_mos_temp_c",
    "bmsStatus.remainCap": "batt_remain_cap_mah",
    "bmsStatus.fullCap": "batt_full_cap_mah",
    "bmsStatus.designCap": "batt_design_cap_mah",
    "bmsStatus.errCode": "bms_err_code",
    "bmsStatus.f32ShowSoc": "bms_precise_soc",
    # --- mpptStatus (mppt.*) ---
    "mpptStatus.inWatts": "solar_in_w",
    "mpptStatus.outWatts": "mppt_out_w",
    "mpptStatus.inVol": "solar_in_vol_dv",
    "mpptStatus.inAmp": "solar_in_amp_ma",
    "mpptStatus.mpptTemp": "mppt_temp_c",
    "mpptStatus.carOutWatts": "car_12v_out_w",
    "mpptStatus.carState": "car_12v_enabled",
    "mpptStatus.dcdc12vWatts": "dcdc_12v_w",
    "mpptStatus.dcdc12vVol": "dcdc_12v_vol_mv",
    "mpptStatus.pv2InWatts": "solar2_in_w",
    "mpptStatus.pv2InVol": "solar2_in_vol_dv",
    "mpptStatus.pv2InAmp": "solar2_in_amp_ma",
    "mpptStatus.pv2MpptTemp": "solar2_mppt_temp_c",
    "mpptStatus.chgState": "mppt_chg_state",
    "mpptStatus.faultCode": "mppt_fault_code",
    # --- emsStatus (ems.*) ---
    "emsStatus.chgRemainTime": "chg_remain_time_min",
    "emsStatus.dsgRemainTime": "dsg_remain_time_min",
    "emsStatus.maxChargeSoc": "max_charge_soc",
    "emsStatus.minDsgSoc": "min_discharge_soc",
    "emsStatus.lcdShowSoc": "ems_lcd_soc",
    "emsStatus.f32LcdShowSoc": "ems_precise_soc",
    "emsStatus.chgState": "ems_chg_state",
    "emsStatus.fanLevel": "fan_level",
    "emsStatus.openUpsFlag": "ups_enabled",
}


def parse_delta_report(
    payload: dict,
    field_map: Optional[Dict[str, str]] = None,
) -> Dict[str, float]:
    """Parse a Delta 2 Max IoT-API JSON report.

    The IoT-API format is: {"typeCode": "pdStatus", "params": {"soc": 85, ...}}
    field_map uses "typeCode.paramKey" as lookup key.
    """
    if field_map is None:
        field_map = DELTA2MAX_FIELD_MAP

    type_code = payload.get("typeCode", "")
    params = payload.get("params", {})
    if not isinstance(params, dict) or not type_code:
        return {}

    parsed: Dict[str, float] = {}
    for param_key, value in params.items():
        if not isinstance(value, (int, float)):
            continue
        lookup = f"{type_code}.{param_key}"
        dest_key = field_map.get(lookup)
        if dest_key is None:
            continue
        # Temperature offset: bmsStatus.temp has +15 offset
        if dest_key == "batt_temp_raw":
            parsed["batt_temp_c"] = float(value) - 15.0
        else:
            parsed[dest_key] = float(value)

    # --- Unit conversions ---

    # Voltages: mV -> V
    for mv_key, v_key in [
        ("batt_voltage_mv", "batt_voltage_v"),
        ("ac_out_vol_mv", "ac_out_vol_v"),
        ("ac_in_vol_mv", "ac_in_vol_v"),
        ("ac_cfg_out_vol_mv", "ac_cfg_out_vol_v"),
        ("dc_in_vol_mv", "dc_in_vol_v"),
        ("batt_max_cell_vol_mv", "batt_max_cell_vol_mv"),  # stays mV (cell level)
        ("batt_min_cell_vol_mv", "batt_min_cell_vol_mv"),  # stays mV (cell level)
        ("dcdc_12v_vol_mv", "dcdc_12v_vol_v"),
    ]:
        if mv_key in parsed and v_key != mv_key:
            parsed[v_key] = parsed.pop(mv_key) / 1000.0

    # Voltages: dV -> V (deci-volt, amplified 10x)
    for dv_key, v_key in [
        ("solar_in_vol_dv", "solar_in_vol_v"),
        ("solar2_in_vol_dv", "solar2_in_vol_v"),
    ]:
        if dv_key in parsed:
            parsed[v_key] = parsed.pop(dv_key) / 10.0

    # Currents: mA -> A
    for ma_key, a_key in [
        ("batt_current_ma", "batt_current_a"),
        ("ac_out_amp_ma", "ac_out_amp_a"),
        ("ac_in_amp_ma", "ac_in_amp_a"),
        ("solar_in_amp_ma", "solar_in_amp_a"),
        ("solar2_in_amp_ma", "solar2_in_amp_a"),
    ]:
        if ma_key in parsed:
            parsed[a_key] = parsed.pop(ma_key) / 1000.0

    return parsed
