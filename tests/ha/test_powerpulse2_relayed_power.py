"""PowerPulse 2 charging power relayed by the PowerOcean on `241/33` (#691).

The wallbox's own heartbeat (`2/33`) reports its charging power once a
minute; its PowerOcean publishes the same reading about every 5 s on its own
topic. These tests replay the reporter's real `241/33` frames into the
PowerOcean coordinator and check that the reading lands on the wallbox only
while it charges, and nowhere else.

The download masks the wallbox serial in the frame as 16 x X, so the wallbox
here carries that serial: the routing under test is by the frame's serial.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import DOMAIN
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.diagnostics import _device_diagnostics
from custom_components.ecoflow_energy.ecoflow.const import DEVICE_TYPE_SMARTPLUG
from custom_components.ecoflow_energy.ecoflow.parsers.powerpulse_proto import (
    RELAYED_WALLBOX_POWER_KEY,
)
from tests.ha.test_powerpulse2_charge_action import (
    POWEROCEAN_DEVICE,
    POWEROCEAN_SN,
    POWERPULSE2_DEVICE,
    _connected_mqtt,
    _entry,
    _wire_entry,
)

_FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "powerpulse"
        / "c376_relayed_power_691_20261107.json"
    ).read_text()
)
_PO_TOPIC = f"/app/device/property/{POWEROCEAN_SN}"
_MASKED_WALLBOX = {**POWERPULSE2_DEVICE, "sn": "X" * 16}


def _frame(label: str) -> bytes:
    for frame in _FIXTURE["frames"]:
        if frame["label"] == label:
            return bytes.fromhex(frame["hex"])
    raise KeyError(label)


def _wire(
    hass: HomeAssistant, ocean_devices: list[dict[str, Any]]
) -> tuple[MockConfigEntry, list[EcoFlowDeviceCoordinator], EcoFlowDeviceCoordinator]:
    """`_wire_entry` with the wallbox under the frame's masked serial."""
    entry = _entry(hass, [*ocean_devices, _MASKED_WALLBOX])
    oceans = []
    for device in ocean_devices:
        coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
        coordinator._mqtt_client = _connected_mqtt()
        oceans.append(coordinator)
    wallbox = EcoFlowDeviceCoordinator(hass, entry, _MASKED_WALLBOX)
    wallbox._mqtt_client = _connected_mqtt()
    coordinators = {c.device_sn: c for c in [*oceans, wallbox]}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinators
    return entry, oceans, wallbox


def _relay(ocean: EcoFlowDeviceCoordinator, label: str) -> None:
    """Deliver one recorded `241/33` frame the way the PowerOcean receives it."""
    parsed = ocean._parse_message(_PO_TOPIC, _frame(label))
    assert parsed is not None and RELAYED_WALLBOX_POWER_KEY in parsed
    ocean._apply_data(parsed)


async def test_relayed_power_lands_on_a_charging_wallbox_and_nowhere_else(
    hass: HomeAssistant,
) -> None:
    """While the wallbox charges, each relayed reading replaces its power
    (3010 from the heartbeat, then 3410 and 3430 from the PowerOcean). The
    PowerOcean's store and the wallbox's liveness stay untouched.

    Mutation probes: handing over with `own_connection=True` moves the
    wallbox's `_last_mqtt_ts`; not popping the key leaves it in the
    PowerOcean's store.
    """
    _entry_, oceans, wallbox = _wire(hass, [POWEROCEAN_DEVICE])
    ocean = oceans[0]
    wallbox._apply_data({"ev_charge_status": "charging", "ev_charge_power_w": 3010.0})
    own_ts = wallbox._last_mqtt_ts

    _relay(ocean, "charging")
    assert wallbox.device_data["ev_charge_power_w"] == 3410.0
    _relay(ocean, "charging_later")
    assert wallbox.device_data["ev_charge_power_w"] == 3430.0

    assert wallbox._last_mqtt_ts == own_ts
    assert "ev_charge_power_w" not in ocean.device_data
    assert RELAYED_WALLBOX_POWER_KEY not in ocean.device_data
    assert ocean.relayed_power_stats == {"handed_over": 2}
    assert wallbox.relayed_power_stats == {"applied": 2}
    # The download shows both directions, and nothing on a device without any.
    for coordinator in (ocean, wallbox):
        coordinator._mqtt_client = None
    assert _device_diagnostics(ocean)["relayed_wallbox_power"] == {"handed_over": 2}
    assert _device_diagnostics(wallbox)["relayed_wallbox_power"] == {"applied": 2}
    assert "relayed_wallbox_settings" not in _device_diagnostics(wallbox)


async def test_relayed_power_is_dropped_unless_the_wallbox_charges(
    hass: HomeAssistant,
) -> None:
    """A relayed reading arriving after the heartbeat ended the session, or
    before any status, must not overwrite the wallbox's own reading.

    Mutation probe: removing the `charging` guard writes 3410 over the
    heartbeat's 0.0 and fails here.
    """
    _entry_, oceans, wallbox = _wire(hass, [POWEROCEAN_DEVICE])
    ocean = oceans[0]

    _relay(ocean, "charging")
    assert "ev_charge_power_w" not in wallbox.device_data

    wallbox._apply_data({"ev_charge_status": "finished", "ev_charge_power_w": 0.0})
    _relay(ocean, "charging")
    assert wallbox.device_data["ev_charge_power_w"] == 0.0

    assert wallbox.relayed_power_stats == {"dropped_not_charging": 2}
    assert ocean.relayed_power_stats is None
    assert "ev_charge_power_w" not in ocean.device_data


async def test_relayed_power_without_its_wallbox_is_counted_and_dropped(
    hass: HomeAssistant,
) -> None:
    """A serial no PowerPulse 2 of the entry carries, and a wallbox with two
    PowerOceans (no unique sibling), are both unrouted: counted, not applied,
    not buffered for later."""
    # The entry's wallbox has another serial than the frame names.
    _entry_, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
    wallbox._apply_data({"ev_charge_status": "charging"})
    _relay(oceans[0], "charging")
    assert "ev_charge_power_w" not in wallbox.device_data
    assert oceans[0].relayed_power_stats == {"unrouted": 1}
    assert wallbox.relayed_power_stats is None


async def test_relayed_power_with_two_powerocean_is_unrouted(
    hass: HomeAssistant,
) -> None:
    """With two PowerOceans neither is the wallbox's sibling."""
    second = {**POWEROCEAN_DEVICE, "sn": "HJ31TEST00000002"}
    _entry_, oceans, wallbox = _wire(hass, [POWEROCEAN_DEVICE, second])
    wallbox._apply_data({"ev_charge_status": "charging"})
    _relay(oceans[0], "charging")
    assert "ev_charge_power_w" not in wallbox.device_data
    assert oceans[0].relayed_power_stats == {"unrouted": 1}
    assert wallbox.relayed_power_stats is None


async def test_descriptor_only_frame_hands_nothing_over(
    hass: HomeAssistant,
) -> None:
    """Outside a session the item has no `pwr`: nothing is relayed, the
    wallbox keeps its reading and no counter moves."""
    _entry_, oceans, wallbox = _wire(hass, [POWEROCEAN_DEVICE])
    ocean = oceans[0]
    wallbox._apply_data({"ev_charge_status": "charging", "ev_charge_power_w": 3010.0})

    parsed = ocean._parse_message(_PO_TOPIC, _frame("descriptor_only"))
    assert parsed is None or RELAYED_WALLBOX_POWER_KEY not in parsed
    if parsed:
        ocean._apply_data(parsed)

    assert wallbox.device_data["ev_charge_power_w"] == 3010.0
    assert ocean.relayed_power_stats is None
    assert wallbox.relayed_power_stats is None


_READING = {"ev_charge_power_w": 3410.0}


def _charging(wallbox: EcoFlowDeviceCoordinator) -> None:
    wallbox._apply_data({"ev_charge_status": "charging", "ev_charge_power_w": 3010.0})


async def test_a_power_only_frame_leaves_the_powerocean_untouched(
    hass: HomeAssistant,
) -> None:
    """The reporter's `241/33` carries nothing for the PowerOcean itself: no
    listener call, no new snapshot, no value-change bookkeeping on it.

    Mutation probe: dropping `or carried_power` from the early return in
    `_apply_data` makes the PowerOcean publish an update for the frame.
    """
    _entry_, oceans, wallbox = _wire(hass, [POWEROCEAN_DEVICE])
    ocean = oceans[0]
    _charging(wallbox)
    snapshot = ocean._snapshot
    unchanged = ocean._unchanged_updates
    with patch.object(ocean, "async_set_updated_data") as updated:
        _relay(ocean, "charging")
    assert updated.call_count == 0
    assert ocean._snapshot is snapshot
    assert ocean._unchanged_updates == unchanged
    assert wallbox.device_data["ev_charge_power_w"] == 3410.0


async def test_a_mixed_frame_applies_the_rest_to_the_powerocean(
    hass: HomeAssistant,
) -> None:
    """Relayed power beside a reading of the PowerOcean's own: the relay key
    goes to the wallbox, the rest is applied to the PowerOcean as before.

    Mutation probe: returning early whenever power was carried drops the
    PowerOcean's own reading.
    """
    _entry_, oceans, wallbox = _wire(hass, [POWEROCEAN_DEVICE])
    ocean = oceans[0]
    _charging(wallbox)
    with patch.object(ocean, "async_set_updated_data") as updated:
        ocean._apply_data(
            {RELAYED_WALLBOX_POWER_KEY: {"X" * 16: dict(_READING)}, "soc_pct": 57}
        )
    assert updated.call_count == 1
    assert ocean.device_data["soc_pct"] == 57
    assert RELAYED_WALLBOX_POWER_KEY not in ocean.device_data
    assert wallbox.device_data["ev_charge_power_w"] == 3410.0


async def test_a_frame_from_another_connection_is_dropped_unrouted(
    hass: HomeAssistant,
) -> None:
    """A relay key reaching the PowerOcean through a hand-over (not its own
    connection) is popped and dropped: no routing, no counter.

    Mutation probe: removing the `own_connection` check hands it on.
    """
    _entry_, oceans, wallbox = _wire(hass, [POWEROCEAN_DEVICE])
    ocean = oceans[0]
    _charging(wallbox)
    ocean._apply_data(
        {RELAYED_WALLBOX_POWER_KEY: {"X" * 16: dict(_READING)}}, own_connection=False
    )
    assert wallbox.device_data["ev_charge_power_w"] == 3010.0
    assert ocean.relayed_power_stats is None
    assert RELAYED_WALLBOX_POWER_KEY not in ocean.device_data


async def test_a_failing_hand_over_is_counted_and_contained(
    hass: HomeAssistant,
) -> None:
    """A wallbox that raises on the hand-over costs the PowerOcean nothing.

    Mutation probe: removing the try around the hand-over lets the error
    escape `_apply_data`.
    """
    _entry_, oceans, wallbox = _wire(hass, [POWEROCEAN_DEVICE])
    ocean = oceans[0]
    with patch.object(
        wallbox, "apply_relayed_wallbox_power", side_effect=RuntimeError("boom")
    ):
        _relay(ocean, "charging")
    assert ocean.relayed_power_stats == {"handoff_failed": 1}


async def test_a_wallbox_never_routes_a_relay_to_itself(
    hass: HomeAssistant,
) -> None:
    """A relay key naming the receiving coordinator itself is unrouted. On a
    wallbox `target is self` and the sibling check both refuse it (its
    sibling is the PowerOcean), so this pins the outcome, not one clause.

    Mutation probe: removing both clauses applies the reading.
    """
    _entry_, _oceans, wallbox = _wire(hass, [POWEROCEAN_DEVICE])
    _charging(wallbox)
    wallbox._apply_data(
        {RELAYED_WALLBOX_POWER_KEY: {"X" * 16: {"ev_charge_power_w": 1.0}}}
    )
    assert wallbox.device_data["ev_charge_power_w"] == 3010.0
    assert wallbox.relayed_power_stats == {"unrouted": 1}


async def test_a_serial_of_another_device_type_is_unrouted(
    hass: HomeAssistant,
) -> None:
    """A Smart Plug of the same entry whose one PowerOcean is this one is
    still not a wallbox: only the device type check refuses it.

    Mutation probe: removing the device type check applies the power to the
    plug.
    """
    plug_device: dict[str, Any] = {
        "sn": "HW52TEST00000001",
        "name": "Smart Plug",
        "product_name": "Smart Plug",
        "device_type": DEVICE_TYPE_SMARTPLUG,
        "online": 1,
    }
    entry, oceans, wallbox = _wire(hass, [POWEROCEAN_DEVICE])
    plug = EcoFlowDeviceCoordinator(hass, entry, plug_device)
    plug._apply_data({"ev_charge_status": "charging"})
    hass.data[DOMAIN][entry.entry_id][plug.device_sn] = plug
    assert plug.powerocean_sibling() is oceans[0]
    oceans[0]._apply_data({RELAYED_WALLBOX_POWER_KEY: {plug.device_sn: dict(_READING)}})
    assert "ev_charge_power_w" not in plug.device_data
    assert oceans[0].relayed_power_stats == {"unrouted": 1}


async def test_a_wallbox_paired_with_another_powerocean_is_unrouted(
    hass: HomeAssistant,
) -> None:
    """A wallbox whose PowerOcean is another coordinator does not take the
    reading from this one.

    Mutation probe: removing the sibling check applies it.
    """
    _entry_, oceans, wallbox = _wire(hass, [POWEROCEAN_DEVICE])
    _charging(wallbox)
    other = object()
    with patch.object(wallbox, "powerocean_sibling", return_value=other):
        _relay(oceans[0], "charging")
    assert wallbox.device_data["ev_charge_power_w"] == 3010.0
    assert oceans[0].relayed_power_stats == {"unrouted": 1}
