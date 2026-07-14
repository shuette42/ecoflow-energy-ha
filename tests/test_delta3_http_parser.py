"""Tests for the Delta 3 Max Plus HTTP quota parser.

Covers the flat field map (direct scaling, integer rounding), the
charge/discharge state enum, the derived output-flow booleans
(value != 4 = flowing), and the nested per-outlet AC power array.
"""

from __future__ import annotations

import pytest

from ecoflow_energy.ecoflow.parsers.delta3_http import (
    DELTA3_HTTP_FIELD_MAP,
    parse_delta3_http_quota,
)

# Synthetic but plausible full quota response covering every mapped key
# plus the enum, the three flow fields and the nested AC-outlet array.
FULL_QUOTA: dict = {
    # Battery / SoC
    "cmsBattSoc": 85.6,
    "bmsBattSoc": 84.9,
    # Remaining time (minutes)
    "cmsChgRemTime": 143,
    "cmsDsgRemTime": 5999,
    # Power (W, direct)
    "powInSumW": 512.4,
    "powOutSumW": 231.5,
    "powGetAcIn": 500.0,
    "powGetPv": 210.7,
    "powGetPv2": 98.2,
    "powGet12v": 24.3,
    "powGetTypec1": 45.0,
    "powGetTypec2": 0,
    "powGetTypec3": 18.6,
    "powGetQcusb1": 10.1,
    "powGetQcusb2": 0,
    # SoC limits / backup reserve
    "cmsMaxChgSoc": 100,
    "cmsMinDsgSoc": 5,
    "backupReverseSoc": 30,
    # Boolean flags
    "xboostEn": 1,
    "enBeep": 0,
    "energyBackupEn": 1,
    "bypassOutDisable": 0,
    # Enum
    "cmsChgDsgState": 2,
    # Output flow states (4 = no flow)
    "flowInfoAcOut": 2,
    "flowInfoAc2Out": 4,
    "flowInfo12v": 0,
    # Per-outlet AC power (signed, item[0]=AC1, item[2]=AC2)
    "powGetAcOutList": {"powGetAcOutItem": [-120.4, 0, -85.6]},
}

EXPECTED_FULL: dict = {
    "cms_batt_soc": 86,
    "bms_batt_soc": 85,
    "chg_remain_time_min": 143,
    "dsg_remain_time_min": 5999,
    "pow_in_sum_w": 512,
    "pow_out_sum_w": 232,
    "ac_in_w": 500,
    "pv1_in_w": 211,
    "pv2_in_w": 98,
    "dc_12v_out_w": 24,
    "typec1_w": 45,
    "typec2_w": 0,
    "typec3_w": 19,
    "usb_qc1_w": 10,
    "usb_qc2_w": 0,
    "max_charge_soc_pct": 100,
    "min_discharge_soc_pct": 5,
    "backup_reserve_soc_pct": 30,
    "xboost_enabled": 1,
    "beeper_enabled": 0,
    "backup_reserve_enabled": 1,
    "bypass_out_disabled": 0,
    "chg_dsg_state": "charging",
    "ac_out_flow": 1,
    "ac2_out_flow": 0,
    "dc_12v_out_flow": 1,
    "ac1_out_w": 120,
    "ac2_out_w": 86,
}


class TestDelta3FieldMap:
    def test_full_quota_parses_every_mapped_key(self) -> None:
        assert parse_delta3_http_quota(FULL_QUOTA) == EXPECTED_FULL

    def test_full_quota_covers_entire_field_map(self) -> None:
        # Guard: the fixture must not silently miss newly added map entries.
        assert set(DELTA3_HTTP_FIELD_MAP) <= set(FULL_QUOTA)

    def test_float_values_round_to_clean_ints(self) -> None:
        result = parse_delta3_http_quota({"cmsBattSoc": 85.6})
        assert result["cms_batt_soc"] == 86
        assert isinstance(result["cms_batt_soc"], int)

    def test_non_numeric_value_is_dropped(self) -> None:
        assert parse_delta3_http_quota({"cmsBattSoc": "not-a-number"}) == {}

    def test_remain_time_passes_through_uncapped(self) -> None:
        result = parse_delta3_http_quota({"cmsChgRemTime": 143999})
        assert result["chg_remain_time_min"] == 143999

    def test_backup_reserve_soc_passes_through_unclamped(self) -> None:
        result = parse_delta3_http_quota({"backupReverseSoc": 90})
        assert result["backup_reserve_soc_pct"] == 90

    def test_unmapped_keys_are_ignored(self) -> None:
        raw = {"someKey": 42, "another.key": 3.14, "bpSoc": 80}
        assert parse_delta3_http_quota(raw) == {}

    def test_handles_empty_input(self) -> None:
        assert parse_delta3_http_quota({}) == {}


class TestDelta3ChgDsgState:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(0, "idle"), (1, "discharging"), (2, "charging")],
    )
    def test_known_values_map_to_labels(self, raw: int, expected: str) -> None:
        result = parse_delta3_http_quota({"cmsChgDsgState": raw})
        assert result["chg_dsg_state"] == expected

    def test_unknown_value_omits_key(self) -> None:
        result = parse_delta3_http_quota({"cmsChgDsgState": 7})
        assert "chg_dsg_state" not in result


class TestDelta3FlowStates:
    @pytest.mark.parametrize(
        "http_key,sensor_key",
        [
            ("flowInfoAcOut", "ac_out_flow"),
            ("flowInfoAc2Out", "ac2_out_flow"),
            ("flowInfo12v", "dc_12v_out_flow"),
        ],
    )
    def test_value_4_means_off(self, http_key: str, sensor_key: str) -> None:
        result = parse_delta3_http_quota({http_key: 4})
        assert result[sensor_key] == 0

    @pytest.mark.parametrize(
        "http_key,sensor_key",
        [
            ("flowInfoAcOut", "ac_out_flow"),
            ("flowInfoAc2Out", "ac2_out_flow"),
            ("flowInfo12v", "dc_12v_out_flow"),
        ],
    )
    @pytest.mark.parametrize("raw", [0, 2])
    def test_non_4_values_mean_on(
        self, http_key: str, sensor_key: str, raw: int
    ) -> None:
        result = parse_delta3_http_quota({http_key: raw})
        assert result[sensor_key] == 1


class TestDelta3AcOutArray:
    def test_signed_values_become_absolute(self) -> None:
        raw = {"powGetAcOutList": {"powGetAcOutItem": [-150.4, 0, -75.6]}}
        result = parse_delta3_http_quota(raw)
        assert result["ac1_out_w"] == 150
        assert result["ac2_out_w"] == 76

    def test_dotted_key_form_is_accepted(self) -> None:
        raw = {"powGetAcOutList.powGetAcOutItem": [-150, 0, -75]}
        result = parse_delta3_http_quota(raw)
        assert result["ac1_out_w"] == 150
        assert result["ac2_out_w"] == 75

    def test_short_array_emits_only_ac1(self) -> None:
        raw = {"powGetAcOutList": {"powGetAcOutItem": [-150, 0]}}
        result = parse_delta3_http_quota(raw)
        assert result["ac1_out_w"] == 150
        assert "ac2_out_w" not in result

    def test_missing_array_emits_no_ac_keys(self) -> None:
        result = parse_delta3_http_quota({"cmsBattSoc": 80})
        assert "ac1_out_w" not in result
        assert "ac2_out_w" not in result

    def test_non_numeric_items_are_skipped(self) -> None:
        raw = {"powGetAcOutList": {"powGetAcOutItem": ["oops", 0, None]}}
        result = parse_delta3_http_quota(raw)
        assert "ac1_out_w" not in result
        assert "ac2_out_w" not in result

    def test_non_list_payload_is_ignored(self) -> None:
        raw = {"powGetAcOutList": {"powGetAcOutItem": "garbage"}}
        assert parse_delta3_http_quota(raw) == {}
