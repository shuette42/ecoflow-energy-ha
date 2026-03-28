"""EcoFlow Smart Plug HTTP Quota API response parser.

Parses the response from GET /iot-open/sign/device/quota/all into
flat sensor keys matching SMARTPLUG_SENSORS in const.py.

The Smart Plug API uses a "2_1." prefix for heartbeat data and "2_2." for tasks.
Key fields: switchSta, watts, current (mA), volt (V), temp, freq, brightness.
"""

from typing import Any, Dict, Optional


def _safe_float(val: Any) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def parse_smartplug_http_quota(quota_data: dict) -> Dict[str, Any]:
    """Parse a Smart Plug GET /quota/all response into flat sensor keys.

    Args:
        quota_data: The "data" dict from the API response.

    Returns:
        Dict mapping sensor keys to values.
    """
    result: Dict[str, Any] = {}

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
