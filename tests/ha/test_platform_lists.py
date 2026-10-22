"""The platform lists live in ``__init__.py`` and setup and unload use them.

The lists used to sit in the root ``const.py``, which made that module import
Home Assistant. They moved to the package ``__init__``, so a slip there means a
config entry loads no entities at all. These tests pin the contents and the
three places that hand the lists to Home Assistant.
"""

from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import Platform

from custom_components import ecoflow_energy
from custom_components.ecoflow_energy import (
    LOCAL_PLATFORMS,
    PLATFORMS,
    async_unload_entry,
)
from custom_components.ecoflow_energy.const import DOMAIN
from custom_components.ecoflow_energy.coordinator.local_modbus import (
    EcoFlowLocalModbusCoordinator,
)

CLOUD = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.CLIMATE,
    Platform.BUTTON,
]
LOCAL = [Platform.SENSOR, Platform.SWITCH, Platform.NUMBER, Platform.BINARY_SENSOR]


def test_the_lists_hold_exactly_these_platforms() -> None:
    assert PLATFORMS == CLOUD
    assert LOCAL_PLATFORMS == LOCAL


def test_the_root_const_no_longer_carries_the_lists() -> None:
    from custom_components.ecoflow_energy import const

    assert not hasattr(const, "PLATFORMS")
    assert not hasattr(const, "LOCAL_PLATFORMS")


def _forward_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "async_forward_entry_setups"
    ]


def test_setup_hands_the_module_lists_to_home_assistant() -> None:
    tree = ast.parse(inspect.getsource(ecoflow_energy))
    calls = _forward_calls(tree)
    assert len(calls) == 2, "one forward call per mode"
    passed = sorted(ast.unparse(call.args[1]) for call in calls)
    assert passed == ["LOCAL_PLATFORMS", "PLATFORMS"]


def _hass_for(coordinators: dict[str, object]) -> SimpleNamespace:
    unload = AsyncMock(return_value=False)
    return SimpleNamespace(
        data={DOMAIN: {"entry-1": coordinators}},
        config_entries=SimpleNamespace(async_unload_platforms=unload),
    )


@pytest.mark.parametrize(
    ("coordinators", "expected"),
    [
        ({}, CLOUD),
        ({"SERIAL": MagicMock(spec=EcoFlowLocalModbusCoordinator)}, LOCAL),
    ],
    ids=["cloud entry", "local entry"],
)
async def test_unload_hands_back_the_platforms_the_entry_set_up(
    coordinators: dict[str, object], expected: list[Platform]
) -> None:
    hass = _hass_for(coordinators)
    entry = SimpleNamespace(entry_id="entry-1")

    assert await async_unload_entry(hass, entry) is False  # type: ignore[arg-type]

    hass.config_entries.async_unload_platforms.assert_awaited_once_with(entry, expected)
