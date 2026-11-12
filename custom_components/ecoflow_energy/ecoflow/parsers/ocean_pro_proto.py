"""Protobuf telemetry parser for the EcoFlow OCEAN Pro inverter and battery (`HR51`).

Read-only. Three frames carry what this parser publishes:

* `254/21` (`DisplayPropertyUpload`): the six system figures it shares with the
  Smart Panel 40 (`CMS_CORE_FIELD_MAP`, one table for both units), plus the
  stored energy, the five PV strings, the inverter AC port per leg and the
  device temperatures. Like the panel's it is **incremental**: a field missing
  from a push is unchanged, not zero, so this parser returns only what a frame
  carries and the coordinator's `_device_data.update()` keeps the rest. There
  are no circuits on this unit, so nothing is zero-filled.
* `254/22` (`RuntimePropertyUpload`): the grid frequency only.
* `32/177` (`BpCloudHeartbeatReport`, sent by the battery pack itself): pack
  SoC, state of health, cycle count and the lifetime charge/discharge energy.
  A property frame carries two consecutive heartbeats of the one pack; the
  newer one (by its own timestamp) is used.

Field numbers, types and scales come from 52 recorded frames of issue #427
(arithmetic identities checked) and a second recording of 2026-10-01, which
added no new field.

The unit's CT fields (1485-1487, 1673-1675) are the 0xFFFFFF "not installed"
value on every frame seen and are not in the table at all. The same value on
any field that is read is dropped rather than published as 16.8 MW.

Only stateless helpers are imported from the sibling parsers, the way
`smart_panel_40_proto` imports from `stream_ac5000_proto`.
"""

from __future__ import annotations

import struct
from math import isfinite
from typing import Any

from ..proto.decoder import decode_header_message
from .powerocean_http import ENERGY_TOTAL_PLACEHOLDER_WH
from .smart_panel_40_proto import CMS_CORE_FIELD_MAP
from .stream_ac5000_proto import _TYPE_FLOAT, _TYPE_INT, _compile, _walk
from .stream_proto import _pdata_candidates

_CMD_FUNC_EMS = 254
_CMD_ID_PROPERTY = 21
_CMD_ID_RUNTIME = 22
_CMD_FUNC_BMS = 32
_CMD_ID_HEARTBEAT = 177

#: "Not installed" value the unit puts on fields it has no sensor for.
_NOT_INSTALLED = 0xFFFFFF
#: Never published as a reading: the placeholder above and the u32 maximum
#: (it would show stored energy as 4.29 GWh).
_UNUSABLE_VALUES = (_NOT_INSTALLED, 0xFFFFFFFF)

# `254/21` fields only this unit reads, on top of `CMS_CORE_FIELD_MAP`.
# Source: the 52 recorded frames of #427, decoded field by field.
_HR51_EXTRA_254_21: dict[str, tuple[str, str, float]] = {
    # cms_batt_store_energy, u32 Wh (= the pack's own remaining energy)
    "1682": ("battery_stored_energy_wh", _TYPE_INT, 1),
    # dt_pv_pwr_current, dt_pv2..pv5_pwr_current, W per string (sum = 517)
    "1476": ("pv_string_1_power_w", _TYPE_FLOAT, 1),
    "1477": ("pv_string_2_power_w", _TYPE_FLOAT, 1),
    "1478": ("pv_string_3_power_w", _TYPE_FLOAT, 1),
    "1479": ("pv_string_4_power_w", _TYPE_FLOAT, 1),
    "1480": ("pv_string_5_power_w", _TYPE_FLOAT, 1),
    # Inverter AC port per leg: plug_in_info_l1/l2_vol (V) and _amp (A),
    # pow_get_l1/l2 (W, negative = the inverter is delivering).
    "1463": ("inverter_ac_l1_voltage_v", _TYPE_FLOAT, 1),
    "1464": ("inverter_ac_l1_current_a", _TYPE_FLOAT, 1),
    "1465": ("inverter_ac_l2_voltage_v", _TYPE_FLOAT, 1),
    "1466": ("inverter_ac_l2_current_a", _TYPE_FLOAT, 1),
    "1467": ("inverter_ac_l1_power_w", _TYPE_FLOAT, 1),
    "1468": ("inverter_ac_l2_power_w", _TYPE_FLOAT, 1),
    # inv_ntc_temp2 (i32) and dev_temperature_1..5 (f32), degC
    "1469": ("inverter_temperature_c", _TYPE_INT, 1),
    "1553": ("device_temperature_1_c", _TYPE_FLOAT, 1),
    "1554": ("device_temperature_2_c", _TYPE_FLOAT, 1),
    "1555": ("device_temperature_3_c", _TYPE_FLOAT, 1),
    "1556": ("device_temperature_4_c", _TYPE_FLOAT, 1),
    "1557": ("device_temperature_5_c", _TYPE_FLOAT, 1),
}
_PROPERTY_TREE: dict[int, Any] = _compile({**CMS_CORE_FIELD_MAP, **_HR51_EXTRA_254_21})

# `254/22`: dt_pcs_grid_Fre_l1, real Hz (59.96..60.03 on the capture).
_RUNTIME_TREE: dict[int, Any] = _compile({"641": ("grid_frequency_hz", _TYPE_FLOAT, 1)})

# `32/177` BpCloudHeartbeatReport (BmsCloud.proto). `bp_timestamp` only picks
# the newer of two heartbeats and never leaves this module.
_TS_KEY = "bp_timestamp"
_HEARTBEAT_TREE: dict[int, Any] = _compile(
    {
        "2": (_TS_KEY, _TYPE_INT, 1),
        "10": ("battery_pack_soc_pct", _TYPE_FLOAT, 1),
        "19": ("battery_pack_soh_pct", _TYPE_FLOAT, 1),
        "22": ("battery_pack_cycles", _TYPE_INT, 1),
        "27": ("bp_accu_chg_energy", _TYPE_INT, 1),
        "28": ("bp_accu_dsg_energy", _TYPE_INT, 1),
    }
)
_LIFETIME_WH_KEYS = (
    ("bp_accu_chg_energy", "batt_charge_energy_kwh"),
    ("bp_accu_dsg_energy", "batt_discharge_energy_kwh"),
)


def _drop_unusable(walked: dict[str, Any]) -> None:
    """Remove NaN/inf (they abort Home Assistant's rounding) and the
    0xFFFFFF "not installed" value and the unsigned-32 maximum (an unread
    u32 register), in place."""
    for key in [
        k
        for k, v in walked.items()
        if (isinstance(v, float) and not isfinite(v)) or v in _UNUSABLE_VALUES
    ]:
        del walked[key]


def _walk_payload(pdata: bytes, tree: dict[int, Any]) -> dict[str, Any]:
    walked: dict[str, Any] = {}
    _walk(pdata, tree, walked, set())
    _drop_unusable(walked)
    return walked


def _parse_heartbeat(pdata: bytes) -> dict[str, Any]:
    """One `32/177` payload into output keys plus its `bp_timestamp`."""
    walked = _walk_payload(pdata, _HEARTBEAT_TREE)
    out = {k: v for k, v in walked.items() if k not in dict(_LIFETIME_WH_KEYS)}
    # Wh -> kWh. Same zero and placeholder guard as the pack loop in
    # `powerocean_proto`: these are `total_increasing`, and a zero or the
    # unsigned-32 placeholder would publish as a meter reset.
    for wire_key, sensor_key in _LIFETIME_WH_KEYS:
        val = walked.get(wire_key)
        if (
            isinstance(val, (int, float))
            and val > 0
            and val != ENERGY_TOTAL_PLACEHOLDER_WH
        ):
            out[sensor_key] = float(val) / 1000.0
    return out


def parse_hr51_proto_message(payload: bytes) -> dict[str, Any] | None:
    """Parse one HR51 frame into the keys it carries.

    Returns only what the frame holds (incremental pushes), or None when it
    holds nothing this parser reads (`254/25`, `240/x`, ...). A `get_reply`
    bundle is read header by header and combined in header order.
    """
    try:
        headers, _ = decode_header_message(payload)
    except (IndexError, ValueError, struct.error):
        return None
    if not headers:
        return None

    result: dict[str, Any] = {}
    heartbeats: list[dict[str, Any]] = []
    for header in headers:
        key = (header.get("cmd_func"), header.get("cmd_id"))
        if key == (_CMD_FUNC_EMS, _CMD_ID_PROPERTY):
            tree = _PROPERTY_TREE
        elif key == (_CMD_FUNC_EMS, _CMD_ID_RUNTIME):
            tree = _RUNTIME_TREE
        elif key == (_CMD_FUNC_BMS, _CMD_ID_HEARTBEAT):
            tree = _HEARTBEAT_TREE
        else:
            continue
        for pdata in _pdata_candidates(header):
            try:
                decoded = (
                    _parse_heartbeat(pdata)
                    if tree is _HEARTBEAT_TREE
                    else _walk_payload(pdata, tree)
                )
            except (IndexError, ValueError, struct.error):
                # Not valid protobuf: try the next candidate. A clean decode
                # ends the attempt whether it produced fields or not.
                continue
            if decoded:
                if tree is _HEARTBEAT_TREE:
                    heartbeats.append(decoded)
                else:
                    result.update(decoded)
            break

    if heartbeats:
        # Two heartbeats of one pack per frame: the newer wins, a tie goes to
        # the later one. Its timestamp is a picking aid, not a sensor.
        order = sorted(
            range(len(heartbeats)), key=lambda n: (heartbeats[n].get(_TS_KEY, 0), n)
        )
        newest = heartbeats[order[-1]]
        result.update({k: v for k, v in newest.items() if k != _TS_KEY})

    return result or None
