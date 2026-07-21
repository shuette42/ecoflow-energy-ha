"""End-to-end integration test for the Delta 3 Max Plus (Standard mode).

Sets up a full Home Assistant config entry for a Delta 3 device, feeds a
realistic HTTP quota response through the coordinator, and asserts that the
expected entities exist with the expected states, that the device reports as
available, and that the setup produces no WARNING/ERROR log records for the
integration.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    CONF_ACCESS_KEY,
    CONF_DEVICES,
    CONF_MODE,
    CONF_SECRET_KEY,
    DOMAIN,
    MODE_STANDARD,
)

from .conftest import MOCK_DELTA3_DEVICE, MOCK_MQTT_CREDENTIALS

# Realistic GET /quota/all response for a Delta 3 Max Plus. Values are
# plausible for a unit charging from AC while feeding a 12V load.
DELTA3_QUOTA_FIXTURE: dict = {
    "cmsBattSoc": 82.0,
    "bmsBattSoc": 81.4,
    "cmsChgRemTime": 96,
    "cmsDsgRemTime": 4200,
    "powInSumW": 640.3,
    "powOutSumW": 48.7,
    "powGetAcIn": 638.0,
    "powGetPv": 0.0,
    "powGetPv2": 0.0,
    "powGet12v": 47.6,
    "powGetTypec1": 0.0,
    "powGetTypec2": 0.0,
    "powGetTypec3": 0.0,
    "powGetQcusb1": 0.0,
    "powGetQcusb2": 0.0,
    "cmsMaxChgSoc": 100,
    "cmsMinDsgSoc": 5,
    "backupReverseSoc": 20,
    "xboostEn": 1,
    "enBeep": 0,
    "energyBackupEn": 1,
    "bypassOutDisable": 0,
    # Charging (2) with AC not flowing out (4), 12V flowing (0).
    "cmsChgDsgState": 2,
    "flowInfoAcOut": 4,
    "flowInfoAc2Out": 4,
    "flowInfo12v": 0,
    "powGetAcOutList": {"powGetAcOutItem": [0, 0, 0]},
}


def _delta3_config_entry() -> MockConfigEntry:
    """Standard-mode config entry carrying a single Delta 3 device."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_ACCESS_KEY: "test_ak",
            CONF_SECRET_KEY: "test_sk",
            CONF_MODE: MODE_STANDARD,
            CONF_DEVICES: [MOCK_DELTA3_DEVICE],
        },
        unique_id="test_ak",
    )


class TestDelta3EndToEnd:
    async def test_delta3_entities_populated_from_quota(
        self,
        hass: HomeAssistant,
        mock_mqtt_client,
        caplog,
    ) -> None:
        """Full setup of a Delta 3 device yields populated, available entities."""
        caplog.set_level(logging.WARNING, logger="custom_components.ecoflow_energy")

        entry = _delta3_config_entry()
        entry.add_to_hass(hass)

        with (
            patch(
                "custom_components.ecoflow_energy.coordinator.IoTApiClient",
            ) as iot_cls,
            patch(
                "custom_components.ecoflow_energy.coordinator.EcoFlowHTTPQuota",
            ) as http_cls,
        ):
            iot = iot_cls.return_value
            iot.get_mqtt_credentials = AsyncMock(return_value=MOCK_MQTT_CREDENTIALS)
            iot.get_device_list = AsyncMock(return_value=[MOCK_DELTA3_DEVICE])
            iot.refresh_credentials = AsyncMock(return_value=MOCK_MQTT_CREDENTIALS)

            http = http_cls.return_value
            http.get_quota_all = AsyncMock(return_value=dict(DELTA3_QUOTA_FIXTURE))
            http.last_error_code = None

            result = await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        assert result is True

        coordinators = hass.data[DOMAIN][entry.entry_id]
        sn = MOCK_DELTA3_DEVICE["sn"]
        assert sn in coordinators
        coordinator = coordinators[sn]
        assert coordinator.device_available is True

        registry = er.async_get(hass)

        def state_for(platform: str, key: str) -> str:
            entity_id = registry.async_get_entity_id(
                platform, DOMAIN, f"{sn}_{key}"
            )
            assert entity_id is not None, f"missing entity {platform} {key}"
            state = hass.states.get(entity_id)
            assert state is not None, f"no state for {entity_id}"
            return state.state

        # SoC sensor: 82.0 -> clean int 82.
        assert state_for("sensor", "cms_batt_soc") == "82"

        # Power sensor: input total 640.3 -> 640.
        assert state_for("sensor", "pow_in_sum_w") == "640"

        # 12V power sensor: 47.6 -> 48.
        assert state_for("sensor", "dc_12v_out_w") == "48"

        # Charge/discharge enum: raw 2 -> translated option key "charging".
        assert state_for("sensor", "chg_dsg_state") == "charging"

        # Flow binary sensors: 12V flowing (value 0 != 4) -> on;
        # AC output not flowing (value 4) -> off.
        assert state_for("binary_sensor", "dc_12v_out_flow") == "on"
        assert state_for("binary_sensor", "ac_out_flow") == "off"

        # None of the spot-checked entities should be unavailable.
        for platform, key in (
            ("sensor", "cms_batt_soc"),
            ("sensor", "pow_in_sum_w"),
            ("sensor", "chg_dsg_state"),
            ("binary_sensor", "dc_12v_out_flow"),
        ):
            assert state_for(platform, key) not in ("unavailable", "unknown")

        # Zero-noise logging: no WARNING/ERROR from the integration during setup.
        integration_problems = [
            r
            for r in caplog.records
            if r.name.startswith("custom_components.ecoflow_energy")
            and r.levelno >= logging.WARNING
        ]
        assert not integration_problems, (
            f"unexpected WARNING/ERROR logs: "
            f"{[r.getMessage() for r in integration_problems]}"
        )
