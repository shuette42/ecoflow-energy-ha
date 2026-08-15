"""Entity gating for the PowerGlow heating rod (#7).

The heating rod is an optional accessory of a PowerOcean, not part of it.
Its four readings were created for every PowerOcean regardless, so owners
without the accessory got four entities that can never hold a value. Home
Assistant keeps an entity in the registry after a later release stops
creating it, which makes a wrongly created entity permanent for that owner -
hence the platform-level tests here.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_POWEROCEAN,
    DOMAIN,
    MODE_ENHANCED,
    POWEROCEAN_SENSORS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

POWEROCEAN_DEVICE: dict[str, Any] = {
    "sn": "HJ31TEST00000001",
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}

HEATING_ROD_KEYS = {
    "heating_rod_power_w",
    "heating_rod_water_temp_c",
    "heating_rod_target_power_w",
    "heating_rod_target_temp_c",
}

# The PowerPulse wallbox is the second accessory to use the same gate
# (PLAN-079). It is listed here so the assertions below stay a statement about
# which readings are optional, rather than a count that any new definition
# silently changes.
WALLBOX_KEYS = {
    "ev_charge_power_w",
    "ev_session_energy_wh",
    "ev_session_duration_s",
    "ev_charge_status",
    "ev_vehicle_id",
}

ACCESSORY_KEYS = HEATING_ROD_KEYS | WALLBOX_KEYS


def _entry(devices: list[dict[str, Any]] | None = None) -> MockConfigEntry:
    """Build an Enhanced-mode entry for one or more PowerOceans."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: devices or [POWEROCEAN_DEVICE],
        },
        unique_id="test@example.com",
    )


async def _setup(
    hass: HomeAssistant, device_data: dict[str, Any] | None = None
) -> tuple[EcoFlowDeviceCoordinator, list[Any]]:
    """Run the sensor platform setup and return coordinator plus entities."""
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN_DEVICE)
    for key, value in (device_data or {}).items():
        coordinator.set_device_value(key, value)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        POWEROCEAN_DEVICE["sn"]: coordinator
    }

    created: list[Any] = []
    await sensor_setup(hass, entry, created.extend)
    return coordinator, created


def _keys(entities: list[Any]) -> set[str]:
    return {
        entity._definition.key for entity in entities if hasattr(entity, "_definition")
    }


class TestDefinitions:
    def test_all_four_heating_rod_defs_are_marked_accessory(self) -> None:
        marked = {
            sensor.key for sensor in POWEROCEAN_SENSORS if sensor.accessory
        }
        assert HEATING_ROD_KEYS <= marked

    def test_no_other_powerocean_sensor_is_gated(self) -> None:
        """A gated definition without a matching parser key would never be
        created at all, so the flag stays limited to the accessories."""
        gated = {sensor.key for sensor in POWEROCEAN_SENSORS if sensor.accessory}
        assert gated == ACCESSORY_KEYS


class TestGating:
    async def test_no_heating_rod_entities_without_the_accessory(
        self, hass: HomeAssistant
    ) -> None:
        _, created = await _setup(hass)

        assert not _keys(created) & HEATING_ROD_KEYS

    async def test_regular_sensors_are_unaffected(self, hass: HomeAssistant) -> None:
        _, created = await _setup(hass)
        keys = _keys(created)

        assert "soc_pct" in keys
        assert "mppt_pv1_power_w" in keys

    async def test_reported_accessory_creates_its_entities(
        self, hass: HomeAssistant
    ) -> None:
        _, created = await _setup(
            hass,
            {
                "heating_rod_power_w": 0.0,
                "heating_rod_water_temp_c": 58.0,
                "heating_rod_target_power_w": 3500.0,
                "heating_rod_target_temp_c": 60.0,
            },
        )

        assert _keys(created) & HEATING_ROD_KEYS == HEATING_ROD_KEYS

    async def test_only_reported_readings_are_created(
        self, hass: HomeAssistant
    ) -> None:
        """Gating is per reading, so a partial report creates a partial set
        and the rest follows when it arrives."""
        _, created = await _setup(hass, {"heating_rod_power_w": 1750.0})

        assert _keys(created) & HEATING_ROD_KEYS == {"heating_rod_power_w"}

    async def test_a_leftover_registry_entry_does_not_resurrect_the_entity(
        self, hass: HomeAssistant
    ) -> None:
        """Every owner who ran beta.1 to beta.7 already has the four entries.
        Treating an existing entry as proof of the accessory would leave the
        bug in place for exactly the people it affects."""
        registry = er.async_get(hass)
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{POWEROCEAN_DEVICE['sn']}_heating_rod_power_w",
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        )

        _, created = await _setup(hass)

        assert not _keys(created) & HEATING_ROD_KEYS


class TestLateAccessory:
    async def test_entities_appear_on_the_first_report(
        self, hass: HomeAssistant
    ) -> None:
        """An accessory can be attached while HA runs, and the first quota
        poll can land after setup."""
        coordinator, created = await _setup(hass)
        assert not _keys(created) & HEATING_ROD_KEYS

        coordinator.async_set_updated_data({"heating_rod_water_temp_c": 58.0})
        await hass.async_block_till_done()

        assert _keys(created) & HEATING_ROD_KEYS == {"heating_rod_water_temp_c"}

    async def test_no_duplicate_on_further_updates(
        self, hass: HomeAssistant
    ) -> None:
        coordinator, created = await _setup(hass)

        coordinator.set_device_value("heating_rod_water_temp_c", 58.0)
        coordinator.async_set_updated_data(dict(coordinator.device_data))
        await hass.async_block_till_done()
        coordinator.set_device_value("heating_rod_water_temp_c", 59.0)
        coordinator.async_set_updated_data(dict(coordinator.device_data))
        await hass.async_block_till_done()

        added = [
            entity
            for entity in created
            if getattr(entity, "_definition", None)
            and entity._definition.key == "heating_rod_water_temp_c"
        ]
        assert len(added) == 1

    async def test_two_powerocean_devices_stay_independent(
        self, hass: HomeAssistant
    ) -> None:
        """One system reporting a heating rod must not create entities on
        the other."""
        second = dict(POWEROCEAN_DEVICE, sn="HJ31TEST00000002")
        entry = _entry(devices=[POWEROCEAN_DEVICE, second])
        entry.add_to_hass(hass)
        with_rod = EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN_DEVICE)
        without_rod = EcoFlowDeviceCoordinator(hass, entry, second)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            POWEROCEAN_DEVICE["sn"]: with_rod,
            second["sn"]: without_rod,
        }
        created: list[Any] = []
        await sensor_setup(hass, entry, created.extend)

        with_rod.set_device_value("heating_rod_power_w", 1750.0)
        with_rod.async_set_updated_data(dict(with_rod.device_data))
        await hass.async_block_till_done()

        by_device = {
            (entity.coordinator.device_sn, entity._definition.key)
            for entity in created
            if hasattr(entity, "_definition")
        }
        assert (POWEROCEAN_DEVICE["sn"], "heating_rod_power_w") in by_device
        assert not [
            key for sn, key in by_device if sn == second["sn"] and key in HEATING_ROD_KEYS
        ]


class TestUnload:
    async def test_the_listener_is_tied_to_the_config_entry(
        self, hass: HomeAssistant
    ) -> None:
        """Platforms unload before the coordinators shut down. A listener
        outliving its platform would call async_add_entities on a platform
        that has already been reset."""
        entry = _entry()
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN_DEVICE)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            POWEROCEAN_DEVICE["sn"]: coordinator
        }

        calls: list[Any] = []
        await sensor_setup(hass, entry, calls.append)
        assert coordinator._listeners
        calls.clear()

        await entry._async_process_on_unload(hass)
        coordinator.set_device_value("heating_rod_power_w", 1750.0)
        coordinator.async_set_updated_data(dict(coordinator.device_data))
        await hass.async_block_till_done()

        assert not calls


class TestStaleEntries:
    async def test_disabled_leftover_is_removed_once_data_arrives(
        self, hass: HomeAssistant
    ) -> None:
        """Skipping creation alone leaves the entry in the device's entity
        list, which is what the reporter sees."""
        registry = er.async_get(hass)
        stale = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{POWEROCEAN_DEVICE['sn']}_heating_rod_power_w",
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        )

        coordinator, _ = await _setup(hass)
        assert registry.async_get(stale.entity_id) is not None

        coordinator.set_device_value("soc_pct", 61.0)
        coordinator.async_set_updated_data(dict(coordinator.device_data))
        await hass.async_block_till_done()

        assert registry.async_get(stale.entity_id) is None

    async def test_an_entry_the_user_disabled_is_kept(
        self, hass: HomeAssistant
    ) -> None:
        """Switching a reading off does not erase what it recorded while it
        was on, so a user-disabled entry is not a leftover."""
        registry = er.async_get(hass)
        by_user = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{POWEROCEAN_DEVICE['sn']}_heating_rod_power_w",
            disabled_by=er.RegistryEntryDisabler.USER,
        )

        coordinator, _ = await _setup(hass)
        coordinator.set_device_value("soc_pct", 61.0)
        coordinator.async_set_updated_data(dict(coordinator.device_data))
        await hass.async_block_till_done()

        assert registry.async_get(by_user.entity_id) is not None

    async def test_an_enabled_entry_is_kept(self, hass: HomeAssistant) -> None:
        """An owner who enabled the reading has recorded history, and an
        accessory can fall silent without ceasing to exist."""
        registry = er.async_get(hass)
        enabled = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{POWEROCEAN_DEVICE['sn']}_heating_rod_power_w",
        )

        coordinator, _ = await _setup(hass)
        coordinator.set_device_value("soc_pct", 61.0)
        coordinator.async_set_updated_data(dict(coordinator.device_data))
        await hass.async_block_till_done()

        assert registry.async_get(enabled.entity_id) is not None

    async def test_nothing_is_removed_before_data_arrives(
        self, hass: HomeAssistant
    ) -> None:
        """Before the first update, "not reported" and "not connected yet"
        look the same."""
        registry = er.async_get(hass)
        stale = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{POWEROCEAN_DEVICE['sn']}_heating_rod_power_w",
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        )

        await _setup(hass)

        assert registry.async_get(stale.entity_id) is not None

    async def test_a_reporting_accessory_keeps_its_entry(
        self, hass: HomeAssistant
    ) -> None:
        registry = er.async_get(hass)
        entry = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{POWEROCEAN_DEVICE['sn']}_heating_rod_power_w",
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        )

        coordinator, _ = await _setup(hass, {"heating_rod_power_w": 1750.0})
        coordinator.async_set_updated_data(dict(coordinator.device_data))
        await hass.async_block_till_done()

        assert registry.async_get(entry.entity_id) is not None


class TestEnhancedModePath:
    async def test_protobuf_data_without_the_accessory_creates_nothing(
        self, hass: HomeAssistant
    ) -> None:
        """The heating rod is read from the polled API quota only, and that
        quota is never polled with account sign-in. Pinning this in code
        keeps the gap visible until PLAN-056 settles the data path."""
        coordinator, created = await _setup(hass)
        coordinator.async_set_updated_data(
            {"soc_pct": 61.0, "batt_w": -220.0, "mppt_power_w": 1400.0}
        )
        await hass.async_block_till_done()

        assert not _keys(created) & HEATING_ROD_KEYS
