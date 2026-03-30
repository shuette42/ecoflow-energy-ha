"""EcoFlow Smart Plug HTTP Quota API response parser.

Parses the response from GET /iot-open/sign/device/quota/all into
flat sensor keys matching SMARTPLUG_SENSORS in const.py.

The Smart Plug API uses a "2_1." prefix for heartbeat data and "2_2." for tasks.
Key fields: switchSta, watts, current (mA), volt (V), temp, freq, brightness.
"""

from __future__ import annotations

from typing import Any

from . import _safe_float


def parse_smartplug_http_quota(quota_data: dict) -> dict[str, Any]:
    """Parse a Smart Plug GET /quota/all response into flat sensor keys.

    Args:
        quota_data: The "data" dict from the API response.

    Returns:
        Dict mapping sensor keys to values.
    """
    result: dict[str, Any] = {}

    # --- Core measurements ---
    _prefix = "2_1."

    # Power (deci-W → W) — API returns value in 0.1 W units
    if f"{_prefix}watts" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}watts"])
        if v is not None:
            result["power_w"] = v / 10.0

    # Current (mA → A)
    if f"{_prefix}current" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}current"])
        if v is not None:
            result["current_a"] = v / 1000.0

    # Voltage (V)
    if f"{_prefix}volt" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}volt"])
        if v is not None:
            result["voltage_v"] = v

    # Frequency (Hz)
    if f"{_prefix}freq" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}freq"])
        if v is not None:
            result["frequency_hz"] = v

    # Temperature (°C)
    if f"{_prefix}temp" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}temp"])
        if v is not None:
            result["temperature_c"] = v

    # --- Switch state ---
    if f"{_prefix}switchSta" in quota_data:
        val = quota_data[f"{_prefix}switchSta"]
        if isinstance(val, bool):
            result["switch_state"] = 1 if val else 0
        elif isinstance(val, (int, float)):
            result["switch_state"] = 1 if val else 0

    # --- Diagnostics ---
    if f"{_prefix}brightness" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}brightness"])
        if v is not None:
            result["led_brightness"] = v

    if f"{_prefix}maxWatts" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}maxWatts"])
        if v is not None:
            result["max_power_w"] = v

    # Max current (deci-A → A) — API returns value in 0.1 A units
    if f"{_prefix}maxCur" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}maxCur"])
        if v is not None:
            result["max_current_a"] = v / 10.0

    if f"{_prefix}errCode" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}errCode"])
        if v is not None:
            result["error_code"] = int(v)

    if f"{_prefix}warnCode" in quota_data:
        v = _safe_float(quota_data[f"{_prefix}warnCode"])
        if v is not None:
            result["warning_code"] = int(v)

    return result


# MQTT field → (sensor_key, scale_factor)
# Scale factors match parse_smartplug_http_quota exactly:
#   watts:   deci-W → W  (/10)
#   current: mA → A      (/1000)
#   volt:    V → V        (1)
#   maxCur:  deci-A → A   (/10)
#   maxWatts: W → W       (1)  — no scaling in HTTP parser
_MQTT_FIELD_MAP: dict[str, tuple[str, float]] = {
    "watts": ("power_w", 0.1),
    "current": ("current_a", 0.001),
    "volt": ("voltage_v", 1.0),
    "freq": ("frequency_hz", 1.0),
    "temp": ("temperature_c", 1.0),
    "brightness": ("led_brightness", 1.0),
    "maxWatts": ("max_power_w", 1.0),
    "maxCur": ("max_current_a", 0.1),
    "errCode": ("error_code", 1.0),
    "warnCode": ("warning_code", 1.0),
}


def parse_smartplug_report(data: dict[str, Any]) -> dict[str, Any]:
    """Parse a Smart Plug MQTT report message.

    MQTT reports may arrive in different envelope formats:
    1. {"params": {"2_1.watts": 150, ...}} — same keys as HTTP quota
    2. {"param": {"watts": 150, ...}} — direct field names (cmdId/cmdFunc format)
    3. Direct dict with field names
    """
    # Extract inner payload from envelope
    params = data.get("params") or data.get("param") or data

    # If keys have "2_1." prefix, reuse the HTTP parser (same format)
    if any(k.startswith("2_1.") for k in params):
        return parse_smartplug_http_quota(params)

    # Direct field names — apply same scaling as HTTP parser
    result: dict[str, Any] = {}

    for api_key, (sensor_key, scale) in _MQTT_FIELD_MAP.items():
        val = params.get(api_key)
        if val is None:
            continue
        fval = _safe_float(val)
        if fval is None:
            continue
        if sensor_key in ("error_code", "warning_code"):
            result[sensor_key] = int(fval)
        else:
            result[sensor_key] = fval * scale

    # Switch state: handle bool and int
    switch_val = params.get("switchSta")
    if switch_val is not None:
        if isinstance(switch_val, bool):
            result["switch_state"] = 1 if switch_val else 0
        elif isinstance(switch_val, (int, float)):
            result["switch_state"] = 1 if switch_val else 0

    return result
