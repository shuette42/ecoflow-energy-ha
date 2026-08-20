"""PowerStream microinverter JSON quota parser for the IoT Developer API.

The PowerStream reports through the Developer API as flat JSON under a
`20_1.` namespace, either from the HTTP quota endpoint or from the
`/quota` MQTT topic. It is a Standard Mode device: everything this parser
needs arrives over HTTP, there is no protobuf stream involved.

Scaling is deciwatt and the reporter capture from #188 proves it rather
than assuming it, because the device's own power balance closes exactly
at 0.1 W per count:

    pv1InputWatts 510 + pv2InputWatts 580 = 1090 = pvToInvWatts
                                                 = invOutputWatts
    invOutputWatts 1090 - invToPlugWatts 10 = 1080 = -gridConsWatts

That reads as 51.0 W and 58.0 W from the two strings, 109.0 W out of the
inverter, 1.0 W drawn by the attached smart plug and 108.0 W exported.
Two further readings agree on the same scale: `ratedPower` 8000 is the
800 W model, and the configured feed-in cap appears as 6000 in three
independent limit fields.

The second half of that balance is also what shows the grid sign:
`gridConsWatts` is negative while the unit exports, so negative is
export. That is observed here rather than inferred from the field name.

Deliberately unmapped, each for a stated reason:

- **Currents.** `pv1InputCur` 17 against 51.0 W at 33.8 V implies 1.51 A,
  not the 1.7 A a /10 scale would give, and `invOutputCur` 565 against
  109.0 W at 243.5 V only reconciles through an assumed power factor.
  One snapshot cannot settle a scale, so no current becomes a reading.
- **`geneWatt` / `consWatt`.** `geneWatt` 1193 does not match the 1090 the
  strings report, so it measures something else - most likely the
  smart-plug system around the inverter. Unclear, left out.
- **`history*`.** All seven counters read 0 in the capture, so nothing
  about them is verified.
- **`chgRemainTime` / `dsgRemainTime`.** Both are populated while the
  battery is idle, the same placeholder behaviour the Delta 3 has.
  Gating them needs a battery state enum whose values are not known.
- **Status enums and per-subsystem error codes.** Their values are
  observed, their meanings are not.
- **`20_134.*` scheduled tasks.**

`batInputWatts` is mapped as positive = charging, following the field
name, and is kept out of the energy counters on purpose: it read 0
throughout the capture, so the sign is the one value here taken from a
name rather than from an observation, and a wrong sign must not reach a
monotonic total.
"""

from __future__ import annotations

from typing import Any

_PREFIX = "20_1."

# Quota key (without the namespace prefix) -> (sensor key, multiplier).
# Every entry is a plain number; the multiplier turns the device's unit
# into the entity's unit.
POWERSTREAM_HTTP_FIELD_MAP: dict[str, tuple[str, float]] = {
    # --- PV strings (deciwatt -> W) ---
    "pv1InputWatts": ("pv1_w", 0.1),
    "pv2InputWatts": ("pv2_w", 0.1),
    # --- Power paths (deciwatt -> W) ---
    "invOutputWatts": ("inv_output_w", 0.1),
    "gridConsWatts": ("grid_w", 0.1),
    "batInputWatts": ("batt_w", 0.1),
    "plugTotalWatts": ("plug_total_w", 0.1),
    # --- Settings the device reports back (deciwatt -> W) ---
    "permanentWatts": ("permanent_watts_w", 0.1),
    "ratedPower": ("rated_power_w", 0.1),
    # --- Battery ---
    "batOpVolt": ("batt_voltage_v", 0.1),
    "batTemp": ("batt_temp_c", 0.1),
    # --- AC side ---
    "invOpVolt": ("ac_voltage_v", 0.1),
    "invFreq": ("ac_frequency_hz", 0.1),
    # --- PV input voltage ---
    "pv1InputVolt": ("pv1_voltage_v", 0.1),
    "pv2InputVolt": ("pv2_voltage_v", 0.1),
}

# Keys reported as clean integers (percent, dBm). Kept apart from the
# float path so a percentage never arrives with a decimal tail.
POWERSTREAM_HTTP_INT_FIELD_MAP: dict[str, str] = {
    "batSoc": "soc_pct",
    "lowerLimit": "lower_limit_pct",
    "upperLimit": "upper_limit_pct",
    "wifiRssi": "wifi_rssi_dbm",
}

# Indicator brightness. The device scale is 0..1023 (vendor documentation
# for WN511_SET_BRIGHTNESS_PACK), the entity is a percentage, matching
# how the Smart Plug and the Stream report theirs.
_BRIGHTNESS_KEY = "invBrightness"
_BRIGHTNESS_MAX = 1023.0

# Power supply priority, per the vendor documentation for
# WN511_SET_SUPPLY_PRIORITY_PACK. Unknown values are dropped so the enum
# sensor never receives an option it does not have.
_SUPPLY_PRIORITY_MAP: dict[int, str] = {
    0: "power_supply",
    1: "battery_storage",
}


def _numeric(value: Any) -> float | None:
    """Return the value as a float, or None if it is not a real number.

    Booleans are rejected on purpose: `bool` is a subclass of `int` in
    Python, and a flag arriving on a power key would otherwise be
    published as 0 W / 1 W.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def parse_powerstream_quota(raw: dict) -> dict[str, Any]:
    """Parse a PowerStream quota payload into flat sensor keys.

    Accepts the `20_1.`-prefixed dict as delivered by GET /quota/all and
    by the `/quota` MQTT topic. Unmapped and non-numeric entries are
    dropped, so no raw camelCase key reaches the device data store; the
    full payload stays available through diagnostics.
    """
    if not isinstance(raw, dict):
        return {}

    result: dict[str, Any] = {}

    for quota_key, (sensor_key, multiplier) in POWERSTREAM_HTTP_FIELD_MAP.items():
        value = _numeric(raw.get(f"{_PREFIX}{quota_key}"))
        if value is not None:
            result[sensor_key] = round(value * multiplier, 2)

    for quota_key, sensor_key in POWERSTREAM_HTTP_INT_FIELD_MAP.items():
        value = _numeric(raw.get(f"{_PREFIX}{quota_key}"))
        if value is not None:
            result[sensor_key] = int(round(value))

    brightness = _numeric(raw.get(f"{_PREFIX}{_BRIGHTNESS_KEY}"))
    if brightness is not None:
        result["led_brightness"] = int(round(brightness * 100.0 / _BRIGHTNESS_MAX))

    priority = _numeric(raw.get(f"{_PREFIX}supplyPriority"))
    if priority is not None:
        option = _SUPPLY_PRIORITY_MAP.get(int(priority))
        if option is not None:
            result["supply_priority"] = option

    # PV total. The device sends `pvToInvWatts` as well and it equalled the
    # sum of the strings in every frame of the capture, but that field is
    # the PV power reaching the inverter rather than the PV production, so
    # the sum is the honest source and there is only one of them.
    pv_values = [result[key] for key in ("pv1_w", "pv2_w") if key in result]
    if pv_values:
        result["solar_w"] = round(sum(pv_values), 2)

    return result
