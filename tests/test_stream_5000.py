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
        # 254/40 is the one family this parser maps nothing from: its two
        # containers (f60, f62) are unidentified, see the parser docstring.
        decoded = {
            (f["cmds"][0]["cmd_func"], f["cmds"][0]["cmd_id"]): parse_stream_ac5000_message(
                bytes.fromhex(f["hex"])
            )
            for f in _frames()
        }
        assert decoded[(32, 50)], "BMS heartbeat produced nothing"
        assert decoded[(32, 2)], "SoC limit frame produced nothing"
        assert decoded[(254, 39)], "telemetry frame produced nothing"

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
        """What leaves the battery reaches the house and the grid.

        This is the check that separates "the bytes parsed" from "the parser
        belongs to this device". A wrong field map still yields numbers.
        """
        state = _parse_all()
        out_of_battery = state["batt_discharge_power_w"]
        into_house = state["home_from_batt_w"]
        onto_grid = state["grid_export_power_w"]
        assert abs(out_of_battery - (into_house + onto_grid)) <= 2

    def test_the_soc_limits_are_a_usable_range(self) -> None:
        state = _parse_all()
        assert 0 <= state["min_discharge_soc_pct"] < state["max_charge_soc_pct"] <= 100


class TestNoIdentifiersInTheFixture:
    """The fixture ships in a public repo and carries a real device's bytes."""

    def test_no_identifier_survived_masking(self) -> None:
        raw = b"".join(bytes.fromhex(f["hex"]) for f in _frames())
        text = raw.decode("latin1")
        # Three separate shapes, because one regex per shape is what the
        # older guard got wrong: its [A-Z0-9]{15,} could never match a
        # lowercase dashed UUID it was believed to cover.
        assert not re.search(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", text)
        assert not re.search(r"(?:[0-9A-F]{2}[:-]){5}[0-9A-F]{2}", text)
        for run in re.findall(r"[0-9A-Za-z]{6,}", text):
            assert set(run) == {"X"}, f"unmasked identifier-shaped run: {run}"


class TestControlsStayOff:
    """Reading the same telemetry is not evidence a write is accepted."""

    @pytest.mark.parametrize(
        "get_defs", [_get_number_defs, _get_switch_defs, _get_select_defs]
    )
    def test_an_es21_gets_no_controls(self, get_defs) -> None:
        assert get_defs(DEVICE_TYPE_STREAM_AC5000, ES21_SN) == []

    @pytest.mark.parametrize(
        "get_defs", [_get_number_defs, _get_switch_defs, _get_select_defs]
    )
    def test_an_es22_keeps_its_controls(self, get_defs) -> None:
        assert get_defs(DEVICE_TYPE_STREAM_AC5000, ES22_SN)

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
