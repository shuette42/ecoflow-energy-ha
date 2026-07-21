"""Delta 3 generation SET command builders.

The Delta 3 line uses a flat JSON envelope with a fixed command id, documented
in the official IoT Developer API for the DELTA 3 MAX PLUS. Every control shares
the same envelope and differs only in the `params` payload:

    {
        "cmdId": 17, "cmdFunc": 254, "dest": 2,
        "dirDest": 1, "dirSrc": 1, "needAck": true,
        "params": {"cfgAcOutOpen": true}
    }

The transport wrapper (`sn`, `id`, `version`) is added by the coordinator, so
these builders return the command body only.

Value ranges below are the vendor's own bounds. They are enforced here rather
than only on the entity, so a command built from any caller stays inside what
the device accepts.
"""

from __future__ import annotations

from typing import Any

# Shared envelope. `dirSrc` follows the HTTP examples in the official docs; the
# MQTT example spells it `dirSoc`, which reads like a typo in the vendor docs
# (the field is a direction *source*). Verified against hardware before release.
_CMD_ID = 17
_CMD_FUNC = 254
_DEST = 2
_DIR_DEST = 1
_DIR_SRC = 1

# Switch controls: entity key -> params key. All take a plain bool.
DELTA3_SWITCH_PARAMS: dict[str, str] = {
    "ac_out_switch": "cfgAcOutOpen",
    "ac2_out_switch": "cfgAc2OutOpen",
    "dc_12v_out_switch": "cfgDc12vOutOpen",
    "xboost_switch": "cfgXboostEn",
    "beeper_switch": "cfgBeepEn",
    "bypass_out_disable_switch": "cfgBypassOutDisable",
}

# Energy backup is the only nested payload: {"cfgEnergyBackup": {"energyBackupEn": bool}}
DELTA3_ENERGY_BACKUP_KEY = "energy_backup_switch"

# Number controls: entity key -> (params key, min, max). Bounds are vendor-documented.
DELTA3_NUMBER_PARAMS: dict[str, tuple[str, int, int]] = {
    "backup_reserve_soc": ("cfgBackupReverseSoc", 0, 50),
    "max_charge_soc": ("cfgMaxChgSoc", 50, 100),
    "min_discharge_soc": ("cfgMinDsgSoc", 0, 30),
}


def _envelope(params: dict[str, Any], need_ack: bool = True) -> dict[str, Any]:
    """Wrap params in the shared Delta 3 command envelope."""
    return {
        "cmdId": _CMD_ID,
        "cmdFunc": _CMD_FUNC,
        "dest": _DEST,
        "dirDest": _DIR_DEST,
        "dirSrc": _DIR_SRC,
        "needAck": need_ack,
        "params": params,
    }


def build_switch_command(entity_key: str, turn_on: bool) -> dict[str, Any] | None:
    """Build a SET command for a Delta 3 switch.

    Returns None for an unknown key so the caller can log instead of sending
    a payload the device would reject.
    """
    if entity_key == DELTA3_ENERGY_BACKUP_KEY:
        return _envelope({"cfgEnergyBackup": {"energyBackupEn": bool(turn_on)}})

    params_key = DELTA3_SWITCH_PARAMS.get(entity_key)
    if params_key is None:
        return None
    return _envelope({params_key: bool(turn_on)})


def build_number_command(entity_key: str, value: float) -> dict[str, Any] | None:
    """Build a SET command for a Delta 3 number, clamped to the vendor range."""
    entry = DELTA3_NUMBER_PARAMS.get(entity_key)
    if entry is None:
        return None
    params_key, low, high = entry
    clamped = max(low, min(high, int(round(value))))
    return _envelope({params_key: clamped})
