"""Tests for entity definitions in const.py — uniqueness and completeness."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from ecoflow_energy.const import (
    DELTA_PROFILE_R331,
    DELTA_PROFILE_R351,
    POWEROCEAN_SENSORS,
    DELTA2MAX_SENSORS,
    DELTA3_SELECTS,
    DELTA3_SENSORS,
    DELTA3_POWER_TO_ENERGY,
    SMARTPLUG_SENSORS,
    STREAM_SENSORS,
    STREAM_NUMBERS,
    STREAM_BINARY_SENSORS,
    DELTA2MAX_BINARY_SENSORS,
    DELTA2MAX_SWITCHES,
    DELTA2MAX_NUMBERS,
    POWEROCEAN_BINARY_SENSORS,
    STREAM_SWITCHES,
    SMARTPLUG_SWITCHES,
    SMARTPLUG_NUMBERS,
    RAW_FRAME_BUNDLE_MAX_BYTES,
    RAW_FRAME_KEYS_MAX,
    RAW_FRAME_LOG_KEYS_MAX,
    RAW_FRAME_LOG_PER_KEY_MAX,
    RAW_FRAME_MAX_BYTES,
    RAW_FRAME_PER_KEY_MAX,
    get_device_name,
    get_device_type,
    get_delta_profile,
)
from ecoflow_energy.ecoflow.const import (
    _NOT_THIS_FAMILY_KEYWORDS,
    _SN_PREFIX_DISPLAY_NAMES,
    _SN_PREFIX_MAP,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestDeltaProfileRouting:
    def test_delta2max_r351_by_name(self):
        assert get_delta_profile("Delta 2 Max", "DAEBK5ZZ12340001") == DELTA_PROFILE_R351

    def test_legacy_delta_max_r331_by_name(self):
        assert get_delta_profile("Delta Max", "RXXX123456789012") == DELTA_PROFILE_R331

    def test_r331_sn_prefix_wins(self):
        assert get_delta_profile("Delta 2 Max", "R331ABCDEF123456") == DELTA_PROFILE_R331

    def test_delta2_without_max_is_r331(self):
        assert get_delta_profile("Delta 2", "DAEBK5ZZ12340001") == DELTA_PROFILE_R331

    def test_unknown_sn_unknown_name_defaults_r351(self):
        assert get_delta_profile("Delta Pro", "XXXX123456789012") == DELTA_PROFILE_R351


class TestDeviceTypeRouting:
    def test_powerstream_is_not_a_stream(self) -> None:
        """"PowerStream" contains "stream", and the match is by substring.

        A PowerStream microinverter was classified as a Stream battery and
        given its whole entity set. It connected, reported nothing this
        parser understands and settled on stale, so the owner saw a full
        device with 54 readings that could never fill (#188). Unsupported is
        the honest answer and the one that leads somewhere: it asks for a
        capture instead of pretending.
        """
        assert get_device_type("PowerStream", "HW51TEST00000001") == "unknown"
        assert get_device_type("Power Stream", "HW51TEST00000001") == "unknown"

    def test_the_real_stream_family_still_routes(self) -> None:
        """The guard above must not cost the devices it sits in front of.

        A guard, not a regression detector: it passes on the code before the
        PowerStream fix too. It is here so that a future addition to the
        not-this-family list cannot quietly swallow a real Stream.
        """
        for name in ("Stream AC Pro", "Stream Micro", "Stream Ultra", "STREAM AC"):
            assert get_device_type(name, "") == "stream", name

    def test_stream_detected_by_bk31_prefix(self) -> None:
        assert get_device_type("", "BK31_TEST_DEVICE") == "stream"

    def test_stream_detected_by_bk11_prefix(self) -> None:
        assert get_device_type("", "BK11TEST00000001") == "stream"

    def test_stream_detected_by_bk41_prefix(self) -> None:
        assert get_device_type("", "BK41TEST00000001") == "stream"

    def test_stream_detected_by_bk51_prefix(self) -> None:
        assert get_device_type("", "BK51TEST00000001") == "stream"

    def test_stream_detected_by_bk61_prefix(self) -> None:
        assert get_device_type("", "BK61TEST00000001") == "stream"

    def test_delta3_by_product_name(self) -> None:
        # "DELTA 3 Max Plus" contains "delta" but must route to delta3,
        # not the Delta 2 Max parser (root cause of the #110 report).
        assert get_device_type("DELTA 3 Max Plus", "") == "delta3"

    def test_j32d_powerocean_by_sn_prefix(self) -> None:
        # European PowerOcean variant (#89): app API returns empty
        # product_name, so classification relies on the SN prefix.
        assert get_device_type("", "J32DTEST00000001") == "powerocean"

    def test_j32e_powerocean_by_sn_prefix(self) -> None:
        # Single-phase European PowerOcean variant (#89): same empty
        # product_name behavior as J32D, classified via SN prefix.
        assert get_device_type("", "J32ETEST00000001") == "powerocean"

    def test_j32b_powerocean_by_sn_prefix(self) -> None:
        # PowerOcean variant (#194): a raw capture from the reporter unit
        # carries a cmd_func 96 frame, which is the PowerOcean command
        # family, so the unit is routed to the PowerOcean parser.
        assert get_device_type("", "J32BTEST00000001") == "powerocean"

    def test_j327_powerocean_by_sn_prefix(self) -> None:
        # Single-phase 5 kW hybrid inverter (#225): the reporter capture holds
        # `96/8`, `96/1`, `96/7` and `96/13`, all of which the PowerOcean
        # parser already decodes, so routing delivers live data rather than
        # recognition alone.
        assert get_device_type("", "J327TEST00000001") == "powerocean"

    def test_powerocean_plus_by_sn_prefix(self) -> None:
        # PowerOcean Plus variants (#88): higher-power 3-phase hybrid units,
        # Enhanced mode only, classified via SN prefix like J32D/J32E.
        assert get_device_type("", "R371TEST00000001") == "powerocean"
        assert get_device_type("", "R374TEST00000001") == "powerocean"
        assert get_device_type("", "HJ3CTEST00000001") == "powerocean"

    def test_hj35_powerocean_by_sn_prefix(self) -> None:
        # PowerOcean gateway variant (#165), routed on a third-party report
        # rather than a capture from an owner of this integration.
        assert get_device_type("", "HJ35TEST00000001") == "powerocean"

    def test_delta3_by_sn_prefix(self) -> None:
        assert get_device_type("", "D3M1TEST00000001") == "delta3"

    def test_delta3_classic_by_p321_sn_prefix(self) -> None:
        assert get_device_type("", "P321TEST00000001") == "delta3"

    def test_base_delta3_by_p231_sn_prefix(self) -> None:
        """#182: the base DELTA 3 pushes the same frames as a Max Plus and
        decodes through the same binding, so it shares the device type."""
        assert get_device_type("", "P231TEST00000001") == "delta3"

    def test_delta3_max_by_d3n1_sn_prefix(self) -> None:
        """#216: the DELTA 3 Max sits in the same product family as the three
        Delta 3 prefixes already routed here.

        The app's own device registry names it `product_ps_delta_delta_3_m`,
        against `_m_p` for the Max Plus, `_c` for the Classic and the bare
        stem for the base unit. All four are the same line.
        """
        assert get_device_type("", "D3N1TEST00000001") == "delta3"

    def test_powerocean_plus_20kw_by_r372_sn_prefix(self) -> None:
        """#205: the 20kW Plus sits between the 15kW R371 and the 30kW R374.

        Same product type and same `product_smart_re_307` family stem as both
        of its neighbours, which are supported.
        """
        assert get_device_type("", "R372TEST00000001") == "powerocean"

    def test_base_delta3_display_name(self) -> None:
        # A base DELTA 3 reports an empty product name, same as the BK series.
        assert get_device_name("", "P231TEST00000001") == "DELTA 3 (0001)"
        assert get_device_name("", "P231ABCDXYZ") == "DELTA 3"
        assert get_device_name("DELTA 3 Plus", "P231TEST00000001") == "DELTA 3 Plus"

    def test_stream_display_name_by_bk_prefix(self) -> None:
        assert get_device_name("", "BK11TEST00000001") == "Stream Ultra (0001)"
        assert get_device_name("", "BK31TEST00000001") == "Stream AC Pro (0001)"
        assert get_device_name("", "BK41TEST00000001") == "Stream Max (0001)"
        assert get_device_name("", "BK51TEST00000001") == "Stream AC (0001)"
        assert get_device_name("", "BK61TEST00000001") == "Stream Ultra X (0001)"

    def test_device_name_prefers_product_name(self) -> None:
        # A non-empty product name always wins over any prefix-derived name.
        assert get_device_name("Stream Ultra X", "BK61TEST00000001") == "Stream Ultra X"

    def test_device_name_only_for_prefixes_without_a_product_name(self) -> None:
        # The prefix-derived friendly name exists for the families that report
        # an empty product name (Stream, base DELTA 3). Every other device type
        # returns an empty string so callers keep their own fallback.
        assert get_device_name("", "R351TEST00000001") == ""
        assert get_device_name("", "HW52FAKE00000001") == ""
        assert get_device_name("", "J32DTEST00001234") == ""
        assert get_device_name("", "P321TEST00005678") == ""
        assert get_device_name("", "") == ""

    def test_stream_display_name_without_numeric_tail(self) -> None:
        # A non-numeric serial tail drops the suffix rather than guessing.
        assert get_device_name("", "BK11ABCDXYZ") == "Stream Ultra"

    def test_delta3_keyword_wins_over_delta(self) -> None:
        # The delta3 keyword check runs before the generic delta check.
        assert get_device_type("Delta3", "") == "delta3"

    def test_delta2_max_unchanged(self) -> None:
        assert get_device_type("DELTA 2 Max", "R351TEST00000001") == "delta"

    def test_stream_ac5000_by_es22_prefix(self) -> None:
        # STREAM AC 5000 (#177): its own device type, not the BK-series
        # Stream one - it shares no telemetry command with them.
        assert get_device_type("", "ES22TEST00000001") == "stream_ac5000"

    def test_stream_ac5000_display_name(self) -> None:
        # Reports an empty product name through the app API.
        assert get_device_name("", "ES22TEST00000001") == "STREAM AC 5000 (0001)"

    def test_sn_prefix_wins_over_a_matching_product_name(self) -> None:
        # "STREAM AC 5000" contains the BK-series "stream" keyword. The
        # prefix is exact evidence, so it decides even when the product
        # name is populated and would match a different family.
        assert get_device_type("STREAM AC 5000", "ES22TEST00000001") == "stream_ac5000"

    def test_a_known_prefix_wins_over_the_not_this_family_guard(self) -> None:
        """Stating the cost of that ordering, so the two tests below have a
        reason to exist.

        The prefix is consulted first, which means the guard only ever runs
        for a serial the prefix map does not know.
        """
        assert get_device_type("PowerStream", "ES22TEST00000001") == "stream_ac5000"

    def test_the_powerstream_prefix_stays_out_of_the_map(self) -> None:
        """A PowerStream reports HW51 and only HW52 is mapped, which is the
        whole reason the #188 guard still fires after the reorder."""
        assert "HW51" not in _SN_PREFIX_MAP
        assert get_device_type("PowerStream", "HW51TEST00000001") == "unknown"

    def test_no_mapped_prefix_belongs_to_a_guarded_family(self) -> None:
        """A prefix added for a device the guard rejects would silently
        reinstate #188: the map answers before the guard is consulted, so the
        unsupported-device notice that asks for a capture never appears."""
        for prefix, device_type in _SN_PREFIX_MAP.items():
            name = _SN_PREFIX_DISPLAY_NAMES.get(prefix, "").lower()
            for keyword in _NOT_THIS_FAMILY_KEYWORDS:
                assert keyword not in name, (
                    f"{prefix} routes to {device_type} but names itself "
                    f"'{name}', which the not-this-family guard rejects"
                )

    def test_bk21_smart_meter_stays_unknown(self) -> None:
        # Smart Meter support is deferred: it must remain unknown so it
        # shows up as a visible skip, not silently mapped to a wrong parser.
        assert get_device_type("", "BK21TEST00000001") == "unknown"

    def test_plain_delta_pro_stays_delta(self) -> None:
        # "DELTA Pro" (no "3") keeps its existing Delta behavior.
        assert get_device_type("DELTA Pro", "") == "delta"


def _extract_sensor_keys(var_name: str) -> list[str]:
    """Extract sensor keys from a named list variable.

    Uses runtime import for lists that are dynamically extended (e.g. pack sensors),
    falls back to AST extraction for lists defined purely as literals.
    """
    # Runtime approach — covers dynamically extended lists
    _RUNTIME_MAP = {
        "POWEROCEAN_SENSORS": POWEROCEAN_SENSORS,
        "DELTA2MAX_SENSORS": DELTA2MAX_SENSORS,
        "SMARTPLUG_SENSORS": SMARTPLUG_SENSORS,
        "STREAM_SENSORS": STREAM_SENSORS,
        "STREAM_NUMBERS": STREAM_NUMBERS,
        "STREAM_BINARY_SENSORS": STREAM_BINARY_SENSORS,
        "DELTA2MAX_BINARY_SENSORS": DELTA2MAX_BINARY_SENSORS,
        "DELTA2MAX_SWITCHES": DELTA2MAX_SWITCHES,
        "DELTA2MAX_NUMBERS": DELTA2MAX_NUMBERS,
        "POWEROCEAN_BINARY_SENSORS": POWEROCEAN_BINARY_SENSORS,
        "STREAM_SWITCHES": STREAM_SWITCHES,
        "SMARTPLUG_SWITCHES": SMARTPLUG_SWITCHES,
        "SMARTPLUG_NUMBERS": SMARTPLUG_NUMBERS,
    }
    runtime_list = _RUNTIME_MAP.get(var_name)
    if runtime_list is not None:
        return [item.key for item in runtime_list]

    # Fallback: AST extraction for unknown list names
    source = (REPO_ROOT / "custom_components/ecoflow_energy/const.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        target_name = None
        value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
            value = node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
            value = node.value

        if target_name == var_name and isinstance(value, ast.List):
            keys = []
            for elt in value.elts:
                if isinstance(elt, ast.Call) and elt.args:
                    first_arg = elt.args[0]
                    if isinstance(first_arg, ast.Constant):
                        keys.append(first_arg.value)
            return keys
    return []


# EMS and system-level diagnostics, kept apart from the core sensor count so
# that adding one does not silently move the core number.
_PO_EMS_EXTENDED = {
    "ems_charge_upper_limit_pct", "ems_discharge_lower_limit_pct",
    "ems_keep_soc_pct", "ems_backup_ratio_pct",
    "mppt1_fault_code", "mppt2_fault_code",
    "pcs_ac_error_code", "pcs_dc_error_code", "pcs_ac_warning_code",
    "wifi_status", "ethernet_status", "cellular_status",
    "ems_led_brightness", "ems_work_state",
    "ems_total_battery_capacity_wh", "pcs_max_output_power_w",
    "pcs_max_input_power_w", "bp_max_charge_power_w",
    "bp_max_discharge_power_w",
    # From the EMS change report cmd_id=17 (run state, fault flags, AFCI)
    # and the two cmd_id=8 fields that had no entity before.
    "mppt1_warning_code", "mppt2_warning_code",
    "afci_self_test_result", "ems_self_check_state",
    "sys_heat_state", "sys_calibration_state", "parallel_mode",
    "battery_limit_reason", "ems_sg_ready_state",
}


class TestPowerOceanSensors:
    def test_heating_rod_power_is_optional_and_disabled(self):
        sensors = {sensor.key: sensor for sensor in POWEROCEAN_SENSORS}

        heating_rod_power = sensors["heating_rod_power_w"]
        assert heating_rod_power.device_class == "power"
        assert heating_rod_power.state_class == "measurement"
        assert heating_rod_power.disabled_by_default is True
        assert heating_rod_power.unit == "W"
        assert heating_rod_power.entity_category == "diagnostic"
        assert heating_rod_power.suggested_display_precision == 0

    def test_keys_unique(self):
        keys = _extract_sensor_keys("POWEROCEAN_SENSORS")
        assert len(keys) > 30, f"Expected 30+ sensors, got {len(keys)}"
        assert len(keys) == len(set(keys)), "Duplicate PowerOcean sensor keys"

    def test_energy_dashboard_sensors_exist(self):
        keys = _extract_sensor_keys("POWEROCEAN_SENSORS")
        for expected in [
            "solar_energy_kwh",
            "home_energy_kwh",
            "grid_import_energy_kwh",
            "grid_export_energy_kwh",
            "batt_charge_energy_kwh",
            "batt_discharge_energy_kwh",
        ]:
            assert expected in keys, f"Missing Energy Dashboard sensor: {expected}"

    def test_mppt_sensor_definitions(self):
        """Four MPPT strings are defined; only the first two are enabled.

        Strings 3 and 4 exist on PowerOcean Plus only. On every other
        PowerOcean the parser never emits those keys, so an enabled entity
        would sit at "unknown" for the lifetime of the installation.
        """
        sensors = {sensor.key: sensor for sensor in POWEROCEAN_SENSORS}
        for index in range(1, 5):
            for suffix in ("power_w", "voltage_v", "current_a"):
                assert f"mppt_pv{index}_{suffix}" in sensors

        for suffix in ("power_w", "voltage_v", "current_a"):
            assert sensors[f"mppt_pv1_{suffix}"].disabled_by_default is False
            assert sensors[f"mppt_pv2_{suffix}"].disabled_by_default is False
            assert sensors[f"mppt_pv3_{suffix}"].disabled_by_default is True
            assert sensors[f"mppt_pv4_{suffix}"].disabled_by_default is True

    def test_existing_sensors_count(self):
        """Core sensor count, excluding optional Plus and extended EMS fields."""
        keys = _extract_sensor_keys("POWEROCEAN_SENSORS")
        non_pack = [k for k in keys if not k.startswith("pack")]
        mppt_plus = {
            f"mppt_pv{index}_{suffix}"
            for index in (3, 4)
            for suffix in ("power_w", "voltage_v", "current_a")
        }
        ems_extended = _PO_EMS_EXTENDED
        original = [
            k for k in non_pack if k not in ems_extended and k not in mppt_plus
        ]
        assert len(original) == 68, f"Expected 68 core sensors, got {len(original)}"

    def test_mppt_plus_sensor_count(self):
        """6 PowerOcean Plus MPPT sensors (strings 3 and 4)."""
        keys = _extract_sensor_keys("POWEROCEAN_SENSORS")
        mppt_plus = [
            k for k in keys if k.startswith(("mppt_pv3_", "mppt_pv4_"))
        ]
        assert len(mppt_plus) == 6, f"Expected 6 Plus MPPT sensors, got {len(mppt_plus)}"

    def test_pack_sensors_count(self):
        """120 pack sensors (5 packs x 24 sensors)."""
        keys = _extract_sensor_keys("POWEROCEAN_SENSORS")
        pack_keys = [k for k in keys if k.startswith("pack")]
        assert len(pack_keys) == 120, f"Expected 120 pack sensors, got {len(pack_keys)}"

    def test_pack_sensors_per_pack(self):
        """Each pack has exactly 24 sensors."""
        keys = _extract_sensor_keys("POWEROCEAN_SENSORS")
        for n in range(1, 6):
            pack_keys = [k for k in keys if k.startswith(f"pack{n}_")]
            assert len(pack_keys) == 24, f"Expected 24 sensors for pack{n}, got {len(pack_keys)}"

    def test_ems_extended_count(self):
        """28 EMS/system extended sensors."""
        keys = _extract_sensor_keys("POWEROCEAN_SENSORS")
        ems_extended = _PO_EMS_EXTENDED
        found = [k for k in keys if k in ems_extended]
        assert len(found) == 28, f"Expected 28 EMS extended sensors, got {len(found)}"

    def test_total_sensor_count(self):
        """Total PowerOcean sensors = 68 + 6 + 120 + 28 = 222."""
        keys = _extract_sensor_keys("POWEROCEAN_SENSORS")
        assert len(keys) == 222, f"Expected 222 total sensors, got {len(keys)}"


    def test_only_soc_has_battery_device_class(self):
        """Only the primary soc_pct should have device_class='battery'.

        Pack SoC and bp_real_soc_pct must NOT use device_class='battery'
        because HA picks battery-class entities for the device header.
        """
        battery_sensors = [
            s for s in POWEROCEAN_SENSORS if s.device_class == "battery"
        ]
        keys = {s.key for s in battery_sensors}
        assert keys == {"soc_pct"}, (
            f"Expected battery device_class only on {{'soc_pct'}}, "
            f"but found: {keys}"
        )


class TestDelta2MaxSensors:
    def test_keys_unique(self):
        keys = _extract_sensor_keys("DELTA2MAX_SENSORS")
        assert len(keys) > 40, f"Expected 40+ sensors, got {len(keys)}"
        assert len(keys) == len(set(keys)), "Duplicate Delta sensor keys"

    def test_slave_battery_sensors_exist(self):
        keys = _extract_sensor_keys("DELTA2MAX_SENSORS")
        for pack in (1, 2):
            prefix = f"slave{pack}"
            for suffix in (
                "_soc", "_soh", "_voltage_v", "_current_a", "_temp_c",
                "_cycles", "_in_w", "_out_w", "_remain_cap_mah",
                "_full_cap_mah", "_max_cell_vol_mv", "_min_cell_vol_mv",
                "_max_cell_temp_c", "_min_cell_temp_c", "_max_mos_temp_c",
                "_err_code",
            ):
                assert f"{prefix}{suffix}" in keys, f"Missing slave sensor: {prefix}{suffix}"

    def test_slave_sensors_count(self):
        keys = _extract_sensor_keys("DELTA2MAX_SENSORS")
        slave_keys = [k for k in keys if k.startswith("slave")]
        assert len(slave_keys) == 32, f"Expected 32 slave sensors, got {len(slave_keys)}"

    def test_only_soc_sensors_have_battery_device_class(self):
        """Only the primary SoC sensor should have device_class='battery'.

        SoH, secondary SoC variants (bms_precise_soc, ems_lcd_soc,
        ems_precise_soc) and slave-pack SoC must NOT use
        device_class='battery' because HA picks battery-class entities
        for the device header.
        """
        battery_sensors = [
            s for s in DELTA2MAX_SENSORS if s.device_class == "battery"
        ]
        keys = {s.key for s in battery_sensors}
        assert keys == {"soc"}, (
            f"Expected battery device_class only on {{'soc'}}, "
            f"but found: {keys}"
        )

    def test_soh_no_battery_device_class(self):
        """SoH sensors (main + slave) must not have device_class='battery'."""
        soh_sensors = [s for s in DELTA2MAX_SENSORS if "soh" in s.key]
        for s in soh_sensors:
            assert s.device_class != "battery", (
                f"{s.key} has device_class='battery' but SoH is not SoC"
            )

    def test_switch_defs_unique(self):
        keys = _extract_sensor_keys("DELTA2MAX_SWITCHES")
        assert len(keys) == 7
        assert len(keys) == len(set(keys))

    def test_number_defs_unique(self):
        keys = _extract_sensor_keys("DELTA2MAX_NUMBERS")
        assert len(keys) == 8
        assert len(keys) == len(set(keys))


class TestSmartPlugEntities:
    def test_sensor_keys_unique(self):
        keys = _extract_sensor_keys("SMARTPLUG_SENSORS")
        assert len(keys) >= 11, f"Expected 11+ sensors, got {len(keys)}"
        assert len(keys) == len(set(keys)), "Duplicate Smart Plug sensor keys"

    def test_number_defs_unique(self):
        keys = _extract_sensor_keys("SMARTPLUG_NUMBERS")
        assert len(keys) == 2
        assert len(keys) == len(set(keys))
        assert "led_brightness" in keys
        assert "max_watts" in keys

    def test_switch_defs_unique(self):
        keys = _extract_sensor_keys("SMARTPLUG_SWITCHES")
        assert len(keys) == 1
        assert "plug_switch" in keys


class TestStreamEntities:
    def test_sensor_keys_unique(self) -> None:
        keys = _extract_sensor_keys("STREAM_SENSORS")
        assert len(keys) >= 10, f"Expected 10+ sensors, got {len(keys)}"
        assert len(keys) == len(set(keys)), "Duplicate Stream sensor keys"

    def test_core_stream_sensors_exist(self) -> None:
        keys = _extract_sensor_keys("STREAM_SENSORS")
        for expected in (
            "soc_pct",
            "batt_w",
            "batt_charge_power_w",
            "batt_discharge_power_w",
            "ac_grid_connection_power_w",
            "ac_voltage_v",
            "ac_frequency_hz",
            "backup_reserve_pct",
        ):
            assert expected in keys, f"Missing Stream sensor: {expected}"

    def test_meter_dependent_stream_sensors_disabled_by_default(self) -> None:
        sensors = {sensor.key: sensor for sensor in STREAM_SENSORS}
        for key in (
            "solar_w",
            "home_w",
            "grid_w",
            "solar_energy_kwh",
            "home_energy_kwh",
        ):
            assert sensors[key].disabled_by_default is True
            assert sensors[key].entity_category == "diagnostic"

    def test_stream_raw_battery_energy_not_exposed(self) -> None:
        keys = _extract_sensor_keys("STREAM_SENSORS")
        assert "batt_charge_energy_wh" not in keys
        assert "batt_discharge_energy_wh" not in keys

    def test_stream_battery_capacity_ah_is_diagnostic(self) -> None:
        sensors = {sensor.key: sensor for sensor in STREAM_SENSORS}
        for key in ("batt_charge_capacity_ah", "batt_discharge_capacity_ah"):
            assert sensors[key].disabled_by_default is True
            assert sensors[key].entity_category == "diagnostic"

    def test_switch_defs_unique(self) -> None:
        keys = _extract_sensor_keys("STREAM_SWITCHES")
        assert keys == []

    def test_binary_sensor_defs_unique(self) -> None:
        keys = _extract_sensor_keys("STREAM_BINARY_SENSORS")
        assert keys == ["ac_outlet_1_enabled", "ac_outlet_2_enabled"]

    def test_number_defs_unique(self) -> None:
        keys = _extract_sensor_keys("STREAM_NUMBERS")
        assert keys == ["backup_reserve"]

    def test_only_soc_has_battery_device_class(self) -> None:
        """Only the primary soc_pct should have device_class='battery'.

        soc_precise_pct and bms_soh_pct are percentages too, but must NOT
        use device_class='battery' because HA picks battery-class entities
        for the device header (Issue #32).
        """
        battery_sensors = [
            s for s in STREAM_SENSORS if s.device_class == "battery"
        ]
        keys = {s.key for s in battery_sensors}
        assert keys == {"soc_pct"}, (
            f"Expected battery device_class only on {{'soc_pct'}}, "
            f"but found: {keys}"
        )

    def test_soh_and_precise_soc_no_battery_device_class(self) -> None:
        """SoH and precise-SoC variants must not have device_class='battery'."""
        for s in STREAM_SENSORS:
            if s.key in ("bms_soh_pct", "soc_precise_pct"):
                assert s.device_class != "battery", (
                    f"{s.key} has device_class='battery' but is not the primary SoC"
                )


class TestDelta3Energy:
    """Delta 3 Energy Dashboard sensors are integrated from live power keys."""

    ENERGY_KEYS = (
        "solar_energy_kwh",
        "solar2_energy_kwh",
        "ac_in_energy_kwh",
        "out_energy_kwh",
    )

    def test_energy_dashboard_sensors_exist(self) -> None:
        sensors = {s.key: s for s in DELTA3_SENSORS}
        for key in self.ENERGY_KEYS:
            assert key in sensors, f"Missing Delta 3 energy sensor: {key}"
            s = sensors[key]
            assert s.device_class == "energy", f"{key} must be device_class=energy"
            assert s.state_class == "total_increasing", f"{key} must be total_increasing"
            assert s.unit == "kWh", f"{key} must be kWh"

    def test_power_to_energy_mapping(self) -> None:
        assert DELTA3_POWER_TO_ENERGY == {
            "pv1_in_w": "solar_energy_kwh",
            "pv2_in_w": "solar2_energy_kwh",
            "ac_in_w": "ac_in_energy_kwh",
            "pow_out_sum_w": "out_energy_kwh",
        }
        sensor_keys = {s.key for s in DELTA3_SENSORS}
        for power_key, energy_key in DELTA3_POWER_TO_ENERGY.items():
            assert power_key in sensor_keys, f"Source power sensor missing: {power_key}"
            assert energy_key in sensor_keys, f"Target energy sensor missing: {energy_key}"


class TestBatteryDeviceClassSingleton:
    """At most one battery device_class sensor per device type.

    HA uses the battery-class entity for the device-card header; more
    than one makes the header pick arbitrarily.
    """

    def test_at_most_one_battery_device_class_per_device_type(self):
        import re as _re

        from ecoflow_energy import const as _const

        for name in dir(_const):
            if not _re.fullmatch(r"[A-Z0-9]+_SENSORS", name):
                continue
            sensor_list = getattr(_const, name)
            battery_keys = [
                s.key for s in sensor_list if s.device_class == "battery"
            ]
            assert len(battery_keys) <= 1, (
                f"{name} has multiple battery device_class sensors: "
                f"{battery_keys} - HA uses battery-class for the device header"
            )


class TestEnergySensorPrecision:
    """kWh sensors must keep sub-kWh resolution.

    suggested_display_precision is applied to the state, not only to the
    display, so a precision of 0 stores whole kWh and destroys the resolution
    the device actually reports. On a lifetime counter that makes day-to-day
    deltas unusable: every reading carries up to 0.5 kWh of rounding, and a
    delta across several packs accumulates it.
    """

    def test_no_kwh_sensor_rounds_to_whole_kwh(self):
        import re as _re

        from ecoflow_energy import const as _const

        offenders = []
        for name in dir(_const):
            if not _re.fullmatch(r"[A-Z0-9]+_SENSORS", name):
                continue
            for sensor in getattr(_const, name):
                if (
                    sensor.unit == "kWh"
                    and sensor.suggested_display_precision == 0
                ):
                    offenders.append(f"{name}.{sensor.key}")

        assert not offenders, (
            "kWh sensors rounded to whole kWh in the stored state: "
            f"{offenders} - use a precision of at least 2"
        )


# Sensors that carry a numeric value but deliberately hold no precision, with
# the reason. Anything added here is a decision someone made on purpose, which
# is the point: the test below refuses the silent version of that decision.
_PRECISION_WAIVED = {
    "pcs_power_factor": (
        "a ratio between 0 and 1, so any whole-number rounding erases it"
    ),
}

# Key endings that can only ever name a whole number: a fault or error code, a
# cycle counter, a cell count, a number of packs. None of them has a fractional
# reading on any device or firmware, so all of them round to 0 places.
_INTEGER_ONLY_SUFFIXES = (
    "_code",
    "_cycles",
    "_count",
    "_alive_num",
    "_online_sum",
)


class TestIntegerSensorPrecision:
    """Whole-number sensors must say so, or they are shown with a decimal.

    The mirror image of the kWh rule above. Every parser in this integration
    converts what it reads to a float (`_safe_float`, or `float(value)` in the
    Delta MQTT path), so a cycle count of 412 reaches the entity as 412.0.
    `_round_value` in sensor.py only casts back to int when a precision of 0
    is set, and without it Home Assistant stores and shows "412.0" for a
    counter that cannot have a fractional part (#220).

    A count is the case where this is most obviously wrong: "3.0 battery
    packs" is not a rounding artefact a user can ignore, it reads as though
    the number could have been 3.5.
    """

    def _sensor_lists(self):
        # Keyed on content, not on the variable name: a list whose elements
        # are sensor definitions is a sensor list. A name pattern would
        # silently skip a future `STREAM_MICRO_SENSORS`-style list, and a
        # silently skipped list is exactly the regression this class exists
        # to refuse.
        from ecoflow_energy import const as _const

        for name in dir(_const):
            value = getattr(_const, name)
            if (
                isinstance(value, list)
                and value
                and all(
                    isinstance(item, _const.EcoFlowSensorDef) for item in value
                )
            ):
                yield name, value

    def test_discovery_sees_every_known_sensor_list(self):
        """The discovery above must never quietly find nothing.

        If a refactor renames the definition class or turns the lists into
        tuples, `_sensor_lists` would yield an empty set and every test in
        this class would pass vacuously. Pinning the known lists as a floor
        turns that silence into a failure; new lists are picked up without
        being named here.
        """
        found = {name for name, _sensors in self._sensor_lists()}
        expected = {
            "POWEROCEAN_SENSORS",
            "DELTA2MAX_SENSORS",
            "SMARTPLUG_SENSORS",
            "STREAM_SENSORS",
            "STREAMAC5000_SENSORS",
            "DELTA3_SENSORS",
        }
        missing = expected - found
        assert not missing, (
            f"sensor list discovery no longer sees {sorted(missing)}; "
            "the precision rules below are not being checked against them"
        )

    def test_counters_and_codes_round_to_whole_numbers(self):
        """The rule itself, keyed on what the sensor is rather than on a count.

        A new `mppt3_fault_code` or `pack6_cycles` added without the attribute
        fails here, and so does one that regresses to a fractional precision.
        Naming is the only signal available at definition level, but it is a
        reliable one for this family: every key ending this way is a code or a
        tally, and none of them is enum-backed.
        """
        offenders = []
        for list_name, sensors in self._sensor_lists():
            for sensor in sensors:
                if not sensor.key.endswith(_INTEGER_ONLY_SUFFIXES):
                    continue
                if sensor.options:
                    # Resolves to a translated string, so it has no numeric
                    # display for a precision to apply to.
                    continue
                if sensor.suggested_display_precision != 0:
                    offenders.append(
                        f"{list_name}.{sensor.key} "
                        f"(precision={sensor.suggested_display_precision})"
                    )

        assert not offenders, (
            "these sensors can only hold whole numbers but do not round to "
            "one, so Home Assistant shows them with a decimal:\n  "
            + "\n  ".join(offenders)
            + "\n  add suggested_display_precision=0"
        )

    def test_every_numeric_sensor_states_its_precision(self):
        """No numeric sensor may leave its precision to whatever the parser did.

        Broader than the suffix rule and the reason #220 covered 42 definitions
        rather than the three that were reported: an unset precision means the
        stored state is whatever float the parser happened to produce. A sensor
        that genuinely needs fractional digits belongs in _PRECISION_WAIVED with
        its reason, so the next reader can see it was chosen and not forgotten.
        """
        undeclared = []
        for list_name, sensors in self._sensor_lists():
            for sensor in sensors:
                if sensor.suggested_display_precision is not None:
                    continue
                if sensor.options:
                    continue
                if sensor.key in _PRECISION_WAIVED:
                    continue
                undeclared.append(f"{list_name}.{sensor.key}")

        assert not undeclared, (
            "numeric sensors without a display precision:\n  "
            + "\n  ".join(undeclared)
            + "\n  set suggested_display_precision, or waive it in "
            "_PRECISION_WAIVED with a reason"
        )

    def test_the_waiver_list_stays_honest(self):
        """A waiver for a sensor that no longer exists, or that has since been
        given a precision, is a stale exemption that would hide the next one."""
        by_key = {
            sensor.key: sensor
            for _name, sensors in self._sensor_lists()
            for sensor in sensors
        }

        for waived in _PRECISION_WAIVED:
            assert waived in by_key, f"{waived} is waived but no longer defined"
            assert by_key[waived].suggested_display_precision is None, (
                f"{waived} now declares a precision and no longer needs a waiver"
            )


class TestBinarySensors:
    def test_powerocean_binary_sensors(self):
        keys = _extract_sensor_keys("POWEROCEAN_BINARY_SENSORS")
        # PowerOcean has no binary sensors in Standard Mode
        assert isinstance(keys, list)

    def test_delta_binary_sensors(self):
        keys = _extract_sensor_keys("DELTA2MAX_BINARY_SENSORS")
        assert len(keys) >= 4
        assert "ac_enabled" in keys


# ---------------------------------------------------------------------------
# Mode reach
# ---------------------------------------------------------------------------

# Key families the Standard-Mode parsers build with an f-string, so a literal
# search of the source cannot find them. Verified against powerocean.py:
# pack{n}_ (line 122, 504), mppt_pv{n}_ (230), grid_phase_{a,b,c}_ (246, 253).
_DYNAMIC_KEY_FAMILIES = ("pack", "mppt_pv", "grid_phase_")

# Sources for the two device families whose Enhanced-only reach was wrong in
# the 1.16.0 betas. Each entry is (definition list, Standard-Mode parser).
_STANDARD_MODE_SOURCES = (
    ("DELTA3_SENSORS", DELTA3_SENSORS, "delta3_http.py"),
    # Controls belong here too. A select that Standard Mode cannot read is the
    # same failure as a sensor it cannot fill, with a write on top.
    ("DELTA3_SELECTS", DELTA3_SELECTS, "delta3_http.py"),
    ("POWEROCEAN_SENSORS", POWEROCEAN_SENSORS, "powerocean.py"),
    ("POWEROCEAN_BINARY_SENSORS", POWEROCEAN_BINARY_SENSORS, "powerocean.py"),
)

_PARSER_DIR = (
    Path(__file__).parent.parent
    / "custom_components"
    / "ecoflow_energy"
    / "ecoflow"
    / "parsers"
)


def _integrator_derived_keys() -> set[str]:
    """Energy keys the Riemann integrator produces, not any parser."""
    import ecoflow_energy.const as const_module

    derived: set[str] = set()
    for name in dir(const_module):
        if name.endswith("_POWER_TO_ENERGY"):
            derived |= set(getattr(const_module, name).values())
    return derived


class TestModeReach:
    """Every entity offered in Standard Mode must have a Standard-Mode source.

    The heating rod shipped reading from a quota that account sign-in never
    polls, and the fix for it left the mirror image in place: 37 definitions
    whose only source is the protobuf push path were offered with developer
    keys, 8 of them enabled by default. Both are the same mistake - an entity
    created against a path that does not run for that user. Nothing failed,
    because a permanently empty sensor looks exactly like a device that has
    not reported yet.

    A definition that this test cannot trace to a Standard-Mode parser is
    either Enhanced-only and needs the flag, or it is reachable in a way the
    test does not know about and belongs in one of the two lists above. Both
    outcomes are a decision someone has to make on purpose.
    """

    def test_every_standard_mode_key_has_a_standard_mode_source(self) -> None:
        derived = _integrator_derived_keys()
        unreachable: list[str] = []

        for list_name, defs, parser_file in _STANDARD_MODE_SOURCES:
            source = (_PARSER_DIR / parser_file).read_text()
            for defn in defs:
                if getattr(defn, "enhanced_only", False):
                    continue
                if getattr(defn, "accessory", False):
                    continue
                if defn.key in derived:
                    continue
                if defn.key.startswith(_DYNAMIC_KEY_FAMILIES):
                    continue
                if defn.key not in source:
                    unreachable.append(f"{list_name}.{defn.key} ({parser_file})")

        assert not unreachable, (
            "these entities are created in Standard Mode but nothing there can "
            "fill them:\n  " + "\n  ".join(unreachable)
        )

    def test_the_flag_is_not_applied_where_a_source_exists(self) -> None:
        """The opposite error: hiding an entity that Standard Mode could fill.

        A guard, not a regression detector: it passes before this change as
        well, because the error it looks for was never made. It earns its
        place by making the next batch of flags cheap to trust.
        """
        wrongly_hidden: list[str] = []

        for list_name, defs, parser_file in _STANDARD_MODE_SOURCES:
            source = (_PARSER_DIR / parser_file).read_text()
            for defn in defs:
                if not getattr(defn, "enhanced_only", False):
                    continue
                if defn.key in source:
                    wrongly_hidden.append(f"{list_name}.{defn.key} ({parser_file})")

        assert not wrongly_hidden, (
            "marked Enhanced-only although the Standard-Mode parser names them:\n  "
            + "\n  ".join(wrongly_hidden)
        )


class TestFrameCaptureFootprint:
    """The stated worst case has to be the one the constants produce.

    The frame capture comments are the only place the memory cost of the
    buffers is written down, and the only reason anyone can audit them. A
    comment that says 30 KiB next to constants that produce 120 KiB is worse
    than no comment, because it is trusted.
    """

    # "20 * 3 * 2048 B = 122 880 B (120 KiB)" - thousands are spaced, so the
    # result is read back with its spaces removed.
    _ARITHMETIC = re.compile(
        r"(\d+) \* (\d+) \* (\d+) B = ([\d ]+?) B \((\d+) KiB\)"
    )

    def _statements(self) -> list[tuple[int, int, int, int, int]]:
        source = (REPO_ROOT / "custom_components/ecoflow_energy/const.py").read_text()
        return [
            (
                int(keys),
                int(per_key),
                int(budget),
                int(total.replace(" ", "")),
                int(kib),
            )
            for keys, per_key, budget, total, kib in self._ARITHMETIC.findall(source)
        ]

    def test_every_worst_case_multiplies_out(self) -> None:
        statements = self._statements()

        assert statements, "no footprint arithmetic found in const.py"
        for keys, per_key, budget, total, kib in statements:
            assert keys * per_key * budget == total
            assert total == kib * 1024

    def test_every_worst_case_uses_the_real_constants(self) -> None:
        """A comment may only multiply numbers the code actually holds."""
        buffers = {
            (RAW_FRAME_LOG_KEYS_MAX, RAW_FRAME_LOG_PER_KEY_MAX),
            (RAW_FRAME_KEYS_MAX, RAW_FRAME_PER_KEY_MAX),
        }
        budgets = {RAW_FRAME_MAX_BYTES, RAW_FRAME_BUNDLE_MAX_BYTES}

        for keys, per_key, budget, _total, _kib in self._statements():
            assert (keys, per_key) in buffers
            assert budget in budgets

    def test_both_buffers_state_their_worst_case(self) -> None:
        """Neither buffer may lose its figure to an edit of the other."""
        stated = {(keys, per_key) for keys, per_key, *_ in self._statements()}

        assert (RAW_FRAME_LOG_KEYS_MAX, RAW_FRAME_LOG_PER_KEY_MAX) in stated
        assert (RAW_FRAME_KEYS_MAX, RAW_FRAME_PER_KEY_MAX) in stated

    def test_the_bundle_budget_is_the_larger_one(self) -> None:
        """Inverted, the split would cut exactly the frames it exists for."""
        assert RAW_FRAME_BUNDLE_MAX_BYTES > RAW_FRAME_MAX_BYTES
