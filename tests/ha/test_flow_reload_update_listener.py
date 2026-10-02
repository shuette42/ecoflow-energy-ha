"""Config flows reload an entry exactly once, and never on top of its listener.

Home Assistant 2026.9 reports ``async_update_reload_and_abort`` on an entry
that has an update listener (the listener already reloads it), and 2026.12
stops allowing it. These tests enforce that rule on any Home Assistant version
by failing the call, and count the reloads that actually happen.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.config_flow_setup import update_and_reload
from custom_components.ecoflow_energy.const import CONF_UNIT_ID, DOMAIN

from .test_local_modbus_coordinator import StubClient
from .test_local_modbus_entities import _poll_frame
from .test_local_reconfigure_loaded import (
    LINK,
    NEW_HOST,
    PROBE,
    _loaded_local_entry,
    _one_client_device,
    _reconfigure,
)

_REAL = ConfigFlow.async_update_reload_and_abort


def _strict(self: ConfigFlow, entry: Any, **kwargs: Any) -> Any:
    """What Home Assistant 2026.12 does: refuse a flow reload on a listening entry."""
    if entry.update_listeners:
        raise AssertionError("flow reload on an entry with an update listener")
    return _REAL(self, entry, **kwargs)


@pytest.fixture(autouse=True)
def _ha_2026_12(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ConfigFlow, "async_update_reload_and_abort", _strict)


@pytest.fixture
def links() -> list[StubClient]:
    """Every link the Local coordinator builds, one per setup."""
    return []


def _counting_link(links: list[StubClient]) -> Any:
    def build(*_args: Any, **_kwargs: Any) -> StubClient:
        stub = StubClient([_poll_frame()] * 50)
        links.append(stub)
        return stub

    return patch(LINK, side_effect=build)


async def test_unchanged_address_reloads_once_without_a_flow_reload(
    hass: HomeAssistant, links: list[StubClient]
) -> None:
    """Nothing changes: the listener stays quiet, the flow schedules one reload."""
    with _counting_link(links):
        entry = await _loaded_local_entry(hass)
        with patch(PROBE, side_effect=_one_client_device(hass, entry)):
            result = await _reconfigure(hass, entry, entry.data["host"])

    assert result["reason"] == "local_updated"
    assert len(links) == 2  # setup + exactly one reload
    assert entry.state is ConfigEntryState.LOADED


async def test_a_changed_unit_id_is_reloaded_by_the_listener_alone(
    hass: HomeAssistant, links: list[StubClient]
) -> None:
    """Same address, other unit id: the entry stays loaded and the update reloads it."""
    from homeassistant.config_entries import SOURCE_RECONFIGURE
    from homeassistant.const import CONF_HOST, CONF_PORT

    with _counting_link(links):
        entry = await _loaded_local_entry(hass)
        with patch(PROBE, side_effect=_one_client_device(hass, entry)):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"next_step_id": "reconfigure_local"}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_HOST: entry.data[CONF_HOST], CONF_PORT: 502, CONF_UNIT_ID: 7},
            )
            await hass.async_block_till_done()

    assert result["reason"] == "local_updated"
    assert entry.data[CONF_UNIT_ID] == 7
    assert len(links) == 2
    assert entry.state is ConfigEntryState.LOADED


async def test_a_released_entry_gets_the_flow_reload(
    hass: HomeAssistant, links: list[StubClient]
) -> None:
    """A new address unloads the entry, which drops the listener; the flow reloads."""
    with _counting_link(links):
        entry = await _loaded_local_entry(hass)
        with patch(PROBE, side_effect=_one_client_device(hass, entry)):
            result = await _reconfigure(hass, entry, NEW_HOST)

    assert result["reason"] == "local_updated"
    assert entry.data["host"] == NEW_HOST
    assert len(links) == 2
    assert entry.state is ConfigEntryState.LOADED


class _Flow:
    """The two flow calls the helper uses, recorded."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.flow_reloads = 0

    def async_update_reload_and_abort(self, entry: Any, **kwargs: Any) -> Any:
        if entry.update_listeners:
            raise AssertionError("flow reload on an entry with an update listener")
        self.flow_reloads += 1
        self.hass.config_entries.async_update_entry(
            entry, **{k: v for k, v in kwargs.items() if k != "reason"}
        )
        return {"type": FlowResultType.ABORT, "reason": kwargs["reason"]}

    def async_abort(self, *, reason: str) -> Any:
        return {"type": FlowResultType.ABORT, "reason": reason}


@pytest.mark.parametrize("changed", [True, False])
async def test_the_helper_on_a_listening_entry_never_reloads_from_the_flow(
    hass: HomeAssistant, changed: bool
) -> None:
    """Also the Local -> cloud switch path: any listening entry, any change."""
    entry = MockConfigEntry(domain=DOMAIN, title="old", data={"a": 1})
    entry.add_to_hass(hass)
    fired: list[Any] = []

    async def listener(_hass: HomeAssistant, _entry: Any) -> None:
        fired.append(_entry)

    entry.add_update_listener(listener)
    flow = _Flow(hass)
    with patch.object(hass.config_entries, "async_schedule_reload") as scheduled:
        result = update_and_reload(
            flow,  # type: ignore[arg-type]
            entry,
            title="new" if changed else "old",
            data={"a": 2} if changed else {"a": 1},
            reason="mode_switched",
        )
        await hass.async_block_till_done()

    assert result["reason"] == "mode_switched"
    assert flow.flow_reloads == 0
    # One reload either way: the listener for a change, the helper otherwise.
    assert len(fired) == (1 if changed else 0)
    assert scheduled.call_count == (0 if changed else 1)
    assert entry.title == ("new" if changed else "old")


async def test_the_helper_on_an_entry_without_listener_uses_the_flow_reload(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="old", data={"a": 1})
    entry.add_to_hass(hass)
    flow = _Flow(hass)

    update_and_reload(flow, entry, data={"a": 2}, reason="local_updated")  # type: ignore[arg-type]

    assert flow.flow_reloads == 1
    assert entry.data == {"a": 2}
