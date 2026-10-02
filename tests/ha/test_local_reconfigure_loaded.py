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
        if entry.state is ConfigEntryState.LOADED:
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
