"""Entity gating for the Ocean 2's grid phase B/C voltage (review finding).

The Ocean 2 Plus (`RE41`) is single-phase. Its telemetry frame carries a
per-phase block with one entry (see the parser's `_grid_phases()`), so
`grid_phase_b_voltage_v` and `grid_phase_c_voltage_v` never report on that
unit. Both were declared without `accessory=True`, which `sensor.py` only
checks conditionally (`reading_reported()`) - everything else is created
unconditionally at setup - so a three-phase-only definition stayed
permanently `unavailable` for every `RE41` owner. Same shape as the higher
Stream PV strings in test_stream_pv_string_gating.py, and the same reason to
test it at platform setup level: Home Assistant keeps an entity in the
registry after a later release stops creating it, so a wrongly created entity
is permanent for that owner.
"""

from __future__ import annotations

from typing import Any

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
    DEVICE_TYPE_OCEAN2,
    DOMAIN,
    MODE_ENHANCED,
    OCEAN2_SENSORS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

from .conftest import add_entities_collector

# The RE41 Ocean 2 Plus, the single-phase unit the finding is about.
OCEAN2_DEVICE: dict[str, Any] = {
    "sn": "RE41TEST00000001",
    "name": "Ocean 2 Plus",
    "product_name": "Ocean 2 Plus",
    "device_type": DEVICE_TYPE_OCEAN2,
    "online": 1,
}

GRID_PHASE_BC_KEYS = {"grid_phase_b_voltage_v", "grid_phase_c_voltage_v"}

# What a single-phase RE41 reports: phase A only, plus a baseline reading so
# the report is not entirely about the phase block.
SINGLE_PHASE_REPORT: dict[str, Any] = {
    "soc_pct": 62.0,
    "grid_phase_a_voltage_v": 231.4,
}

# What a three-phase RE11/RE17 reports: all three phases.
THREE_PHASE_REPORT: dict[str, Any] = {
    "soc_pct": 58.0,
    "grid_phase_a_voltage_v": 231.4,
    "grid_phase_b_voltage_v": 230.9,
    "grid_phase_c_voltage_v": 232.1,
}


def _entry() -> MockConfigEntry:
    """Build an account sign-in entry for one Ocean 2 (Enhanced Mode only)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [OCEAN2_DEVICE],
        },
        unique_id="test@example.com",
    )


async def _setup(
    hass: HomeAssistant, device_data: dict[str, Any] | None = None
) -> tuple[EcoFlowDeviceCoordinator, list[Any]]:
    """Run the sensor platform setup and return coordinator plus entities."""
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, OCEAN2_DEVICE)
    for key, value in (device_data or {}).items():
        coordinator.set_device_value(key, value)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        OCEAN2_DEVICE["sn"]: coordinator
    }

    created: list[Any] = []
    await sensor_setup(hass, entry, add_entities_collector(created))
    return coordinator, created


def _keys(entities: list[Any]) -> set[str]:
    return {
        entity._definition.key for entity in entities if hasattr(entity, "_definition")
    }


class TestDefinitions:
    def test_phase_b_and_c_are_gated(self) -> None:
        gated = {sensor.key for sensor in OCEAN2_SENSORS if sensor.accessory}

        assert gated >= GRID_PHASE_BC_KEYS

    def test_phase_a_is_not_gated(self) -> None:
        """Every Ocean 2 variant, single-phase or three-phase, reports phase
        A - gating it would only delay it by an update."""
        gated = {sensor.key for sensor in OCEAN2_SENSORS if sensor.accessory}

        assert "grid_phase_a_voltage_v" not in gated


class TestGating:
    async def test_single_phase_report_creates_no_phase_b_c_entities(
        self, hass: HomeAssistant
    ) -> None:
        _, created = await _setup(hass, SINGLE_PHASE_REPORT)

        assert not _keys(created) & GRID_PHASE_BC_KEYS

    async def test_the_reported_phase_a_is_unaffected(
        self, hass: HomeAssistant
    ) -> None:
        _, created = await _setup(hass, SINGLE_PHASE_REPORT)

        assert "grid_phase_a_voltage_v" in _keys(created)

    async def test_a_three_phase_report_creates_all_three(
        self, hass: HomeAssistant
    ) -> None:
        _, created = await _setup(hass, THREE_PHASE_REPORT)

        assert _keys(created) & GRID_PHASE_BC_KEYS == GRID_PHASE_BC_KEYS
