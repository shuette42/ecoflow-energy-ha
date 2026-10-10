"""Platform outcomes for supported and read-only PowerPulse prefixes."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ecoflow_energy.button import async_setup_entry as button_setup
from custom_components.ecoflow_energy.number import async_setup_entry as number_setup
from custom_components.ecoflow_energy.select import async_setup_entry as select_setup
from custom_components.ecoflow_energy.switch import async_setup_entry as switch_setup

from .conftest import add_entities_collector
from .test_powerpulse2_controls import (
    POWEROCEAN_DEVICE,
    POWERPULSE2_DEVICE,
    _report_descriptor,
    _wire_entry,
)


@pytest.mark.parametrize(
    "prefix,allowed", [("C371", False), ("C379", False), ("C374", True), ("C376", True)]
)
@pytest.mark.parametrize("sibling", [False, True])
async def test_control_creation_by_prefix(
    hass: HomeAssistant, prefix: str, allowed: bool, sibling: bool
) -> None:
    with patch.dict(POWERPULSE2_DEVICE, {"sn": prefix + "TEST00000001"}):
        entry, _, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE] if sibling else [])
    _report_descriptor(wallbox)
    wallbox.set_device_value("ev_charge_status", "available")
    wallbox.set_device_value("ev_max_current_a", 16.0)
    wallbox.set_device_value("ev_charge_current_a", 6.0)
    wallbox.set_device_value("ev_charge_mode", "solar")
    wallbox.set_device_value("ev_solar_min_current_a", 6.0)
    wallbox.set_device_value("ev_custom_current_a", 6.0)
    wallbox.set_device_value("ev_phase_setting", 0)
    wallbox.set_device_value("ev_settings_switch_bits", 0)
    for setup, expected in [
        (button_setup, 2),
        (number_setup, 3 if sibling else 1),
        (select_setup, 2 * int(sibling)),
        (switch_setup, 3 * int(sibling)),
    ]:
        entities: list[Any] = []
        await setup(hass, entry, add_entities_collector(entities))
        # Exclude the sibling PowerOcean's own controls.
        wallbox_entities = [
            e for e in entities if e.unique_id.startswith(prefix + "TEST")
        ]
        assert len(wallbox_entities) == (expected if allowed else 0)
