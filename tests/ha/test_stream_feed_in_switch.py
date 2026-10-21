"""Stream Ultra X (BK61) Feed-in Control switch, Standard Mode.

Two things have to hold. The switch exists only where its write can work (a
BK61 on developer keys), and it reads the unit's own feed-in mode instead of
inventing a state. The command body is covered in tests/test_stream_commands.py
and the quota mapping in tests/test_stream_http_parser.py; this file covers
setup gating, the write path and the read-back.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    AUTH_METHOD_DEVELOPER,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SECRET_KEY,
    CONF_USER_ID,
    DEVICE_TYPE_STREAM,
    DEVICE_TYPE_STREAM_AC5000,
    DOMAIN,
    MODE_ENHANCED,
    MODE_STANDARD,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.stream_commands import (
    build_feed_in_control_command,
)
from custom_components.ecoflow_energy.switch import (
    EcoFlowSwitch,
)
from custom_components.ecoflow_energy.switch import (
    async_setup_entry as switch_setup,
)

from .conftest import add_entities_collector

FEED_IN_KEY = "feed_in_control_switch"
STATE_KEY = "feed_in_control_enabled"


def _device(prefix: str, device_type: str = DEVICE_TYPE_STREAM) -> dict[str, Any]:
    return {
        "sn": f"{prefix}TEST00000001",
        "name": f"Test {prefix}",
        "product_name": f"Test {prefix}",
        "device_type": device_type,
        "online": 1,
    }


def _entry(device: dict[str, Any], enhanced: bool) -> MockConfigEntry:
    if enhanced:
        data = {
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [device],
        }
        unique_id = "test@example.com"
    else:
        data = {
            CONF_AUTH_METHOD: AUTH_METHOD_DEVELOPER,
            CONF_ACCESS_KEY: "test_ak",
            CONF_SECRET_KEY: "test_sk",
            CONF_MODE: MODE_STANDARD,
            CONF_DEVICES: [device],
        }
        unique_id = "test_ak"
    return MockConfigEntry(
        domain=DOMAIN, title="EcoFlow Energy", data=data, unique_id=unique_id
    )


def _coordinator(
    hass: HomeAssistant,
    prefix: str = "BK61",
    enhanced: bool = False,
    data: dict[str, Any] | None = None,
    device_type: str = DEVICE_TYPE_STREAM,
) -> tuple[EcoFlowDeviceCoordinator, MockConfigEntry]:
    device = _device(prefix, device_type)
    entry = _entry(device, enhanced)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    coordinator._enhanced_mode = enhanced
    coordinator._auth_method = AUTH_METHOD_APP if enhanced else AUTH_METHOD_DEVELOPER
    coordinator._device_data = dict(data or {})
    coordinator.async_set_updated_data(dict(coordinator._device_data))
    coordinator.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    return coordinator, entry


async def _created_switch_keys(
    hass: HomeAssistant,
    coordinator: EcoFlowDeviceCoordinator,
    entry: MockConfigEntry,
) -> set[str]:
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        coordinator.device_sn: coordinator
    }
    entities: list[Any] = []
    await switch_setup(hass, entry, add_entities_collector(entities))
    return {e._definition.key for e in entities}


def _switch(coordinator: EcoFlowDeviceCoordinator) -> EcoFlowSwitch:
    from custom_components.ecoflow_energy.const import STREAMFEEDIN_SWITCHES

    defn = next(d for d in STREAMFEEDIN_SWITCHES if d.key == FEED_IN_KEY)
    entity = EcoFlowSwitch(coordinator, defn)
    entity.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
    entity.entity_id = f"switch.{FEED_IN_KEY}"
    return entity


class TestSetupGate:
    """The switch exists for a BK61 on developer keys and nowhere else."""

    async def test_bk61_standard_creates_the_switch(self, hass: HomeAssistant) -> None:
        coordinator, entry = _coordinator(hass, "BK61", enhanced=False)
        assert FEED_IN_KEY in await _created_switch_keys(hass, coordinator, entry)

    async def test_bk61_enhanced_does_not(self, hass: HomeAssistant) -> None:
        coordinator, entry = _coordinator(hass, "BK61", enhanced=True)
        assert FEED_IN_KEY not in await _created_switch_keys(hass, coordinator, entry)

    @pytest.mark.parametrize("prefix", ["BK31", "BK11"])
    async def test_other_stream_models_standard_do_not(
        self, hass: HomeAssistant, prefix: str
    ) -> None:
        coordinator, entry = _coordinator(hass, prefix, enhanced=False)
        assert FEED_IN_KEY not in await _created_switch_keys(hass, coordinator, entry)

    async def test_ac5000_standard_does_not(self, hass: HomeAssistant) -> None:
        coordinator, entry = _coordinator(
            hass, "ES21", enhanced=False, device_type=DEVICE_TYPE_STREAM_AC5000
        )
        assert FEED_IN_KEY not in await _created_switch_keys(hass, coordinator, entry)

    async def test_bk31_enhanced_keeps_outlets_and_gets_no_feed_in(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, entry = _coordinator(hass, "BK31", enhanced=True)
        keys = await _created_switch_keys(hass, coordinator, entry)
        assert {"ac_outlet_1_switch", "ac_outlet_2_switch"} <= keys
        assert FEED_IN_KEY not in keys


class TestWrite:
    async def test_turn_on_sends_the_on_body(self, hass: HomeAssistant) -> None:
        coordinator, _ = _coordinator(hass, data={STATE_KEY: False})
        coordinator._http_client = MagicMock()
        coordinator._http_client.set_quota = AsyncMock(return_value={"code": "0"})
        entity = _switch(coordinator)

        await entity.async_turn_on()

        coordinator._http_client.set_quota.assert_awaited_once_with(
            build_feed_in_control_command(True)
        )
        assert coordinator._http_client.set_quota.call_args[0][0]["params"] == {
            "cfgFeedGridMode": 2
        }
        assert entity.is_on is True

    async def test_turn_off_sends_the_off_body(self, hass: HomeAssistant) -> None:
        coordinator, _ = _coordinator(hass, data={STATE_KEY: True})
        coordinator._http_client = MagicMock()
        coordinator._http_client.set_quota = AsyncMock(return_value={"code": "0"})
        entity = _switch(coordinator)

        await entity.async_turn_off()

        assert coordinator._http_client.set_quota.call_args[0][0]["params"] == {
            "cfgFeedGridMode": 1
        }
        assert entity.is_on is False

    async def test_failed_write_raises_and_leaves_state_alone(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _ = _coordinator(hass, data={STATE_KEY: False})
        coordinator._http_client = MagicMock()
        coordinator._http_client.set_quota = AsyncMock(return_value=None)
        entity = _switch(coordinator)

        with pytest.raises(HomeAssistantError):
            await entity.async_turn_on()

        # No optimistic lock: the device never got the command.
        assert entity._optimistic_lock_until == 0.0
        assert entity.is_on is False

    async def test_app_login_is_refused_without_a_request(
        self, hass: HomeAssistant
    ) -> None:
        """Second line behind the setup filter: no HTTP write on an app login."""
        coordinator, _ = _coordinator(hass, enhanced=True)
        coordinator._http_client = MagicMock()
        coordinator._http_client.set_quota = AsyncMock(return_value={"code": "0"})

        assert await coordinator.async_set_stream_feed_in_control(True) is False
        coordinator._http_client.set_quota.assert_not_called()


class TestReadBack:
    @pytest.mark.parametrize(
        ("reported", "expected"), [(True, True), (False, False), (None, None)]
    )
    async def test_is_on_follows_the_reported_mode(
        self, hass: HomeAssistant, reported: bool | None, expected: bool | None
    ) -> None:
        coordinator, _ = _coordinator(hass, data={STATE_KEY: reported})
        assert _switch(coordinator).is_on is expected
