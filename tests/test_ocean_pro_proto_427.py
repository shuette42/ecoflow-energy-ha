"""Tests for the OCEAN Pro (`HR51`) protobuf parser (issue #427).

Real frames come from `tests/fixtures/ocean_pro/hr51_frames_427.json` (two
captures of one installation, serial masked at the source). A hand-built
payload appears only beside a real one and only where the real frames cannot
reach the case; each such test says why in its docstring.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from ecoflow_energy.ecoflow.parsers.ocean_pro_proto import (
    _HR51_EXTRA_254_21,
    _PROPERTY_TREE,
    parse_hr51_proto_message,
)
from ecoflow_energy.ecoflow.parsers.smart_panel_40_proto import (
    _HR61_FIELD_MAP,
    CMS_CORE_FIELD_MAP,
)
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_fixed32,
    encode_field_varint,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "ocean_pro" / "hr51_frames_427.json"
_FRAMES: dict[str, bytes] = {
    f["name"]: bytes.fromhex(f["hex"])
    for f in json.loads(_FIXTURE.read_text())["frames"]
}

_CORE_KEYS = {
    "battery_soc_pct",
    "backup_reserve_pct",
    "grid_power_w",
    "load_power_w",
    "pv_power_w",
    "battery_power_w",
}


def _real(name: str) -> dict[str, Any]:
    parsed = parse_hr51_proto_message(_FRAMES[name])
    assert parsed is not None, f"frame {name} produced no HR51 fields"
    return parsed


def _header(cmd_func: int, cmd_id: int, inner: bytes) -> bytes:
    """One HeaderMessage header (field 1 of the frame) around a payload."""
    body = encode_field_bytes(1, inner)
    body += encode_field_varint(8, cmd_func) + encode_field_varint(9, cmd_id)
    return encode_field_bytes(1, body)


def _frame(cmd_func: int, cmd_id: int, inner: bytes) -> bytes:
    return _header(cmd_func, cmd_id, inner)


def _heartbeat(ts: int, soc: float, chg_wh: int, dsg_wh: int) -> bytes:
    """A `32/177` payload: timestamp, pack SoC, lifetime charge/discharge Wh."""
    return (
        encode_field_varint(2, ts)
        + encode_field_fixed32(10, soc)
        + encode_field_varint(27, chg_wh)
        + encode_field_varint(28, dsg_wh)
    )


class TestCoreKeys:
    @pytest.mark.parametrize("name", ["core_with_soc_and_grid_hz", "full_state_bundle"])
    def test_core_six_decode_and_balance(self, name: str) -> None:
        data = _real(name)
        assert data.keys() >= _CORE_KEYS
        assert data["battery_soc_pct"] == 100.0
        assert data["backup_reserve_pct"] == 50
        assert data["battery_power_w"] == 0.0
        # load = grid + pv - battery, the identity the panel parser also holds
        assert data["load_power_w"] == pytest.approx(
            data["grid_power_w"] + data["pv_power_w"] - data["battery_power_w"],
            abs=0.01,
        )
        strings = [data[f"pv_string_{n}_power_w"] for n in range(1, 6)]
        assert sum(strings) == pytest.approx(data["pv_power_w"], abs=0.01)

    def test_known_values_of_the_first_frame(self) -> None:
        data = _real("core_with_soc_and_grid_hz")
        assert data["grid_power_w"] == pytest.approx(-260.5)
        assert data["pv_power_w"] == pytest.approx(1526.92, abs=0.01)
        assert data["grid_frequency_hz"] == pytest.approx(59.987, abs=0.001)


class TestHr51Extras:
    def test_full_state_frame_carries_stored_energy_legs_and_temperatures(self) -> None:
        data = _real("full_state_bundle")
        assert data["battery_stored_energy_wh"] == 10738
        assert data["inverter_temperature_c"] == 59
        assert data["inverter_ac_l1_voltage_v"] == pytest.approx(121.67, abs=0.01)
        assert data["inverter_ac_l2_voltage_v"] == pytest.approx(121.29, abs=0.01)
        # Negative power = the inverter is delivering
        assert data["inverter_ac_l1_power_w"] < 0
        assert data["inverter_ac_l2_power_w"] < 0
        temps = [data[f"device_temperature_{n}_c"] for n in range(1, 6)]
        assert all(50.0 < t < 65.0 for t in temps)
        assert temps[0] == pytest.approx(58.8, abs=0.01)


class TestHeartbeat:
    def test_newer_real_heartbeat_wins_with_kwh_scale(self) -> None:
        data = _real("heartbeat_pair_full")
        assert data["battery_pack_soc_pct"] == 100.0
        assert data["battery_pack_soh_pct"] == 100.0
        assert data["battery_pack_cycles"] == 28
        assert data["batt_charge_energy_kwh"] == pytest.approx(323.232)
        # The pair holds 302.686 then 302.689 kWh; the newer one is published
        assert data["batt_discharge_energy_kwh"] == pytest.approx(302.689)
        assert "bp_timestamp" not in data

    def test_timestamp_decides_not_frame_order(self) -> None:
        """Beside the real pair (ascending in frame order): the real frame
        cannot tell "newer by timestamp" from "later in the frame", so this
        hand-built one lists the newer heartbeat first."""
        payload = _header(32, 177, _heartbeat(2000, 90.0, 5_000, 7_000)) + _header(
            32, 177, _heartbeat(1000, 80.0, 4_000, 6_000)
        )
        data = parse_hr51_proto_message(payload)
        assert data is not None
        assert data["battery_pack_soc_pct"] == 90.0
        assert data["batt_charge_energy_kwh"] == pytest.approx(5.0)
        assert data["batt_discharge_energy_kwh"] == pytest.approx(7.0)

    @pytest.mark.parametrize("bad_wh", [0, 4_294_967_295])
    @pytest.mark.parametrize(
        ("field_no", "key"),
        [(27, "batt_charge_energy_kwh"), (28, "batt_discharge_energy_kwh")],
    )
    def test_zero_or_placeholder_lifetime_counter_publishes_no_key(
        self, field_no: int, key: str, bad_wh: int
    ) -> None:
        """A `total_increasing` counter at 0 or at the unsigned-32 placeholder
        would read as a meter reset. The real frames only carry good values,
        so the bad ones are hand-built; the pack SoC beside them still comes
        through and the valid sibling counter is kept."""
        other_field = 28 if field_no == 27 else 27
        inner = (
            encode_field_varint(2, 1000)
            + encode_field_fixed32(10, 55.0)
            + encode_field_varint(field_no, bad_wh)
            + encode_field_varint(other_field, 12_345)
        )
        data = parse_hr51_proto_message(_frame(32, 177, inner))
        assert data is not None
        assert key not in data
        assert data["battery_pack_soc_pct"] == 55.0
        other_key = (
            "batt_discharge_energy_kwh" if field_no == 27 else "batt_charge_energy_kwh"
        )
        assert data[other_key] == pytest.approx(12.345)


class TestIncrementalHold:
    def test_frame_without_262_has_no_soc_key_and_the_merge_holds(self) -> None:
        data = _real("core_without_soc")
        assert "battery_soc_pct" not in data
        store: dict[str, Any] = {"battery_soc_pct": 73.0}
        store.update(data)
        assert store["battery_soc_pct"] == 73.0

    def test_soc_at_the_floor_is_published_as_50(self) -> None:
        assert _real("core_soc_at_floor_50")["battery_soc_pct"] == 50.0


class TestRuntimeAndUnusable:
    def test_runtime_frame_yields_only_grid_frequency(self) -> None:
        """254/22 alone, hand-built with a second field (515) that the
        property table maps: only the frequency may come out of 254/22."""
        inner = encode_field_fixed32(641, 60.02) + encode_field_fixed32(515, 100.0)
        data = parse_hr51_proto_message(_frame(254, 22, inner))
        assert data == {"grid_frequency_hz": pytest.approx(60.02, abs=0.001)}

    def test_sentinel_and_non_finite_values_are_dropped(self) -> None:
        """The real frames never carry these (the CT fields are not even
        mapped), so they are hand-built on read fields, each beside one
        good value that must survive."""
        inner = (
            encode_field_fixed32(1476, 16_777_215.0)  # 0xFFFFFF on a float field
            + encode_field_varint(1682, 0xFFFFFF)  # 0xFFFFFF on an int field
            + encode_field_fixed32(1478, math.nan)
            + encode_field_fixed32(1479, math.inf)
            + encode_field_fixed32(1480, -math.inf)
            + encode_field_fixed32(1477, 12.5)
        )
        data = parse_hr51_proto_message(_frame(254, 21, inner))
        assert data == {"pv_string_2_power_w": pytest.approx(12.5)}

    def test_u32_maximum_is_dropped_on_an_int_field(self) -> None:
        """No recorded frame carries 0xFFFFFFFF, so it is hand-built beside a
        good value on the same field family: stored energy must not read
        4.29 GWh, and the neighbouring reading must survive."""
        inner = encode_field_varint(1682, 0xFFFFFFFF) + encode_field_fixed32(1477, 12.5)
        data = parse_hr51_proto_message(_frame(254, 21, inner))
        assert data == {"pv_string_2_power_w": pytest.approx(12.5)}

    def test_safety_only_frame_returns_none(self) -> None:
        assert parse_hr51_proto_message(_FRAMES["safety_only"]) is None


class TestSharedTable:
    def test_core_entries_are_identical_in_hr61_and_hr51_tables(self) -> None:
        assert CMS_CORE_FIELD_MAP, "core table is empty"
        for path, mapping in CMS_CORE_FIELD_MAP.items():
            assert _HR61_FIELD_MAP[path] == mapping
            assert _PROPERTY_TREE[int(path)] == mapping

    def test_hr51_extras_do_not_restate_a_core_field(self) -> None:
        core_names = {m[0] for m in CMS_CORE_FIELD_MAP.values()}
        assert not CMS_CORE_FIELD_MAP.keys() & _HR51_EXTRA_254_21.keys()
        assert not core_names & {m[0] for m in _HR51_EXTRA_254_21.values()}

    def test_core_table_rejects_item_assignment(self) -> None:
        with pytest.raises(TypeError):
            CMS_CORE_FIELD_MAP["9999"] = ("x", "int", 1)  # type: ignore[index]
