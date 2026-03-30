"""Tests for entity definitions in const.py — uniqueness and completeness."""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _extract_sensor_keys(var_name: str) -> list[str]:
    """Extract the first positional arg (key) from each call in a list variable."""
    source = (REPO_ROOT / "custom_components/ecoflow_energy/const.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        # Type-annotated: ``SENSORS: list[...] = [...]``  → AnnAssign
        # Plain: ``SENSORS = [...]``  → Assign
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
