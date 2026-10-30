"""Stream (BK-series) JSON quota parser for the IoT Developer API.

Standard mode delivers the Stream telemetry as a flat camelCase JSON
object, either from the HTTP quota endpoint or from the `/quota` MQTT
topic. Before this parser existed the coordinator fell through to
`parsed = raw`, so the raw quota keys landed in the device data store
unmapped and no entity ever picked them up (issue #139).

The Enhanced mode protobuf path (`stream_proto.py`) is unaffected: both
paths deliberately emit the same sensor keys, so a device can move
between modes without duplicating sensors or restarting the energy
integration.

Scope is deliberately narrow. Only keys whose meaning follows
unambiguously from the name and the observed values are mapped;
everything else is dropped so no raw camelCase key can leak into
`_device_data`. The raw snapshot stays available via diagnostics.

Field notes:

- `powGetPvSum` is the PV total and maps onto the existing `solar_w`
  key, so Standard and Enhanced feed the same solar sensor and the
  Riemann integration is not duplicated.
- `powGetPv` .. `powGetPv4` are the per-string PV inputs. Units with
  fewer strings simply omit the higher keys (or report 0).
- `powGetBpCms` is the signed battery path. The sign convention follows
  the protobuf path (positive = charging, negative = discharging), so
  the derived charge/discharge split is identical in both modes.
- Values are treated as plain watts. The reference diagnostics list the
  key names but not their values, so unlike the Smart Plug deciwatt case
  this is an assumption, not a verified fact: the Stream protobuf path
  reports plain watts and nothing in the payload suggests a divisor.
  Reporter feedback comparing an entity against the app decides it.
- `cmsBattSoc` is the state of charge of the system, which is what the app
  shows and what a pair of units on a parallel cable reports as one battery.
  `soc` is this unit's own BMS reading and only equals it on a single unit;
  it maps onto `unit_soc_pct` and stands in for the system figure when a
  quota carries no `cmsBattSoc`.
- `plugInInfoPvVol` is the PV input voltage. It is exposed as a
  disabled-by-default diagnostic because the per-string breakdown, not
  the summed voltage, is what users are after.
- BMS extras such as `cycles` are present in the payload but have no
  sensor definition yet and are therefore not mapped.
- The `gridConnection*` keys, `powGetSchuko2` and `relay2Onoff` /
  `relay3Onoff` are the JSON names of fields the protobuf path already
  maps (`grid_connection_power` 616, `grid_connection_vol` 613,
  `grid_connection_amp` 614, `grid_connection_freq` 615, `relay2_onoff`
  980, `relay3_onoff` 982), so they feed the same keys (#659).
  `gridConnectionPower` and both relays are in every recorded quota of a
  STREAM AC Pro, Ultra and Ultra X. Voltage, current, frequency and the
  second socket's power are in none of them; they are mapped for the
  `/quota` push, which the reporter of #659 saw fill them.
  `gridConnectionPower` is the signed grid connection the protobuf path
  falls back to when field 992 is absent: negative is drawn from the
  grid, positive is fed out.
"""

from __future__ import annotations

from typing import Any

from .stream_proto import SOC_FALLBACK_KEY

# Mapping: flat quota key -> sensor key. Every value in this map is a
# plain number in its native unit (W, V, A, Hz, %); no scaling is applied.
STREAM_HTTP_FIELD_MAP: dict[str, str] = {
    # --- PV (W) ---
    "powGetPvSum": "solar_w",
    "powGetPv": "pv1_w",
    "powGetPv2": "pv2_w",
    "powGetPv3": "pv3_w",
    "powGetPv4": "pv4_w",
    # --- System power paths (W) ---
    "powGetSysLoad": "home_w",
    "powGetSysGrid": "grid_w",
    "powGetBpCms": "batt_w",
    "powGetSysLoadFromPv": "home_from_solar_w",
    "powGetSchuko1": "ac_outlet_1_w",
    "powGetSchuko2": "ac_outlet_2_w",
    "gridConnectionPower": "ac_grid_connection_power_w",
    # --- Voltage (V), current (A), frequency (Hz) ---
    "plugInInfoPvVol": "pv_voltage_v",
    "gridConnectionVol": "ac_voltage_v",
    "gridConnectionAmp": "ac_current_a",
    "gridConnectionFreq": "ac_frequency_hz",
}

# AC socket relays: the live relay state, the same fields the protobuf path
# reads as 980 and 982. The quota sends JSON booleans.
STREAM_HTTP_RELAY_MAP: dict[str, str] = {
    "relay2Onoff": "ac_outlet_1_enabled",
    "relay3Onoff": "ac_outlet_2_enabled",
}

# Keys that are reported as clean integers (percent). Kept separate so the
# power path can stay float and match the protobuf output exactly.
STREAM_HTTP_INT_FIELD_MAP: dict[str, str] = {
    # Same split as protobuf fields 262/242: `cmsBattSoc` is the system
    # figure a linked pair reports as one battery, `soc` this unit's own BMS.
    # A Stream Ultra X quota can carry `cmsBattSoc` alone (19 keys in the
    # #139 diagnostics), a Stream Ultra both (#323).
    "cmsBattSoc": "soc_pct",
    "soc": "unit_soc_pct",
    "soh": "bms_soh_pct",
}


# `feedGridMode` is the Stream "Feed-in control" setting: 2 lets the system
# feed into the grid, 1 stops it. Read as a switch state, so the parser
# publishes a bool and nothing else: the generic switch read treats any
# non-zero number as "on", which would show a raw 1 as on while feed-in is off.
STREAM_FEED_IN_MODE_KEY = "feedGridMode"
STREAM_FEED_IN_STATE_KEY = "feed_in_control_enabled"
_FEED_IN_MODE_TO_ENABLED: dict[int, bool] = {2: True, 1: False}


def _feed_in_enabled(value: Any) -> bool | None:
    """Return the feed-in state for a `feedGridMode` value, None if unknown.

    `bool` is a subclass of `int` and `True == 1`, so a flag must be rejected
    before the table lookup or it would read as "feed-in off". Strings such as
    "2" are not accepted either: only the integer the device reports counts.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return _FEED_IN_MODE_TO_ENABLED.get(value)


def _numeric(value: Any) -> float | None:
    """Return the value as a float, or None if it is not a real number.

    Booleans are rejected on purpose: `bool` is a subclass of `int` in
    Python and a flag arriving on a power key would otherwise be
    published as 0 W / 1 W.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def parse_stream_quota(raw: dict) -> dict[str, Any]:
    """Parse a Stream JSON quota payload into flat sensor keys.

    Maps the keys listed in STREAM_HTTP_FIELD_MAP and derives the
    charge/discharge split from the signed battery power, mirroring what
    the protobuf parser produces. Unmapped and non-numeric entries are
    dropped.
    """
    if not isinstance(raw, dict):
        return {}

    result: dict[str, Any] = {}

    for quota_key, sensor_key in STREAM_HTTP_FIELD_MAP.items():
        if quota_key not in raw:
            continue
        value = _numeric(raw[quota_key])
        if value is not None:
            result[sensor_key] = value

    for quota_key, sensor_key in STREAM_HTTP_INT_FIELD_MAP.items():
        if quota_key not in raw:
            continue
        value = _numeric(raw[quota_key])
        if value is not None:
            result[sensor_key] = int(round(value))

    # Only a real boolean, or exactly 0 or 1, is a relay state. Anything else
    # is dropped rather than read as on or off.
    for quota_key, sensor_key in STREAM_HTTP_RELAY_MAP.items():
        relay = raw.get(quota_key)
        if isinstance(relay, bool):
            result[sensor_key] = relay
        elif type(relay) is int and relay in (0, 1):
            result[sensor_key] = bool(relay)

    # Absent key = no update. A present key with a value outside 1/2 is
    # published as None (Unknown) so a unit reporting 3 after 2 cannot keep
    # showing "on".
    if STREAM_FEED_IN_MODE_KEY in raw:
        result[STREAM_FEED_IN_STATE_KEY] = _feed_in_enabled(
            raw[STREAM_FEED_IN_MODE_KEY]
        )

    # A single unit is its own system: a quota that carries only `soc`
    # still feeds the battery sensor. Offered on the same private key as the
    # protobuf path and promoted by the coordinator, not here: a `/quota`
    # push is not guaranteed to be a whole snapshot, and a partial one
    # carrying `soc` alone must not overwrite the system figure the last poll
    # delivered.
    if "soc_pct" not in result and "unit_soc_pct" in result:
        result[SOC_FALLBACK_KEY] = result["unit_soc_pct"]

    # Derived battery split, identical to the protobuf path so the energy
    # integration behaves the same in both modes.
    batt_w = result.get("batt_w")
    if batt_w is not None:
        result["batt_charge_power_w"] = batt_w if batt_w > 0 else 0.0
        result["batt_discharge_power_w"] = abs(batt_w) if batt_w < 0 else 0.0

    return result
