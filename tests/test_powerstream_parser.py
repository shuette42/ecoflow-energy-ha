"""Tests for the PowerStream microinverter quota parser.

The fixture is the reporter capture from #188 with the identifying and
location-revealing keys removed. It is the only PowerStream data that
exists here, so the assertions that matter most are the ones checking the
device against itself: the power balance closes at 0.1 W per count, which
is what makes the deciwatt scale a measurement rather than a convention.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecoflow_energy.const import POWERSTREAM_POWER_TO_ENERGY, POWERSTREAM_SENSORS
from ecoflow_energy.ecoflow.parsers.powerstream_http import parse_powerstream_quota

FIXTURE = Path(__file__).parent / "fixtures" / "powerstream" / "hw51_quota_masked.json"


@pytest.fixture(scope="module")
def capture() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def parsed(capture: dict) -> dict:
    return parse_powerstream_quota(capture)


class TestTheCaptureItself:
    """The capture has to keep saying what the field map was built on."""

    def test_the_power_balance_closes_at_a_tenth_of_a_watt(self, capture: dict) -> None:
        """This is the evidence for the deciwatt scale, not an illustration.

        Every power reading in the field map rests on this arithmetic, so if
        a future edit swaps the fixture for one where it no longer holds, the
        scale has lost its proof and this test has to say so.
        """
        pv1 = capture["20_1.pv1InputWatts"]
        pv2 = capture["20_1.pv2InputWatts"]
        assert pv1 + pv2 == capture["20_1.pvToInvWatts"]
        assert pv1 + pv2 == capture["20_1.invOutputWatts"]

        to_plug = capture["20_1.invToPlugWatts"]
        assert (
            capture["20_1.invOutputWatts"] - to_plug == -capture["20_1.gridConsWatts"]
        )

    def test_the_unit_is_exporting_in_this_capture(self, capture: dict) -> None:
        """The sign convention is read off an export, so there has to be one."""
        assert capture["20_1.gridConsWatts"] < 0


class TestFieldMapping:
    def test_power_is_scaled_from_deciwatt(self, parsed: dict) -> None:
        assert parsed["pv1_w"] == 51.0
        assert parsed["pv2_w"] == 58.0
        assert parsed["inv_output_w"] == 109.0
        assert parsed["plug_total_w"] == 1.0

    def test_solar_is_the_sum_of_the_strings(self, parsed: dict) -> None:
        assert parsed["solar_w"] == 109.0

    def test_solar_follows_a_single_string(self) -> None:
        """A unit with one panel still reports a solar total."""
        result = parse_powerstream_quota({"20_1.pv1InputWatts": 510})

        assert result["solar_w"] == 51.0
        assert "pv2_w" not in result

    def test_no_solar_key_without_any_string(self) -> None:
        result = parse_powerstream_quota({"20_1.batSoc": 87})

        assert "solar_w" not in result

    def test_grid_keeps_its_sign(self, parsed: dict) -> None:
        """Negative is export, which is where the balance above ends up."""
        assert parsed["grid_w"] == -108.0

    def test_battery_and_electrical_readings(self, parsed: dict) -> None:
        assert parsed["soc_pct"] == 87
        assert parsed["batt_w"] == 0.0
        assert parsed["batt_voltage_v"] == 56.6
        assert parsed["batt_temp_c"] == 34.0
        assert parsed["ac_voltage_v"] == 243.5
        assert parsed["ac_frequency_hz"] == 49.9
        assert parsed["pv1_voltage_v"] == 33.8
        assert parsed["pv2_voltage_v"] == 34.0

    def test_settings_are_reported_back(self, parsed: dict) -> None:
        assert parsed["permanent_watts_w"] == 0.0
        assert parsed["rated_power_w"] == 800.0
        assert parsed["lower_limit_pct"] == 0
        assert parsed["upper_limit_pct"] == 90
        assert parsed["supply_priority"] == "battery_storage"

    def test_brightness_becomes_a_percentage(self, parsed: dict) -> None:
        """The device scale is 0..1023, the entity is a percent."""
        assert parsed["led_brightness"] == 13

    def test_wifi_signal_stays_negative_and_whole(self, parsed: dict) -> None:
        assert parsed["wifi_rssi_dbm"] == -44

    def test_supply_priority_maps_both_documented_values(self) -> None:
        assert parse_powerstream_quota({"20_1.supplyPriority": 0})[
            "supply_priority"
        ] == ("power_supply")
        assert parse_powerstream_quota({"20_1.supplyPriority": 1})[
            "supply_priority"
        ] == ("battery_storage")

    def test_an_undocumented_priority_is_dropped(self) -> None:
        """An enum sensor handed an option it does not have logs an error."""
        assert "supply_priority" not in parse_powerstream_quota(
            {"20_1.supplyPriority": 7}
        )


class TestWhatIsDeliberatelyNotMapped:
    """Each of these was looked at and left out. See the module docstring
    of the parser for the reason attached to each one."""

    @pytest.mark.parametrize(
        "sensor_key",
        [
            "pv1_current_a",
            "pv2_current_a",
            "ac_current_a",
            "chg_remain_time_min",
            "dsg_remain_time_min",
        ],
    )
    def test_unverified_readings_produce_no_key(
        self, parsed: dict, sensor_key: str
    ) -> None:
        assert sensor_key not in parsed

    def test_no_raw_quota_key_survives(self, parsed: dict) -> None:
        """Nothing camelCase may reach the device data store."""
        assert not [key for key in parsed if key.startswith("20_1.")]
        assert "geneWatt" not in parsed
        assert "historyPvToInvWatts" not in parsed

    def test_unknown_keys_are_ignored(self) -> None:
        result = parse_powerstream_quota({"20_1.somethingNew": 42, "20_1.batSoc": 50})

        assert result == {"soc_pct": 50}

    def test_a_key_without_the_namespace_is_not_read(self) -> None:
        """The Stream sends `soc` unprefixed; the two must not cross."""
        assert parse_powerstream_quota({"batSoc": 87}) == {}


class TestRobustness:
    def test_booleans_never_become_measurements(self) -> None:
        """`bool` is an `int` in Python, so a flag on a power key would
        otherwise publish as 0 W or 1 W."""
        result = parse_powerstream_quota(
            {"20_1.invOutputWatts": True, "20_1.batSoc": False}
        )

        assert result == {}

    def test_strings_are_dropped(self) -> None:
        assert parse_powerstream_quota({"20_1.invOutputWatts": "1090"}) == {}

    def test_a_non_dict_payload_returns_nothing(self) -> None:
        assert parse_powerstream_quota(None) == {}
        assert parse_powerstream_quota([]) == {}

    def test_an_empty_payload_returns_nothing(self) -> None:
        assert parse_powerstream_quota({}) == {}


class TestEntityContract:
    def test_every_parsed_key_has_a_sensor(self, parsed: dict) -> None:
        """A parsed key with no entity is data nobody can see."""
        defined = {definition.key for definition in POWERSTREAM_SENSORS}

        assert set(parsed) <= defined, sorted(set(parsed) - defined)

    def test_every_sensor_is_fed_by_the_parser_or_the_integrator(
        self, parsed: dict
    ) -> None:
        """The other direction: an entity nothing fills reads unknown forever,
        which is the #188 failure in miniature."""
        integrated = set(POWERSTREAM_POWER_TO_ENERGY.values())

        for definition in POWERSTREAM_SENSORS:
            assert definition.key in parsed or definition.key in integrated, (
                definition.key
            )

    def test_the_energy_map_only_integrates_readings_that_exist(
        self, parsed: dict
    ) -> None:
        for power_key in POWERSTREAM_POWER_TO_ENERGY:
            assert power_key in parsed, power_key

    def test_signed_and_unconfirmed_readings_stay_out_of_the_counters(self) -> None:
        """`grid_w` has no observed import case and `batt_w` has its sign from
        a field name. Either one in a monotonic counter is unrecoverable."""
        assert "grid_w" not in POWERSTREAM_POWER_TO_ENERGY
        assert "batt_w" not in POWERSTREAM_POWER_TO_ENERGY
