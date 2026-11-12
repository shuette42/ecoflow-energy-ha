"""OCEAN Pro (`HR51`): the coordinator routes its frames to the HR51 parser (#427).

The parser has its own tests. These feed the owner's recorded frames through
the coordinator's `_parse_message` and `_apply_data`, at both dispatch sites:
the property push (`/app/device/property/<sn>`) and the bundled `get_reply`.
The serials are fictional (the real one is masked in the fixtures).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    DEVICE_TYPE_OCEAN_PRO,
    DEVICE_TYPE_SMART_PANEL_40,
    DOMAIN,
    MODE_ENHANCED,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.parsers.ocean_pro_proto import (
    parse_hr51_proto_message,
)
from custom_components.ecoflow_energy.ecoflow.parsers.smart_panel_40_proto import (
    parse_hr61_proto_message,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures"
_HR51_FRAMES: dict[str, dict[str, Any]] = {
    f["name"]: f
    for f in json.loads((_FIXTURES / "ocean_pro" / "hr51_frames_427.json").read_text())[
        "frames"
    ]
}
_HR61_DATA = json.loads((_FIXTURES / "hr61" / "hr61_frames.json").read_text())
_HR61_FRAMES: dict[int, dict[str, Any]] = {
    f["i"]: f
    for f in (_HR61_DATA if isinstance(_HR61_DATA, list) else _HR61_DATA["frames"])
}

OCEAN_PRO_DEVICE: dict[str, Any] = {
    "sn": "HR51TESTDUMMYAAA",
    "name": "OCEAN Pro",
    "product_name": "",
    "device_type": DEVICE_TYPE_OCEAN_PRO,
    "online": 1,
}
PANEL_DEVICE: dict[str, Any] = {
    "sn": "HR61TESTDUMMYAAA",
    "name": "Smart Panel 40",
    "product_name": "",
    "device_type": DEVICE_TYPE_SMART_PANEL_40,
    "online": 1,
}


def _topic(frame: dict[str, Any], sn: str) -> str:
    if frame["topic"] == "get_reply":
        return f"/app/user123/{sn}/thing/property/get_reply"
    return f"/app/device/property/{sn}"


def _entry(device: dict[str, Any]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [device],
        },
        unique_id="test@example.com",
    )


def _coordinator(
    hass: HomeAssistant, device: dict[str, Any]
) -> EcoFlowDeviceCoordinator:
    entry = _entry(device)
    entry.add_to_hass(hass)
    return EcoFlowDeviceCoordinator(hass, entry, device)


def _route(
    coordinator: EcoFlowDeviceCoordinator, frame: dict[str, Any]
) -> dict[str, Any] | None:
    sn = coordinator.device_sn
    return coordinator._parse_message(_topic(frame, sn), bytes.fromhex(frame["hex"]))


@pytest.mark.parametrize(
    "name",
    [
        # property push site
        "core_with_soc_and_grid_hz",
        # get_reply bundle site
        "full_state_bundle",
    ],
)
async def test_an_hr51_frame_reaches_the_hr51_parser(
    hass: HomeAssistant, name: str
) -> None:
    coordinator = _coordinator(hass, OCEAN_PRO_DEVICE)
    frame = _HR51_FRAMES[name]
    expected = parse_hr51_proto_message(bytes.fromhex(frame["hex"]))
    assert expected is not None and "pv_string_1_power_w" in expected

    parsed = _route(coordinator, frame)

    assert parsed == expected, f"{name}: not routed to parse_hr51_proto_message"
    coordinator._apply_data(parsed)
    for key, value in expected.items():
        assert coordinator._device_data[key] == value, key


@pytest.mark.parametrize(
    "hr51_name", ["core_with_soc_and_grid_hz", "full_state_bundle"]
)
async def test_an_hr61_serial_still_routes_to_the_panel_parser(
    hass: HomeAssistant, hr51_name: str
) -> None:
    coordinator = _coordinator(hass, PANEL_DEVICE)

    panel_frame = _HR61_FRAMES[16]
    assert _route(coordinator, panel_frame) == parse_hr61_proto_message(
        bytes.fromhex(panel_frame["hex"])
    )

    # An HR51 frame fed to a panel coordinator is read by the panel parser:
    # none of the keys only the HR51 parser produces may appear.
    parsed = _route(coordinator, _HR51_FRAMES[hr51_name]) or {}
    hr51_only = {
        "battery_pack_soc_pct",
        "pv_string_1_power_w",
        "batt_charge_energy_kwh",
    }
    assert not hr51_only & parsed.keys(), sorted(hr51_only & parsed.keys())


async def test_a_frame_without_the_soc_field_keeps_the_stored_soc(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, OCEAN_PRO_DEVICE)
    coordinator._apply_data({"battery_soc_pct": 77.0})

    frame = _HR51_FRAMES["core_without_soc"]
    parsed = _route(coordinator, frame)
    assert parsed is not None
    assert "battery_soc_pct" not in parsed, "pick another frame: this one carries 262"
    coordinator._apply_data(parsed)

    assert coordinator._device_data["battery_soc_pct"] == 77.0
    for key, value in parsed.items():
        assert coordinator._device_data[key] == value, key
