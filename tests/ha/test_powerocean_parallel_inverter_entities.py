"""A PowerOcean pair's unit rows become per-inverter entities (#436).

The parser tests next door prove the keys; these prove the path around them:
a real 96/50 frame from the #347 pair goes in through the MQTT handler, the
keys land in the coordinator's device data, and the sensor platform creates
the six per-inverter entities from them. A PowerOcean that has not sent the
list gets none, the same gate the heating rod and the schedule slots use.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ecoflow_energy.const import DOMAIN
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

from .conftest import add_entities_collector
from .test_powerglow_gating import INVERTER_KEYS, POWEROCEAN_DEVICE, _entry, _keys

_FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "powerocean"
    / "parallel_energy_stream_masked.json"
)
_TOPIC = f"/app/device/property/{POWEROCEAN_DEVICE['sn']}"


def _pair_frame(index: int) -> bytes:
    for frame in json.loads(_FIXTURE.read_text())["frames"]:
        if (
            frame["source"].startswith("andy-j32e-pair")
            and frame["frame_index"] == index
        ):
            return bytes.fromhex(frame["hex"])
    raise AssertionError(f"no pair frame #{index}")


async def _setup(
    hass: HomeAssistant, payloads: list[bytes]
) -> tuple[EcoFlowDeviceCoordinator, list[Any]]:
    """Feed frames through the MQTT handler, then run the sensor platform."""
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN_DEVICE)
    for payload in payloads:
        coordinator._on_mqtt_message(_TOPIC, payload)
    await hass.async_block_till_done()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        POWEROCEAN_DEVICE["sn"]: coordinator
    }
    created: list[Any] = []
    await sensor_setup(hass, entry, add_entities_collector(created))
    return coordinator, created


async def test_pair_frame_creates_the_six_inverter_entities(
    hass: HomeAssistant,
) -> None:
    coordinator, created = await _setup(hass, [_pair_frame(1)])

    assert coordinator.device_data["inverter_1_solar_w"] == pytest.approx(
        2515.444, abs=0.01
    )
    assert coordinator.device_data["inverter_2_solar_w"] == 0.0
    assert coordinator.device_data["inverter_2_soc_pct"] == 96.0
    assert _keys(created) & INVERTER_KEYS == INVERTER_KEYS
    assert not any("PAIR-TEST-UNIT" in str(v) for v in coordinator.device_data.values())


async def test_a_powerocean_without_the_list_gets_no_inverter_entities(
    hass: HomeAssistant,
) -> None:
    coordinator, created = await _setup(hass, [])

    assert not _keys(created) & INVERTER_KEYS
    assert "soc_pct" in _keys(created)
    assert not any(key.startswith("inverter_") for key in coordinator.device_data)
