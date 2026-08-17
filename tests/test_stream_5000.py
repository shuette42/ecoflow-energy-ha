"""Tests for the STREAM 5000 (ES21) variant of the STREAM AC 5000 family.

The ES21 reads with the ES22 parser and is not written to. Both halves are
pinned here: the read path against real frames from issue #231, the write
path against the allowlist that keeps controls off a variant no frame has
confirmed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ecoflow_energy.const import (
    STREAM_AC5000_CONTROL_PREFIXES,
    STREAMAC5000_NUMBERS,
    STREAMAC5000_SELECTS,
    STREAMAC5000_SWITCHES,
    supports_stream_ac5000_controls,
)
from ecoflow_energy.ecoflow.const import (
    DEVICE_TYPE_STREAM_AC5000,
    _SN_PREFIX_MAP,
    get_device_name,
    get_device_type,
)
from ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import (
    parse_stream_ac5000_message,
)
from ecoflow_energy.number import _get_number_defs
from ecoflow_energy.select import _get_select_defs
from ecoflow_energy.switch import _get_switch_defs

FIXTURES = Path(__file__).parent / "fixtures" / "stream_ac5000"
ES21_FRAMES = FIXTURES / "es21_frames_masked.json"

ES21_SN = "ES21ZE1B2J5P0137"
ES22_SN = "ES22ZE1B2J5P0137"


def _frames() -> list[dict]:
    return json.loads(ES21_FRAMES.read_text(encoding="utf-8"))["frames"]


def _parse_all() -> dict[str, object]:
    """Replay every captured frame the way the coordinator would."""
    state: dict[str, object] = {}
    for frame in _frames():
        parsed = parse_stream_ac5000_message(bytes.fromhex(frame["hex"]))
        if parsed:
            state.update(parsed)
    return state


class TestRouting:
    """An ES21 reaches the STREAM AC 5000 parser and carries its own name."""

    def test_the_prefix_routes_to_the_stream_ac5000_device_type(self) -> None:
        assert get_device_type("", ES21_SN) == DEVICE_TYPE_STREAM_AC5000

    def test_the_name_is_the_one_the_app_registry_uses(self) -> None:
        # The app API returns an empty product name for this family, so the
        # name comes from the prefix table. "STREAM 5000" is the product's
        # own name, not one derived from the ES22 it shares a parser with.
        assert get_device_name("", ES21_SN) == "STREAM 5000 (0137)"

    def test_a_reported_product_name_still_wins(self) -> None:
        assert get_device_name("EcoFlow STREAM 5000", ES21_SN) == (
            "EcoFlow STREAM 5000"
        )


class TestReadPath:
    """The captured frames decode with the ES22 parser, unchanged."""

    def test_every_captured_frame_is_understood(self) -> None:
        # Frame by frame rather than keyed by command: three of the six
        # frames are 254/39 deltas, and a dict would keep only the last,
        # letting the other two regress to None unnoticed.
        for frame in _frames():
            cmd = (frame["cmds"][0]["cmd_func"], frame["cmds"][0]["cmd_id"])
            parsed = parse_stream_ac5000_message(bytes.fromhex(frame["hex"]))
            if cmd == (254, 40):
                # The one family this parser maps nothing from: its
                # containers (f60-f62) are unidentified on either model,
                # see the parser docstring. Pinned so mapping them later
                # is a conscious change here too.
                assert parsed is None
            else:
                assert parsed, f"frame {cmd} produced nothing"

    @pytest.mark.parametrize(
        "key",
        [
            "soc_pct",
            "batt_voltage_v",
            "batt_temp_c",
            "bms_soh_pct",
            "batt_remain_cap_mah",
            "grid_export_power_w",
            "grid_import_power_w",
            "home_w",
            "home_from_batt_w",
            "max_charge_soc_pct",
            "min_discharge_soc_pct",
        ],
    )
    def test_the_reading_is_present(self, key: str) -> None:
        assert key in _parse_all()

    def test_the_flow_model_closes(self) -> None:
        """Readings from independent message families agree with each other.

        This is the check that separates "the bytes parsed" from "the parser
        belongs to this device", and both sides of each comparison must come
        from different wire fields for it to check anything. The derived
        battery keys are deliberately not used here: `batt_discharge_power_w`
        is built in `_finalize` from the same `f12` edges an identity would
        compare it against, so that identity holds by construction even with
        a wrong field map.
        """
        state = _parse_all()
        # The `f11.1` home node total against the `f12` edges feeding home.
        home_total = state["home_w"]
        home_edges = state["home_from_batt_w"] + state["home_from_grid_w"]
        assert abs(home_total - home_edges) <= 10
        # The grid meter block (`f15.3`) against the balance derived from
        # the `f12` grid edges. A swapped edge pair flips the sign here.
        grid_meter = state["grid_w"]
        grid_edges = state["grid_import_power_w"] - state["grid_export_power_w"]
        assert abs(grid_meter - grid_edges) <= 2

    def test_the_soc_limits_are_a_usable_range(self) -> None:
        state = _parse_all()
        assert 0 <= state["min_discharge_soc_pct"] < state["max_charge_soc_pct"] <= 100


# The identifier guard for this fixture, and every other one, is in
# `test_fixture_identifiers.py`.


class TestControlsStayOff:
    """Reading the same telemetry is not evidence a write is accepted."""

    @pytest.mark.parametrize(
        "get_defs", [_get_number_defs, _get_switch_defs, _get_select_defs]
    )
    def test_an_es21_gets_no_controls(self, get_defs) -> None:
        assert get_defs(DEVICE_TYPE_STREAM_AC5000, ES21_SN) == []

    @pytest.mark.parametrize(
        ("get_defs", "expected"),
        [
            (_get_number_defs, STREAMAC5000_NUMBERS),
            (_get_switch_defs, STREAMAC5000_SWITCHES),
            (_get_select_defs, STREAMAC5000_SELECTS),
        ],
    )
    def test_an_es22_keeps_its_controls(self, get_defs, expected) -> None:
        # Identity, not truthiness: a getter that lost part of the list
        # would still be truthy.
        assert get_defs(DEVICE_TYPE_STREAM_AC5000, ES22_SN) is expected

    def test_an_unknown_serial_gets_no_controls(self) -> None:
        # The default matters more than the ES21 case: it is what a prefix
        # added later inherits before anyone has thought about it.
        assert not supports_stream_ac5000_controls("")
        assert not supports_stream_ac5000_controls("ES29ZE1B2J5P0137")

    def test_every_family_prefix_was_decided_on(self) -> None:
        """Adding a prefix without deciding about writes fails here.

        The allowlist is not self-enforcing: a new prefix silently inherits
        "no controls", which is the safe default but also a silent one. This
        pins the split so the choice is visible in a diff.
        """
        family = {
            prefix
            for prefix, device_type in _SN_PREFIX_MAP.items()
            if device_type == DEVICE_TYPE_STREAM_AC5000
        }
        read_only = family - STREAM_AC5000_CONTROL_PREFIXES
        assert family == {"ES21", "ES22"}
        assert STREAM_AC5000_CONTROL_PREFIXES == {"ES22"}
        assert read_only == {"ES21"}
