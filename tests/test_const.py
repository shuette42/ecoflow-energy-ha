"""Tests for entity definitions in const.py — uniqueness and completeness."""

import ast
from pathlib import Path

from ecoflow_energy.const import (
    POWEROCEAN_SENSORS,
    DELTA2MAX_SENSORS,
    SMARTPLUG_SENSORS,
    DELTA2MAX_BINARY_SENSORS,
    DELTA2MAX_SWITCHES,
    DELTA2MAX_NUMBERS,
    POWEROCEAN_BINARY_SENSORS,
    SMARTPLUG_SWITCHES,
    SMARTPLUG_NUMBERS,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


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
        "DELTA2MAX_BINARY_SENSORS": DELTA2MAX_BINARY_SENSORS,
        "DELTA2MAX_SWITCHES": DELTA2MAX_SWITCHES,
        "DELTA2MAX_NUMBERS": DELTA2MAX_NUMBERS,
        "POWEROCEAN_BINARY_SENSORS": POWEROCEAN_BINARY_SENSORS,
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


class TestPowerOceanSensors:
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

    def test_existing_sensors_count(self):
        """Original 63 sensors still present (non-pack, non-EMS-extended)."""
        keys = _extract_sensor_keys("POWEROCEAN_SENSORS")
        non_pack = [k for k in keys if not k.startswith("pack")]
        ems_extended = {
            "ems_charge_upper_limit_pct", "ems_discharge_lower_limit_pct",
            "ems_keep_soc_pct", "ems_backup_ratio_pct",
            "mppt1_fault_code", "mppt2_fault_code",
            "pcs_ac_error_code", "pcs_dc_error_code", "pcs_ac_warning_code",
            "wifi_status", "ethernet_status", "cellular_status",
            "ems_led_brightness", "ems_work_state",
            "ems_total_battery_capacity_wh", "pcs_max_output_power_w",
            "pcs_max_input_power_w", "bp_max_charge_power_w",
            "bp_max_discharge_power_w",
        }
        original = [k for k in non_pack if k not in ems_extended]
        assert len(original) == 63, f"Expected 63 original sensors, got {len(original)}"

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
        """19 EMS/system extended sensors."""
        keys = _extract_sensor_keys("POWEROCEAN_SENSORS")
        ems_extended = {
            "ems_charge_upper_limit_pct", "ems_discharge_lower_limit_pct",
            "ems_keep_soc_pct", "ems_backup_ratio_pct",
            "mppt1_fault_code", "mppt2_fault_code",
            "pcs_ac_error_code", "pcs_dc_error_code", "pcs_ac_warning_code",
            "wifi_status", "ethernet_status", "cellular_status",
            "ems_led_brightness", "ems_work_state",
            "ems_total_battery_capacity_wh", "pcs_max_output_power_w",
            "pcs_max_input_power_w", "bp_max_charge_power_w",
            "bp_max_discharge_power_w",
        }
        found = [k for k in keys if k in ems_extended]
        assert len(found) == 19, f"Expected 19 EMS extended sensors, got {len(found)}"

    def test_total_sensor_count(self):
        """Total PowerOcean sensors = 63 + 120 + 19 = 202."""
        keys = _extract_sensor_keys("POWEROCEAN_SENSORS")
        assert len(keys) == 202, f"Expected 202 total sensors, got {len(keys)}"


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


class TestBinarySensors:
    def test_powerocean_binary_sensors(self):
        keys = _extract_sensor_keys("POWEROCEAN_BINARY_SENSORS")
        # PowerOcean has no binary sensors in Standard Mode
        assert isinstance(keys, list)

    def test_delta_binary_sensors(self):
        keys = _extract_sensor_keys("DELTA2MAX_BINARY_SENSORS")
        assert len(keys) >= 4
        assert "ac_enabled" in keys
