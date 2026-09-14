"""The PowerPulse 2 wallbox start/stop button platform (ADR-009, PLAN-136).

Phase 3b only: the coordinator action itself (`async_set_powerpulse_charge_action`)
is covered by `test_powerpulse2_charge_action.py`. Every test here goes through
`button.async_setup_entry` and the resulting `EcoFlowButton` entities, never the
coordinator method directly - that is what distinguishes this file from that one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.button import async_setup_entry as button_setup
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
    DEVICE_TYPE_POWERPULSE2,
    DOMAIN,
    MODE_ENHANCED,
    MODE_STANDARD,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ecoflow_energy.number import async_setup_entry as number_setup

from .conftest import add_entities_collector

POWEROCEAN_SN = "HJ31TEST00000001"
POWEROCEAN2_SN = "HJ31TEST00000002"
POWERPULSE2_SN = "C376TEST00000007"
DESCRIPTOR_SN = "X" * 16

POWEROCEAN_DEVICE: dict[str, Any] = {
    "sn": POWEROCEAN_SN,
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}

POWEROCEAN2_DEVICE: dict[str, Any] = {
    "sn": POWEROCEAN2_SN,
    "name": "PowerOcean 2",
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


def _connected_mqtt() -> MagicMock:
    mock_mqtt = MagicMock()
    mock_mqtt.is_connected.return_value = True
    return mock_mqtt


def _mqtt(coordinator: EcoFlowDeviceCoordinator) -> MagicMock:
    """The MagicMock installed on a coordinator's MQTT client, typed as what it is."""
    return cast(MagicMock, coordinator.mqtt_client)


def _enhanced_entry(
    hass: HomeAssistant, devices: list[dict[str, Any]]
) -> MockConfigEntry:
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


def _standard_entry(
    hass: HomeAssistant, devices: list[dict[str, Any]]
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_ACCESS_KEY: "test_ak",
            CONF_SECRET_KEY: "test_sk",
            CONF_MODE: MODE_STANDARD,
            CONF_DEVICES: devices,
        },
        unique_id="test_ak",
    )
    entry.add_to_hass(hass)
    return entry


def _wire_entry(
    hass: HomeAssistant,
    ocean_devices: list[dict[str, Any]],
    *,
    standard_mode: bool = False,
) -> tuple[MockConfigEntry, list[EcoFlowDeviceCoordinator], EcoFlowDeviceCoordinator]:
    """Build one entry with N PowerOcean devices plus one wallbox.

    Returns the entry, the list of PowerOcean coordinators (each with its own
    connected MQTT mock), and the wallbox coordinator. Mirrors
    `test_powerpulse2_charge_action.py::_wire_entry` (kept separate rather than
    imported, per the mission brief).
    """
    devices = [*ocean_devices, POWERPULSE2_DEVICE]
    entry = (
        _standard_entry(hass, devices)
        if standard_mode
        else _enhanced_entry(hass, devices)
    )
    oceans = []
    for device in ocean_devices:
        coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
        coordinator._mqtt_client = _connected_mqtt()
        oceans.append(coordinator)
    wallbox = EcoFlowDeviceCoordinator(hass, entry, POWERPULSE2_DEVICE)
    wallbox._mqtt_client = _connected_mqtt()
    coordinators = {c.device_sn: c for c in [*oceans, wallbox]}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinators
    return entry, oceans, wallbox


def _report_descriptor(wallbox: EcoFlowDeviceCoordinator) -> None:
    wallbox.set_device_value("ev_charger_sn", DESCRIPTOR_SN)
    wallbox.set_device_value("ev_charger_dev_addr", 215)


class TestButtonCreation:
    async def test_two_buttons_created_with_one_sibling_and_descriptor(
        self, hass: HomeAssistant
    ) -> None:
        entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))

        assert len(entities) == 2
        unique_ids = {e.unique_id for e in entities}
        assert unique_ids == {
            f"{POWERPULSE2_SN}_ev_start_charging",
            f"{POWERPULSE2_SN}_ev_stop_charging",
        }
        for e in entities:
            assert e.device_info == wallbox.device_info

    async def test_two_buttons_without_a_powerocean_once_the_heartbeat_reports(
        self, hass: HomeAssistant
    ) -> None:
        """No PowerOcean in the entry routes through the wallbox's own MQTT
        client (PLAN-140). The descriptor never arrives on this account, so
        the heartbeat (`ev_charge_status`) is the gate instead."""
        entry, _oceans, wallbox = _wire_entry(hass, [])
        wallbox.set_device_value("ev_charge_status", "available")

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))

        assert len(entities) == 2
        unique_ids = {e.unique_id for e in entities}
        assert unique_ids == {
            f"{POWERPULSE2_SN}_ev_start_charging",
            f"{POWERPULSE2_SN}_ev_stop_charging",
        }
        for e in entities:
            assert e.device_info == wallbox.device_info

    async def test_zero_buttons_without_a_powerocean_before_the_first_heartbeat(
        self, hass: HomeAssistant
    ) -> None:
        entry, _oceans, wallbox = _wire_entry(hass, [])

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))
        assert entities == []

        wallbox.set_device_value("ev_charge_status", "charging")
        wallbox.async_update_listeners()

        assert len(entities) == 2

    async def test_the_descriptor_alone_does_not_create_buttons_on_the_own_route(
        self, hass: HomeAssistant
    ) -> None:
        """On the own route the gate is the heartbeat, not the descriptor -
        a wallbox without a PowerOcean sibling never reports the descriptor
        at all (it comes from `241/44`, which such an account never sends)."""
        entry, _oceans, wallbox = _wire_entry(hass, [])
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))

        assert entities == []

    async def test_zero_buttons_with_two_powerocean_siblings(
        self, hass: HomeAssistant
    ) -> None:
        entry, _oceans, wallbox = _wire_entry(
            hass, [POWEROCEAN_DEVICE, POWEROCEAN2_DEVICE]
        )
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))

        assert entities == []

    async def test_zero_buttons_in_standard_mode(self, hass: HomeAssistant) -> None:
        """Standard Mode has no write path for the wallbox (ADR-009)."""
        entry, _oceans, wallbox = _wire_entry(
            hass, [POWEROCEAN_DEVICE], standard_mode=True
        )
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))

        assert entities == []

    async def test_buttons_appear_when_the_descriptor_arrives_later(
        self, hass: HomeAssistant
    ) -> None:
        entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))
        assert entities == []

        _report_descriptor(wallbox)
        wallbox.async_update_listeners()

        assert len(entities) == 2


class TestButtonAvailability:
    async def test_available_follows_sibling_connection_not_charge_state(
        self, hass: HomeAssistant
    ) -> None:
        entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))
        button_entity = entities[0]

        for status in ("available", "charging", "finishing"):
            wallbox.set_device_value("ev_charge_status", status)
            assert button_entity.available is True

        _mqtt(oceans[0]).is_connected.return_value = False
        assert button_entity.available is False

        _mqtt(oceans[0]).is_connected.return_value = True
        assert button_entity.available is True

    async def test_available_follows_the_wallbox_own_availability(
        self, hass: HomeAssistant
    ) -> None:
        """A wallbox marked unreachable takes its buttons with it, whatever
        the sibling's connection says."""
        entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))
        button_entity = entities[0]
        assert button_entity.available is True

        wallbox._device_available = False
        assert button_entity.available is False
        wallbox._device_available = True
        assert button_entity.available is True

    async def test_available_follows_the_wallbox_own_connection_without_a_powerocean(
        self, hass: HomeAssistant
    ) -> None:
        """No sibling in the entry (PLAN-140): the wallbox's own MQTT client
        decides availability, and the charge state never flips it."""
        entry, _oceans, wallbox = _wire_entry(hass, [])
        wallbox.set_device_value("ev_charge_status", "available")

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))
        button_entity = entities[0]

        for status in ("available", "charging", "finishing"):
            wallbox.set_device_value("ev_charge_status", status)
            assert button_entity.available is True

        _mqtt(wallbox).is_connected.return_value = False
        assert button_entity.available is False

        _mqtt(wallbox).is_connected.return_value = True
        assert button_entity.available is True

    async def test_available_is_false_when_a_second_powerocean_appears_after_setup(
        self, hass: HomeAssistant
    ) -> None:
        """A second PowerOcean joining the entry after setup makes the route
        ambiguous (`charge_action_route()` returns None), so a button
        created on the "sibling" route goes unavailable."""
        entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))
        button_entity = entities[0]
        assert button_entity.available is True

        second_ocean = EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN2_DEVICE)
        second_ocean._mqtt_client = _connected_mqtt()
        coordinators: dict[str, EcoFlowDeviceCoordinator] = hass.data[DOMAIN][
            entry.entry_id
        ]
        coordinators[second_ocean.device_sn] = second_ocean

        assert button_entity.available is False

    @pytest.mark.parametrize("ocean_devices", [[], [POWEROCEAN_DEVICE]])
    async def test_available_is_false_once_the_entry_table_is_gone(
        self, hass: HomeAssistant, ocean_devices: list[dict[str, Any]]
    ) -> None:
        """Teardown pops the entry's coordinator table (ADR-009 decision 2).
        A button on either route reads unavailable from then on; the own
        route in particular must not read "no PowerOcean, so mine"."""
        entry, _oceans, wallbox = _wire_entry(hass, ocean_devices)
        _report_descriptor(wallbox)
        wallbox.set_device_value("ev_charge_status", "charging")

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))
        button_entity = entities[0]
        assert button_entity.available is True

        hass.data[DOMAIN].pop(entry.entry_id)

        assert button_entity.available is False


class TestButtonPress:
    async def test_press_calls_the_coordinator_action_with_start_or_stop(
        self, hass: HomeAssistant
    ) -> None:
        entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))
        start_button = next(
            e for e in entities if e.unique_id.endswith("ev_start_charging")
        )
        stop_button = next(
            e for e in entities if e.unique_id.endswith("ev_stop_charging")
        )

        mock_action = AsyncMock()
        with patch.object(wallbox, "async_set_powerpulse_charge_action", mock_action):
            await start_button.async_press()
            await stop_button.async_press()

        assert [call.args for call in mock_action.await_args_list] == [
            ("start",),
            ("stop",),
        ]

    async def test_press_error_propagates(self, hass: HomeAssistant) -> None:
        entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await button_setup(hass, entry, add_entities_collector(entities))
        start_button = entities[0]

        mock_action = AsyncMock(
            side_effect=HomeAssistantError("The wallbox reports charging.")
        )
        with (
            patch.object(wallbox, "async_set_powerpulse_charge_action", mock_action),
            pytest.raises(HomeAssistantError),
        ):
            await start_button.async_press()


class TestDiagnosticsDoesNotLeakTheDescriptor:
    async def test_ev_charger_serial_value_never_appears_in_diagnostics(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """The descriptor value must never leak, even if a section is added later.

        `_device_diagnostics` only ever dumps `device_data.keys()`, never
        values (see `diagnostics.py`), so this is a regression guard rather
        than evidence of a fix.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, POWERPULSE2_DEVICE
        )
        secret_sn = "C376" + "A" * 12
        coordinator.set_device_value("ev_charger_sn", secret_sn)
        hass.data.setdefault(DOMAIN, {})[standard_config_entry.entry_id] = {
            coordinator.device_sn: coordinator
        }

        result = await async_get_config_entry_diagnostics(hass, standard_config_entry)

        assert secret_sn not in json.dumps(result)


def _max_current_entities(entities: list[Any]) -> list[Any]:
    """Filter setup's collected entities down to the wallbox's own number.

    A PowerOcean sibling in the same entry contributes its own numbers
    (backup reserve, solar surplus threshold) to the same collector, so a
    bare `entities == []`/`len(entities)` would be testing the sibling's
    entities as much as the wallbox's.
    """
    return [e for e in entities if e.unique_id == f"{POWERPULSE2_SN}_ev_max_current_a"]


class TestNumberCreation:
    """The wallbox maximum-current number (PLAN-146). Sibling route only -
    no "own" route counterpart exists for this control, unlike the buttons.
    """

    async def test_max_current_number_created_with_one_sibling_after_first_report(
        self, hass: HomeAssistant
    ) -> None:
        entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await number_setup(hass, entry, add_entities_collector(entities))
        assert _max_current_entities(entities) == []

        wallbox.set_device_value("ev_max_current_a", 16.0)
        wallbox.async_set_updated_data(dict(wallbox._device_data))

        matches = _max_current_entities(entities)
        assert len(matches) == 1
        number_entity = matches[0]
        assert number_entity.unique_id == f"{POWERPULSE2_SN}_ev_max_current_a"
        assert number_entity.native_value == 16
        assert number_entity.native_min_value == 6
        assert number_entity.native_max_value == 16
        assert number_entity.native_step == 1
        assert number_entity.native_unit_of_measurement == "A"

    async def test_no_max_current_number_without_a_powerocean_even_after_the_report(
        self, hass: HomeAssistant
    ) -> None:
        """The "own" route has no evidenced write for this control (PLAN-146
        decision 2), so no number appears even once the reading is there -
        unlike the buttons, which do get created on this route."""
        entry, _oceans, wallbox = _wire_entry(hass, [])
        wallbox.set_device_value("ev_max_current_a", 16.0)

        entities: list[Any] = []
        await number_setup(hass, entry, add_entities_collector(entities))

        assert entities == []

    async def test_no_max_current_number_with_two_poweroceans(
        self, hass: HomeAssistant
    ) -> None:
        entry, _oceans, wallbox = _wire_entry(
            hass, [POWEROCEAN_DEVICE, POWEROCEAN2_DEVICE]
        )
        wallbox.set_device_value("ev_max_current_a", 16.0)

        entities: list[Any] = []
        await number_setup(hass, entry, add_entities_collector(entities))

        assert _max_current_entities(entities) == []

    async def test_no_max_current_number_in_standard_mode(
        self, hass: HomeAssistant
    ) -> None:
        entry, _oceans, wallbox = _wire_entry(
            hass, [POWEROCEAN_DEVICE], standard_mode=True
        )
        wallbox.set_device_value("ev_max_current_a", 16.0)

        entities: list[Any] = []
        await number_setup(hass, entry, add_entities_collector(entities))

        assert entities == []

    async def test_max_current_number_reads_the_new_value_from_a_real_frame(
        self, hass: HomeAssistant
    ) -> None:
        """A real `2/34` ParamReport frame, parsed and applied through the
        normal MQTT ingest path (`_parse_message` + `_apply_data`), reaches
        `coordinator.data` and therefore the entity's `native_value` - not
        just the `set_device_value` + `async_set_updated_data` shortcut the
        other creation tests use (PLAN-146 review F-05).
        """
        entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        _report_descriptor(wallbox)

        entities: list[Any] = []
        await number_setup(hass, entry, add_entities_collector(entities))
        assert _max_current_entities(entities) == []

        fixture_path = (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "powerpulse"
            / "c376_param_set_echo_20260824.json"
        )
        fixture = json.loads(fixture_path.read_text())
        raw = bytes.fromhex(fixture["frames"][0]["hex"])
        topic = f"/app/device/property/{POWERPULSE2_SN}"
        parsed = wallbox._parse_message(topic, raw)
        assert parsed is not None
        wallbox._apply_data(parsed)

        matches = _max_current_entities(entities)
        assert len(matches) == 1
        assert matches[0].native_value == 11


class TestNumberSetValue:
    async def test_set_value_calls_the_coordinator_with_an_int_no_optimistic_apply(
        self, hass: HomeAssistant
    ) -> None:
        entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        wallbox.set_device_value("ev_max_current_a", 16.0)
        wallbox.async_set_updated_data(dict(wallbox._device_data))

        entities: list[Any] = []
        await number_setup(hass, entry, add_entities_collector(entities))
        number_entity = _max_current_entities(entities)[0]

        mock_set = AsyncMock()
        with patch.object(wallbox, "async_set_powerpulse_max_current", mock_set):
            await number_entity.async_set_native_value(11)

        mock_set.assert_awaited_once_with(11)
        assert mock_set.await_args is not None
        assert isinstance(mock_set.await_args.args[0], int)
        # No optimistic apply: the store still holds the value from before
        # the write until the wallbox's own report changes it.
        assert number_entity.native_value == 16

        mock_set.reset_mock()
        with (
            patch.object(wallbox, "async_set_powerpulse_max_current", mock_set),
            pytest.raises(HomeAssistantError),
        ):
            await number_entity.async_set_native_value(11.5)

        mock_set.assert_not_called()


class TestNumberAvailability:
    async def test_number_available_follows_the_sibling_connection(
        self, hass: HomeAssistant
    ) -> None:
        entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        wallbox.set_device_value("ev_max_current_a", 16.0)

        entities: list[Any] = []
        await number_setup(hass, entry, add_entities_collector(entities))
        number_entity = _max_current_entities(entities)[0]
        assert number_entity.available is True

        _mqtt(oceans[0]).is_connected.return_value = False
        assert number_entity.available is False

        _mqtt(oceans[0]).is_connected.return_value = True
        assert number_entity.available is True

    async def test_available_is_false_when_a_second_powerocean_appears_after_setup(
        self, hass: HomeAssistant
    ) -> None:
        """Mirrors `TestButtonAvailability`'s test of the same name: a
        second PowerOcean joining the entry after setup makes the route
        ambiguous (`charge_action_route()` returns None), so the number
        goes unavailable too."""
        entry, oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        wallbox.set_device_value("ev_max_current_a", 16.0)

        entities: list[Any] = []
        await number_setup(hass, entry, add_entities_collector(entities))
        number_entity = _max_current_entities(entities)[0]
        assert number_entity.available is True

        second_ocean = EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN2_DEVICE)
        second_ocean._mqtt_client = _connected_mqtt()
        coordinators: dict[str, EcoFlowDeviceCoordinator] = hass.data[DOMAIN][
            entry.entry_id
        ]
        coordinators[second_ocean.device_sn] = second_ocean

        assert number_entity.available is False

    async def test_available_is_false_once_the_entry_table_is_gone(
        self, hass: HomeAssistant
    ) -> None:
        """Mirrors `TestButtonAvailability`'s test of the same name:
        teardown pops the entry's coordinator table (ADR-009 decision 2),
        and the number reads unavailable from then on."""
        entry, _oceans, wallbox = _wire_entry(hass, [POWEROCEAN_DEVICE])
        wallbox.set_device_value("ev_max_current_a", 16.0)

        entities: list[Any] = []
        await number_setup(hass, entry, add_entities_collector(entities))
        number_entity = _max_current_entities(entities)[0]
        assert number_entity.available is True

        hass.data[DOMAIN].pop(entry.entry_id)

        assert number_entity.available is False
