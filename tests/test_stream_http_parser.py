"""Tests for the Stream (BK-series) JSON quota parser."""

from ecoflow_energy.ecoflow.parsers.stream_http import parse_stream_quota


class TestFieldMapping:
    def test_per_string_pv_is_mapped(self) -> None:
        """The per-string PV inputs issue #139 asks for reach sensor keys."""
        result = parse_stream_quota({
            "powGetPv": 518.0,
            "powGetPv2": 301.0,
            "powGetPv3": 188.0,
            "powGetPv4": 12.0,
        })

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
        result = parse_stream_quota({
            "powGetSysLoad": 255.0,
            "powGetSysGrid": 79.0,
            "powGetSysLoadFromPv": 176.0,
            "powGetSchuko1": 40.0,
            "plugInInfoPvVol": 38.5,
        })

        assert result["home_w"] == 255.0
        assert result["grid_w"] == 79.0
        assert result["home_from_solar_w"] == 176.0
        assert result["ac_outlet_1_w"] == 40.0
        assert result["pv_voltage_v"] == 38.5

    def test_percent_keys_become_integers(self) -> None:
        """SoC and SoH are whole percent, matching the protobuf path."""
        result = parse_stream_quota({"soc": 87.4, "soh": 100})

        assert result["soc_pct"] == 87
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
        result = parse_stream_quota({
            "powGetPv": 100.0,
            "bmsFaultState": 0,
            "packSn": "BK11TEST00000001",
            "energyStrategyOperateMode.operateSelfPoweredOpen": True,
        })

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
