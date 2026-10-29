"""PowerPulse 2 session energy by source (Ref #634).

Heartbeat fields 45, 46 and 47 carry the energy of the current session from
solar, grid and battery, in Wh like field 42. Checked against the EcoFlow app
on a C371; the three add up to field 42 within a few Wh while a session runs.
The frames are built on the wire and go through the whole header decode, with
distinct numbers per field so a swapped mapping cannot pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ecoflow_energy.const import POWEROCEAN_SENSORS, POWERPULSE2_SENSORS
from ecoflow_energy.ecoflow.parsers.powerpulse_proto import parse_powerpulse_message
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

SOURCE_KEYS = (
    "ev_session_solar_energy_wh",
    "ev_session_grid_energy_wh",
    "ev_session_battery_energy_wh",
)


def _heartbeat(plug_status: int, solar: int, grid: int, battery: int) -> bytes:
    """An unmasked `2/33` heartbeat with a session of 1000 Wh split three ways."""
    pdata = (
        encode_field_varint(1, plug_status)
        + encode_field_varint(40, 1788786565)
        + encode_field_varint(41, 120)
        + encode_field_varint(42, 1000)
        + encode_field_varint(43, 102067)
        + encode_field_varint(44, 103067)
        + encode_field_varint(45, solar)
        + encode_field_varint(46, grid)
        + encode_field_varint(47, battery)
    )
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 2)
        + encode_field_varint(9, 33)
    )
    return encode_field_bytes(1, header)


def _parse(plug_status: int) -> dict[str, Any]:
    result = parse_powerpulse_message(_heartbeat(plug_status, 600, 300, 100))
    assert result is not None
    return result


def test_source_split_decodes_while_a_session_runs() -> None:
    """Fields 45, 46, 47 become solar, grid, battery, each with its own value."""
    result = _parse(3)
    assert result["ev_session_solar_energy_wh"] == 600
    assert result["ev_session_grid_energy_wh"] == 300
    assert result["ev_session_battery_energy_wh"] == 100
    total = sum(result[key] for key in SOURCE_KEYS)
    assert total == result["ev_session_energy_wh"] == 1000
    assert not [key for key in result if key.startswith("_")]


def test_source_split_is_absent_not_zero_without_a_session() -> None:
    """Status 1 ("no session") withholds all three, like the other four."""
    result = _parse(1)
    for key in SOURCE_KEYS:
        assert key not in result, key
    assert "ev_session_energy_wh" not in result
    assert result["ev_total_energy_wh"] == 103067


def test_source_sensors_belong_to_the_powerpulse_2_only() -> None:
    """Three total_increasing Wh energy sensors, not offered to a PowerOcean."""
    by_key = {definition.key: definition for definition in POWERPULSE2_SENSORS}
    for key in SOURCE_KEYS:
        definition = by_key[key]
        assert definition.unit == "Wh", key
        assert definition.device_class == "energy", key
        assert definition.state_class == "total_increasing", key
        assert definition.enhanced_only is True, key
    assert not {definition.key for definition in POWEROCEAN_SENSORS} & set(SOURCE_KEYS)


FIXTURES = Path(__file__).parent / "fixtures" / "powerpulse"
RECORDINGS = (
    "c374_frames_20260911.json",
    "c376_frames.json",
    "c376_settings_480_afternoon_20261004.json",
    "c376_standalone_charge_ctrl_20260912.json",
    "c376_standalone_current_ctrl_20260914.json",
)


def test_the_three_sources_add_up_to_the_session_energy_on_recorded_frames() -> None:
    """On every recorded C374 and C376 frame that carries all of them, field 42
    minus the sum of the sources is 0 to 9 Wh, and 0 once the session ended."""
    checked = ended = 0
    for name in RECORDINGS:
        for frame in json.loads((FIXTURES / name).read_text())["frames"]:
            result = parse_powerpulse_message(bytes.fromhex(frame["hex"]))
            if not result or not all(k in result for k in SOURCE_KEYS):
                continue
            gap = result["ev_session_energy_wh"] - sum(result[k] for k in SOURCE_KEYS)
            assert 0 <= gap <= 9, (name, frame["ts_iso"], gap)
            checked += 1
            if result["ev_session_status"] == "finished":
                assert gap == 0, (name, frame["ts_iso"])
                ended += 1
    assert checked >= 40
    assert ended >= 2


def test_a_frame_without_the_sources_does_not_invent_them() -> None:
    """Field 42 alone yields no source keys, so a stale value is the store's
    business and the parser never fills a missing source with 0."""
    pdata = (
        encode_field_varint(1, 3)
        + encode_field_varint(40, 1788786565)
        + encode_field_varint(41, 120)
        + encode_field_varint(42, 1000)
        + encode_field_varint(43, 102067)
        + encode_field_varint(44, 103067)
    )
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 2)
        + encode_field_varint(9, 33)
    )
    result = parse_powerpulse_message(encode_field_bytes(1, header))
    assert result is not None
    assert result["ev_session_energy_wh"] == 1000
    for key in SOURCE_KEYS:
        assert key not in result, key


def test_source_split_decodes_in_every_status_that_has_a_session() -> None:
    """Status 6 (finished) keeps the split, only status 1 withholds it."""
    result = _parse(6)
    assert result["ev_session_solar_energy_wh"] == 600
    assert result["ev_session_grid_energy_wh"] == 300
    assert result["ev_session_battery_energy_wh"] == 100
