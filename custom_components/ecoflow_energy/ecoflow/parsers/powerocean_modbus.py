"""PowerOcean local Modbus register map.

Turns the raw register blocks read by ``ecoflow/modbus_local.py`` into the
sensor keys the PowerOcean entities already use. Offsets are the protocol
table's, relative to ``MODBUS_BASE_ADDRESS`` (added by the client). Signs match
the cloud stream: grid positive is import, battery positive is charge, so the
import/export and charge/discharge splits come from ``remap_proto_keys`` and no
sign is flipped here.

Registers that no sensor reads are left out on purpose; blocks may span them
(gaps inside a block read fine, the limit is 125 registers per request).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..modbus_local import (
    BACKUP_RATIO_OFFSET,
    BRIGHTNESS_OFFSET,
    decode_ascii,
    decode_f32_ws,
    decode_u16,
    decode_u32_ws,
)
from .powerocean_proto import remap_proto_keys

# (offset, register count): protocol version + product category + product number,
# serial (16 ASCII bytes), firmware package (4 bytes).
SETUP_BLOCKS: tuple[tuple[int, int], ...] = ((0x0000, 3), (0x0003, 8), (0x000B, 2))

# (offset, register count), each block at most 125 registers.
POLL_BLOCKS: tuple[tuple[int, int], ...] = (
    # load, grid, solar, battery power, SoC, system status (0x0211-0x0212),
    # system state 2 (0x0213-0x0214)
    (0x0206, 15),
    (0x0217, 18),  # backup ratio ... battery capacity (0x0227-0x0228)
    (0x023D, 6),  # battery voltage, battery current, battery temperature
    # frequency, PV1-PV3 voltage, PV1/PV2 current, feed limit (0x0260). The
    # block spans 0x025F, which the protocol marks write-only; the reference
    # device answers the whole block read anyway (checked 2026-10-02).
    (0x0251, 17),
    (0x0800, 1),  # fault count
    (0x0820, 1),  # batteries online
    (0x0870, 98),  # lifetime energy counters up to 0x08D1
)

# Bits of the system status word (0x0211) that are decoded. Bit 0: 0 grid
# connected, 1 off-grid. Bit 1: 0 normal, 1 abnormal. Bit 11: the device
# reports that Modbus control is active. Bit 12: 0 battery management system
# not connected, 1 connected. Bits 4-6 are not decoded as a mode: the device
# shows an undocumented value 7 there while Backup Reserve is above 0, and that
# is not an error.
_OFF_GRID_BIT = 0
_SYSTEM_ABNORMAL_BIT = 1
_CONTROL_ACTIVE_BIT = 11
_BMS_CONNECTED_BIT = 12

# System State 2 (0x0213, UINT32): one alert per bit, bit 0 first. The codes are
# the stable values of the System Alerts sensor, so a name here is never
# reworded once released. A bit past the end of the table is reported as
# ``bit_<n>`` instead of being dropped.
_SYSTEM_ALERT_NAMES: tuple[str, ...] = (
    "system_shutdown",  # BIT0
    "upgrade_shutdown",  # BIT1
    "epo_triggered",  # BIT2
    "low_power_mode",  # BIT3
    "fan_failure",  # BIT4
    "system_failure",  # BIT5
    "battery_reverse_connection",  # BIT6
    "battery_disconnected",  # BIT7
    "auxiliary_power_failure",  # BIT8
    "pcs_timeout",  # BIT9
    "pcs_failure",  # BIT10
    "igbt_self_test_failure",  # BIT11
    "high_temperature_protection",  # BIT12
    "battery_overheating",  # BIT13
    "ntc_circuit_failure",  # BIT14
    "system_reset",  # BIT15
    "hardware_version_error",  # BIT16
    "parallel_sync_error",  # BIT17
    "low_temperature_protection",  # BIT18
    "parallel_master_slave_conflict",  # BIT19
    "parallel_slave_setting_error",  # BIT20
    "parallel_inverter_error",  # BIT21
    "parallel_meter_fault",  # BIT22
)
# The state shown while no bit is set.
SYSTEM_ALERTS_NONE = "none"


def _system_alerts(state2: int) -> str:
    """Active alert codes of System State 2 in bit order, ``none`` when clear."""
    active = [
        _SYSTEM_ALERT_NAMES[bit] if bit < len(_SYSTEM_ALERT_NAMES) else f"bit_{bit}"
        for bit in range(state2.bit_length())
        if (state2 >> bit) & 1
    ]
    return ",".join(active) if active else SYSTEM_ALERTS_NONE


# Stage 1 supports the three-phase PowerOcean only (category 1, number 1).
SUPPORTED_PRODUCT_CATEGORY = 1
SUPPORTED_PRODUCT_NUMBER = 1


@dataclass(frozen=True)
class _Register:
    key: str
    offset: int
    count: int
    decode: Callable[[bytes], int | float]
    counter: bool = False


_REGISTERS: tuple[_Register, ...] = (
    _Register("home_w", 0x0206, 2, decode_f32_ws),
    _Register("grid_w", 0x0208, 2, decode_f32_ws),
    _Register("solar_w", 0x020A, 2, decode_f32_ws),
    _Register("batt_w", 0x020C, 2, decode_f32_ws),
    _Register("soc_pct", 0x020E, 1, decode_u16),
    _Register("local_system_status", 0x0211, 2, decode_u32_ws),
    _Register("local_system_state2", 0x0213, 2, decode_u32_ws),
    _Register("ems_backup_ratio_pct", BACKUP_RATIO_OFFSET, 1, decode_u16),
    _Register("local_indicator_brightness_pct", BRIGHTNESS_OFFSET, 1, decode_u16),
    _Register("ems_total_battery_capacity_wh", 0x0227, 2, decode_u32_ws),
    # Whole-system battery figures. The current is positive while charging, and
    # is not the cloud `bp_current_a`, which is a single pack.
    _Register("batt_voltage_v", 0x023D, 2, decode_f32_ws),
    _Register("batt_current_a", 0x023F, 2, decode_f32_ws),
    _Register("batt_temp_c", 0x0241, 2, decode_f32_ws),
    _Register("pcs_ac_freq_hz", 0x0251, 2, decode_f32_ws),
    _Register("mppt_pv1_voltage_v", 0x0253, 2, decode_f32_ws),
    _Register("mppt_pv2_voltage_v", 0x0255, 2, decode_f32_ws),
    _Register("mppt_pv1_current_a", 0x0259, 2, decode_f32_ws),
    _Register("mppt_pv2_current_a", 0x025B, 2, decode_f32_ws),
    # Maximum grid feed power, current value: the effective limit after the
    # device's safety rules, the same quantity as the cloud `ems_feed_power_limit_w`.
    _Register("ems_feed_power_limit_w", 0x0260, 2, decode_u32_ws),
    _Register("fault_count", 0x0800, 1, decode_u16),
    _Register("bp_online_sum", 0x0820, 1, decode_u16),
    _Register("grid_import_lifetime_energy_kwh", 0x0870, 2, decode_f32_ws, True),
    _Register("grid_export_lifetime_energy_kwh", 0x0880, 2, decode_f32_ws, True),
    _Register("batt_charge_energy_kwh", 0x08B0, 2, decode_f32_ws, True),
    _Register("batt_discharge_energy_kwh", 0x08C0, 2, decode_f32_ws, True),
    _Register("solar_lifetime_energy_kwh", 0x08D0, 2, decode_f32_ws, True),
)

# The device's lifetime counters: the one list the coordinator holds monotonic.
LIFETIME_COUNTER_KEYS: frozenset[str] = frozenset(
    register.key for register in _REGISTERS if register.counter
)


def _slice(blocks: Mapping[int, bytes], offset: int, count: int) -> bytes | None:
    """Cut ``count`` registers at ``offset`` out of the block that holds them."""
    for start, raw in blocks.items():
        if start <= offset and offset + count <= start + len(raw) // 2:
            low = (offset - start) * 2
            return raw[low : low + count * 2]
    return None


def parse_registers(blocks: Mapping[int, bytes]) -> dict[str, Any]:
    """Map polled register blocks onto PowerOcean sensor keys.

    A register whose block is absent or too short is skipped, a non-finite
    float is dropped. A lifetime counter that reads 0 or less is dropped too:
    a counter that has run for years is never 0, so that is a bad read, and
    published as the first value it would register as a meter reset.

    The per-pack SoC registers (0x0821-0x0823) are deliberately not mapped:
    they hold the SoC the app shows the user, the cloud stream's ``packN_soc``
    is the BMS figure, and the two differ by up to five points.
    """
    result: dict[str, Any] = {}
    for register in _REGISTERS:
        raw = _slice(blocks, register.offset, register.count)
        if raw is None:
            continue
        value = register.decode(raw)
        if isinstance(value, float) and not math.isfinite(value):
            continue
        if register.counter and value <= 0:
            continue
        result[register.key] = value

    # Derived from the status word, and only when the word was read: a frame
    # without it says nothing about control, grid state or health, so no False
    # is invented.
    status = result.get("local_system_status")
    if status is not None:
        result["local_off_grid"] = bool((status >> _OFF_GRID_BIT) & 1)
        result["local_system_abnormal"] = bool((status >> _SYSTEM_ABNORMAL_BIT) & 1)
        result["modbus_control_active"] = bool((status >> _CONTROL_ACTIVE_BIT) & 1)
        result["local_bms_connected"] = bool((status >> _BMS_CONNECTED_BIT) & 1)

    # Same rule for System State 2: no word read, no "none" invented.
    state2 = result.get("local_system_state2")
    if state2 is not None:
        result["local_system_alerts"] = _system_alerts(state2)

    return remap_proto_keys(result)


def parse_device_info(blocks: Mapping[int, bytes]) -> dict[str, Any]:
    """Decode the setup blocks into identity fields (only those present)."""
    info: dict[str, Any] = {}
    for key, offset in (
        ("protocol_version", 0x0000),
        ("product_category", 0x0001),
        ("product_number", 0x0002),
    ):
        raw = _slice(blocks, offset, 1)
        if raw is not None:
            info[key] = decode_u16(raw)

    raw = _slice(blocks, 0x0003, 8)
    if raw is not None:
        info["serial"] = decode_ascii(raw)

    # A UINT32 arrives word-swapped like every other 32-bit register: the
    # wire bytes 25 0a 05 01 are the value 0x0501250a, shown by the app as
    # 5.1.37.10 (confirmed by the owner of the reference device).
    raw = _slice(blocks, 0x000B, 2)
    if raw is not None:
        info["firmware"] = ".".join(str(byte) for byte in raw[2:4] + raw[0:2])
    return info


def is_supported_device(info: Mapping[str, Any]) -> bool:
    """True for the three-phase PowerOcean (stage 1 scope)."""
    return (
        info.get("product_category") == SUPPORTED_PRODUCT_CATEGORY
        and info.get("product_number") == SUPPORTED_PRODUCT_NUMBER
    )
