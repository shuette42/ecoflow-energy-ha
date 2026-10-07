"""Tests for RIVER 3 (`R655`) support, read-only, on top of the Delta 3 path (#296).

The frames come from the reporter's diagnostics download of three units
(2026-10-04), copied unchanged into
`tests/fixtures/delta3/r655_frames_issue296.json`; the `unit` field tags them
U0, U1 and U2 in download order. The units sat on AC input without a USB-C
load; the DC 2 W on U0 is a USB-A port (`pow_get_qcusb1` -1.55 W). The EcoFlow
app showed, at the time of the recording:

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
from typing import Any, ClassVar

import pytest

from custom_components.ecoflow_energy.const import (
    DELTA3_BINARY_SENSORS,
    DELTA3_NUMBERS,
    DELTA3_SELECTS,
    DELTA3_SENSORS,
    DELTA3_SWITCHES,
    RIVER3_SENSOR_KEYS,
    filter_defs_for_serial,
    readback_binary_defs_for_serial,
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
P231_SN = "P231TEST00000001"

_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "delta3" / "r655_frames_issue296.json"
)
_FRAMES: list[dict[str, Any]] = json.loads(_FIXTURE_PATH.read_text())["frames"]

# The reporter's second run on U1 (2026-10-04 18:52-18:57 CEST): USB-C load,
# AC input unplugged, AC output off and on again, AC input back. Kept in its
# own file so the first recording's idle readings stay as they were.
_RUN2_PATH = _FIXTURE_PATH.with_name("r655_frames_issue296_ac_usb.json")
_RUN2: list[dict[str, Any]] = json.loads(_RUN2_PATH.read_text())["frames"]

# RIVER 3 Plus (R631), one unit, 2026-10-07 17:09-18:12 UTC: on AC input, a
# refrigerator of about 70 W on the AC output, a phone on USB-C, 64 %.
_R631_PATH = _FIXTURE_PATH.with_name("r631_frames_issue296.json")
_R631: list[dict[str, Any]] = json.loads(_R631_PATH.read_text())["frames"]

# Sensors the coordinator integrates from a live power key, so they are not in
# any frame and cannot be checked against the recording.
_DERIVED_ENERGY_KEYS = {"ac_in_energy_kwh", "out_energy_kwh"}


def _replay(
    unit: str, frames: list[dict[str, Any]] | None = None
) -> tuple[dict[str, Any], dict[str, Any], set[str]]:
    """Run every frame of one unit through decode and parse, in recorded order.

    Returns the last raw status-frame value per decoded key, the parsed sensor
    values with later frames winning (the way the coordinator merges pushes),
    and every sensor key a frame of the unit produced with a value. A key the
    parser returns as None is not a value and does not count.
    """
    raw: dict[str, Any] = {}
    parsed: dict[str, Any] = {}
    seen: set[str] = set()
    for frame in _FRAMES if frames is None else frames:
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

    def test_river3_plus_routes_to_delta3(self) -> None:
        """R631 is routed on its own recording, see TestRiver3PlusRecording."""
        assert get_device_type("", R631_SN) == DEVICE_TYPE_DELTA3
        assert get_device_name("", R631_SN) == "RIVER 3 Plus"

    def test_routing_control_unknown_prefix(self) -> None:
        """The negative control: a neighbouring prefix nobody recorded stays unknown."""
        assert get_device_type("", "R632TEST0000ABCD") == DEVICE_TYPE_UNKNOWN


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

    def test_usb_a_is_the_dc_2_w_the_app_showed(self) -> None:
        """U0: output 25 W in the app as AC 23 + DC 2; the frame sends USB-A -1.55 W."""
        raw, parsed, _ = _replay("U0")

        assert raw["pow_get_qcusb1"] == pytest.approx(-1.547, abs=0.01)
        assert parsed["usb_qc1_w"] == 2

    @pytest.mark.parametrize(("unit", "cycles"), [("U0", 18), ("U1", 13), ("U2", 12)])
    def test_bms_block(self, unit: str, cycles: int) -> None:
        _, parsed, _ = _replay(unit)

        assert parsed["bms_cell_count"] == 6
        assert parsed["bms_design_cap_mah"] == 12800
        assert parsed["bms_soh_pct"] == 100
        assert parsed["bms_cycles"] == cycles

    @pytest.mark.parametrize("unit", ["U0", "U1", "U2"])
    def test_idle_state_shows_the_discharge_runtime(self, unit: str) -> None:
        """Idle shows the discharge time, the figure the app shows (3 d 12-13 h).

        Before this the parser cleared both directions while idle, which is why
        the reporter saw both remaining-time sensors as unknown.
        """
        raw, parsed, _ = _replay(unit)

        assert raw["cms_chg_dsg_state"] == 0
        assert parsed["chg_dsg_state"] == "idle"
        assert parsed["dsg_remain_time_min"] == raw["cms_dsg_rem_time"] > 0
        assert parsed["chg_remain_time_min"] is None

        sensor_keys = _keys(filter_defs_for_serial(DELTA3_SENSORS, R655_SN))
        assert {"chg_remain_time_min", "dsg_remain_time_min"} <= sensor_keys


class TestAcOutputAndUsbRun:
    """The second run: AC output off and on, a phone on USB-C (#296).

    Frame times are the recording's own, in CEST; the actions are the
    reporter's: USB-C load 18:52:52, AC input out 18:52:56, AC output off about
    18:55:20 and on about 18:56:20, AC input back 18:56:28.
    """

    @staticmethod
    def _series(key: str) -> list[tuple[float, Any]]:
        out: list[tuple[float, Any]] = []
        for frame in _RUN2:
            single = dict(frame, unit="U1")
            _, parsed, _ = _replay("U1", [single])
            if key in parsed:
                out.append((frame["ts"], parsed[key]))
        return out

    def test_ac_output_reads_off_then_on(self) -> None:
        # 2026-10-04 18:55:20 and 18:56:20 CEST as epoch seconds.
        off_at, on_at = 1791132920, 1791132980
        series = self._series("ac_out_flow")

        before = [v for ts, v in series if ts < off_at]
        between = [v for ts, v in series if off_at <= ts < on_at]
        after = [v for ts, v in series if ts >= on_at]
        assert before and set(before) == {1}
        assert between == [0]
        assert after and set(after) == {1}

    def test_usb_c_load_reads_as_a_positive_output(self) -> None:
        """The frame sends -19.94 W; the app showed 20-21 W of DC output."""
        raw, _, _ = _replay("U1", _RUN2)
        values = {v for _, v in self._series("typec1_w") if v}

        assert values and values <= {18, 19, 20}
        assert raw["pow_get_typec1"] < 0

    def test_remaining_time_follows_the_app(self) -> None:
        """8 h 20 discharging and 10 h 34 with AC off, as in the app.

        Once charging, the only frame that carries the state reads 10 min. The
        1 min the app showed arrives in a partial frame without the state,
        which the parser leaves alone by design (a partial push keeps the
        previous value), so the sensor catches up on the next full frame.
        """
        dsg = [v for _, v in self._series("dsg_remain_time_min") if v is not None]
        chg = [v for _, v in self._series("chg_remain_time_min") if v is not None]

        assert 500 in dsg and 634 in dsg
        assert set(chg) == {10}


class TestReadOnlyEntitySet:
    """A RIVER 3 gets no control and only the sensors the recording backs."""

    CONTROL_LISTS: ClassVar[dict[str, list[Any]]] = {
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

    def test_r655_gets_no_binary_sensor_from_the_list(self) -> None:
        assert filter_defs_for_serial(DELTA3_BINARY_SENSORS, R655_SN) == []

    def test_r655_reads_back_its_ac_output_only(self) -> None:
        """The AC Output switch is excluded, its state stays as a binary sensor."""
        defs = readback_binary_defs_for_serial(DELTA3_SWITCHES, R655_SN)

        assert [(d.key, d.name) for d in defs] == [("ac_out_flow", "AC Output")]

    def test_readback_keeps_the_switch_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An Enhanced-only switch read back stays Enhanced-only (fictional prefix)."""
        from custom_components.ecoflow_energy import const

        switch = next(s for s in DELTA3_SWITCHES if s.enhanced_only)
        monkeypatch.setitem(
            const._SN_PREFIX_EXCLUDED_KEYS, "ZZ99", frozenset({switch.key})
        )

        (definition,) = readback_binary_defs_for_serial(
            DELTA3_SWITCHES, "ZZ99TEST00000000"
        )
        assert definition.key == switch.state_key
        assert definition.enhanced_only is True
        assert definition.accessory == switch.accessory

    @pytest.mark.parametrize("serial", [D3M1_SN, P231_SN])
    def test_readback_control_writable_models_get_none(self, serial: str) -> None:
        """A model that keeps its switches gets no read-only twin of them."""
        assert readback_binary_defs_for_serial(DELTA3_SWITCHES, serial) == []

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
            "typec2_w",
            "usb_qc2_w",
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

        Every key is either produced with a value by a recorded frame of either
        run, or is one of the two derived energy counters.
        """
        produced: set[str] = set()
        for unit in ("U0", "U1", "U2"):
            produced |= _replay(unit)[2]
        produced |= _replay("U1", _RUN2)[2]
        produced |= _replay("U0", _R631)[2]

        missing = set(RIVER3_SENSOR_KEYS) - _DERIVED_ENERGY_KEYS - produced

        assert not missing, f"allowed but never produced by a recorded frame: {missing}"

    def test_documented_sensor_count_matches_the_set(self) -> None:
        """Both public documents state the count, so the set decides it."""
        count = len(filter_defs_for_serial(DELTA3_SENSORS, R655_SN))
        binary = len(readback_binary_defs_for_serial(DELTA3_SWITCHES, R655_SN))
        root = Path(__file__).resolve().parents[1]

        readme = (root / "README.md").read_text(encoding="utf-8")
        row = next(
            line for line in readme.splitlines() if line.startswith("| **RIVER 3** |")
        )
        assert f"| {count} + {binary} binary |" in row

        reference = (root / "documentation" / "README.md").read_text(encoding="utf-8")
        bullet = next(
            line for line in reference.splitlines() if line.startswith("- [RIVER 3]")
        )
        assert f"- {count} sensors and {binary} binary sensor" in bullet

    def test_river3_plus_documented_count_matches(self) -> None:
        """The RIVER 3 Plus rows state the count of the set it shares."""
        count = len(filter_defs_for_serial(DELTA3_SENSORS, R631_SN))
        binary = len(readback_binary_defs_for_serial(DELTA3_SWITCHES, R631_SN))
        root = Path(__file__).resolve().parents[1]

        readme = (root / "README.md").read_text(encoding="utf-8")
        row = next(
            line
            for line in readme.splitlines()
            if line.startswith("| **RIVER 3 Plus** |")
        )
        assert f"| {count} + {binary} binary |" in row

        reference = (root / "documentation" / "README.md").read_text(encoding="utf-8")
        bullet = next(
            line
            for line in reference.splitlines()
            if line.startswith("- [RIVER 3 Plus]")
        )
        assert f"- {count} sensors and {binary} binary sensor" in bullet


class TestRiver3PlusRecording:
    """RIVER 3 Plus (`R631`): its own frames, decoded through the same path (#296).

    The reporter's conditions: AC input plugged in, a refrigerator of about
    70 W on the AC output, a phone on USB-C at about 27 W and falling, 64 %.
    """

    @staticmethod
    def _frame_at(ts_iso_prefix: str) -> dict[str, Any]:
        return next(f for f in _R631 if f["ts_iso"].startswith(ts_iso_prefix))

    def test_full_status_frame_matches_the_owner(self) -> None:
        """17:23 UTC: AC input passed through to the load, phone on USB-C."""
        raw, parsed, _ = _replay("U0", [self._frame_at("2026-10-07T17:23:22")])

        assert raw["pow_get_ac_in"] == pytest.approx(83.04, abs=0.01)
        assert parsed["ac_in_w"] == 83
        assert parsed["pow_in_sum_w"] == parsed["pow_out_sum_w"] == 83
        assert raw["pow_get_typec1"] == pytest.approx(-14.38, abs=0.01)
        assert parsed["typec1_w"] == 14
        assert parsed["cms_batt_soc"] == 64
        assert parsed["ac_out_flow"] == 1

    def test_output_follows_the_refrigerator(self) -> None:
        """In equals out throughout; the load cycles between 0 W and ~83 W."""
        totals = []
        for frame in _R631:
            _, parsed, _ = _replay("U0", [frame])
            if "pow_out_sum_w" in parsed:
                assert parsed["pow_in_sum_w"] == parsed["pow_out_sum_w"]
                totals.append(parsed["pow_out_sum_w"])

        assert 0 in totals
        assert max(totals) == 83

    def test_bms_block_is_a_seven_cell_pack(self) -> None:
        _, parsed, _ = _replay("U0", _R631)

        assert parsed["bms_cell_count"] == 7
        assert parsed["bms_design_cap_mah"] == 12800
        assert parsed["bms_voltage_v"] == pytest.approx(23.22, abs=0.01)

    def test_gets_the_river3_entity_set(self) -> None:
        """Same exclusion as the RIVER 3: same sensors, AC output, no control."""
        assert _keys(filter_defs_for_serial(DELTA3_SENSORS, R631_SN)) == set(
            RIVER3_SENSOR_KEYS
        )
        readback = readback_binary_defs_for_serial(DELTA3_SWITCHES, R631_SN)
        assert [d.key for d in readback] == ["ac_out_flow"]
        for definitions in (DELTA3_SWITCHES, DELTA3_NUMBERS, DELTA3_SELECTS):
            assert filter_defs_for_serial(definitions, R631_SN) == []
