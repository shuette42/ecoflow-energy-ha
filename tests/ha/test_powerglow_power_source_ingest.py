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

import pytest
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

    assert not {PV, BATTERY, GRID} & set(parsed or {})
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


async def test_a_212_33_after_a_zero_watt_report_restores_the_shares(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    _ingest(coordinator, "pv_battery_grid")
    _ingest_212_8(coordinator, 0)

    _ingest(coordinator, "pv_battery_no_grid")

    data = coordinator._device_data
    assert (data[PV], data[BATTERY], data[GRID]) == (112.0, 41.0, 0.0)


async def test_a_212_8_without_power_leaves_the_published_shares_alone(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    _ingest(coordinator, "pv_battery_grid")
    header = (
        encode_field_bytes(1, encode_field_varint(5, 154))
        + encode_field_varint(8, 212)
        + encode_field_varint(9, 8)
    )
    parsed = coordinator._parse_message(TOPIC, encode_field_bytes(1, header))
    if parsed:
        coordinator._apply_data(parsed)

    assert POWER not in coordinator._device_data
    data = coordinator._device_data
    assert (data[PV], data[BATTERY], data[GRID]) == (116.0, 33.0, 3.0)


# The bare 212/33 rule (#700): the rod sends 212/33 every ~5 s, idle or not, and
# at idle every frame is the bare serial. It reads as 0 W for all three sources
# only while Heating Rod Power is known at 0 W.
SHARES = (PV, BATTERY, GRID)


async def test_a_bare_report_after_a_restart_at_zero_watt_sets_the_shares_to_zero(
    hass: HomeAssistant,
) -> None:
    """After a restart the store is empty. The get-all reply lands the rod at
    0 W (real frame, no shares), and the next bare 212/33 turns the shares from
    unknown into 0 W."""
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    _ingest(coordinator, "rod_off_212_8")
    assert coordinator._device_data[POWER] == 0.0
    assert not set(SHARES) & set(coordinator._device_data)

    parsed = _ingest(coordinator, "serial_only")

    assert parsed is not None
    assert [parsed[key] for key in SHARES] == [0.0, 0.0, 0.0]
    assert [coordinator._device_data[key] for key in SHARES] == [0.0, 0.0, 0.0]


async def test_a_bare_report_while_the_rod_draws_power_leaves_the_shares_unset(
    hass: HomeAssistant,
) -> None:
    """The v1.26.0 promise: a bare frame during a run never drops the shares."""
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    coordinator._device_data[POWER] = 2159.0

    parsed = _ingest(coordinator, "serial_only")

    assert not set(SHARES) & set(parsed or {})
    assert not set(SHARES) & set(coordinator._device_data)


async def test_a_bare_report_while_the_rod_draws_power_keeps_the_stored_shares(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    _ingest(coordinator, "pv_battery_grid")
    coordinator._device_data[POWER] = 2159.0

    _ingest(coordinator, "serial_only")

    assert [coordinator._device_data[key] for key in SHARES] == [116.0, 33.0, 3.0]


async def test_a_bare_report_with_unknown_rod_power_sets_nothing(
    hass: HomeAssistant,
) -> None:
    """Right after a restart, before the get-all reply: unknown stays unknown."""
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    assert POWER not in coordinator._device_data

    parsed = _ingest(coordinator, "serial_only")

    assert not set(SHARES) & set(parsed or {})
    assert not set(SHARES) & set(coordinator._device_data)


@pytest.mark.parametrize("bare_first", [True, False])
async def test_the_bare_rule_does_not_depend_on_the_header_order(
    hass: HomeAssistant, bare_first: bool
) -> None:
    """No capture holds both reports in one envelope, so the two real frames are
    joined: an envelope is a repeated field 1, and concatenating two messages
    is a valid message. The rod's 0 W arrives in the same envelope as the bare
    serial, in either order, so the rule must read the rod after the loop."""
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    bare, rod = _frame("serial_only"), _frame("rod_off_212_8")

    parsed = coordinator._parse_message(TOPIC, bare + rod if bare_first else rod + bare)
    assert parsed is not None
    coordinator._apply_data(parsed)

    assert coordinator._device_data[POWER] == 0.0
    assert [coordinator._device_data[key] for key in SHARES] == [0.0, 0.0, 0.0]


async def test_the_zeroed_shares_count_as_reported_at_notify_time(
    hass: HomeAssistant,
) -> None:
    """`last_applied_keys` is cleared after the notify, so a listener is the
    only place to see it: the shares dropped by the 0 W report must be in it,
    or the share sensors would not write on report."""
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    _ingest(coordinator, "pv_battery_grid")
    seen: list[frozenset[str]] = []
    coordinator.async_add_listener(lambda: seen.append(coordinator.last_applied_keys))

    _ingest(coordinator, "rod_off_212_8")

    assert seen, "the apply never notified a listener"
    assert {POWER, *SHARES} <= seen[-1]
    assert coordinator.last_applied_keys == frozenset()
