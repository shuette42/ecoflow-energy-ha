"""Start Charging and Stop Charging for a PowerPulse 1 on a PowerOcean (#731).

Reports are the reporter's real frames
(`tests/fixtures/powerpulse/r372_start_stop_731_20261114.json`, two starts and
two stops of the app); the coordinator keeps a dummy PowerPulse 1 serial and
`_frame` swaps the recording's masked serial for it after parsing. The button
writes the app's `209/100` on the PowerOcean topic and returns only once the
wallbox's charging status follows: the order state report that comes first does
not count.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.button import async_setup_entry as button_setup
from custom_components.ecoflow_energy.const import (
    CONF_ACCESS_KEY,
    CONF_DEVICES,
    CONF_MODE,
    CONF_SECRET_KEY,
    DOMAIN,
    MODE_STANDARD,
    POWERPULSE2_BUTTONS,
    filter_defs_for_serial,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.energy_stream import (
    build_wallbox_charge_action_payload,
)
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)
from tests.ha.conftest import add_entities_collector
from tests.ha.test_powerpulse2_charge_action import POWEROCEAN_DEVICE, _mqtt
from tests.ha.test_powerpulse2_controls import POWEROCEAN2_DEVICE
from tests.ha.test_wallbox_powerpulse1_controls import (
    OTHER_PP1_SN,
    PP1_SN,
    PP2_SN,
    WINDOW,
    _ocean,
)

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "powerpulse"
    / "r372_start_stop_731_20261114.json"
)
FRAMES = {f["label"]: f for f in json.loads(FIXTURE.read_text())["frames"]}
MASK = "X" * 16  # the serial of every frame in the recording
IDS = ("ev_start_charging", "ev_stop_charging")


def _frame(ocean: EcoFlowDeviceCoordinator, label: str) -> dict[str, Any]:
    """A recorded frame as the coordinator parses it, with one replacement.

    The frame bytes are exactly as recorded. The recording masked the wallbox
    serial as sixteen X; after parsing, that serial is replaced by the dummy
    PowerPulse 1 serial the coordinator holds, because the press is refused
    for a serial that is not a PowerPulse 1 and a confirmation must match the
    serial the write was addressed to.
    """
    topic = f"/app/user123/{ocean.device_sn}/thing/property"
    parsed = ocean._parse_message(topic, bytes.fromhex(FRAMES[label]["hex"]))
    assert parsed is not None
    if parsed.get("ev_charger_sn") == MASK:
        parsed["ev_charger_sn"] = PP1_SN
    return parsed


def _assert_addressed(
    ocean: EcoFlowDeviceCoordinator, sent: bytes, action: str
) -> dict[str, Any]:
    """The 209/100 names the wallbox in `pdata` field 1 and the PowerOcean as
    the device, and the two serials differ, so a swap cannot pass.
    """
    (header,), _ = decode_header_message(sent)
    assert (header["cmd_func"], header["cmd_id"]) == (209, 100)
    assert ocean.device_sn != PP1_SN
    control = "02" if action == "start" else "01"
    assert header["pdata"] == "0a10" + PP1_SN.encode().hex() + "10" + control
    assert header["device_sn"] == ocean.device_sn
    return header


def _report(ocean: EcoFlowDeviceCoordinator, status: str, serial: str) -> None:
    """Put the wallbox's last report in the store, as an earlier frame would."""
    ocean.set_device_value("ev_charge_status", status)
    ocean.set_device_value("ev_charger_sn", serial)


async def _collect(hass: HomeAssistant, ocean: EcoFlowDeviceCoordinator) -> list[Any]:
    """Set the button platform up and return every entity it added (a list, so
    a duplicate stays visible)."""
    entry = hass.config_entries.async_entries(DOMAIN)[-1]
    found: list[Any] = []
    await button_setup(hass, entry, add_entities_collector(found))
    await hass.async_block_till_done()
    return found


async def _buttons(
    hass: HomeAssistant, ocean: EcoFlowDeviceCoordinator
) -> dict[str, Any]:
    """Set the button platform up and return the PowerOcean's buttons by key."""
    found = await _collect(hass, ocean)
    return {e._definition.key: e for e in found if e.coordinator is ocean}


async def _pressed(
    hass: HomeAssistant, status: str, action: str
) -> tuple[EcoFlowDeviceCoordinator, Any, asyncio.Task[None]]:
    """A PowerOcean whose PowerPulse 1 reports `status`, and the press of `action`.

    The store keeps the dummy PowerPulse 1 serial throughout, the one the
    frames carry after `_frame` and a confirmation must match.
    """
    ocean = _ocean(hass)
    _report(ocean, status, PP1_SN)
    buttons = await _buttons(hass, ocean)
    button = buttons[f"ev_{action}_charging"]
    task = asyncio.create_task(button.async_press())
    await asyncio.sleep(0.05)
    return ocean, button, task


# --- gate ---


async def test_no_buttons_before_the_wallbox_reports(hass: HomeAssistant) -> None:
    ocean = _ocean(hass)
    assert await _buttons(hass, ocean) == {}


async def test_no_buttons_for_a_powerpulse2_reported_by_the_powerocean(
    hass: HomeAssistant,
) -> None:
    """The PowerPulse 2 device keeps the only pair.

    Mutation probe: removing the two button keys from `POWERPULSE1_CONTROL_KEYS`
    lets `wallbox_control_withheld` pass them, so this case gets two buttons.
    """
    ocean = _ocean(hass)
    _report(ocean, "finishing", PP2_SN)
    assert await _buttons(hass, ocean) == {}


async def test_a_powerpulse1_gets_exactly_the_two_buttons(hass: HomeAssistant) -> None:
    ocean = _ocean(hass)
    _report(ocean, "finishing", PP1_SN)
    found = await _collect(hass, ocean)
    # A second report must not add a second pair.
    ocean.async_set_updated_data(dict(ocean.device_data))
    await hass.async_block_till_done()
    mine = [e for e in found if e.coordinator is ocean]
    assert sorted(e._definition.key for e in mine) == sorted(IDS)
    assert sorted(e.unique_id for e in mine) == [
        f"{POWEROCEAN_DEVICE['sn']}_ev_start_charging",
        f"{POWEROCEAN_DEVICE['sn']}_ev_stop_charging",
    ]


async def test_the_buttons_follow_the_first_report(hass: HomeAssistant) -> None:
    """Nothing at setup, the two buttons once a PowerPulse 1 reports."""
    ocean = _ocean(hass)
    entry = hass.config_entries.async_entries(DOMAIN)[-1]
    found: list[Any] = []
    await button_setup(hass, entry, add_entities_collector(found))
    assert found == []
    _report(ocean, "finishing", PP1_SN)
    ocean.async_set_updated_data(dict(ocean.device_data))
    await hass.async_block_till_done()
    assert sorted(e._definition.key for e in found) == sorted(IDS)
    # The next report finds both created and adds nothing.
    ocean.async_set_updated_data(dict(ocean.device_data))
    await hass.async_block_till_done()
    assert sorted(e._definition.key for e in found) == sorted(IDS)


async def test_standard_mode_gets_no_buttons(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_ACCESS_KEY: "test_ak",
            CONF_SECRET_KEY: "test_sk",
            CONF_MODE: MODE_STANDARD,
            CONF_DEVICES: [POWEROCEAN_DEVICE],
        },
        unique_id="test_ak",
    )
    entry.add_to_hass(hass)
    ocean = EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN_DEVICE)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {POWEROCEAN_DEVICE["sn"]: ocean}
    _report(ocean, "finishing", PP1_SN)
    assert not ocean.enhanced_mode
    assert await _buttons(hass, ocean) == {}


def test_a_powerocean_prefix_keeps_both_buttons() -> None:
    keys = {
        d.key
        for d in filter_defs_for_serial(POWERPULSE2_BUTTONS, POWEROCEAN_DEVICE["sn"])
    }
    assert keys == set(IDS)


async def test_available_does_not_depend_on_the_charge_action_route(
    hass: HomeAssistant,
) -> None:
    """Two PowerOceans in one entry leave `charge_action_route()` empty; the
    PowerOcean's own buttons stay available.

    Mutation probe: falling through to the route check makes them unavailable.
    """
    ocean = _ocean(hass, POWEROCEAN2_DEVICE)
    _report(ocean, "finishing", PP1_SN)
    assert ocean.charge_action_route() is None
    buttons = await _buttons(hass, ocean)
    assert set(buttons) == set(IDS)
    assert all(b.available for b in buttons.values())
    cast(MagicMock, ocean._mqtt_client).is_connected.return_value = False
    assert not any(b.available for b in buttons.values())


# --- press ---


async def test_start_returns_on_the_status_not_the_order_state(
    hass: HomeAssistant,
) -> None:
    """The order state report (209/8, 0.55 s) does not confirm; the status
    report 4 s later does. One publish, the builder's 209/100, and nothing
    after it: the app's trailing parameter write is not sent.

    Mutation probe: adding `suspended_charger` to the stop-confirm set (the
    stop test) and dropping `validate` (the refusal test) fail this file.
    """
    ocean, _, task = await _pressed(hass, "finishing", "start")
    assert _mqtt(ocean).send_proto_set.call_count == 1
    sent = _mqtt(ocean).send_proto_set.call_args.args[0]
    header = _assert_addressed(ocean, sent, "start")
    assert sent == build_wallbox_charge_action_payload(
        PP1_SN, ocean.device_sn, action="start", seq=header["seq"]
    )

    ocean._apply_data(_frame(ocean, "report_order_state_1_after_start_1"))
    await asyncio.sleep(0.05)
    assert ocean.device_data["ev_charge_status"] == "finishing"
    assert not task.done()

    ocean._apply_data(_frame(ocean, "report_status_4_after_start_1"))
    await task
    assert ocean.device_data["ev_charge_status"] == "suspended_charger"
    assert ocean._pending_write is None
    assert _mqtt(ocean).send_proto_set.call_count == 1


async def test_stop_returns_on_the_status_not_the_order_state(
    hass: HomeAssistant,
) -> None:
    """The order state report 1.3 s after a stop still carries the running
    session; only the status report 7 s later ends it.

    Mutation probe: adding `suspended_charger` to the stop-confirm set in
    `POWERPULSE2_CHARGE_ACTION_CONFIRMED` lets the order state report finish
    the press.
    """
    ocean, _, task = await _pressed(hass, "suspended_charger", "stop")
    sent = _mqtt(ocean).send_proto_set.call_args.args[0]
    header = _assert_addressed(ocean, sent, "stop")
    assert sent == build_wallbox_charge_action_payload(
        PP1_SN, ocean.device_sn, action="stop", seq=header["seq"]
    )

    ocean._apply_data(_frame(ocean, "report_order_state_3_after_stop_1"))
    await asyncio.sleep(0.05)
    assert ocean.device_data["ev_charge_status"] == "suspended_charger"
    assert not task.done()

    ocean._apply_data(_frame(ocean, "report_status_6_after_stop_1"))
    await task
    assert ocean.device_data["ev_charge_status"] == "finishing"
    assert ocean._pending_write is None
    assert _mqtt(ocean).send_proto_set.call_count == 1


@pytest.mark.parametrize(
    ("status", "action"),
    [("suspended_charger", "start"), ("charging", "start"), ("finishing", "stop")],
)
async def test_a_press_in_the_wrong_state_sends_nothing(
    hass: HomeAssistant, status: str, action: str
) -> None:
    """Start while a session is open, stop while none is: refused up front.

    Mutation probe: dropping the `validate` precondition sends all three.
    """
    ocean = _ocean(hass)
    _report(ocean, status, PP1_SN)
    button = (await _buttons(hass, ocean))[f"ev_{action}_charging"]
    with pytest.raises(HomeAssistantError) as err:
        await button.async_press()
    assert err.value.translation_key == "powerpulse_action_state"
    assert _mqtt(ocean).send_proto_set.call_count == 0
    assert ocean._pending_write is None


async def test_no_report_inside_the_window_is_not_confirmed(
    hass: HomeAssistant,
) -> None:
    """The acknowledgement proves nothing, the wallbox's status does.

    Mutation probe: raising `wallbox_not_confirmed` (the settings text, "did
    not report the new setting") changes the key and fails here.
    """
    with patch(WINDOW, 0.05):
        _, _, task = await _pressed(hass, "finishing", "start")
        with pytest.raises(HomeAssistantError) as err:
            await task
    assert err.value.translation_key == "powerpulse_action_not_confirmed"


async def test_a_report_of_another_wallbox_does_not_confirm(
    hass: HomeAssistant,
) -> None:
    with patch(WINDOW, 0.3):
        ocean, _, task = await _pressed(hass, "finishing", "start")
        other = _frame(ocean, "report_status_4_after_start_1")
        other["ev_charger_sn"] = OTHER_PP1_SN
        ocean._apply_data(other)
        await asyncio.sleep(0.05)
        assert not task.done()
        with pytest.raises(HomeAssistantError) as err:
            await task
    assert err.value.translation_key == "powerpulse_action_not_confirmed"


@pytest.mark.parametrize("action", ["start", "stop"])
async def test_a_press_without_a_reported_status_sends_nothing(
    hass: HomeAssistant, action: str
) -> None:
    """The button exists, then the status reads None: refused up front.

    Mutation probe: letting a missing status pass the precondition sends both.
    """
    ocean = _ocean(hass)
    _report(ocean, "finishing", PP1_SN)
    button = (await _buttons(hass, ocean))[f"ev_{action}_charging"]
    ocean._device_data["ev_charge_status"] = None
    with pytest.raises(HomeAssistantError) as err:
        await button.async_press()
    assert err.value.translation_key == "powerpulse_action_state"
    assert _mqtt(ocean).send_proto_set.call_count == 0
    assert ocean._pending_write is None


async def test_the_buttons_refuse_once_the_reported_wallbox_is_a_powerpulse2(
    hass: HomeAssistant,
) -> None:
    """Buttons made for a PowerPulse 1 stay when `ev_charger_sn` later names a
    PowerPulse 2: they go unavailable and a direct press publishes nothing.

    Mutation probes: removing the serial check from the press validator makes
    the press publish; dropping `not wallbox_control_withheld` from `available`
    keeps them available.
    """
    ocean = _ocean(hass)
    _report(ocean, "finishing", PP1_SN)
    buttons = await _buttons(hass, ocean)
    assert all(b.available for b in buttons.values())
    ocean.set_device_value("ev_charger_sn", PP2_SN)
    assert not any(b.available for b in buttons.values())
    for button in buttons.values():
        with pytest.raises(HomeAssistantError) as err:
            await button.async_press()
        assert err.value.translation_key == "wallbox_not_reported"
    assert _mqtt(ocean).send_proto_set.call_count == 0
    assert ocean._pending_write is None


async def test_a_second_press_while_one_is_pending_is_refused(
    hass: HomeAssistant,
) -> None:
    """Mutation probe: dropping the pending-write check sends a second 209/100."""
    ocean, button, task = await _pressed(hass, "finishing", "start")
    with pytest.raises(HomeAssistantError) as err:
        await button.async_press()
    assert err.value.translation_key == "accessory_write_in_progress"
    assert _mqtt(ocean).send_proto_set.call_count == 1
    ocean._apply_data(_frame(ocean, "report_status_4_after_start_1"))
    await task
    assert _mqtt(ocean).send_proto_set.call_count == 1


@pytest.mark.parametrize(
    ("status", "action", "confirmed"),
    [("preparing", "start", "charging"), ("charging", "stop", "available")],
)
async def test_the_other_valid_pairs_are_sent_and_confirmed(
    hass: HomeAssistant, status: str, action: str, confirmed: str
) -> None:
    """Start from `preparing` ends on `charging`, stop from `charging` on
    `available`. No recorded frame shows these statuses: the status values are
    set by hand on a recorded status report, the serial and the rest are the
    recording's.

    Mutation probe: removing `preparing` from the start precondition refuses
    the first case; removing `available` from the stop confirmations leaves the
    second press unconfirmed.
    """
    ocean, _, task = await _pressed(hass, status, action)
    assert _mqtt(ocean).send_proto_set.call_count == 1
    _assert_addressed(ocean, _mqtt(ocean).send_proto_set.call_args.args[0], action)
    assert not task.done()
    report = _frame(ocean, "report_status_4_after_start_1")
    report["ev_charge_status"] = confirmed
    ocean._apply_data(report)
    await task
    assert ocean.device_data["ev_charge_status"] == confirmed
    assert ocean._pending_write is None


async def test_a_status_action_is_not_a_powerglow_write(hass: HomeAssistant) -> None:
    """The shared write body refuses `expected_value=None` for the heating rod.

    Mutation probe: dropping the `kind` check lets the rod path build a
    wallbox-shaped write.
    """
    ocean = _ocean(hass)
    with pytest.raises(ValueError, match="status action"):
        await ocean._async_powerglow_write(
            "start", "ev_charge_status", None, {"action": "start"}, kind="powerglow"
        )
    assert _mqtt(ocean).send_proto_set.call_count == 0
