"""Removal of the PowerOcean-side relay copies of a PowerPulse 2's readings.

Until PLAN-132 a `C376` reported through the PowerOcean it was coupled to, so
its four wallbox readings carried `<powerocean_sn>_<key>`. The wallbox now
reads on its own channel and the `(241, 3)` relay is retired, so those four
registry entries are fed by nothing and would sit on the PowerOcean's page
permanently unavailable - hence they are removed.

The one case that must NOT remove anything is an entry that also holds a
PowerPulse 1 (`AC31`): that older wallbox reports the same four keys through
the same PowerOcean on a different, untouched channel, and a unique id alone
cannot tell its live reading apart from the PowerPulse 2's stale relay copy.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy import _async_remove_relayed_wallbox_entities
from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_POWERPULSE2,
    DOMAIN,
    MODE_ENHANCED,
)

POWEROCEAN_SN = "HJ31TEST00000001"
POWERPULSE2_SN = "C376TEST00000007"
POWERPULSE1_SN = "AC31TEST00000001"

RELAYED_KEYS = (
    "ev_charge_power_w",
    "ev_session_energy_wh",
    "ev_session_duration_s",
    "ev_charge_status",
)

POWEROCEAN_DEVICE: dict[str, Any] = {
    "sn": POWEROCEAN_SN,
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}

POWERPULSE2_DEVICE: dict[str, Any] = {
    "sn": POWERPULSE2_SN,
    "name": "PowerPulse 2",
    "product_name": "",
    "device_type": DEVICE_TYPE_POWERPULSE2,
    "online": 1,
}

POWERPULSE1_DEVICE: dict[str, Any] = {
    "sn": POWERPULSE1_SN,
    "name": "PowerPulse 1",
    "product_name": "",
    "online": 1,
}


def _entry(hass: HomeAssistant, devices: list[dict[str, Any]]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: devices,
        },
        unique_id="test@example.com",
    )
    entry.add_to_hass(hass)
    return entry


def _register(
    hass: HomeAssistant, entry: MockConfigEntry, platform: str, unique_id: str
) -> str:
    registry = er.async_get(hass)
    return registry.async_get_or_create(
        platform, DOMAIN, unique_id, config_entry=entry
    ).entity_id


def _ids(hass: HomeAssistant) -> set[str]:
    return {e.entity_id for e in er.async_get(hass).entities.values()}


def _register_relayed(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, str]:
    """Register the four PowerOcean-side wallbox entries a pre-migration
    install still carries, keyed by the sensor key they were registered
    under."""
    return {
        key: _register(hass, entry, "sensor", f"{POWEROCEAN_SN}_{key}")
        for key in RELAYED_KEYS
    }


class TestRelayedCopiesAreRemoved:
    def test_the_four_relay_entries_are_removed(self, hass: HomeAssistant) -> None:
        entry = _entry(hass, [POWEROCEAN_DEVICE, POWERPULSE2_DEVICE])
        stale = _register_relayed(hass, entry)
        # Positive control: without this, "not in the registry afterwards"
        # would also be true of a registry the helper never touched.
        assert set(stale.values()) <= _ids(hass)

        _async_remove_relayed_wallbox_entities(hass, entry)

        assert not _ids(hass) & set(stale.values())

    def test_the_vehicle_id_entry_survives(self, hass: HomeAssistant) -> None:
        """`ev_vehicle_id` is not one of the four relayed keys - it belongs
        to the PowerPulse 1 reading, never the PowerPulse 2."""
        entry = _entry(hass, [POWEROCEAN_DEVICE, POWERPULSE2_DEVICE])
        _register_relayed(hass, entry)
        vehicle_id = _register(hass, entry, "sensor", f"{POWEROCEAN_SN}_ev_vehicle_id")

        _async_remove_relayed_wallbox_entities(hass, entry)

        assert vehicle_id in _ids(hass)

    def test_an_unrelated_powerocean_entity_survives(self, hass: HomeAssistant) -> None:
        entry = _entry(hass, [POWEROCEAN_DEVICE, POWERPULSE2_DEVICE])
        _register_relayed(hass, entry)
        solar = _register(hass, entry, "sensor", f"{POWEROCEAN_SN}_solar_w")

        _async_remove_relayed_wallbox_entities(hass, entry)

        assert solar in _ids(hass)


class TestPowerPulse1KeepsTheRelay:
    def test_nothing_is_removed_when_a_powerpulse_1_is_present(
        self, hass: HomeAssistant
    ) -> None:
        """The most important case: a PowerPulse 1 (`AC31`) on the same
        entry reports the same four keys through the same PowerOcean on a
        channel this change does not touch, and a unique id cannot tell its
        live reading apart from the PowerPulse 2's dead relay copy. In doubt,
        nothing is removed."""
        entry = _entry(
            hass, [POWEROCEAN_DEVICE, POWERPULSE2_DEVICE, POWERPULSE1_DEVICE]
        )
        stale = _register_relayed(hass, entry)

        _async_remove_relayed_wallbox_entities(hass, entry)

        assert set(stale.values()) <= _ids(hass)


class TestNoPowerOcean:
    def test_nothing_happens_without_a_powerocean_in_the_entry(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(hass, [POWERPULSE2_DEVICE])
        registered = _register(
            hass, entry, "sensor", f"{POWEROCEAN_SN}_ev_charge_power_w"
        )

        _async_remove_relayed_wallbox_entities(hass, entry)

        assert registered in _ids(hass)


class TestIdempotency:
    def test_running_twice_changes_nothing_further(self, hass: HomeAssistant) -> None:
        entry = _entry(hass, [POWEROCEAN_DEVICE, POWERPULSE2_DEVICE])
        _register_relayed(hass, entry)
        vehicle_id = _register(hass, entry, "sensor", f"{POWEROCEAN_SN}_ev_vehicle_id")

        _async_remove_relayed_wallbox_entities(hass, entry)
        remaining_after_first = _ids(hass)
        _async_remove_relayed_wallbox_entities(hass, entry)

        assert _ids(hass) == remaining_after_first
        assert vehicle_id in _ids(hass)
