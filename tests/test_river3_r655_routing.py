"""Tests for RIVER 3 (`R655`) support, read-only, on top of the Delta 3 path (#296).

The frames come from the reporter's diagnostics download of three units
(2026-10-04), copied unchanged into
`tests/fixtures/delta3/r655_frames_issue296.json`; the `unit` field tags them
U0, U1 and U2 in download order. The units sat on AC input without a USB load,
and the EcoFlow app showed, at the time of the recording:

    U0  AC in 23 W, output 25 W (AC 23 + DC 2), 99 %, remaining 3 d 13 h
    U1  AC in  0 W, output  0 W, 99 %, remaining 3 d 12 h
    U2  AC in 19 W, output 19 W, 98 %, remaining 3 d 12 h

What the recording proves is telemetry only, so these tests pin two things:
the three units decode through the Delta 3 messages to those readings, and the
serial prefix gets no switch, number or select and only the sensors the frames
back. The control for the entity filter is a D3M1 serial, which keeps all of
them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.ecoflow_energy.const import (
    DELTA3_BINARY_SENSORS,
    DELTA3_NUMBERS,
    DELTA3_SELECTS,
    DELTA3_SENSORS,
    DELTA3_SWITCHES,
    RIVER3_SENSOR_KEYS,
    filter_defs_for_serial,
)
from custom_components.ecoflow_energy.ecoflow.const import (
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_UNKNOWN,
    get_device_name,
    get_device_type,
)
from custom_components.ecoflow_energy.ecoflow.parsers.delta3_proto import (
    parse_delta3_bms_heartbeat,
    parse_delta3_cms_heartbeat,
    parse_delta3_display_property,
)
from custom_components.ecoflow_energy.ecoflow.proto.runtime import (
    decode_proto_runtime_headers,
)

# Fictional serials - never a real device.
R655_SN = "R655TEST0000ABCD"
R631_SN = "R631TEST0000ABCD"
D3M1_SN = "D3M1TEST00000001"

_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "delta3" / "r655_frames_issue296.json"
)
_FRAMES: list[dict[str, Any]] = json.loads(_FIXTURE_PATH.read_text())["frames"]

# Sensors the coordinator integrates from a live power key, so they are not in
# any frame and cannot be checked against the recording.
_DERIVED_ENERGY_KEYS = {"ac_in_energy_kwh", "out_energy_kwh"}

# Allowed on the strength of the raw remaining-time fields, not of a value: all
# three units reported themselves idle, and the parser keeps a remaining time
# only for the direction that is active, so neither sensor ever carries a value
# in the recording (see test_idle_state_leaves_remaining_times_unknown).
_IDLE_GATED_KEYS = {"chg_remain_time_min", "dsg_remain_time_min"}
_IDLE_GATED_RAW_FIELDS = {"cms_chg_rem_time", "cms_dsg_rem_time"}


def _replay(unit: str) -> tuple[dict[str, Any], dict[str, Any], set[str]]:
    """Run every frame of one unit through decode and parse, in recorded order.

    Returns the last raw status-frame value per decoded key, the parsed sensor
    values with later frames winning (the way the coordinator merges pushes),
    and every sensor key a frame of the unit produced with a value. A key the
    parser returns as None is not a value and does not count.
    """
    raw: dict[str, Any] = {}
    parsed: dict[str, Any] = {}
    seen: set[str] = set()
    for frame in _FRAMES:
        if frame["unit"] != unit:
            continue
        for result in decode_proto_runtime_headers(
            bytes.fromhex(frame["hex"]), device_type=DEVICE_TYPE_DELTA3
        ):
            fields = {k: v for k, v in result.mapped.items() if not k.startswith("_")}
            if result.mapped.get("_is_delta3_display"):
                raw.update(fields)
                values = parse_delta3_display_property(fields)
            elif result.mapped.get("_is_delta3_bms_heartbeat"):
                values = parse_delta3_bms_heartbeat(fields)
            elif result.mapped.get("_is_delta3_cms_heartbeat"):
                values = parse_delta3_cms_heartbeat(fields)
            else:
                continue
            parsed.update(values)
            seen.update(k for k, v in values.items() if v is not None)
    return raw, parsed, seen


def _keys(definitions: list[Any]) -> set[str]:
    return {definition.key for definition in definitions}


class TestRouting:
    """The serial prefix reaches the Delta 3 device type and a display name."""

    def test_r655_routes_to_delta3(self) -> None:
        assert get_device_type("", R655_SN) == DEVICE_TYPE_DELTA3 == "delta3"

    def test_r655_display_name(self) -> None:
        assert get_device_name("", R655_SN) == "RIVER 3"

    def test_river3_plus_is_not_covered(self) -> None:
        """R631 has no recording behind it, so the R655 entry must not reach it."""
        assert get_device_type("", R631_SN) == DEVICE_TYPE_UNKNOWN


class TestRecordedFrames:
    """The three units decode through the Delta 3 messages to the app's values."""

    @pytest.mark.parametrize(
        ("unit", "pow_in", "pow_out", "ac_in_raw", "soc", "dsg_rem_raw"),
        [
            ("U0", 24, 26, 23.19, 99, 5109),
            ("U1", 0, 0, 0.0, 99, 5057),
            ("U2", 20, 20, 18.72, 98, 5024),
        ],
    )
    def test_status_frame_values(
        self,
        unit: str,
        pow_in: int,
        pow_out: int,
        ac_in_raw: float,
        soc: int,
        dsg_rem_raw: int,
    ) -> None:
        raw, parsed, _ = _replay(unit)

        assert parsed["pow_in_sum_w"] == pow_in
        assert parsed["pow_out_sum_w"] == pow_out
        assert raw["pow_get_ac_in"] == pytest.approx(ac_in_raw, abs=0.01)
        assert parsed["ac_in_w"] == round(ac_in_raw)
        assert parsed["cms_batt_soc"] == soc
        assert raw["cms_dsg_rem_time"] == dsg_rem_raw

    @pytest.mark.parametrize(("unit", "cycles"), [("U0", 18), ("U1", 13), ("U2", 12)])
    def test_bms_block(self, unit: str, cycles: int) -> None:
        _, parsed, _ = _replay(unit)

        assert parsed["bms_cell_count"] == 6
        assert parsed["bms_design_cap_mah"] == 12800
        assert parsed["bms_soh_pct"] == 100
        assert parsed["bms_cycles"] == cycles

    @pytest.mark.parametrize("unit", ["U0", "U1", "U2"])
    def test_idle_state_leaves_remaining_times_unknown(self, unit: str) -> None:
        """Why the reporter saw both remaining-time sensors as unknown.

        The status frame carries a remaining time for each direction while the
        unit reports itself idle, and the parser keeps a remaining time only for
        the direction that is active. Both sensors exist for this serial, so
        the gap is the gate and not a missing entity.
        """
        raw, parsed, _ = _replay(unit)

        assert raw["cms_chg_dsg_state"] == 0
        assert raw["cms_dsg_rem_time"] > 0
        assert parsed["chg_dsg_state"] == "idle"
        assert parsed["chg_remain_time_min"] is None
        assert parsed["dsg_remain_time_min"] is None

        sensor_keys = _keys(filter_defs_for_serial(DELTA3_SENSORS, R655_SN))
        assert {"chg_remain_time_min", "dsg_remain_time_min"} <= sensor_keys


class TestReadOnlyEntitySet:
    """A RIVER 3 gets no control and only the sensors the recording backs."""

    CONTROL_LISTS = {
        "switch": DELTA3_SWITCHES,
        "number": DELTA3_NUMBERS,
        "select": DELTA3_SELECTS,
    }

    @pytest.mark.parametrize("platform", sorted(CONTROL_LISTS))
    def test_r655_gets_no_control(self, platform: str) -> None:
        definitions = self.CONTROL_LISTS[platform]

        assert filter_defs_for_serial(definitions, R655_SN) == []

    @pytest.mark.parametrize("platform", sorted(CONTROL_LISTS))
    def test_filter_control_d3m1_keeps_every_control(self, platform: str) -> None:
        """The negative control: the same filter does not empty a D3M1."""
        definitions = self.CONTROL_LISTS[platform]

        assert definitions, "the Delta 3 list this control reads from is empty"
        assert len(filter_defs_for_serial(definitions, D3M1_SN)) == len(definitions)

    def test_r655_gets_no_binary_sensor(self) -> None:
        assert filter_defs_for_serial(DELTA3_BINARY_SENSORS, R655_SN) == []

    def test_r655_sensors_are_exactly_the_recorded_set(self) -> None:
        keys = _keys(filter_defs_for_serial(DELTA3_SENSORS, R655_SN))

        assert keys == set(RIVER3_SENSOR_KEYS)
        # The sensors a unit without a load, a solar panel or a USB device
        # cannot back stay off, whatever the Delta 3 list offers.
        assert not keys & {
            "pv1_in_w",
            "pv2_in_w",
            "solar_energy_kwh",
            "solar2_energy_kwh",
            "dc_12v_out_w",
            "typec1_w",
            "usb_qc1_w",
            "ac1_out_w",
            "bms_accu_chg_energy_kwh",
            "max_charge_soc_pct",
            "ac_charge_power_limit_w",
        }

    def test_d3m1_sensors_are_unchanged(self) -> None:
        assert _keys(filter_defs_for_serial(DELTA3_SENSORS, D3M1_SN)) == _keys(
            DELTA3_SENSORS
        )

    def test_every_allowed_sensor_is_a_delta3_sensor(self) -> None:
        """A misspelt key in the allowed set would silently drop that sensor."""
        assert set(RIVER3_SENSOR_KEYS) <= _keys(DELTA3_SENSORS)

    def test_every_allowed_sensor_is_in_the_recording(self) -> None:
        """The allowed set is what the frames back, not what looks plausible.

        Every key is either produced with a value by a recorded frame, or named
        as an explicit allowance: the two derived energy counters, and the two
        idle-gated remaining times, which the raw fields in the frames back.
        """
        produced: set[str] = set()
        raw_fields: set[str] = set()
        for unit in ("U0", "U1", "U2"):
            raw, _, seen = _replay(unit)
            produced |= seen
            raw_fields |= set(raw)

        allowances = _DERIVED_ENERGY_KEYS | _IDLE_GATED_KEYS
        missing = set(RIVER3_SENSOR_KEYS) - allowances - produced

        assert not missing, f"allowed but never produced by a recorded frame: {missing}"
        # The idle-gated allowance holds only while the recording is idle and
        # carries the raw field behind each sensor.
        assert not _IDLE_GATED_KEYS & produced
        assert raw_fields >= _IDLE_GATED_RAW_FIELDS

    def test_documented_sensor_count_matches_the_set(self) -> None:
        """Both public documents state the count, so the set decides it."""
        count = len(filter_defs_for_serial(DELTA3_SENSORS, R655_SN))
        root = Path(__file__).resolve().parents[1]

        readme = (root / "README.md").read_text(encoding="utf-8")
        row = next(
            line for line in readme.splitlines() if line.startswith("| **RIVER 3** |")
        )
        assert f"| {count} |" in row

        reference = (root / "documentation" / "README.md").read_text(encoding="utf-8")
        bullet = next(
            line for line in reference.splitlines() if line.startswith("- [RIVER 3]")
        )
        assert f"- {count} sensors" in bullet
