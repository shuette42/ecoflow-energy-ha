"""The PowerGlow power sources through the coordinator's ingest path (#588,
PLAN-217).

`tests/test_powerglow_power_sources.py` proves the decode and the remap from
the real frames; this proves what the coordinator does with them: a report
that carries the total lands the three shares in `_device_data`, and a report
that carries only the rod's serial leaves the stored shares exactly as they
were, so a sensor never drops to 0 W between two real readings.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.ecoflow_energy.const import DEVICE_TYPE_POWEROCEAN
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)
from tests.ha.test_powerpulse_edev_ingest import _coordinator

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "powerglow"
    / "hf33_power_source_frames.json"
)

POWEROCEAN_DEVICE: dict[str, Any] = {
    "sn": "HJ31TEST00000001",
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}
TOPIC = f"/app/user123/{POWEROCEAN_DEVICE['sn']}/thing/property"

PV = "heating_rod_power_from_pv_w"
BATTERY = "heating_rod_power_from_battery_w"
GRID = "heating_rod_power_from_grid_w"


def _frame(name: str) -> bytes:
    frames = json.loads(FIXTURE.read_text())["frames"]
    return bytes.fromhex(next(f for f in frames if f["name"] == name)["hex"])


def _ingest(coordinator: Any, name: str) -> dict[str, Any] | None:
    parsed = coordinator._parse_message(TOPIC, _frame(name))
    if parsed:
        coordinator._apply_data(parsed)
    return parsed


async def test_a_report_with_the_total_lands_all_three_shares(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)

    parsed = _ingest(coordinator, "pv_battery_grid")

    assert parsed is not None
    assert (parsed[PV], parsed[BATTERY], parsed[GRID]) == (116.0, 33.0, 3.0)
    data = coordinator._device_data
    assert (data[PV], data[BATTERY], data[GRID]) == (116.0, 33.0, 3.0)


async def test_a_serial_only_report_leaves_the_stored_shares_untouched(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    _ingest(coordinator, "pv_battery_grid")

    parsed = _ingest(coordinator, "serial_only")

    assert not parsed or not {PV, BATTERY, GRID} & set(parsed)
    data = coordinator._device_data
    assert (data[PV], data[BATTERY], data[GRID]) == (116.0, 33.0, 3.0)


async def test_a_serial_only_report_first_does_not_invent_a_zero(
    hass: HomeAssistant,
) -> None:
    """Before any reading, "not sent" must stay unknown rather than 0 W."""
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)

    _ingest(coordinator, "serial_only")

    assert not {PV, BATTERY, GRID} & set(coordinator._device_data)


async def test_a_later_report_without_grid_overwrites_the_grid_with_zero(
    hass: HomeAssistant,
) -> None:
    """The shares add up to the total, so an absent grid share beside the total
    is a real 0 W and replaces the 3 W of the report before it."""
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    _ingest(coordinator, "pv_battery_grid")

    _ingest(coordinator, "pv_battery_no_grid")

    data = coordinator._device_data
    assert (data[PV], data[BATTERY], data[GRID]) == (112.0, 41.0, 0.0)


POWER = "heating_rod_power_w"


def _ingest_212_8(coordinator: Any, power_w: int) -> dict[str, Any] | None:
    """Feed a 212/8 report whose rod draws `power_w` (field 4)."""
    header = (
        encode_field_bytes(1, encode_field_varint(4, power_w))
        + encode_field_varint(8, 212)
        + encode_field_varint(9, 8)
    )
    parsed = coordinator._parse_message(TOPIC, encode_field_bytes(1, header))
    if parsed:
        coordinator._apply_data(parsed)
    return parsed


async def test_a_rod_drawing_nothing_zeroes_the_published_shares(
    hass: HomeAssistant,
) -> None:
    """212/33 may stop when the rod stops, so 212/8 saying 0 W ends the shares."""
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    _ingest(coordinator, "pv_battery_grid")

    _ingest_212_8(coordinator, 0)

    data = coordinator._device_data
    assert data[POWER] == 0.0
    assert (data[PV], data[BATTERY], data[GRID]) == (0.0, 0.0, 0.0)


async def test_a_rod_still_drawing_leaves_the_shares_alone(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    _ingest(coordinator, "pv_battery_grid")

    _ingest_212_8(coordinator, 152)

    data = coordinator._device_data
    assert (data[PV], data[BATTERY], data[GRID]) == (116.0, 33.0, 3.0)


async def test_a_zero_watt_report_before_any_212_33_creates_no_share_keys(
    hass: HomeAssistant,
) -> None:
    """The zeroing only touches shares already published, so a rod that never
    sent 212/33 does not grow three sensors from a 212/8."""
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)

    parsed = _ingest_212_8(coordinator, 0)

    assert parsed is not None and parsed[POWER] == 0.0
    assert not {PV, BATTERY, GRID} & set(parsed)
    assert not {PV, BATTERY, GRID} & set(coordinator._device_data)
