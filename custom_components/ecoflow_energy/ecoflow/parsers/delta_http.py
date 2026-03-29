"""Delta 2 Max HTTP Quota API response parser.

Parses the response from GET /iot-open/sign/device/quota/all into
flat sensor keys matching DELTA2MAX_SENSORS in const.py.

The HTTP API returns keys in "module.field" format (e.g. "pd.soc"),
different from MQTT which uses "typeCode" format (e.g. "pdStatus").

Reference: EcoFlow IoT Developer Platform — DELTA 2 MAX section.
"""

from __future__ import annotations

from typing import Any

from . import _safe_float

# Mapping: HTTP API key ("module.field") -> sensor key
# Based on GetAllQuotaResponse from EcoFlow IoT Developer Platform
DELTA2MAX_HTTP_FIELD_MAP: dict[str, str] = {
    # --- pd (Power Distribution) ---
    "pd.soc": "soc",
    "pd.wattsInSum": "watts_in_sum",
    "pd.wattsOutSum": "watts_out_sum",
    "pd.remainTime": "remain_time_min",
    "pd.usb1Watts": "usb1_w",
    "pd.usb2Watts": "usb2_w",
    "pd.qcUsb1Watts": "usb_qc1_w",
    "pd.qcUsb2Watts": "usb_qc2_w",
    "pd.typec1Watts": "typec1_w",
    "pd.typec2Watts": "typec2_w",
    "pd.dcOutState": "dc_out_enabled",
    "pd.carWatts": "car_out_w",
    "pd.carState": "car_state",
    "pd.chgDsgState": "chg_dsg_state",
    "pd.errCode": "pd_err_code",
    "pd.standbyMin": "standby_timeout_min",
    # --- inv (Inverter) ---
    "inv.inputWatts": "ac_in_w",
    "inv.outputWatts": "ac_out_w",
    "inv.invOutVol": "ac_out_vol_mv",
    "inv.invOutAmp": "ac_out_amp_ma",
    "inv.invOutFreq": "ac_out_freq_hz",
    "inv.acInVol": "ac_in_vol_mv",
    "inv.acInAmp": "ac_in_amp_ma",
    "inv.acInFreq": "ac_in_freq_hz",
    "inv.cfgAcEnabled": "ac_enabled",
    "inv.cfgAcXboost": "ac_xboost",
    "inv.outTemp": "inv_out_temp_c",
    "inv.dcInVol": "dc_in_vol_mv",
    "inv.dcInAmp": "dc_in_amp_ma",
    "inv.dcInTemp": "dc_in_temp_c",
    "inv.fanState": "inv_fan_state",
    "inv.chargerType": "charger_type",
    "inv.acChgRatedPower": "ac_chg_rated_power_w",
    "inv.FastChgWatts": "ac_fast_chg_watts",
    "inv.SlowChgWatts": "ac_slow_chg_watts",
    "inv.standbyMin": "inv_standby_min",
    "inv.errCode": "inv_err_code",
    # --- bms_bmsStatus (Battery Management) ---
    "bms_bmsStatus.vol": "batt_voltage_mv",
    "bms_bmsStatus.amp": "batt_current_ma",
    "bms_bmsStatus.temp": "batt_temp_raw",
    "bms_bmsStatus.soc": "bms_soc",
    "bms_bmsStatus.soh": "bms_soh_pct",
    "bms_bmsStatus.maxCellTemp": "batt_max_cell_temp_c",
    "bms_bmsStatus.minCellTemp": "batt_min_cell_temp_c",
    "bms_bmsStatus.maxCellVol": "batt_max_cell_vol_mv",
    "bms_bmsStatus.minCellVol": "batt_min_cell_vol_mv",
    "bms_bmsStatus.maxMosTemp": "batt_max_mos_temp_c",
    "bms_bmsStatus.remainCap": "batt_remain_cap_mah",
    "bms_bmsStatus.fullCap": "batt_full_cap_mah",
    "bms_bmsStatus.designCap": "batt_design_cap_mah",
    "bms_bmsStatus.errCode": "bms_err_code",
    "bms_bmsStatus.f32ShowSoc": "bms_precise_soc",
    "bms_bmsStatus.cycles": "bms_cycles",
    # --- bms_emsStatus (Energy Management) ---
    "bms_emsStatus.chgRemainTime": "chg_remain_time_min",
    "bms_emsStatus.dsgRemainTime": "dsg_remain_time_min",
    "bms_emsStatus.maxChargeSoc": "max_charge_soc",
    "bms_emsStatus.minDsgSoc": "min_discharge_soc",
    "bms_emsStatus.lcdShowSoc": "ems_lcd_soc",
    "bms_emsStatus.f32LcdShowSoc": "ems_precise_soc",
    "bms_emsStatus.chgState": "ems_chg_state",
    "bms_emsStatus.fanLevel": "fan_level",
    "bms_emsStatus.openUpsFlag": "ups_enabled",
    # --- mppt (Solar) ---
    "mppt.inWatts": "solar_in_w",
    "mppt.outWatts": "mppt_out_w",
    "mppt.inVol": "solar_in_vol_dv",
    "mppt.inAmp": "solar_in_amp_ma",
    "mppt.mpptTemp": "mppt_temp_c",
    "mppt.carOutWatts": "car_12v_out_w",
    "mppt.carState": "car_12v_enabled",
    "mppt.dcdc12vWatts": "dcdc_12v_w",
    "mppt.dcdc12vVol": "dcdc_12v_vol_dv",
    "mppt.pv2InWatts": "solar2_in_w",
    "mppt.pv2InVol": "solar2_in_vol_dv",
    "mppt.pv2InAmp": "solar2_in_amp_ca",
    "mppt.pv2MpptTemp": "solar2_mppt_temp_c",
    "mppt.chgState": "mppt_chg_state",
    "mppt.faultCode": "mppt_fault_code",
}



def parse_delta_http_quota(quota_data: dict) -> dict[str, Any]:
    """Parse a Delta 2 Max GET /quota/all response into flat sensor keys.

    The API returns keys like "pd.soc": 83, "inv.outputWatts": 0, etc.
    This function maps them to sensor keys and applies unit conversions.
    """
    result: dict[str, Any] = {}

    for http_key, sensor_key in DELTA2MAX_HTTP_FIELD_MAP.items():
        if http_key in quota_data:
            v = _safe_float(quota_data[http_key])
            if v is not None:
                result[sensor_key] = v

    # --- Temperature offset: bms_bmsStatus.temp has +15 offset ---
    if "batt_temp_raw" in result:
        result["batt_temp_c"] = result.pop("batt_temp_raw") - 15.0

    # --- Voltage conversions: mV -> V ---
    for mv_key, v_key in [
        ("batt_voltage_mv", "batt_voltage_v"),
        ("ac_out_vol_mv", "ac_out_vol_v"),
        ("ac_in_vol_mv", "ac_in_vol_v"),
        ("dc_in_vol_mv", "dc_in_vol_v"),
    ]:
        if mv_key in result:
            result[v_key] = result.pop(mv_key) / 1000.0

    # --- Voltage conversions: dV -> V (deci-volt, amplified 10x) ---
    for dv_key, v_key in [
        ("solar_in_vol_dv", "solar_in_vol_v"),
        ("solar2_in_vol_dv", "solar2_in_vol_v"),
        ("dcdc_12v_vol_dv", "dcdc_12v_vol_v"),
    ]:
        if dv_key in result:
            result[v_key] = result.pop(dv_key) / 10.0

    # --- Current conversions: mA -> A ---
    for ma_key, a_key in [
        ("batt_current_ma", "batt_current_a"),
        ("ac_out_amp_ma", "ac_out_amp_a"),
        ("ac_in_amp_ma", "ac_in_amp_a"),
        ("dc_in_amp_ma", "dc_in_amp_a"),
        ("solar_in_amp_ma", "solar_in_amp_a"),
    ]:
        if ma_key in result:
            result[a_key] = result.pop(ma_key) / 1000.0

    # --- Current conversions: cA -> A (centi-amp, amplified 100x) ---
    for ca_key, a_key in [
        ("solar2_in_amp_ca", "solar2_in_amp_a"),
    ]:
        if ca_key in result:
            result[a_key] = result.pop(ca_key) / 100.0

    # --- Power conversions: amplified 10x -> W ---
    for key in ["mppt_out_w", "car_12v_out_w", "solar2_in_w"]:
        if key in result:
            result[key] /= 10.0

    # --- Power conversions: amplified 100x -> W ---
    if "dcdc_12v_w" in result:
        result["dcdc_12v_w"] /= 100.0

    # --- Temperature conversions: amplified 10x -> °C ---
    if "solar2_mppt_temp_c" in result:
        result["solar2_mppt_temp_c"] /= 10.0

    return result
