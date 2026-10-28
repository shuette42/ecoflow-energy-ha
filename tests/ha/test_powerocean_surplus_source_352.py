"""Which wire field feeds the PowerOcean surplus value (#631).

A PowerOcean Plus (R372) reports the solar surplus in 96/8 field 352, a JTS1
unit in 96/13 `dev_soc`. Both land in `ems_app_surplus_pct`; 96/13 owns it and
352 fills it only on a device that never sent a 96/13 `dev_soc`. Frames are
pushed through the real parse and apply path, and the assertions read the state
the entities read.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
    JTS1EmsChangeReport,
    JTS1EmsParamChangeReport,
)
from tests.test_powerocean_timer_task import _header

from .conftest import MOCK_POWEROCEAN_DEVICE

_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "powerocean_plus_r372_96_8_dev_soc.json"
)
_TOPIC = f"/app/user123/{MOCK_POWEROCEAN_DEVICE['sn']}/thing/property/get_reply"
_CLOCK = "custom_components.ecoflow_energy.coordinator.time.monotonic"
_SURPLUS = "ems_app_surplus_pct"
_INTERMEDIATE = "ems_change_dev_soc_pct"


def _fixture_pdata(index: int) -> bytes:
    frames = json.loads(_FIXTURE.read_text())["frames"]
    (frame,) = [f for f in frames if f["index"] == index]
    return bytes.fromhex(frame["pdata_hex"])


def _change_352(value: int) -> bytes:
    """A 96/8 frame carrying only field 352."""
    return _header(
        96, 8, JTS1EmsChangeReport(ems_change_dev_soc=value).SerializeToString()
    )


def _param_change_96_13(value: int) -> bytes:
    """A 96/13 frame carrying `dev_soc`."""
    return _header(96, 13, JTS1EmsParamChangeReport(dev_soc=value).SerializeToString())


def _coordinator(
    hass: HomeAssistant, entry: MockConfigEntry
) -> EcoFlowDeviceCoordinator:
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, MOCK_POWEROCEAN_DEVICE)
    coordinator._enhanced_mode = True
    return coordinator


def _push(
    coordinator: EcoFlowDeviceCoordinator,
    payload: bytes,
    *,
    own_connection: bool = True,
    clock: float = 5000.0,
) -> None:
    """Parse one frame and hand it to the apply path, as the MQTT thread does."""
    parsed = coordinator._parse_message(_TOPIC, payload)
    assert parsed
    with patch(_CLOCK, return_value=clock):
        coordinator._apply_data(parsed, own_connection=own_connection)


async def test_surplus_from_352_when_no_96_13(
    hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
) -> None:
    """The reporter's surplus-70 frame becomes the surplus value, and only that."""
    coordinator = _coordinator(hass, enhanced_config_entry)

    _push(coordinator, _header(96, 8, _fixture_pdata(55)))

    assert coordinator._device_data[_SURPLUS] == 70
    assert _INTERMEDIATE not in coordinator._device_data
    assert coordinator.surplus_source_diagnostics == {
        "surplus_sources_both": False,
        "dev_soc_352_unapplied": 0,
    }


async def test_surplus_source_precedence(
    hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
) -> None:
    """96/13 owns the value, whichever frame came first."""
    # 96/13 first: the later 352 does not move it.
    first = _coordinator(hass, enhanced_config_entry)
    _push(first, _param_change_96_13(40))
    assert first._device_data[_SURPLUS] == 40
    assert first.surplus_source_diagnostics is None

    _push(first, _change_352(70))

    assert first._device_data[_SURPLUS] == 40
    assert _INTERMEDIATE not in first._device_data
    assert first.surplus_source_diagnostics == {
        "surplus_sources_both": True,
        "dev_soc_352_unapplied": 1,
    }

    # 352 first: it applies until the 96/13 arrives, then 96/13 wins for good.
    second = _coordinator(hass, enhanced_config_entry)
    _push(second, _change_352(70))
    assert second._device_data[_SURPLUS] == 70
    assert second.surplus_source_diagnostics == {
        "surplus_sources_both": False,
        "dev_soc_352_unapplied": 0,
    }

    _push(second, _param_change_96_13(40))
    assert second._device_data[_SURPLUS] == 40
    assert second.surplus_source_diagnostics == {
        "surplus_sources_both": True,
        "dev_soc_352_unapplied": 0,
    }

    _push(second, _change_352(55))
    assert second._device_data[_SURPLUS] == 40
    assert second.surplus_source_diagnostics == {
        "surplus_sources_both": True,
        "dev_soc_352_unapplied": 1,
    }


async def test_auto_sync_inert_on_352_only_device(
    hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
) -> None:
    """Reading 352 never arms the ADR-013 auto-sync, 96/13 still does."""
    # Surplus 70 against a reported field 15 of 90 and a backup floor of 10:
    # the divergent pair the auto-sync exists to correct.
    divergent = JTS1EmsChangeReport(
        ems_change_dev_soc=70, sys_bat_backup_ratio=90, sys_bat_dsg_down_limit=10
    ).SerializeToString()

    only_352 = _coordinator(hass, enhanced_config_entry)
    with patch.object(
        only_352, "_schedule_powerocean_soc_write", MagicMock()
    ) as write_352:
        _push(only_352, _header(96, 8, divergent))
    assert only_352._device_data[_SURPLUS] == 70
    assert only_352._device_data["ems_backup_ratio_pct"] == 90
    assert only_352._last_ems_param_change_ts == 0.0
    write_352.assert_not_called()

    # Positive control: the same pair arriving as a 96/13 does schedule it.
    with_96_13 = _coordinator(hass, enhanced_config_entry)
    with patch.object(
        with_96_13, "_schedule_powerocean_soc_write", MagicMock()
    ) as write_96_13:
        _push(
            with_96_13,
            _header(
                96,
                8,
                JTS1EmsChangeReport(
                    sys_bat_backup_ratio=90, sys_bat_dsg_down_limit=10
                ).SerializeToString(),
            ),
        )
        _push(with_96_13, _param_change_96_13(70))
    write_96_13.assert_called_once()


async def test_dev_soc_352_out_of_range_is_none(
    hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
) -> None:
    """A 352 outside 0..100 is a value the device never populated."""
    coordinator = _coordinator(hass, enhanced_config_entry)

    _push(coordinator, _change_352(4294967295))

    assert _SURPLUS in coordinator._device_data
    assert coordinator._device_data[_SURPLUS] is None
    assert _INTERMEDIATE not in coordinator._device_data


async def test_352_from_relayed_unit_is_ignored(
    hass: HomeAssistant, enhanced_config_entry: MockConfigEntry
) -> None:
    """A neighbour's 352 is not copied and leaves no trace on this device."""
    coordinator = _coordinator(hass, enhanced_config_entry)
    parsed = coordinator._parse_message(_TOPIC, _header(96, 8, _fixture_pdata(55)))
    assert parsed
    assert _INTERMEDIATE in parsed

    with patch(_CLOCK, return_value=5000.0):
        coordinator._apply_data(parsed, own_connection=False)

    assert _SURPLUS not in coordinator._device_data
    assert _INTERMEDIATE not in coordinator._device_data
    assert coordinator.surplus_source_diagnostics is None
