"""Tests for the Stream (BK-series) JSON quota parser."""

import json
from pathlib import Path

import pytest
from ecoflow_energy.ecoflow.parsers.stream_http import parse_stream_quota
from ecoflow_energy.ecoflow.parsers.stream_proto import SOC_FALLBACK_KEY


class TestFieldMapping:
    def test_per_string_pv_is_mapped(self) -> None:
        """The per-string PV inputs issue #139 asks for reach sensor keys."""
        result = parse_stream_quota(
            {
                "powGetPv": 518.0,
                "powGetPv2": 301.0,
                "powGetPv3": 188.0,
                "powGetPv4": 12.0,
            }
        )

        assert result["pv1_w"] == 518.0
        assert result["pv2_w"] == 301.0
        assert result["pv3_w"] == 188.0
        assert result["pv4_w"] == 12.0

    def test_pv_sum_feeds_the_shared_solar_key(self) -> None:
        """Standard and Enhanced must drive the same solar sensor."""
        result = parse_stream_quota({"powGetPvSum": 954.0})

        assert result["solar_w"] == 954.0
        assert "powGetPvSum" not in result

    def test_system_power_paths_are_mapped(self) -> None:
        result = parse_stream_quota(
            {
                "powGetSysLoad": 255.0,
                "powGetSysGrid": 79.0,
                "powGetSysLoadFromPv": 176.0,
                "powGetSchuko1": 40.0,
                "plugInInfoPvVol": 38.5,
            }
        )

        assert result["home_w"] == 255.0
        assert result["grid_w"] == 79.0
        assert result["home_from_solar_w"] == 176.0
        assert result["ac_outlet_1_w"] == 40.0
        assert result["pv_voltage_v"] == 38.5

    def test_system_soc_feeds_battery_soc_and_unit_soc_stays_apart(self) -> None:
        """`cmsBattSoc` is the linked system's SoC, `soc` this unit's own.

        Same split as protobuf fields 262/242: the battery sensor is the
        system figure, the unit's own reading gets its own key (#323).
        """
        result = parse_stream_quota({"soc": 40, "cmsBattSoc": 55.0})

        assert result["soc_pct"] == 55
        assert result["unit_soc_pct"] == 40

    def test_unit_soc_alone_is_offered_as_stand_in_not_promoted(self) -> None:
        """A quota carrying only `soc` offers the unit figure for `soc_pct`.

        Promotion is the coordinator's call: a partial `/quota` push with
        `soc` alone must not overwrite the system figure of the last poll.
        """
        result = parse_stream_quota({"soc": 40})

        assert "soc_pct" not in result
        assert result["unit_soc_pct"] == 40
        assert result[SOC_FALLBACK_KEY] == 40

    def test_system_figure_leaves_no_stand_in(self) -> None:
        result = parse_stream_quota({"soc": 40, "cmsBattSoc": 55.0})

        assert SOC_FALLBACK_KEY not in result

    def test_system_soc_alone_feeds_battery_soc(self) -> None:
        """A Stream Ultra X quota carries `cmsBattSoc` and no `soc` at all.

        The 19-key quota in the #139 diagnostics is exactly that, and before
        the split the battery sensor of such a unit stayed empty in Standard
        Mode.
        """
        result = parse_stream_quota({"cmsBattSoc": 55.0})

        assert result["soc_pct"] == 55
        assert "unit_soc_pct" not in result

    def test_percent_keys_become_integers(self) -> None:
        """SoC and SoH are whole percent, matching the protobuf path.

        `soc` is the unit's own figure and lands on `unit_soc_pct` (#323).
        """
        result = parse_stream_quota({"soc": 87.4, "soh": 100})

        assert result["unit_soc_pct"] == 87
        assert result["bms_soh_pct"] == 100

    def test_missing_keys_are_absent(self) -> None:
        """A unit with two strings must not publish PV 3 and PV 4 as zero."""
        result = parse_stream_quota({"powGetPv": 100.0, "powGetPv2": 50.0})

        assert "pv3_w" not in result
        assert "pv4_w" not in result


class TestBatterySplit:
    def test_charging_splits_positive(self) -> None:
        result = parse_stream_quota({"powGetBpCms": 640.0})

        assert result["batt_w"] == 640.0
        assert result["batt_charge_power_w"] == 640.0
        assert result["batt_discharge_power_w"] == 0.0

    def test_discharging_splits_negative(self) -> None:
        result = parse_stream_quota({"powGetBpCms": -220.0})

        assert result["batt_w"] == -220.0
        assert result["batt_charge_power_w"] == 0.0
        assert result["batt_discharge_power_w"] == 220.0

    def test_idle_splits_to_zero(self) -> None:
        result = parse_stream_quota({"powGetBpCms": 0.0})

        assert result["batt_charge_power_w"] == 0.0
        assert result["batt_discharge_power_w"] == 0.0

    def test_no_battery_key_no_split(self) -> None:
        result = parse_stream_quota({"powGetPv": 10.0})

        assert "batt_charge_power_w" not in result
        assert "batt_discharge_power_w" not in result


class TestRejectedInput:
    def test_unmapped_keys_are_dropped(self) -> None:
        """Raw quota keys must never leak into the device data store."""
        result = parse_stream_quota(
            {
                "powGetPv": 100.0,
                "bmsFaultState": 0,
                "packSn": "BK11TEST00000001",
                "energyStrategyOperateMode.operateSelfPoweredOpen": True,
            }
        )

        assert result == {"pv1_w": 100.0}

    def test_non_numeric_values_are_skipped(self) -> None:
        result = parse_stream_quota({"powGetPv": "not a number", "powGetPv2": None})

        assert result == {}

    def test_booleans_are_not_treated_as_power(self) -> None:
        """bool is an int subclass, so a flag would otherwise publish 1 W."""
        result = parse_stream_quota({"powGetPv": True})

        assert result == {}

    def test_empty_payload(self) -> None:
        assert parse_stream_quota({}) == {}

    def test_non_dict_payload(self) -> None:
        assert parse_stream_quota(None) == {}  # type: ignore[arg-type]


class TestEnergySensorHygiene:
    """Guards against the two traps this mapping can create."""

    def test_per_string_energy_is_opt_in(self) -> None:
        """Summing enabled strings must not silently under-report a 4-string unit."""
        from ecoflow_energy.const import STREAM_SENSORS

        by_key = {sensor.key: sensor for sensor in STREAM_SENSORS}
        for index in range(1, 5):
            sensor = by_key[f"pv{index}_energy_kwh"]
            assert sensor.disabled_by_default is True

    def test_per_string_energy_has_dashboard_attributes(self) -> None:
        from ecoflow_energy.const import STREAM_SENSORS

        by_key = {sensor.key: sensor for sensor in STREAM_SENSORS}
        for index in range(1, 5):
            sensor = by_key[f"pv{index}_energy_kwh"]
            assert sensor.device_class == "energy"
            assert sensor.state_class == "total_increasing"
            assert sensor.unit == "kWh"

    def test_shared_solar_key_is_not_duplicated(self) -> None:
        """PvSum feeds solar_w, so no second total-solar sensor may exist."""
        from ecoflow_energy.const import STREAM_SENSORS
        from ecoflow_energy.ecoflow.parsers.stream_http import STREAM_HTTP_FIELD_MAP

        solar_keys = [k for k in STREAM_HTTP_FIELD_MAP.values() if k == "solar_w"]
        assert len(solar_keys) == 1
        assert sum(1 for s in STREAM_SENSORS if s.key == "solar_w") == 1


class TestRealAcProQuota:
    """The quota a STREAM AC Pro (BK31) actually returns in Standard Mode.

    Taken from a real 15-key quota captured on 1.18.0. The shape matters
    because it is not the one the other tests here assume: the AC Pro reports
    `cmsBattSoc` and carries no `soc` key at all, so on this device the
    stand-in path can never fire from an HTTP poll. The unit figure reaches
    the coordinator only through the MQTT `/quota` push.
    """

    QUOTA: dict = {
        "backupReverseSoc": 20,
        "cmsBattSoc": 94.0,
        "cmsMaxChgSoc": 95,
        "cmsMinDsgSoc": 10,
        "energyStrategyOperateMode.operateIntelligentScheduleModeOpen": False,
        "energyStrategyOperateMode.operateSelfPoweredOpen": False,
        "feedGridMode": 2,
        "gridConnectionPower": -184.0303,
        "powGetBpCms": 0.0,
        "powGetPvSum": 0.0,
        "powGetSysGrid": 327.8393,
        "powGetSysLoad": 327.8393,
        "quota_cloud_ts": "2026-09-01 06:01:50",
        "relay2Onoff": True,
        "relay3Onoff": True,
    }

    def test_battery_soc_comes_from_the_system_key(self) -> None:
        result = parse_stream_quota(self.QUOTA)

        assert result["soc_pct"] == 94

    def test_no_unit_figure_and_no_stand_in_is_offered(self) -> None:
        """There is no `soc` in this quota, so nothing can stand in.

        A test that hands the parser `soc` alongside `cmsBattSoc` is testing
        a shape this device does not produce.
        """
        result = parse_stream_quota(self.QUOTA)

        assert "unit_soc_pct" not in result
        assert SOC_FALLBACK_KEY not in result

    def test_the_charge_window_the_device_reports_is_dropped(self) -> None:
        """The quota carries the SoC window and this parser ignores it.

        `cmsMaxChgSoc` and `cmsMinDsgSoc` are the same two values the
        protobuf path reads as fields 270 and 271, and they sit in every
        Standard Mode quota this device returns. Neither is mapped here, so
        in Standard Mode the integration cannot see the window the device is
        actually set to. The matching Number entities are Enhanced-only, so
        nothing is broken by it today, but the data is on the wire and
        unused. Pinned so it stays a deliberate decision rather than an
        oversight nobody noticed.
        """
        result = parse_stream_quota(self.QUOTA)

        assert "max_charge_soc_pct" not in result
        assert "min_discharge_soc_pct" not in result


class TestSystemSocOfZero:
    """What a system figure of zero does today.

    These pin current behaviour, not desired behaviour. A zero on
    `cmsBattSoc` is published as the battery reading exactly like any other
    value, and the parser offers no stand-in because the key is present.
    Whether that is right is open: a genuinely empty battery reads zero, and
    nothing in a single quota separates that from a unit reporting no usable
    system figure. Change these tests deliberately, not in passing.
    """

    def test_zero_system_figure_becomes_the_battery_reading(self) -> None:
        result = parse_stream_quota({"cmsBattSoc": 0.0, "soc": 94})

        assert result["soc_pct"] == 0
        assert result["unit_soc_pct"] == 94

    def test_zero_system_figure_suppresses_the_stand_in(self) -> None:
        """Presence, not truthiness, decides whether a stand-in is offered."""
        result = parse_stream_quota({"cmsBattSoc": 0.0, "soc": 94})

        assert SOC_FALLBACK_KEY not in result

    def test_zero_unit_figure_still_stands_in(self) -> None:
        """An empty battery reporting only its own figure keeps its sensor."""
        result = parse_stream_quota({"soc": 0})

        assert result["unit_soc_pct"] == 0
        assert result[SOC_FALLBACK_KEY] == 0


FEED_IN_FIXTURE = (
    Path(__file__).parent / "fixtures" / "stream" / "bk61_feed_in_quota_509.json"
)


class TestFeedInControl:
    """`feedGridMode` is published as a bool, never as the raw number.

    The generic switch read treats any non-zero number as on, so a raw 1
    (feed-in off) would show as on.
    """

    def test_mode_2_is_on(self) -> None:
        result = parse_stream_quota({"feedGridMode": 2})

        assert result["feed_in_control_enabled"] is True

    def test_mode_1_is_off(self) -> None:
        result = parse_stream_quota({"feedGridMode": 1})

        assert result["feed_in_control_enabled"] is False

    @pytest.mark.parametrize("value", [0, 3, True, False, "2", "1", 2.5, None])
    def test_other_values_are_unknown_not_a_state(self, value: object) -> None:
        """A present key outside 1/2 must overwrite the old state with None."""
        result = parse_stream_quota({"feedGridMode": value})

        assert "feed_in_control_enabled" in result
        assert result["feed_in_control_enabled"] is None

    def test_absent_key_leaves_the_state_alone(self) -> None:
        """A push without the key is no update: the key must not appear."""
        result = parse_stream_quota({"powGetSysGrid": 79.0})

        assert "feed_in_control_enabled" not in result
        assert "feedGridMode" not in result

    def test_raw_key_does_not_leak(self) -> None:
        assert "feedGridMode" not in parse_stream_quota({"feedGridMode": 2})

    def test_diagnostics_snapshots_from_discussion_509(self) -> None:
        """Both app positions from the BK61 diagnostics read back correctly."""
        snapshots = json.loads(FEED_IN_FIXTURE.read_text())

        on = parse_stream_quota(snapshots["on"])
        off = parse_stream_quota(snapshots["off"])

        assert on["feed_in_control_enabled"] is True
        assert off["feed_in_control_enabled"] is False
        assert on["grid_w"] == 198.0
        assert off["grid_w"] == 0.0


class TestGridConnectionAndSockets:
    """The AC readings that stayed unknown in Standard Mode (#659).

    Each quota key is the JSON name of a field the protobuf path already maps
    onto the same sensor key, so both modes feed one entity.
    """

    def test_the_real_ac_pro_quota_fills_grid_connection_and_both_sockets(
        self,
    ) -> None:
        result = parse_stream_quota(TestRealAcProQuota.QUOTA)

        assert result["ac_grid_connection_power_w"] == pytest.approx(-184.0303)
        assert result["ac_outlet_1_enabled"] is True
        assert result["ac_outlet_2_enabled"] is True

    def test_sockets_switched_off_read_off(self) -> None:
        """Both relays false on the Ultra X in Discussion #509."""
        snapshot = json.loads(FEED_IN_FIXTURE.read_text())["on"]

        result = parse_stream_quota(snapshot)

        assert result["ac_outlet_1_enabled"] is False
        assert result["ac_outlet_2_enabled"] is False
        assert result["ac_grid_connection_power_w"] == pytest.approx(63.671654)

    def test_relay2_is_socket_1_and_relay3_is_socket_2(self) -> None:
        """`relay2_onoff` is field 980 and `relay3_onoff` 982 on the wire."""
        result = parse_stream_quota({"relay2Onoff": True, "relay3Onoff": False})

        assert result["ac_outlet_1_enabled"] is True
        assert result["ac_outlet_2_enabled"] is False

    def test_voltage_current_frequency_and_socket_2_power(self) -> None:
        result = parse_stream_quota(
            {
                "gridConnectionVol": 239.6,
                "gridConnectionAmp": 1.3,
                "gridConnectionFreq": 49.92,
                "powGetSchuko2": 12.0,
            }
        )

        assert result["ac_voltage_v"] == pytest.approx(239.6)
        assert result["ac_current_a"] == pytest.approx(1.3)
        assert result["ac_frequency_hz"] == pytest.approx(49.92)
        assert result["ac_outlet_2_w"] == pytest.approx(12.0)

    @pytest.mark.parametrize("value", ["true", 2, 1.0, None])
    def test_a_relay_value_that_is_not_a_state_is_dropped(self, value) -> None:
        result = parse_stream_quota({"relay2Onoff": value})

        assert "ac_outlet_1_enabled" not in result

    def test_a_relay_sent_as_zero_or_one_still_reads(self) -> None:
        result = parse_stream_quota({"relay2Onoff": 0, "relay3Onoff": 1})

        assert result["ac_outlet_1_enabled"] is False
        assert result["ac_outlet_2_enabled"] is True
