"""Tests for RIVER 2 Max (`R611`) support, read-only, on top of the Delta 2 path (#585).

The frames come from the reporter's diagnostics download of one unit in
Enhanced Mode (2026-10-18, 08:25-08:55 America/Toronto), reduced to their keys
and values in `tests/fixtures/river2max/r611_frames_issue585.json`. The unit
powered network equipment of about 60 W. The EcoFlow app showed, at the times
the reporter noted:

    08:26  AC input unplugged, on battery, 99 %, AC output about 60 W, 4 h 40 min
    08:30  97 %, AC output about 60 W
    08:31  USB-C power bank plugged in, 96 %, DC output 17 W
    08:35  AC input plugged in, 95 %, input about 700 W
    08:37  full

What the recording proves is telemetry only, so these tests pin two things:
the frames decode through the Delta 2 quota parser to those readings, on both
Enhanced topics, and the serial prefix gets no switch, number or binary sensor
and only the sensors the frames back. The control for the entity filter is an
R351 serial, which keeps all of them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.ecoflow_energy.const import (
    DELTA2MAX_BINARY_SENSORS,
    DELTA2MAX_NUMBERS,
    DELTA2MAX_SENSORS,
    DELTA2MAX_SWITCHES,
    RIVER2MAX_SENSOR_KEYS,
    filter_defs_for_serial,
    readback_binary_defs_for_serial,
)
from custom_components.ecoflow_energy.coordinator.mqtt_ingest import MqttIngestMixin
from custom_components.ecoflow_energy.ecoflow.const import (
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_UNKNOWN,
    device_log_tag,
    get_device_name,
    get_device_type,
)

# Fictional serials - never a real device.
R611_SN = "R611TEST0000ABCD"
R351_SN = "R351TEST0000ABCD"

_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "river2max"
    / "r611_frames_issue585.json"
)
_FRAMES: list[dict[str, Any]] = json.loads(_FIXTURE.read_text())["frames"]
_BY_INDEX: dict[int, dict[str, Any]] = {f["frame_index"]: f for f in _FRAMES}

# Sensors the coordinator integrates from a live power key, so they are not in
# any frame and cannot be checked against the recording.
_DERIVED_ENERGY_KEYS = {"ac_in_energy_kwh", "ac_out_energy_kwh"}


class _DeltaIngest(MqttIngestMixin):
    """Minimal host for the real MQTT message parser of a Delta device."""

    device_sn = R611_SN
    device_type = DEVICE_TYPE_DELTA
    device_tag = device_log_tag(device_sn)


def _ingest(frame: dict[str, Any]) -> dict[str, Any]:
    """Run one recorded frame through the real parser on its own topic."""
    if frame["topic"] == "get_reply":
        topic = f"/app/user123/{R611_SN}/thing/property/get_reply"
        payload = json.dumps({"data": {"quotaMap": frame["params"]}}).encode()
    else:
        assert frame["topic"] == "property"
        topic = f"/app/device/property/{R611_SN}"
        payload = json.dumps({"params": frame["params"]}).encode()
    parsed = object.__new__(_DeltaIngest)._parse_message(topic, payload)
    assert parsed is not None
    return parsed


def _keys(definitions: list[Any]) -> set[str]:
    return {definition.key for definition in definitions}


class TestRouting:
    """The serial prefix reaches the Delta device type and a display name."""

    def test_r611_routes_to_delta(self) -> None:
        assert get_device_type("", R611_SN) == DEVICE_TYPE_DELTA

    def test_r611_display_name(self) -> None:
        assert get_device_name("", R611_SN) == "RIVER 2 Max"

    def test_routing_control_unknown_prefix(self) -> None:
        """The negative control: a neighbouring prefix nobody recorded stays unknown."""
        assert get_device_type("", "R612TEST0000ABCD") == DEVICE_TYPE_UNKNOWN


class TestRecordedFrames:
    """The recording decodes to the readings the reporter saw in the app."""

    def test_fixture_covers_both_enhanced_topics(self) -> None:
        topics = {f["topic"] for f in _FRAMES}
        assert topics == {"property", "get_reply"}
        assert len(_FRAMES) == 16

    def test_on_battery_with_load(self) -> None:
        """08:29, on battery: 97 %, about 67 W out, 286 min left (app: 4 h 40 min)."""
        parsed = _ingest(_BY_INDEX[3])
        assert parsed["soc"] == 97
        assert parsed["watts_out_sum"] == 67
        assert parsed["remain_time_min"] == 286

    def test_usb_c_power_bank(self) -> None:
        """08:35, power bank on USB-C: the app showed 17 W DC output at 95 %."""
        parsed = _ingest(_BY_INDEX[5])
        assert parsed["typec1_w"] == 17
        assert parsed["soc"] == 95

    def test_usb_c_reads_zero_once_full(self) -> None:
        """08:38, at 100 %: the port reports 0 W.

        The reporter saw the DC reading disappear from the app once the unit
        was full while the power bank's LED still showed charging, until the
        cable was replugged. The device itself reports 0 W at that moment, so
        the missing reading is on the device side and not a parsing loss.
        """
        parsed = _ingest(_BY_INDEX[7])
        assert parsed["soc"] == 100
        assert parsed["typec1_w"] == 0

    def test_ac_charge(self) -> None:
        """08:37, AC back in: 710 W in (app: about 700 W), mains of 117.8 V."""
        parsed = _ingest(_BY_INDEX[6])
        assert parsed["ac_in_w"] == 710
        assert parsed["ac_out_w"] == 67
        assert parsed["ac_in_vol_v"] == pytest.approx(117.814)
        assert parsed["ac_out_vol_v"] == pytest.approx(116.986)

    def test_passthrough_after_full(self) -> None:
        """08:49, full on AC: about 68 W through, 100 %."""
        parsed = _ingest(_BY_INDEX[11])
        assert parsed["soc"] == 100
        assert parsed["watts_in_sum"] == 68
        assert parsed["watts_out_sum"] == 68

    def test_mains_frequency(self) -> None:
        """A latestQuotas reply carries the 60 Hz of a North American mains."""
        parsed = _ingest(_BY_INDEX[5])
        assert parsed["ac_out_freq_hz"] == 60

    def test_every_frame_but_the_report_interval_parses(self) -> None:
        """Only the first frame, the device's report intervals, carries no reading."""
        for frame in _FRAMES:
            if frame["frame_index"] == 0:
                assert set(frame["params"]) == {
                    "pd.pdInfoFull",
                    "pd.pdInfoIncre",
                    "pd.pdRunIncre",
                    "pd.bmsInfoFull",
                    "pd.bmsInfoIncre",
                    "pd.bmsRunIncre",
                }
                continue
            assert _ingest(frame), frame["frame_index"]


class TestEntitySet:
    """The prefix gets the sensors the frames back and no controls."""

    def test_sensors_are_exactly_the_backed_set(self) -> None:
        """Compared with a literal, not with the constant the filter is built from."""
        kept = _keys(filter_defs_for_serial(DELTA2MAX_SENSORS, R611_SN))
        assert kept == {
            "soc",
            "watts_in_sum",
            "watts_out_sum",
            "ac_in_w",
            "ac_out_w",
            "typec1_w",
            "ac_in_vol_v",
            "ac_out_vol_v",
            "ac_out_freq_hz",
            "ac_in_energy_kwh",
            "ac_out_energy_kwh",
        }
        assert kept == RIVER2MAX_SENSOR_KEYS

    def test_the_totals_are_backed_by_the_kept_frames(self) -> None:
        """`pd.wattsInSum` and `pd.wattsOutSum` read like the AC power keys.

        The 16 kept frames are a sample of 1266 messages (see the fixture's
        `sampling`), so that only 2 of them carry the totals says nothing about
        how often the unit sends them.
        """
        with_total = [f for f in _FRAMES if "pd.wattsInSum" in f["params"]]
        assert len(with_total) == 2
        assert {"watts_in_sum", "watts_out_sum"} <= RIVER2MAX_SENSOR_KEYS
        for frame in with_total:
            parsed = _ingest(frame)
            assert parsed["watts_in_sum"] in (0, 68)
            assert parsed["watts_out_sum"] in (67, 68)

    def test_the_recording_is_a_sample_of_a_fast_push(self) -> None:
        """1259 pushes in 1837 s: the 238 s grid of the frames is the sampling."""
        sampling = json.loads(_FIXTURE.read_text())["sampling"]
        assert sampling["frames_seen"] == 1266
        assert sampling["per_key"]["property:json/unparsed"]["seen"] == 1259
        assert sampling["span_s"] == pytest.approx(1836.8)

    def test_allowed_keys_all_exist_in_the_delta2_definitions(self) -> None:
        """A renamed Delta 2 key must not silently empty the RIVER 2 Max set."""
        assert _keys(DELTA2MAX_SENSORS) >= RIVER2MAX_SENSOR_KEYS

    def test_remaining_time_stays_off_until_the_ac_value_is_known(self) -> None:
        """286 min on battery matches the app; 4092 min on AC has no app value."""
        assert "remain_time_min" not in RIVER2MAX_SENSOR_KEYS
        kept = _keys(filter_defs_for_serial(DELTA2MAX_SENSORS, R611_SN))
        assert "remain_time_min" not in kept

    def test_no_controls_and_no_binary_sensors(self) -> None:
        assert filter_defs_for_serial(DELTA2MAX_SWITCHES, R611_SN) == []
        assert filter_defs_for_serial(DELTA2MAX_NUMBERS, R611_SN) == []
        assert filter_defs_for_serial(DELTA2MAX_BINARY_SENSORS, R611_SN) == []

    def test_no_readback_binary_sensors(self) -> None:
        """Unlike the RIVER 3, no switch state is shown; none was seen to change."""
        assert readback_binary_defs_for_serial(DELTA2MAX_SWITCHES, R611_SN) == []

    def test_every_backed_sensor_has_a_value_in_the_recording(self) -> None:
        seen: set[str] = set()
        for frame in _FRAMES:
            seen.update(k for k, v in _ingest(frame).items() if v is not None)
        missing = RIVER2MAX_SENSOR_KEYS - _DERIVED_ENERGY_KEYS - seen
        assert not missing, sorted(missing)

    def test_battery_voltage_stays_off(self) -> None:
        """`bms_bmsStatus.vol` is 25 and 26, not millivolts.

        The Delta 2 conversion would show 0.026 V, so the sensor is left out
        until the unit is known.
        """
        raw = [
            f["params"]["bms_bmsStatus.vol"]
            for f in _FRAMES
            if "bms_bmsStatus.vol" in f["params"]
        ]
        assert raw
        assert all(20 <= v <= 30 for v in raw)
        assert "batt_voltage_v" not in _keys(
            filter_defs_for_serial(DELTA2MAX_SENSORS, R611_SN)
        )

    def test_control_keeps_the_full_delta2_set(self) -> None:
        """The negative control: an R351 serial is untouched by the exclusion."""
        assert len(filter_defs_for_serial(DELTA2MAX_SENSORS, R351_SN)) == len(
            DELTA2MAX_SENSORS
        )
        assert len(filter_defs_for_serial(DELTA2MAX_SWITCHES, R351_SN)) == len(
            DELTA2MAX_SWITCHES
        )
        assert len(filter_defs_for_serial(DELTA2MAX_NUMBERS, R351_SN)) == len(
            DELTA2MAX_NUMBERS
        )
