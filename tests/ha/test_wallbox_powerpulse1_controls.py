"""PowerPulse 1 settings on a PowerOcean coordinator (#731).

Reports and writes are the reporter's real frames
(`tests/fixtures/powerpulse/r372_settings_731_20261113.json`). The PowerOcean
parses the wallbox's 209/8 report into four settings and its serial; a write
returns only once the next report carries the new value.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ecoflow_energy.const import DOMAIN
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)
from custom_components.ecoflow_energy.select import async_setup_entry as select_setup
from custom_components.ecoflow_energy.switch import async_setup_entry as switch_setup
from tests.ha.conftest import add_entities_collector
from tests.ha.test_powerpulse2_charge_action import (
    POWEROCEAN_DEVICE,
    _connected_mqtt,
    _entry,
    _mqtt,
)
from tests.ha.test_powerpulse_edev_ingest import POWERPULSE2_DEVICE

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "powerpulse"
    / "r372_settings_731_20261113.json"
)
FRAMES = {f["label"]: f for f in json.loads(FIXTURE.read_text())["frames"]}
WINDOW = (
    "custom_components.ecoflow_energy.coordinator.set_commands.POWERGLOW_WRITE_WINDOW_S"
)
SETTINGS = (
    "ev_plug_and_play",
    "ev_charge_mode_raw",
    "ev_battery_discharge_block",
    "ev_continuous_charging",
)
# The fixture's serials are masked; a dummy with the PowerPulse 1 prefix stands
# in where the prefix decides, one with the PowerPulse 2 prefix for any other.
PP1_SN = "AC31" + "0" * 12
PP2_SN = "C376" + "0" * 12
OTHER_PP1_SN = "AC31" + "1" * 12
FOUR = {
    "ev_plug_and_play",
    "ev_charge_mode",
    "ev_continuous_charging",
    "ev_battery_discharge_block",
}
START = {
    "ev_charger_sn": "X" * 16,
    "ev_plug_and_play": False,
    "ev_charge_mode_raw": 2,
    "ev_battery_discharge_block": True,
    "ev_continuous_charging": False,
}


def _parse(ocean: EcoFlowDeviceCoordinator, label: str) -> dict[str, Any]:
    topic = f"/app/user123/{ocean.device_sn}/thing/property"
    parsed = ocean._parse_message(topic, bytes.fromhex(FRAMES[label]["hex"]))
    assert parsed is not None
    return parsed


def _pp1(ocean: EcoFlowDeviceCoordinator, label: str) -> dict[str, Any]:
    """A real frame with the masked serial replaced by a PowerPulse 1 dummy."""
    parsed = _parse(ocean, label)
    parsed["ev_charger_sn"] = PP1_SN
    return parsed


def _ocean(hass: HomeAssistant, *extra: dict[str, Any]) -> EcoFlowDeviceCoordinator:
    entry = _entry(hass, [POWEROCEAN_DEVICE, *extra])
    ocean = EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN_DEVICE)
    ocean._mqtt_client = _connected_mqtt()
    coordinators = {POWEROCEAN_DEVICE["sn"]: ocean}
    for device in extra:
        coordinators[device["sn"]] = EcoFlowDeviceCoordinator(hass, entry, device)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinators
    return ocean


async def test_parser_reads_the_start_state_with_types(hass: HomeAssistant) -> None:
    """Mutation probe: `bool(value & 1)` -> `value` for the switch bits leaves
    an int where a bool is expected and the `type(...) is bool` check fails."""
    parsed = _parse(_ocean(hass), "get_all_reply_start")
    assert {k: parsed.get(k) for k in START} == START
    assert type(parsed["ev_charge_mode_raw"]) is int
    for key in ("ev_plug_and_play", "ev_battery_discharge_block"):
        assert type(parsed[key]) is bool
    assert type(parsed["ev_continuous_charging"]) is bool


@pytest.mark.parametrize(
    ("label", "moved"),
    [
        ("report_after_set_f5_1", {"ev_charge_mode_raw": 1}),
        ("report_after_set_f4_1", {"ev_plug_and_play": True}),
        ("report_after_set_f7_1", {"ev_continuous_charging": True}),
        ("report_after_set_f6_0", {"ev_battery_discharge_block": False}),
    ],
)
async def test_each_report_moves_exactly_its_setting(
    hass: HomeAssistant, label: str, moved: dict[str, Any]
) -> None:
    parsed = _parse(_ocean(hass), label)
    expected = {**START, **moved}
    assert {k: parsed[k] for k in SETTINGS} == {k: expected[k] for k in SETTINGS}


@pytest.mark.parametrize(
    ("entity_key", "value", "write", "report"),
    [
        ("ev_plug_and_play", True, "set_f4_1", "report_after_set_f4_1"),
        ("ev_charge_mode", "fast", "set_f5_1", "report_after_set_f5_1"),
        ("ev_continuous_charging", True, "set_f7_1", "report_after_set_f7_1"),
        ("ev_battery_discharge_block", False, "set_f6_0", "report_after_set_f6_0"),
    ],
)
async def test_write_sends_the_app_pdata_and_returns_on_the_report(
    hass: HomeAssistant, entity_key: str, value: Any, write: str, report: str
) -> None:
    ocean = _ocean(hass)
    ocean._apply_data(_pp1(ocean, "get_all_reply_start"))

    task = asyncio.create_task(ocean.async_set_wallbox_setting(entity_key, value))
    await asyncio.sleep(0.05)
    sent = _mqtt(ocean).send_proto_set.call_args.args[0]
    (header,), _ = decode_header_message(sent)
    (app,), _ = decode_header_message(bytes.fromhex(FRAMES[write]["hex"]))
    # The write addresses the reported serial; the app frame carries the mask.
    pp1_hex, mask_hex = PP1_SN.encode().hex(), ("X" * 16).encode().hex()
    assert header["pdata"].count(pp1_hex) == 1
    assert header["pdata"].replace(pp1_hex, mask_hex) == app["pdata"]
    assert not task.done()

    ocean._apply_data(_pp1(ocean, report))
    await task
    assert ocean._pending_write is None


async def test_write_without_report_is_not_confirmed(hass: HomeAssistant) -> None:
    """The ack proves nothing: without a 209/8 the write fails."""
    ocean = _ocean(hass)
    ocean._apply_data(_pp1(ocean, "get_all_reply_start"))
    with (
        patch(WINDOW, 0.05),
        pytest.raises(HomeAssistantError) as err,
    ):
        await ocean.async_set_wallbox_setting("ev_plug_and_play", True)
    assert err.value.translation_key == "wallbox_not_confirmed"
    assert ocean._pending_write is None


async def test_write_without_a_reported_wallbox_is_refused(
    hass: HomeAssistant,
) -> None:
    ocean = _ocean(hass)
    with pytest.raises(HomeAssistantError) as err:
        await ocean.async_set_wallbox_setting("ev_plug_and_play", True)
    assert err.value.translation_key == "wallbox_not_reported"
    assert _mqtt(ocean).send_proto_set.call_count == 0


@pytest.mark.parametrize(
    ("key", "value"), [("ev_plug_and_play", True), ("ev_charge_mode", "fast")]
)
async def test_write_is_refused_once_another_wallbox_reports(
    hass: HomeAssistant, key: str, value: Any
) -> None:
    """A control stays once created, but `ev_charger_sn` also takes a
    PowerPulse 2's serial: the write checks the stored serial, not the entity.

    Mutation probe: dropping the prefix check in `_require_powerpulse1` sends
    the PowerPulse 1 command to the PowerPulse 2.
    """
    ocean = _ocean(hass)
    ocean._apply_data(_pp1(ocean, "get_all_reply_start"))
    ocean.set_device_value("ev_charger_sn", PP2_SN)
    with pytest.raises(HomeAssistantError) as err:
        await ocean.async_set_wallbox_setting(key, value)
    assert err.value.translation_key == "wallbox_not_reported"
    assert _mqtt(ocean).send_proto_set.call_count == 0


@pytest.mark.parametrize(
    ("change", "report"),
    [
        # Another setting moved, plug-and-play still off.
        ("value", "report_after_set_f5_1"),
        # The new value, but from another wallbox's serial.
        ("serial", "report_after_set_f4_1"),
    ],
)
async def test_a_report_without_the_new_value_leaves_the_write_pending(
    hass: HomeAssistant, change: str, report: str
) -> None:
    ocean = _ocean(hass)
    ocean._apply_data(_pp1(ocean, "get_all_reply_start"))
    with patch(WINDOW, 0.3):
        task = asyncio.create_task(
            ocean.async_set_wallbox_setting("ev_plug_and_play", True)
        )
        await asyncio.sleep(0.05)
        parsed = _pp1(ocean, report)
        if change == "serial":
            parsed["ev_charger_sn"] = OTHER_PP1_SN
        ocean._apply_data(parsed)
        await asyncio.sleep(0.05)
        assert not task.done()
        with pytest.raises(HomeAssistantError) as err:
            await task
    assert err.value.translation_key == "wallbox_not_confirmed"


def _ev_entities(found: list[Any], ocean: EcoFlowDeviceCoordinator) -> dict[str, Any]:
    return {
        e._definition.key: e
        for e in found
        if e.coordinator is ocean and e._definition.key.startswith("ev_")
    }


async def _wallbox_entities(
    hass: HomeAssistant, ocean: EcoFlowDeviceCoordinator, serial: Any
) -> tuple[set[str], dict[str, Any]]:
    """Set the platforms up, then deliver the settings report with `serial`
    (`None`: the report carries the settings without a serial).

    Returns the wallbox controls present before the report and after it.
    """
    entry = hass.config_entries.async_entries(DOMAIN)[-1]
    found: list[Any] = []
    await switch_setup(hass, entry, add_entities_collector(found))
    await select_setup(hass, entry, add_entities_collector(found))
    await hass.async_block_till_done()
    before = set(_ev_entities(found, ocean))
    parsed = _parse(ocean, "get_all_reply_start")
    if serial is None:
        del parsed["ev_charger_sn"]
    else:
        parsed["ev_charger_sn"] = serial
    ocean._apply_data(parsed)
    ocean.async_set_updated_data(dict(ocean.device_data))
    await hass.async_block_till_done()
    return before, _ev_entities(found, ocean)


async def test_controls_appear_only_after_a_report(hass: HomeAssistant) -> None:
    before, after = await _wallbox_entities(hass, _ocean(hass), PP1_SN)
    assert before == set()
    assert set(after) == FOUR


@pytest.mark.parametrize("serial", ["X" * 16, PP2_SN])
async def test_no_controls_while_the_reported_wallbox_is_not_a_powerpulse1(
    hass: HomeAssistant, serial: str
) -> None:
    """The masked fixture serial and a PowerPulse 2 serial both arrive with
    the settings, neither names a PowerPulse 1.

    Mutation probe: flipping the prefix comparison in
    `wallbox_control_withheld` creates the controls for these serials.
    """
    _, after = await _wallbox_entities(hass, _ocean(hass), serial)
    assert after == {}


@pytest.mark.parametrize("serial", [None, 12345])
async def test_no_controls_while_the_report_names_no_wallbox_serial(
    hass: HomeAssistant, serial: Any
) -> None:
    """Settings arrive without a usable serial (a report with the settings
    but no `ev_charger_sn`, or one that is not a string): no control.

    Mutation probe: letting a missing or non-string serial through in
    `wallbox_control_withheld` creates the four controls.
    """
    _, after = await _wallbox_entities(hass, _ocean(hass), serial)
    assert after == {}


async def _controls_at_setup(
    hass: HomeAssistant, ocean: EcoFlowDeviceCoordinator, serial: str
) -> dict[str, Any]:
    """Deliver the settings report with `serial`, then set the platforms up."""
    parsed = _parse(ocean, "get_all_reply_start")
    parsed["ev_charger_sn"] = serial
    ocean._apply_data(parsed)
    entry = hass.config_entries.async_entries(DOMAIN)[-1]
    found: list[Any] = []
    await switch_setup(hass, entry, add_entities_collector(found))
    await select_setup(hass, entry, add_entities_collector(found))
    await hass.async_block_till_done()
    return _ev_entities(found, ocean)


async def test_setup_creates_the_controls_for_a_reported_powerpulse1(
    hass: HomeAssistant,
) -> None:
    assert set(await _controls_at_setup(hass, _ocean(hass), PP1_SN)) == FOUR


async def test_setup_withholds_the_controls_for_a_reported_powerpulse2(
    hass: HomeAssistant,
) -> None:
    """The settings are already in the store when the platforms set up.

    Mutation probe: dropping the `wallbox_control_withheld` term from the
    setup loop of `switch.py` or `select.py` creates that platform's
    controls here.
    """
    assert await _controls_at_setup(hass, _ocean(hass), PP2_SN) == {}


async def test_controls_follow_the_wallbox_not_the_entry(
    hass: HomeAssistant,
) -> None:
    """A PowerPulse 2 elsewhere in the entry does not take the PowerPulse 1's
    controls away: the reporting wallbox decides, not the entry."""
    _, after = await _wallbox_entities(hass, _ocean(hass, POWERPULSE2_DEVICE), PP1_SN)
    assert set(after) == FOUR


@pytest.mark.parametrize(
    ("key", "action", "expected"),
    [
        ("ev_plug_and_play", "on", True),
        ("ev_continuous_charging", "off", False),
        ("ev_battery_discharge_block", "on", True),
        ("ev_charge_mode", "fast", "fast"),
        ("ev_charge_mode", "solar", "solar"),
    ],
)
async def test_entity_hands_the_write_to_the_coordinator(
    hass: HomeAssistant, key: str, action: str, expected: Any
) -> None:
    """The entity writes nothing of its own: no optimistic value, the report
    moves the state.

    Mutation probe: matching the switch branch on an empty key set sends the
    switches down the generic path, which never calls the wallbox write.
    """
    ocean = _ocean(hass)
    _, after = await _wallbox_entities(hass, ocean, PP1_SN)
    entity = after[key]
    with patch.object(ocean, "async_set_wallbox_setting", AsyncMock()) as write:
        if action == "on":
            await entity.async_turn_on()
        elif action == "off":
            await entity.async_turn_off()
        else:
            await entity.async_select_option(action)
    write.assert_awaited_once_with(key, expected)
    assert entity._optimistic_value is None


async def test_diagnostics_mask_the_wallbox_serial(hass: HomeAssistant) -> None:
    """`ev_charger_sn` is new in the PowerOcean store. The export lists store
    keys by name only; the one PowerOcean section that carries values is the
    raw quota, so the serial is placed there too and must come out masked.

    Mutation probe: returning the export without `_redact_serials` leaks it.
    """
    ocean = _ocean(hass)
    ocean.set_device_value("ev_charger_sn", PP1_SN)
    ocean._raw_quota = {"ev_charger_sn": PP1_SN}
    _mqtt(ocean).last_connect_time = 0.0  # the export compares it to 0
    entry = hass.config_entries.async_entries(DOMAIN)[-1]
    result = await async_get_config_entry_diagnostics(hass, entry)
    (device,) = result["devices"]
    assert "ev_charger_sn" in device["data_keys"]
    assert "ev_charger_sn" in device["raw_quota"]["values"]
    assert PP1_SN not in json.dumps(result, default=str)
