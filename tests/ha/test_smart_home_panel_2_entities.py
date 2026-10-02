"""Smart Home Panel 2 (`HD31`): frames in, Home Assistant states out.

The parser is covered on its own in test_smart_home_panel_2_proto.py. These
tests run the recorded frames through the coordinator's own message path -
the dispatch in `_parse_message`, the merge in `_apply_data` - and read the
sensor entities, so a missing dispatch branch, a storage channel that is not
an accessory or a merge that loses keys fails here even while the parser
tests stay green.

Frames are addressed by their position in the fixture's `frames` list.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_SMART_HOME_PANEL_2,
    DOMAIN,
    MODE_ENHANCED,
    SMARTHOMEPANEL2_SENSORS,
    SMARTPANEL40_SENSORS,
    get_device_type,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.entity import label_placeholders
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

from .conftest import add_entities_collector

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "smart_home_panel_2"
    / "hd31_frames_issue464.json"
)

# Position 21: a get_reply bundle with the full state. battery_soc_pct 93,
# grid_voltage_v 122, circuit 1 at 442.1 W, and one storage channel ready
# (storage_ch1_soc_pct 93; channels 2 and 3 are not reported).
FULL_STATE = 21
# Position 10: a property push that carries grid_voltage_v 123 and nothing
# else. (Positions 9 and 11 carry 122, the value the full state already has,
# so they could not show a change.)
GRID_VOLTAGE_PUSH = 10

# The nine readings every panel has, as the base entities.
BASE_KEYS = {
    "grid_power_w",
    "load_power_w",
    "grid_l1_current_a",
    "grid_l2_current_a",
    "grid_voltage_v",
    "battery_soc_pct",
    "battery_remaining_energy_wh",
    "battery_full_capacity_wh",
    "backup_runtime_min",
}
# Power and current for each of the twelve circuits. The panel reports no
# per-circuit voltage, so there is no `circuit_{n}_voltage_v` to create.
CIRCUIT_KEYS = {
    f"circuit_{n}_{reading}"
    for n in range(1, 13)
    for reading in ("power_w", "current_a")
}

PANEL_DEVICE: dict[str, Any] = {
    "sn": "HD31TEST00000001",
    "name": "Smart Home Panel 2",
    "product_name": "",
    "device_type": DEVICE_TYPE_SMART_HOME_PANEL_2,
    "online": 1,
}

# The Smart Panel 40's circuit definitions as they stood before the Smart Home
# Panel 2 shared their builder: (key, name, translation_key, unit, display
# precision, disabled by default), power then voltage then current, circuit by
# circuit, for the panel's forty circuits. Frozen here so the sharing cannot move them.
SP40_CIRCUIT_DEFINITIONS = [
    row
    for n in range(1, 41)
    for row in (
        (
            f"circuit_{n}_power_w",
            f"Circuit {n} Power",
            "circuit_power_w",
            "W",
            0,
            False,
        ),
        (
            f"circuit_{n}_voltage_v",
            f"Circuit {n} Voltage",
            "circuit_voltage_v",
            "V",
            0,
            True,
        ),
        (
            f"circuit_{n}_current_a",
            f"Circuit {n} Current",
            "circuit_current_a",
            "A",
            0,
            True,
        ),
    )
]


def _frames() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text())["frames"]


def _topic(frame: dict[str, Any]) -> str:
    sn = PANEL_DEVICE["sn"]
    if frame["topic"] == "get_reply":
        return f"/app/user123/{sn}/thing/property/get_reply"
    return f"/app/device/property/{sn}"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [PANEL_DEVICE],
        },
        unique_id="test@example.com",
    )


def _feed(coordinator: EcoFlowDeviceCoordinator, index: int) -> dict[str, Any]:
    frame = _frames()[index]
    parsed = coordinator._parse_message(_topic(frame), bytes.fromhex(frame["hex"]))
    assert parsed is not None, (
        f"frame {index} did not reach the Smart Home Panel 2 parser"
    )
    coordinator._apply_data(parsed)
    return parsed


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _ld(field: int, payload: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(payload)) + payload


def _vint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _feed_synthetic(
    coordinator: EcoFlowDeviceCoordinator, cmd_id: int, pdata: bytes
) -> dict[str, Any]:
    """Feed one hand-built panel frame (cmd_func 12, src 11) as a property push."""
    header = _ld(1, pdata) + _vint(2, 11) + _vint(8, 12) + _vint(9, cmd_id)
    parsed = coordinator._parse_message(_topic({"topic": "property"}), _ld(1, header))
    assert parsed is not None, "the synthetic frame did not reach the parser"
    coordinator._apply_data(parsed)
    return parsed


async def _entities(
    hass: HomeAssistant, entry: MockConfigEntry, coordinator: EcoFlowDeviceCoordinator
) -> dict[str, Any]:
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {PANEL_DEVICE["sn"]: coordinator}
    created: list[Any] = []
    await sensor_setup(hass, entry, add_entities_collector(created))
    return {
        entity._definition.key: entity
        for entity in created
        if hasattr(entity, "_definition")
    }


async def test_a_full_state_creates_the_base_sensors_the_circuits_and_the_ready_channel(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, PANEL_DEVICE)
    _feed(coordinator, FULL_STATE)

    entities = await _entities(hass, entry, coordinator)

    # Channel 1 only: an accessory waits for its channel to report, and the
    # bundle carries one.
    assert set(entities) == BASE_KEYS | CIRCUIT_KEYS | {"storage_ch1_soc_pct"}
    assert entities["battery_soc_pct"].native_value == 93
    # 442.1 W in the frame, shown at the circuit power's whole-watt precision.
    assert entities["circuit_1_power_w"].native_value == 442
    # The owner's circuit names reach the entity names through the label the
    # Smart Panel 40 already uses: the slot number, then the reported text.
    # This panel reports its default names ("Circuit 1"), which repeat the
    # slot number, so the label is the number alone: "Circuit 1 Power", not
    # "Circuit 1 Circuit 1 Power". An owner's own name still follows the
    # number (tests/ha/test_smart_panel_40.py, "3 Bathroom Master").
    assert entities["circuit_1_power_w"]._attr_translation_placeholders == {
        "label": "1"
    }
    assert entities["circuit_12_current_a"]._attr_translation_placeholders == {
        "label": "12"
    }


async def test_a_grid_voltage_only_push_changes_only_the_grid_voltage(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, PANEL_DEVICE)
    _feed(coordinator, FULL_STATE)
    before = {
        key: entity.native_value
        for key, entity in (await _entities(hass, entry, coordinator)).items()
    }
    assert before["grid_voltage_v"] == 122

    push = _feed(coordinator, GRID_VOLTAGE_PUSH)

    assert push == {"grid_voltage_v": 123}, "pick a push that carries one field"
    after = {
        key: entity.native_value
        for key, entity in (await _entities(hass, entry, coordinator)).items()
    }
    assert after == {**before, "grid_voltage_v": 123}


def test_the_prefix_maps_the_panel_and_the_smart_panel_40_circuits_are_unchanged() -> (
    None
):
    sn = PANEL_DEVICE["sn"]
    assert get_device_type("", sn) == DEVICE_TYPE_SMART_HOME_PANEL_2

    # The panel shares the Smart Panel 40's circuit builder through two
    # keyword-only knobs; the Smart Panel 40 must keep every definition it had.
    sp40 = [
        (
            d.key,
            d.name,
            d.translation_key,
            d.unit,
            d.suggested_display_precision,
            d.disabled_by_default,
        )
        for d in SMARTPANEL40_SENSORS
        if d.key.startswith("circuit_")
    ]
    assert sp40 == SP40_CIRCUIT_DEFINITIONS

    # The panel's own circuits: the same keys and translations, no voltage,
    # and two decimals on the current.
    panel = {d.key: d for d in SMARTHOMEPANEL2_SENSORS if d.key.startswith("circuit_")}
    assert set(panel) == CIRCUIT_KEYS
    assert panel["circuit_3_power_w"].translation_key == "circuit_power_w"
    assert panel["circuit_3_current_a"].suggested_display_precision == 2


async def test_a_battery_switched_off_clears_its_channel_level(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, PANEL_DEVICE)
    _feed(coordinator, FULL_STATE)
    entities = await _entities(hass, entry, coordinator)
    assert entities["storage_ch1_soc_pct"].native_value == 93

    # Channel 1 reports ready (80.60.1) and connected (80.80.3) as 0: the
    # entity it already has shows unknown instead of its last level.
    off = _ld(80, _ld(60, _vint(1, 0)) + _ld(80, _vint(3, 0) + _vint(8, 0)))
    _feed_synthetic(coordinator, 32, off)

    assert entities["storage_ch1_soc_pct"].native_value is None


async def test_a_panel_without_storage_shows_an_unknown_battery_and_no_channels(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, PANEL_DEVICE)
    nothing_attached = _ld(
        80,
        _vint(2, 0)
        + _vint(3, 0)
        + _ld(60, _vint(1, 0))
        + _ld(80, _vint(3, 0) + _vint(8, 0))
        + _ld(61, _vint(1, 0))
        + _ld(81, _vint(3, 0) + _vint(8, 0))
        + _ld(62, _vint(1, 0))
        + _ld(82, _vint(3, 0) + _vint(8, 0)),
    )
    _feed_synthetic(coordinator, 32, nothing_attached)

    entities = await _entities(hass, entry, coordinator)

    # The all-zero block is "no battery", not a battery at 0 %, and the
    # retracted channel levels do not create "unknown" entities.
    assert entities["battery_soc_pct"].native_value is None
    assert entities["battery_full_capacity_wh"].native_value is None
    assert not [key for key in entities if key.startswith("storage_ch")]


async def test_a_circuit_without_a_name_still_gets_its_entities(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, PANEL_DEVICE)
    # Circuit 3's info block carries no name at all. Its entities wait for the
    # name key, so the parser has to publish it, empty.
    info = (
        _ld(30, _ld(4, b"Circuit 1"))
        + _ld(31, _ld(4, b"Kitchen"))
        + _ld(32, _vint(2, 7))
    )
    _feed_synthetic(coordinator, 32, _ld(81, _ld(1, info)))
    _feed_synthetic(
        coordinator, 1, _ld(2, _ld(1, struct.pack("<3f", 10.0, 20.0, 30.0)))
    )

    entities = await _entities(hass, entry, coordinator)

    assert {f"circuit_{n}_power_w" for n in (1, 2, 3)} <= set(entities)
    assert "circuit_4_power_w" not in entities
    labels = {
        n: entities[f"circuit_{n}_power_w"]._attr_translation_placeholders
        for n in (1, 2, 3)
    }
    assert labels == {1: {"label": "1"}, 2: {"label": "2 Kitchen"}, 3: {"label": "3"}}


@pytest.mark.parametrize(
    ("text", "label", "expected"),
    [
        # The default name repeats the slot number, whatever its case.
        ("Circuit 1", "1", "1"),
        ("CIRCUIT 1", "1", "1"),
        # A different slot's default name, or an owner text that only starts
        # with the default, is the owner's text and stays.
        ("Circuit 10", "1", "1 Circuit 10"),
        ("Circuit 1 Garage", "1", "1 Circuit 1 Garage"),
        ("Circuit 3", "5", "5 Circuit 3"),
        ("Kitchen", "1", "1 Kitchen"),
        # Nothing reported, or only blanks.
        ("", "1", "1"),
        ("   ", "1", "1"),
    ],
)
def test_the_label_drops_only_the_default_name_of_its_own_slot(
    text: str, label: str, expected: str
) -> None:
    # Only the two stores the helper reads are stubbed.
    coordinator = cast(
        EcoFlowDeviceCoordinator,
        SimpleNamespace(device_data={"circuit_1_name": text}, data=None),
    )

    assert label_placeholders(coordinator, label, "circuit_1_name") == {
        "label": expected
    }
