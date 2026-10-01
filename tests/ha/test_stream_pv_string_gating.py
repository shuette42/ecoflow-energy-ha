"""Entity gating for the higher Stream PV strings (#139).

Only the larger Streams drive strings 3 and 4. Standard Mode reads them from
the polled quota, Enhanced Mode from fields 996 and 997 of the status frame.
A unit without those inputs, the Stream AC Pro for one, can still send both
fields as an explicit 0.0, so a report that merely contains the key proves
nothing: the four higher-string entities wait for a reading above zero.

Same shape as the heating rod in test_powerglow_gating.py, and the same reason
for testing it at platform setup level: Home Assistant keeps an entity in the
registry after a later release stops creating it, so a wrongly created entity
is permanent for that owner.
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
    DEVICE_TYPE_STREAM,
    DOMAIN,
    MODE_ENHANCED,
    OCEAN2_SENSORS,
    STREAM_SENSORS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

from .conftest import add_entities_collector

# Stream Ultra X, the four-string unit the report comes from.
STREAM_DEVICE: dict[str, Any] = {
    "sn": "BK61TEST00000001",
    "name": "Stream Ultra X",
    "product_name": "Stream Ultra X",
    "device_type": DEVICE_TYPE_STREAM,
    "online": 1,
}

# The strings the protobuf push has no field number for, power and the energy
# counter integrated from it.
HIGHER_STRING_KEYS = {"pv3_w", "pv4_w", "pv3_energy_kwh", "pv4_energy_kwh"}

# The two the push does carry. They are listed so the assertions stay a
# statement about which strings are optional rather than a count.
LOWER_STRING_KEYS = {"pv1_w", "pv2_w", "pv1_energy_kwh", "pv2_energy_kwh"}

# What a Stream on account sign-in has after a push: the lower strings and the
# readings around them, and nothing for the two above.
TWO_STRING_REPORT: dict[str, Any] = {
    "soc_pct": 64.0,
    "pv1_w": 386.0,
    "pv2_w": 339.0,
    "pv1_energy_kwh": 12.4,
    "pv2_energy_kwh": 11.1,
}


def _entry() -> MockConfigEntry:
    """Build an account sign-in entry for one Stream."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [STREAM_DEVICE],
        },
        unique_id="test@example.com",
    )


async def _setup(
    hass: HomeAssistant, device_data: dict[str, Any] | None = None
) -> tuple[EcoFlowDeviceCoordinator, list[Any]]:
    """Run the sensor platform setup and return coordinator plus entities."""
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, STREAM_DEVICE)
    for key, value in (device_data or {}).items():
        coordinator.set_device_value(key, value)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        STREAM_DEVICE["sn"]: coordinator
    }

    created: list[Any] = []
    await sensor_setup(hass, entry, add_entities_collector(created))
    return coordinator, created


def _report(
    coordinator: EcoFlowDeviceCoordinator, extra: dict[str, Any] | None = None
) -> None:
    """Push a two-string report through both stores.

    The stale-entry cleanup keys off the persistent device data, which is what
    the parsers fill, so an update that only sets the coordinator payload
    leaves it looking like a device that has not reported yet.
    """
    for key, value in dict(TWO_STRING_REPORT, **(extra or {})).items():
        coordinator.set_device_value(key, value)
    coordinator.async_set_updated_data(dict(coordinator.device_data))


def _keys(entities: list[Any]) -> set[str]:
    return {
        entity._definition.key for entity in entities if hasattr(entity, "_definition")
    }


class TestDefinitions:
    def test_the_higher_strings_are_gated(self) -> None:
        gated = {sensor.key for sensor in STREAM_SENSORS if sensor.accessory}

        assert gated >= HIGHER_STRING_KEYS

    def test_the_lower_strings_are_not(self) -> None:
        """Both modes fill strings 1 and 2, so gating them would delay them
        by an update and buy nothing."""
        gated = {sensor.key for sensor in STREAM_SENSORS if sensor.accessory}

        assert not gated & LOWER_STRING_KEYS

    def test_no_other_stream_sensor_is_gated(self) -> None:
        gated = {sensor.key for sensor in STREAM_SENSORS if sensor.accessory}

        assert gated == HIGHER_STRING_KEYS

    def test_the_higher_strings_wait_for_a_nonzero_reading(self) -> None:
        """A unit with no such input sends 0.0 for both fields (a Stream AC
        Pro does), so presence alone would give it four dead entities. A real
        string that is dark at setup appears with its first daylight reading,
        and an owner who already has the entity keeps it across restarts."""
        gated = {
            sensor.key for sensor in STREAM_SENSORS if sensor.accessory_needs_nonzero
        }

        assert gated == HIGHER_STRING_KEYS

    def test_the_gate_does_not_change_the_default(self) -> None:
        """The gate decides whether the entity exists, the default whether it
        is switched on. The reason for the default is unrelated to either
        mode."""
        by_key = {sensor.key: sensor for sensor in STREAM_SENSORS}

        for key in HIGHER_STRING_KEYS:
            assert by_key[key].disabled_by_default is True


class TestCategory:
    """Where the string readings are listed on the device page."""

    def test_every_string_power_sits_with_the_sensors(self) -> None:
        """A wired string is a primary reading, so strings 3 and 4 are listed
        next to strings 1 and 2 and not under Diagnostic (#139)."""
        by_key = {sensor.key: sensor for sensor in STREAM_SENSORS}

        for key in ("pv1_w", "pv2_w", "pv3_w", "pv4_w"):
            assert by_key[key].entity_category is None

    def test_every_string_power_sits_with_the_sensors_on_the_ocean_2(self) -> None:
        by_key = {sensor.key: sensor for sensor in OCEAN2_SENSORS}

        for key in ("pv1_w", "pv2_w", "pv3_w", "pv4_w"):
            assert by_key[key].entity_category is None

    def test_the_energy_counters_stay_diagnostic_as_for_strings_1_and_2(self) -> None:
        by_key = {sensor.key: sensor for sensor in STREAM_SENSORS}

        for key in (
            "pv1_energy_kwh",
            "pv2_energy_kwh",
            "pv3_energy_kwh",
            "pv4_energy_kwh",
        ):
            assert by_key[key].entity_category == "diagnostic"


class TestGating:
    async def test_explicit_zero_strings_create_no_higher_string_entities(
        self, hass: HomeAssistant
    ) -> None:
        """What a Stream AC Pro sends: both fields present, both 0.0."""
        _, created = await _setup(
            hass,
            dict(
                TWO_STRING_REPORT,
                pv3_w=0.0,
                pv4_w=0.0,
                pv3_energy_kwh=0.0,
                pv4_energy_kwh=0.0,
            ),
        )

        assert not _keys(created) & HIGHER_STRING_KEYS

    async def test_two_string_report_creates_no_higher_string_entities(
        self, hass: HomeAssistant
    ) -> None:
        _, created = await _setup(hass, TWO_STRING_REPORT)

        assert not _keys(created) & HIGHER_STRING_KEYS

    async def test_the_lower_strings_are_unaffected(self, hass: HomeAssistant) -> None:
        _, created = await _setup(hass, TWO_STRING_REPORT)
        keys = _keys(created)

        assert keys >= LOWER_STRING_KEYS
        assert "soc_pct" in keys

    async def test_a_four_string_report_creates_all_of_them(
        self, hass: HomeAssistant
    ) -> None:
        """The polled quota carries all four, so a Stream on developer keys
        keeps what it has today."""
        _, created = await _setup(
            hass,
            dict(
                TWO_STRING_REPORT,
                pv3_w=402.0,
                pv4_w=377.0,
                pv3_energy_kwh=9.8,
                pv4_energy_kwh=9.2,
            ),
        )

        assert _keys(created) & HIGHER_STRING_KEYS == HIGHER_STRING_KEYS

    async def test_energy_counters_follow_their_power_key(
        self, hass: HomeAssistant
    ) -> None:
        """The counter is integrated from the power reading and arrives one
        update behind it, so the two are gated one by one rather than as a
        block."""
        _, created = await _setup(hass, dict(TWO_STRING_REPORT, pv3_w=402.0))

        assert _keys(created) & HIGHER_STRING_KEYS == {"pv3_w"}


class TestLateReport:
    async def test_a_string_appears_on_its_first_report(
        self, hass: HomeAssistant
    ) -> None:
        """The first quota poll can land after setup, and nothing may need a
        reload to catch up with it."""
        coordinator, created = await _setup(hass, TWO_STRING_REPORT)
        assert not _keys(created) & HIGHER_STRING_KEYS

        coordinator.async_set_updated_data(dict(TWO_STRING_REPORT, pv3_w=402.0))
        await hass.async_block_till_done()

        assert _keys(created) & HIGHER_STRING_KEYS == {"pv3_w"}

    async def test_no_duplicate_on_further_updates(self, hass: HomeAssistant) -> None:
        coordinator, created = await _setup(hass, TWO_STRING_REPORT)

        for watts in (402.0, 411.0):
            coordinator.set_device_value("pv3_w", watts)
            coordinator.async_set_updated_data(dict(coordinator.device_data))
            await hass.async_block_till_done()

        added = [
            entity
            for entity in created
            if getattr(entity, "_definition", None)
            and entity._definition.key == "pv3_w"
        ]
        assert len(added) == 1


class TestStaleEntries:
    async def test_a_leftover_entry_is_removed_once_data_arrives(
        self, hass: HomeAssistant
    ) -> None:
        """Everyone on account sign-in already has the four entries, and
        skipping creation alone would leave them in the device's entity list,
        which is what the reporter sees."""
        registry = er.async_get(hass)
        stale = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{STREAM_DEVICE['sn']}_pv3_w",
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        )

        coordinator, _ = await _setup(hass)
        assert registry.async_get(stale.entity_id) is not None

        _report(coordinator)
        await hass.async_block_till_done()

        assert registry.async_get(stale.entity_id) is None

    async def test_an_entry_the_owner_enabled_is_kept(
        self, hass: HomeAssistant
    ) -> None:
        """An owner who switched the reading on has whatever it recorded, and
        a string can fall silent without ceasing to exist."""
        registry = er.async_get(hass)
        enabled = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{STREAM_DEVICE['sn']}_pv3_w",
        )

        coordinator, _ = await _setup(hass)
        _report(coordinator)
        await hass.async_block_till_done()

        assert registry.async_get(enabled.entity_id) is not None

    async def test_an_enabled_entry_survives_a_setup_at_night(
        self, hass: HomeAssistant
    ) -> None:
        """`pv3_w` waits for a nonzero reading, and an owner who enabled it
        must not lose it because Home Assistant restarted while the string was
        dark (PLAN-142 shortcut)."""
        registry = er.async_get(hass)
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{STREAM_DEVICE['sn']}_pv3_w",
        )

        _, created = await _setup(hass)

        assert "pv3_w" in _keys(created)

    async def test_nothing_is_removed_before_data_arrives(
        self, hass: HomeAssistant
    ) -> None:
        """Before the first update, not reported and not connected yet look
        the same."""
        registry = er.async_get(hass)
        stale = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{STREAM_DEVICE['sn']}_pv3_w",
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        )

        await _setup(hass)

        assert registry.async_get(stale.entity_id) is not None
