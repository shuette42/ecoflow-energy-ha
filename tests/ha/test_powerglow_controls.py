"""Entities for the PowerGlow heating rod controls (#7, PLAN-190 phase 3b).

The coordinator side (range checks, the wait for the rod's own 212/8 report)
lives in `test_powerglow_coordinator.py`. These tests cover what the platforms
add on top: when the three controls exist, which range the power number offers,
and which coordinator call each control makes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SECRET_KEY,
    CONF_USER_ID,
    DEVICE_TYPE_POWEROCEAN,
    DOMAIN,
    MODE_ENHANCED,
    MODE_STANDARD,
    POWEROCEAN_NUMBERS,
    POWEROCEAN_SELECTS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.energy_stream import (
    POWERGLOW_TARGET_TEMP_C_MAX,
    POWERGLOW_TARGET_TEMP_C_MIN,
    powerglow_max_power_w,
)
from custom_components.ecoflow_energy.number import EcoFlowNumber
from custom_components.ecoflow_energy.number import async_setup_entry as number_setup
from custom_components.ecoflow_energy.select import EcoFlowSelect
from custom_components.ecoflow_energy.select import async_setup_entry as select_setup

from .conftest import add_entities_collector

DEVICE: dict[str, Any] = {
    "sn": "HJ31TEST00000001",
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}

POWER_KEY = "heating_rod_target_power_w"
TEMP_KEY = "heating_rod_target_temp_c"
MODE_KEY = "heating_rod_operating_mode"
ROD_KEYS = {POWER_KEY, TEMP_KEY, MODE_KEY}

# What a rod's 212/8 report puts into the store (`heating_rod_sn` is the
# rod's own 16-character serial, not the PowerOcean's).
REPORT: dict[str, Any] = {
    POWER_KEY: 3500.0,
    TEMP_KEY: 60.0,
    MODE_KEY: "manual",
    "heating_rod_sn": "HF33TEST00000001",
}


def _entry(mode: str = MODE_ENHANCED) -> MockConfigEntry:
    """Build a config entry for one PowerOcean in either mode."""
    if mode == MODE_ENHANCED:
        data: dict[str, Any] = {
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [DEVICE],
        }
    else:
        data = {
            CONF_ACCESS_KEY: "test_ak",
            CONF_SECRET_KEY: "test_sk",
            CONF_MODE: MODE_STANDARD,
            CONF_DEVICES: [DEVICE],
        }
    return MockConfigEntry(
        domain=DOMAIN, title="EcoFlow Energy", data=data, unique_id="test@example.com"
    )


async def _setup(
    hass: HomeAssistant,
    device_data: dict[str, Any] | None = None,
    mode: str = MODE_ENHANCED,
) -> tuple[EcoFlowDeviceCoordinator, list[Any], list[Any]]:
    """Run the number and select platform setup, return coordinator and entities."""
    entry = _entry(mode)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, DEVICE)
    for key, value in (device_data or {}).items():
        coordinator.set_device_value(key, value)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DEVICE["sn"]: coordinator}

    numbers: list[Any] = []
    selects: list[Any] = []
    await number_setup(hass, entry, add_entities_collector(numbers))
    await select_setup(hass, entry, add_entities_collector(selects))
    if device_data:
        # The entities read the published data, not the store behind it.
        coordinator.async_set_updated_data(dict(coordinator.device_data))
    return coordinator, numbers, selects


def _rod_keys(entities: list[Any]) -> set[str]:
    return {e._definition.key for e in entities if e._definition.key in ROD_KEYS}


def _by_key(entities: list[Any], key: str) -> Any:
    return next(e for e in entities if e._definition.key == key)


class TestCreation:
    async def test_absent_before_a_report_and_present_after_one(
        self, hass: HomeAssistant
    ) -> None:
        """The rod is an optional accessory: no 212/8 report, no controls."""
        coordinator, numbers, selects = await _setup(hass)
        assert _rod_keys(numbers) == set()
        assert _rod_keys(selects) == set()

        coordinator.async_set_updated_data(dict(REPORT))
        await hass.async_block_till_done()

        assert _rod_keys(numbers) == {POWER_KEY, TEMP_KEY}
        assert _rod_keys(selects) == {MODE_KEY}
        power = _by_key(numbers, POWER_KEY)
        assert power.unique_id == f"{DEVICE['sn']}_{POWER_KEY}"
        assert power.entity_category is EntityCategory.CONFIG
        assert _by_key(numbers, TEMP_KEY).entity_category is EntityCategory.CONFIG
        mode = _by_key(selects, MODE_KEY)
        assert mode.unique_id == f"{DEVICE['sn']}_{MODE_KEY}"
        assert mode.entity_category is EntityCategory.CONFIG
        assert mode.options == ["off", "solar", "manual"]

    async def test_standard_mode_never_creates_them(self, hass: HomeAssistant) -> None:
        """The write leaves over the app channel, so the controls exist in
        Enhanced Mode only, however much the store holds."""
        _, numbers, selects = await _setup(hass, dict(REPORT), mode=MODE_STANDARD)

        assert _rod_keys(numbers) == set()
        assert _rod_keys(selects) == set()

    def test_declared_ranges_follow_the_coordinator_constants(self) -> None:
        """The declared power ceiling is the highest rod's, and the
        temperature range is the one the coordinator enforces."""
        power = next(d for d in POWEROCEAN_NUMBERS if d.key == POWER_KEY)
        temp = next(d for d in POWEROCEAN_NUMBERS if d.key == TEMP_KEY)
        mode = next(d for d in POWEROCEAN_SELECTS if d.key == MODE_KEY)

        assert (power.min_value, power.max_value) == (0, powerglow_max_power_w("HF33"))
        assert (temp.min_value, temp.max_value) == (
            POWERGLOW_TARGET_TEMP_C_MIN,
            POWERGLOW_TARGET_TEMP_C_MAX,
        )
        assert mode.options == ("off", "solar", "manual")
        assert mode.value_map is None


class TestPowerCeiling:
    @pytest.mark.parametrize(
        ("rod_sn", "ceiling"),
        [
            ("HF32TEST00000001", 6000),
            ("HF33TEST00000001", 9000),
            ("HF30TEST00000001", 3500),
            (None, 9000),
            ("HF32", 9000),
        ],
    )
    async def test_max_follows_the_rod_serial_prefix(
        self, hass: HomeAssistant, rod_sn: str | None, ceiling: int
    ) -> None:
        """Prefix decides the ceiling; no serial, or one that is not 16
        characters, keeps the declared range."""
        report = {k: v for k, v in REPORT.items() if k != "heating_rod_sn"}
        if rod_sn is not None:
            report["heating_rod_sn"] = rod_sn
        coordinator, numbers, _ = await _setup(hass, report)
        power = _by_key(numbers, POWER_KEY)
        temp = _by_key(numbers, TEMP_KEY)

        assert power.native_min_value == 0
        assert power.native_max_value == ceiling
        assert (temp.native_min_value, temp.native_max_value) == (10, 80)


class TestNumberWrites:
    async def _numbers(
        self, hass: HomeAssistant
    ) -> tuple[EcoFlowDeviceCoordinator, AsyncMock, EcoFlowNumber, EcoFlowNumber]:
        coordinator, numbers, _ = await _setup(hass, dict(REPORT))
        write = AsyncMock()
        coordinator.async_set_powerglow_value = write  # type: ignore[method-assign]
        power = _by_key(numbers, POWER_KEY)
        temp = _by_key(numbers, TEMP_KEY)
        power.entity_id = "number.powerocean_heating_rod_target_power"
        temp.entity_id = "number.powerocean_heating_rod_target_temperature"
        return coordinator, write, power, temp

    async def test_each_number_calls_the_coordinator_with_its_setting(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, write, power, temp = await self._numbers(hass)

        await power.async_set_native_value(2000.0)
        write.assert_awaited_once_with("target_power", 2000)
        write.reset_mock()
        await temp.async_set_native_value(55.0)
        write.assert_awaited_once_with("target_temp", 55)

        # The coordinator confirms on the rod's own report; nothing is applied
        # on the way (no optimistic value in the store).
        assert coordinator.device_data[POWER_KEY] == 3500.0
        assert coordinator.device_data[TEMP_KEY] == 60.0

    async def test_a_fractional_value_is_refused_without_a_call(
        self, hass: HomeAssistant
    ) -> None:
        _, write, power, temp = await self._numbers(hass)

        for entity in (power, temp):
            with pytest.raises(HomeAssistantError) as err:
                await entity.async_set_native_value(50.5)
            assert err.value.translation_key == "set_value_rejected"
        write.assert_not_awaited()


class TestModeSelect:
    async def test_current_option_follows_the_state_key(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _, selects = await _setup(hass, dict(REPORT))
        mode = _by_key(selects, MODE_KEY)
        assert mode.current_option == "manual"

        for state, shown in (("solar", "solar"), ("off", "off"), ("weird", None)):
            coordinator.async_set_updated_data({**REPORT, MODE_KEY: state})
            assert mode.current_option == shown

        coordinator.async_set_updated_data(
            {k: v for k, v in REPORT.items() if k != MODE_KEY}
        )
        assert mode.current_option is None

    async def test_selecting_an_option_calls_the_coordinator(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, _, selects = await _setup(hass, dict(REPORT))
        write = AsyncMock()
        coordinator.async_set_powerglow_operating_mode = write  # type: ignore[method-assign]
        mode = _by_key(selects, MODE_KEY)
        assert isinstance(mode, EcoFlowSelect)

        await mode.async_select_option("solar")

        write.assert_awaited_once_with("solar")
        # Confirmed by the rod's own report, not applied here.
        assert coordinator.device_data[MODE_KEY] == "manual"
        assert mode.current_option == "manual"
