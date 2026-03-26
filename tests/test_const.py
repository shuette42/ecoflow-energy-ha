"""Tests for entity definitions in const.py — uniqueness and completeness."""

import ast
from pathlib import Path


def _extract_sensor_keys(var_name: str) -> list[str]:
    """Extract the first positional arg (key) from each call in a list variable."""
    source = Path("custom_components/ecoflow_energy/const.py").read_text()
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

    def test_switch_defs_unique(self):
        keys = _extract_sensor_keys("DELTA2MAX_SWITCHES")
        assert len(keys) == 3
        assert len(keys) == len(set(keys))

    def test_number_defs_unique(self):
        keys = _extract_sensor_keys("DELTA2MAX_NUMBERS")
        assert len(keys) == 4
        assert len(keys) == len(set(keys))


class TestBinarySensors:
    def test_powerocean_binary_sensors(self):
        keys = _extract_sensor_keys("POWEROCEAN_BINARY_SENSORS")
        # PowerOcean has no binary sensors in Standard Mode
        assert isinstance(keys, list)

    def test_delta_binary_sensors(self):
        keys = _extract_sensor_keys("DELTA2MAX_BINARY_SENSORS")
        assert len(keys) >= 5
        assert "ac_enabled" in keys
