"""PowerOcean local Modbus register map (read-only).

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
    (0x0206, 9),  # load, grid, solar, battery power, SoC
    (0x0217, 18),  # backup ratio ... battery capacity (0x0227-0x0228)
    (0x0251, 12),  # frequency, PV1/PV2/PV3 voltage, PV1/PV2 current
    (0x0800, 1),  # fault count
    (0x0820, 4),  # batteries online, pack 1-3 SoC
    (0x0870, 98),  # lifetime energy counters up to 0x08D1
)

# Stage 1 supports the three-phase PowerOcean only (category 1, number 1).
SUPPORTED_PRODUCT_CATEGORY = 1
SUPPORTED_PRODUCT_NUMBER = 1

_MAX_PACKS = 3


@dataclass(frozen=True)
class _Register:
    key: str
    offset: int
    count: int
    decode: Callable[[bytes], int | float]


_REGISTERS: tuple[_Register, ...] = (
    _Register("home_w", 0x0206, 2, decode_f32_ws),
    _Register("grid_w", 0x0208, 2, decode_f32_ws),
    _Register("solar_w", 0x020A, 2, decode_f32_ws),
    _Register("batt_w", 0x020C, 2, decode_f32_ws),
    _Register("soc_pct", 0x020E, 1, decode_u16),
    _Register("ems_backup_ratio_pct", 0x0217, 1, decode_u16),
    _Register("ems_total_battery_capacity_wh", 0x0227, 2, decode_u32_ws),
    _Register("pcs_ac_freq_hz", 0x0251, 2, decode_f32_ws),
    _Register("mppt_pv1_voltage_v", 0x0253, 2, decode_f32_ws),
    _Register("mppt_pv2_voltage_v", 0x0255, 2, decode_f32_ws),
    _Register("mppt_pv1_current_a", 0x0259, 2, decode_f32_ws),
    _Register("mppt_pv2_current_a", 0x025B, 2, decode_f32_ws),
    _Register("fault_count", 0x0800, 1, decode_u16),
    _Register("bp_online_sum", 0x0820, 1, decode_u16),
    _Register("pack1_soc", 0x0821, 1, decode_u16),
    _Register("pack2_soc", 0x0822, 1, decode_u16),
    _Register("pack3_soc", 0x0823, 1, decode_u16),
    _Register("grid_import_lifetime_energy_kwh", 0x0870, 2, decode_f32_ws),
    _Register("grid_export_lifetime_energy_kwh", 0x0880, 2, decode_f32_ws),
    _Register("batt_charge_energy_kwh", 0x08B0, 2, decode_f32_ws),
    _Register("batt_discharge_energy_kwh", 0x08C0, 2, decode_f32_ws),
    _Register("solar_lifetime_energy_kwh", 0x08D0, 2, decode_f32_ws),
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
    float is dropped. ``packN_soc`` is published only for ``N`` up to the
    number of batteries online: an unused slot reads 0, which means "no pack"
    and not 0 %.
    """
    result: dict[str, Any] = {}
    for register in _REGISTERS:
        raw = _slice(blocks, register.offset, register.count)
        if raw is None:
            continue
        value = register.decode(raw)
        if isinstance(value, float) and not math.isfinite(value):
            continue
        result[register.key] = value

    online = result.get("bp_online_sum")
    for number in range(1, _MAX_PACKS + 1):
        key = f"pack{number}_soc"
        if key in result and (online is None or number > online):
            del result[key]

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

    # Firmware bytes are not word-swapped: 25 0a 05 01 is 37.10.5.1.
    raw = _slice(blocks, 0x000B, 2)
    if raw is not None:
        info["firmware"] = ".".join(str(byte) for byte in raw)
    return info


def is_supported_device(info: Mapping[str, Any]) -> bool:
    """True for the three-phase PowerOcean (stage 1 scope)."""
    return (
        info.get("product_category") == SUPPORTED_PRODUCT_CATEGORY
        and info.get("product_number") == SUPPORTED_PRODUCT_NUMBER
    )
