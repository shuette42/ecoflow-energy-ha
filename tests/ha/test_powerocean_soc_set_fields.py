"""Which fields the PowerOcean SoC/surplus pair write sends (#631).

The PowerOcean Plus app sends the pair as fields 1, 2 and 4 and never field 3
(12 of 12 app writes in the reporter's downloads, for example backup 30 and
surplus 70 as `0864101e2046`). Every other PowerOcean keeps the field 3 + field
4 pair. The assertions read the frame the coordinator hands to the MQTT client,
not the lookup.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)

from .conftest import MOCK_POWEROCEAN_DEVICE

# Backup reserve 30, surplus 70 (0x1e, 0x46): fields 1=100, 2=30, [3=70,] 4=70.
_APP_FRAME_PDATA = "0864101e2046"
_PAIR_FRAME_PDATA = "0864101e18462046"


def _sent_pdata(mqtt: MagicMock) -> str:
    """The inner pdata of the one frame the coordinator published."""
    mqtt.send_proto_set.assert_called_once()
    headers, _ = decode_header_message(mqtt.send_proto_set.call_args[0][0])
    (header,) = headers
    assert header["cmd_id"] == 112
    return str(header["pdata"])


async def _write_pair(
    hass: HomeAssistant, entry: MockConfigEntry, sn: str
) -> MagicMock:
    entry.add_to_hass(hass)
    device: dict[str, Any] = {**MOCK_POWEROCEAN_DEVICE, "sn": sn}
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    mqtt = MagicMock()
    mqtt.is_connected.return_value = True
    mqtt.send_proto_set.return_value = True
    coordinator._mqtt_client = mqtt

    assert await coordinator.async_set_powerocean_soc(30, 70) is True
    return mqtt


@pytest.mark.parametrize(
    "sn",
    ["R372TEST00000001", "R373TEST00000001"],
    ids=["r372", "r373-by-prefix"],
)
async def test_plus_family_is_written_like_the_app(
    hass: HomeAssistant, enhanced_config_entry: MockConfigEntry, sn: str
) -> None:
    """A PowerOcean Plus gets fields 1, 2 and 4, as the app sends them."""
    mqtt = await _write_pair(hass, enhanced_config_entry, sn)

    assert _sent_pdata(mqtt) == _APP_FRAME_PDATA


@pytest.mark.parametrize(
    "sn",
    ["HJ31TEST00000001", MOCK_POWEROCEAN_DEVICE["sn"]],
    ids=["hj31", "hw52"],
)
async def test_other_powerocean_keeps_field_3_and_4(
    hass: HomeAssistant, enhanced_config_entry: MockConfigEntry, sn: str
) -> None:
    """A unit outside the Plus family is written with the field 3 + 4 pair."""
    mqtt = await _write_pair(hass, enhanced_config_entry, sn)

    assert _sent_pdata(mqtt) == _PAIR_FRAME_PDATA
