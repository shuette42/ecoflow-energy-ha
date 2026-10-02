"""Reconfiguring a Local entry while it is loaded.

A PowerOcean serves one Modbus client at a time. A loaded Local entry holds
that client, so a reconfigure probe with a new host string (an IP replaced by
a DNS name, for example) was a second client and failed with cannot_connect
until the entry was disabled by hand. The flow now releases the entry's
connection before it probes, and starts the entry again whatever the result.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ecoflow_energy.config_flow_setup import LocalDeviceError
from custom_components.ecoflow_energy.const import CONF_UNIT_ID, DOMAIN

from .test_local_modbus_coordinator import SERIAL, StubClient
from .test_local_modbus_entities import _poll_frame

PROBE = "custom_components.ecoflow_energy.config_flow_reconfigure.read_local_device"
LINK = "custom_components.ecoflow_energy.coordinator.local_modbus.create_link"
NEW_HOST = "powerocean.example.test"


def _one_client_device(hass: HomeAssistant, entry: Any, serial: str = SERIAL) -> Any:
    """A probe that the device answers only while the entry holds no connection."""
    probes: list[ConfigEntryState] = []

    async def probe(_hass: Any, host: str, port: int, unit_id: int) -> dict[str, Any]:
        probes.append(entry.state)
        # A probe on the entry's own address shares its connection; any other
        # address is a second client while the entry holds the first.
        if entry.state is ConfigEntryState.LOADED and host != entry.data[CONF_HOST]:
            raise LocalDeviceError("cannot_connect")
        if host == "unreachable.example.test":
            raise LocalDeviceError("cannot_connect")
        return {"serial": serial}

    probe.states = probes  # type: ignore[attr-defined]
    return probe


async def _loaded_local_entry(hass: HomeAssistant) -> Any:
    from .test_local_modbus_coordinator import _local_entry

    entry = _local_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _reconfigure(hass: HomeAssistant, entry: Any, host: str) -> Any:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_local"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: host, CONF_PORT: 502, CONF_UNIT_ID: 1}
    )
    await hass.async_block_till_done()
    return result


def _link() -> Any:
    return patch(LINK, side_effect=lambda *a, **k: StubClient([_poll_frame()] * 50))


async def test_a_loaded_entry_moves_to_a_new_host(hass: HomeAssistant) -> None:
    with _link():
        entry = await _loaded_local_entry(hass)
        probe = _one_client_device(hass, entry)
        with patch(PROBE, side_effect=probe):
            result = await _reconfigure(hass, entry, NEW_HOST)

    assert result["type"] is FlowResultType.ABORT, result
    assert result["reason"] == "local_updated"
    assert entry.data[CONF_HOST] == NEW_HOST
    assert probe.states == [ConfigEntryState.NOT_LOADED]
    assert entry.state is ConfigEntryState.LOADED


async def test_a_failed_probe_starts_the_entry_again_on_its_old_host(
    hass: HomeAssistant,
) -> None:
    with _link():
        entry = await _loaded_local_entry(hass)
        old_host = entry.data[CONF_HOST]
        probe = _one_client_device(hass, entry)
        with patch(PROBE, side_effect=probe):
            result = await _reconfigure(hass, entry, "unreachable.example.test")

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data[CONF_HOST] == old_host
    assert entry.state is ConfigEntryState.LOADED


async def test_another_inverter_at_the_new_host_starts_the_entry_again(
    hass: HomeAssistant,
) -> None:
    with _link():
        entry = await _loaded_local_entry(hass)
        probe = _one_client_device(hass, entry, serial="HJ31DUMMY0000009")
        with patch(PROBE, side_effect=probe):
            result = await _reconfigure(hass, entry, NEW_HOST)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "serial_mismatch"}
    assert entry.state is ConfigEntryState.LOADED


async def test_an_abort_for_a_serial_held_elsewhere_starts_the_entry_again(
    hass: HomeAssistant,
) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.ecoflow_energy.const import CONF_DEVICES, CONF_MODE

    with _link():
        entry = await _loaded_local_entry(hass)
        MockConfigEntry(
            domain=DOMAIN,
            data={CONF_MODE: "enhanced", CONF_DEVICES: [{"sn": SERIAL}]},
        ).add_to_hass(hass)
        probe = _one_client_device(hass, entry)
        with patch(PROBE, side_effect=probe):
            result = await _reconfigure(hass, entry, NEW_HOST)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.state is ConfigEntryState.LOADED


async def test_an_unchanged_address_keeps_the_entry_running(
    hass: HomeAssistant,
) -> None:
    """Only the unit id changes: the probe shares the entry's own connection."""
    with _link():
        entry = await _loaded_local_entry(hass)
        probe = _one_client_device(hass, entry)
        with patch(PROBE, side_effect=probe):
            result = await _reconfigure(hass, entry, entry.data[CONF_HOST])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "local_updated"
    assert probe.states == [ConfigEntryState.LOADED]
    assert entry.state is ConfigEntryState.LOADED


async def test_a_failing_unload_shows_the_form_instead_of_crashing(
    hass: HomeAssistant,
) -> None:
    with _link():
        entry = await _loaded_local_entry(hass)
        probe = _one_client_device(hass, entry)
        with (
            patch.object(hass.config_entries, "async_unload", return_value=False),
            patch.object(hass.config_entries, "async_schedule_reload") as reload,
            patch(PROBE, side_effect=probe),
        ):
            result = await _reconfigure(hass, entry, NEW_HOST)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    # Nothing was released, so nothing is restarted: a reload of an entry
    # whose unload failed would only raise in the background.
    reload.assert_not_called()
    assert entry.state is ConfigEntryState.LOADED


async def test_an_unexpected_error_mid_probe_starts_the_entry_again(
    hass: HomeAssistant,
) -> None:
    async def broken(*_args: Any) -> dict[str, Any]:
        raise RuntimeError("probe blew up")

    with _link():
        entry = await _loaded_local_entry(hass)
        with (
            patch(PROBE, side_effect=broken),
            pytest.raises(RuntimeError, match="probe blew up"),
        ):
            await _reconfigure(hass, entry, NEW_HOST)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_HOST] != NEW_HOST


async def test_an_entry_waiting_to_retry_is_released_for_the_probe(
    hass: HomeAssistant,
) -> None:
    """A pending setup retry must not claim the one client mid-probe."""
    from .test_local_modbus_coordinator import _local_entry

    entry = _local_entry()
    entry.add_to_hass(hass)
    with patch(LINK, side_effect=lambda *a, **k: StubClient([TimeoutError()] * 5)):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY

    with _link():
        probe = _one_client_device(hass, entry)
        with patch(PROBE, side_effect=probe):
            result = await _reconfigure(hass, entry, NEW_HOST)

    assert result["reason"] == "local_updated"
    assert probe.states == [ConfigEntryState.NOT_LOADED]
    assert entry.state is ConfigEntryState.LOADED


async def test_a_loaded_cloud_entry_is_not_unloaded_for_the_switch(
    hass: HomeAssistant,
) -> None:
    """The cloud entry holds no Modbus client, so the switch probes as before."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.ecoflow_energy.const import (
        CONF_DEVICES,
        CONF_MODE,
        MODE_ENHANCED,
    )

    from .test_local_modbus_coordinator import DEVICE_DICT

    cloud = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_MODE: MODE_ENHANCED, CONF_DEVICES: [DEVICE_DICT]},
    )
    cloud.add_to_hass(hass)
    cloud.mock_state(hass, ConfigEntryState.LOADED)
    seen: list[ConfigEntryState] = []

    async def probe(*_args: Any) -> dict[str, Any]:
        seen.append(cloud.state)
        return {"serial": SERIAL}

    with patch(PROBE, side_effect=probe), _link():
        result = await _reconfigure(hass, cloud, NEW_HOST)

    assert result["reason"] == "mode_switched"
    assert seen == [ConfigEntryState.LOADED]
