"""RIVER 2 Max Remaining Time through the real coordinator paths (#585).

`tests/test_river2max_r611_routing.py` pins the resolver itself. These tests
prove it is wired in on both ways a frame reaches `_device_data`: the MQTT
apply (`_apply_data`) and the HTTP poll. The frames are the recorded ones: 286
min on battery (input 0 W) and 4092 min full on AC (input 68 W), where the app
and the unit's screen showed 99 h 59 min.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_DELTA,
    DOMAIN,
    MODE_ENHANCED,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator

SERIAL = "R611TEST0000ABCD"
DEVICE: dict[str, Any] = {
    "sn": SERIAL,
    "name": "",
    "product_name": "",
    "device_type": DEVICE_TYPE_DELTA,
    "online": 1,
}

ON_BATTERY: dict[str, Any] = {
    "pd.wattsInSum": 0,
    "pd.wattsOutSum": 67,
    "pd.remainTime": 286,
    "pd.soc": 97,
}
FULL_ON_AC: dict[str, Any] = {
    "pd.wattsInSum": 68,
    "pd.wattsOutSum": 68,
    "pd.remainTime": 4092,
    "pd.soc": 100,
}


def _coordinator(hass: HomeAssistant) -> EcoFlowDeviceCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [DEVICE],
        },
        unique_id="test@example.com",
    )
    entry.add_to_hass(hass)
    return EcoFlowDeviceCoordinator(hass, entry, DEVICE)


async def test_mqtt_apply_shows_battery_runtime_and_clears_it_on_ac(
    hass: HomeAssistant,
) -> None:
    from custom_components.ecoflow_energy.ecoflow.parsers.delta_http import (
        parse_delta_http_quota,
    )

    coordinator = _coordinator(hass)
    coordinator._apply_data(parse_delta_http_quota(dict(ON_BATTERY)))
    assert coordinator._device_data["remain_time_min"] == 286

    coordinator._apply_data(parse_delta_http_quota(dict(FULL_ON_AC)))
    assert "remain_time_min" in coordinator._device_data
    assert coordinator._device_data["remain_time_min"] is None


async def test_http_poll_clears_the_ac_value_too(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    client = MagicMock()
    client.get_quota_all = AsyncMock(return_value=dict(FULL_ON_AC))
    coordinator._http_client = client

    await coordinator._async_update_data()

    assert "remain_time_min" in coordinator._device_data
    assert coordinator._device_data["remain_time_min"] is None
    assert coordinator._device_data["soc"] == 100
