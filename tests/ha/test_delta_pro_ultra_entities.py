"""DELTA Pro Ultra (`Y711`): frames in, Home Assistant states out.

The parser is covered on its own in test_delta_pro_ultra_proto.py. These
tests run the recorded frames through the coordinator's own message path -
the dispatch in `_parse_message`, the merge in `_apply_data` - and read the
values the sensor entities render, so a missing dispatch branch, a pack that
is not an accessory or a merge that loses keys fails here even while the
parser tests stay green.

Frames are addressed by their position in the fixture's `frames` list.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    DEVICE_TYPE_DELTA_PRO_ULTRA,
    DOMAIN,
    MODE_ENHANCED,
    get_device_type,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

from .conftest import add_entities_collector

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "delta_pro_ultra"
    / "y711_frames_issue464.json"
)

# Position 11: a get_reply bundle carrying the 2.1 to 2.4 headers, soc 93,
# remain_time_min 34359 and the extra packs 1 to 3 (bp2_soc_pct 98).
FULL_STATE = 11
# Position 27: a 40-byte property push whose 2.1 header carries one field,
# remain_time_min 34347, and nothing else.
PARTIAL_PUSH = 27

# The twenty-one readings every DELTA Pro Ultra has, as the base entities.
BASE_KEYS = {
    "soc",
    "remain_time_min",
    "watts_in_sum",
    "watts_out_sum",
    "ac_in_w",
    "power_io_in_w",
    "power_io_out_w",
    "solar_lv_in_w",
    "solar_hv_in_w",
    "ac_out_l1_1_w",
    "ac_out_l1_2_w",
    "ac_out_l2_1_w",
    "ac_out_l2_2_w",
    "ac_out_tt30_w",
    "ac_out_l14_w",
    "batt_voltage_v",
    "batt_charge_power_w",
    "batt_discharge_power_w",
    "inv_ac_temp_c",
    "pd_temp_c",
    "backup_reserve_pct",
}

ULTRA_DEVICE: dict[str, Any] = {
    "sn": "Y711TEST00000001",
    "name": "DELTA Pro Ultra",
    "product_name": "",
    "device_type": DEVICE_TYPE_DELTA_PRO_ULTRA,
    "online": 1,
}


def _frames() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text())["frames"]


def _topic(frame: dict[str, Any]) -> str:
    sn = ULTRA_DEVICE["sn"]
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
            CONF_DEVICES: [ULTRA_DEVICE],
        },
        unique_id="test@example.com",
    )


def _feed(coordinator: EcoFlowDeviceCoordinator, index: int) -> dict[str, Any]:
    frame = _frames()[index]
    parsed = coordinator._parse_message(_topic(frame), bytes.fromhex(frame["hex"]))
    assert parsed is not None, f"frame {index} did not reach the DELTA Pro Ultra parser"
    coordinator._apply_data(parsed)
    return parsed


async def _rendered(
    hass: HomeAssistant, entry: MockConfigEntry, coordinator: EcoFlowDeviceCoordinator
) -> dict[str, Any]:
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {ULTRA_DEVICE["sn"]: coordinator}
    created: list[Any] = []
    await sensor_setup(hass, entry, add_entities_collector(created))
    return {
        entity._definition.key: entity.native_value
        for entity in created
        if hasattr(entity, "_definition")
    }


async def test_a_full_state_creates_the_base_sensors_and_the_packs_reported(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, ULTRA_DEVICE)
    _feed(coordinator, FULL_STATE)

    values = await _rendered(hass, entry, coordinator)

    packs = {key for key in values if key.startswith("bp")}
    assert set(values) - packs == BASE_KEYS
    # The bundle lists packs 1 to 3; 4 and 5 are never created, because an
    # accessory entity waits for its pack to report.
    assert packs == {
        f"bp{n}_{field}" for n in (1, 2, 3) for field in ("soc_pct", "temp_c")
    }
    assert values["soc"] == 93
    assert values["bp2_soc_pct"] == 98
    assert values["remain_time_min"] == 34359


async def test_a_partial_push_merges_into_the_state_it_does_not_carry(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, ULTRA_DEVICE)
    _feed(coordinator, FULL_STATE)
    push = _feed(coordinator, PARTIAL_PUSH)

    assert push == {"remain_time_min": 34347}, "pick a push that carries one field"
    values = await _rendered(hass, entry, coordinator)

    assert values["remain_time_min"] == 34347
    assert values["soc"] == 93
    assert values["bp2_soc_pct"] == 98


def test_the_serial_prefix_decides_the_type_over_the_product_name() -> None:
    sn = ULTRA_DEVICE["sn"]
    assert get_device_type("", sn) == DEVICE_TYPE_DELTA_PRO_ULTRA
    # "DELTA Pro Ultra" contains the "delta" keyword and would otherwise be
    # classified as a Delta 2 Max.
    assert get_device_type("DELTA Pro Ultra", sn) == DEVICE_TYPE_DELTA_PRO_ULTRA
