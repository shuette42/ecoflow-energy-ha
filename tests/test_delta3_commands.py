"""Tests for the Delta 3 SET command builders.

The envelope and the value ranges are vendor-specified, see
docs/reference/ecoflow-api-delta3-max-plus.md. These tests pin both so a
refactor cannot silently change what reaches the device.
"""

from __future__ import annotations

import pytest

from custom_components.ecoflow_energy.ecoflow.delta3_commands import (
    DELTA3_NUMBER_PARAMS,
    DELTA3_SWITCH_PARAMS,
    build_number_command,
    build_switch_command,
)

EXPECTED_ENVELOPE = {
    "cmdId": 17,
    "cmdFunc": 254,
    "dest": 2,
    "dirDest": 1,
    "dirSrc": 1,
    "needAck": True,
}


class TestEnvelope:
    def test_every_switch_carries_the_documented_envelope(self) -> None:
        for key in DELTA3_SWITCH_PARAMS:
            cmd = build_switch_command(key, True)
            assert cmd is not None
            for field, value in EXPECTED_ENVELOPE.items():
                assert cmd[field] == value, f"{key}: {field}"

    def test_every_number_carries_the_documented_envelope(self) -> None:
        for key in DELTA3_NUMBER_PARAMS:
            cmd = build_number_command(key, 10)
            assert cmd is not None
            for field, value in EXPECTED_ENVELOPE.items():
                assert cmd[field] == value, f"{key}: {field}"

    def test_source_direction_is_dir_src_not_dir_soc(self) -> None:
        """The MQTT sample in the vendor docs spells this `dirSoc`.

        That reads like a typo for a direction *source*, and the HTTP samples
        all use `dirSrc`. Pinned so nobody "fixes" it back.
        """
        cmd = build_switch_command("ac_out_switch", True)
        assert "dirSrc" in cmd
        assert "dirSoc" not in cmd


class TestSwitchCommands:
    @pytest.mark.parametrize(
        ("key", "params_key"),
        [
            ("ac_out_switch", "cfgAcOutOpen"),
            ("ac2_out_switch", "cfgAc2OutOpen"),
            ("dc_12v_out_switch", "cfgDc12vOutOpen"),
            ("xboost_switch", "cfgXboostEn"),
            ("beeper_switch", "cfgBeepEn"),
            ("bypass_out_disable_switch", "cfgBypassOutDisable"),
        ],
    )
    def test_flat_switches_map_to_the_documented_param(
        self, key: str, params_key: str
    ) -> None:
        assert build_switch_command(key, True)["params"] == {params_key: True}
        assert build_switch_command(key, False)["params"] == {params_key: False}

    def test_energy_backup_is_nested(self) -> None:
        """cfgEnergyBackup is the only control with a nested payload."""
        cmd = build_switch_command("energy_backup_switch", True)
        assert cmd["params"] == {"cfgEnergyBackup": {"energyBackupEn": True}}

    def test_values_are_real_booleans(self) -> None:
        """The contract types these as bool, not 0/1."""
        params = build_switch_command("ac_out_switch", 1)["params"]
        assert params["cfgAcOutOpen"] is True

    def test_unknown_key_returns_none(self) -> None:
        assert build_switch_command("no_such_switch", True) is None


class TestNumberCommands:
    @pytest.mark.parametrize(
        ("key", "params_key", "value"),
        [
            ("backup_reserve_soc", "cfgBackupReverseSoc", 41),
            ("max_charge_soc", "cfgMaxChgSoc", 80),
            ("min_discharge_soc", "cfgMinDsgSoc", 20),
        ],
    )
    def test_in_range_values_pass_through(
        self, key: str, params_key: str, value: int
    ) -> None:
        assert build_number_command(key, value)["params"] == {params_key: value}

    @pytest.mark.parametrize(
        ("key", "params_key", "low", "high"),
        [
            ("backup_reserve_soc", "cfgBackupReverseSoc", 0, 50),
            ("max_charge_soc", "cfgMaxChgSoc", 50, 100),
            ("min_discharge_soc", "cfgMinDsgSoc", 0, 30),
        ],
    )
    def test_values_are_clamped_to_the_vendor_range(
        self, key: str, params_key: str, low: int, high: int
    ) -> None:
        assert build_number_command(key, low - 10)["params"][params_key] == low
        assert build_number_command(key, high + 10)["params"][params_key] == high
        assert build_number_command(key, low)["params"][params_key] == low
        assert build_number_command(key, high)["params"][params_key] == high

    def test_backup_reserve_tops_out_at_fifty_not_hundred(self) -> None:
        """Easy to get wrong: this is a ratio, not a SoC target."""
        assert build_number_command("backup_reserve_soc", 100)["params"][
            "cfgBackupReverseSoc"
        ] == 50

    def test_float_input_is_rounded_to_int(self) -> None:
        params = build_number_command("max_charge_soc", 79.6)["params"]
        assert params["cfgMaxChgSoc"] == 80
        assert isinstance(params["cfgMaxChgSoc"], int)

    def test_unknown_key_returns_none(self) -> None:
        assert build_number_command("no_such_number", 50) is None
