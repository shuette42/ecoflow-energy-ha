"""Tests for manifest.json — validates HACS/hassfest requirements."""

import json
from pathlib import Path

MANIFEST_PATH = Path("custom_components/ecoflow_energy/manifest.json")


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


class TestManifest:
    def test_manifest_exists(self):
        assert MANIFEST_PATH.exists()

    def test_domain(self):
        m = _load_manifest()
        assert m["domain"] == "ecoflow_energy"

    def test_iot_class_is_cloud_polling(self):
        """Standard Mode uses HTTP polling — iot_class must reflect that."""
        m = _load_manifest()
        assert m["iot_class"] == "cloud_polling", (
            f"iot_class must be 'cloud_polling' for Standard Mode, got '{m['iot_class']}'"
        )

    def test_config_flow_enabled(self):
        m = _load_manifest()
        assert m["config_flow"] is True

    def test_integration_type(self):
        m = _load_manifest()
        assert m["integration_type"] == "hub"

    def test_no_pycryptodome_dependency(self):
        """cryptography (HA core dep) is used instead of pycryptodome."""
        m = _load_manifest()
        for req in m["requirements"]:
            assert "pycryptodome" not in req.lower(), (
                f"pycryptodome must not be in requirements — use cryptography instead (found: {req})"
            )

    def test_requirements_has_paho_mqtt(self):
        m = _load_manifest()
        assert any("paho-mqtt" in r for r in m["requirements"])

    def test_requirements_has_protobuf(self):
        m = _load_manifest()
        assert any("protobuf" in r for r in m["requirements"])

    def test_version_format(self):
        m = _load_manifest()
        parts = m["version"].split(".")
        assert len(parts) == 3, f"Version must be semver, got '{m['version']}'"
        assert all(p.isdigit() for p in parts)

    def test_codeowners(self):
        m = _load_manifest()
        assert isinstance(m["codeowners"], list)
        assert len(m["codeowners"]) >= 1

    def test_required_keys_present(self):
        m = _load_manifest()
        for key in ("domain", "name", "version", "config_flow", "iot_class", "requirements"):
            assert key in m, f"Required key '{key}' missing from manifest.json"
