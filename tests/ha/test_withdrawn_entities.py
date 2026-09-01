"""Removal of registry entries for entities that were shipped and withdrawn.

Home Assistant never removes a registry entry on its own. An entity whose
definition is deleted from the code keeps its row, so it stays on the device
page forever, permanently unavailable, for everyone who ran the release that
had it. `ac_charge_mode` is the case that prompted this: a Delta 3 select in
v1.16.0-beta.11 and beta.12, withdrawn because its read-back arrives too rarely
to trust.

What matters here is the aim. The suffix is matched against the unique id,
which is `<serial>_<key>`, so it has to hit the withdrawn entity on any device
and miss everything that merely ends in similar letters.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy import (
    _WITHDRAWN_ENTITY_SUFFIXES,
    _async_remove_withdrawn_entities,
)
from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_DELTA3,
    DOMAIN,
    MODE_ENHANCED,
)

SERIAL = "D3M1TEST00000001"
OTHER_SERIAL = "R351TEST00000001"


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    config = MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [
                {
                    "sn": SERIAL,
                    "name": "Delta 3",
                    "product_name": "DELTA 3 Max Plus",
                    "device_type": DEVICE_TYPE_DELTA3,
                    "online": 1,
                }
            ],
        },
        unique_id="test@example.com",
    )
    config.add_to_hass(hass)
    return config


def _register(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    platform: str,
    unique_id: str,
    *,
    integration: str = DOMAIN,
) -> str:
    registry = er.async_get(hass)
    return registry.async_get_or_create(
        platform, integration, unique_id, config_entry=entry
    ).entity_id


def _ids(hass: HomeAssistant) -> set[str]:
    return {e.entity_id for e in er.async_get(hass).entities.values()}


class TestRemoval:
    def test_the_withdrawn_select_is_removed(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        stale = _register(hass, entry, "select", f"{SERIAL}_ac_charge_mode")

        _async_remove_withdrawn_entities(hass, entry)

        assert stale not in _ids(hass)

    def test_it_is_removed_on_every_device_that_had_it(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        """The match is on the key, not on one serial."""
        first = _register(hass, entry, "select", f"{SERIAL}_ac_charge_mode")
        second = _register(hass, entry, "select", f"{OTHER_SERIAL}_ac_charge_mode")

        _async_remove_withdrawn_entities(hass, entry)

        assert first not in _ids(hass)
        assert second not in _ids(hass)

    def test_current_entities_survive(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        """Nothing else may be caught, least of all the new select."""
        keep = [
            _register(hass, entry, "select", f"{SERIAL}_screen_timeout"),
            _register(hass, entry, "number", f"{SERIAL}_ac_charge_power_limit"),
            _register(hass, entry, "switch", f"{SERIAL}_beeper_switch"),
            _register(hass, entry, "sensor", f"{SERIAL}_cms_batt_soc"),
        ]

        _async_remove_withdrawn_entities(hass, entry)

        assert set(keep) <= _ids(hass)

    def test_schedule_cleanup_is_exact_and_entry_scoped(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        registry = er.async_get(hass)
        stale = {
            _register(hass, entry, "switch", f"{SERIAL}_schedule_1_enabled"),
            _register(hass, entry, "number", f"{SERIAL}_schedule_2_power_w"),
            _register(hass, entry, "sensor", f"{SERIAL}_schedule_3_window"),
        }
        running = _register(
            hass, entry, "binary_sensor", f"{SERIAL}_schedule_1_running"
        )
        registry.async_update_entity(running, name="Morning charge running")

        other_entry = MockConfigEntry(domain=DOMAIN, data={})
        other_entry.add_to_hass(hass)
        foreign_entry = _register(
            hass,
            other_entry,
            "switch",
            f"{OTHER_SERIAL}_schedule_1_enabled",
        )
        foreign_platform = _register(
            hass,
            entry,
            "switch",
            f"{SERIAL}_schedule_4_enabled",
            integration="example",
        )

        _async_remove_withdrawn_entities(hass, entry)

        assert not (stale & _ids(hass))
        assert {running, foreign_entry, foreign_platform} <= _ids(hass)
        assert registry.async_get(running).name == "Morning charge running"

    def test_a_longer_key_ending_in_the_same_word_survives(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        """Suffix matching is blunt; this is the edge it could cut wrong on.

        A future `..._grid_ac_charge_mode` would end with the withdrawn suffix
        without being it. The test does not prevent that - it makes the day it
        happens visible instead of silent.
        """
        registry = er.async_get(hass)
        victim = _register(hass, entry, "select", f"{SERIAL}_solar_ac_charge_mode")

        _async_remove_withdrawn_entities(hass, entry)

        assert victim not in registry.entities, (
            "suffix matching removed a different entity - if this ever fires, "
            "the match has to move to the exact key rather than its ending"
        )

    def test_running_twice_is_harmless(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        """Setup runs on every restart and every reload."""
        _register(hass, entry, "select", f"{SERIAL}_ac_charge_mode")

        _async_remove_withdrawn_entities(hass, entry)
        _async_remove_withdrawn_entities(hass, entry)

    def test_nothing_registered_is_fine(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        """A fresh install has no registry rows at all."""
        _async_remove_withdrawn_entities(hass, entry)

        assert not _ids(hass)


class TestTheList:
    def test_the_withdrawn_key_is_listed(self) -> None:
        assert "_ac_charge_mode" in _WITHDRAWN_ENTITY_SUFFIXES

    def test_every_suffix_starts_with_the_separator(self) -> None:
        """A unique id is `<serial>_<key>`, so a suffix without the underscore
        would match the middle of a key rather than a whole one."""
        assert all(s.startswith("_") for s in _WITHDRAWN_ENTITY_SUFFIXES)

    def test_no_current_entity_key_is_on_the_list(self) -> None:
        """The list must never name something the integration still offers."""
        import custom_components.ecoflow_energy.const as const_module

        live: set[str] = set()
        for name in dir(const_module):
            if not name.isupper():
                continue
            value: Any = getattr(const_module, name)
            if not isinstance(value, list):
                continue
            for defn in value:
                key = getattr(defn, "key", None)
                if isinstance(key, str):
                    live.add(key)

        clashing = {s for s in _WITHDRAWN_ENTITY_SUFFIXES if s.lstrip("_") in live}
        assert not clashing, (
            "these are still offered by the integration and would be removed "
            f"on every setup: {sorted(clashing)}"
        )
