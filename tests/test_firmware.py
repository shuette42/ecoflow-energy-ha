"""Tests for firmware revision extraction from HTTP quota responses."""

from custom_components.ecoflow_energy.ecoflow.firmware import (
    decode_version,
    extract_firmware_versions,
)


class TestDecodeVersion:
    """The packed 32-bit revision split into its four components."""

    def test_decodes_real_delta2_values(self):
        # Captured from a Delta 2 Max quota on 2026-08-04.
        assert decode_version(16975450) == "v1.3.6.90"
        assert decode_version(33554523) == "v2.0.0.91"
        assert decode_version(83886173) == "v5.0.0.93"
        assert decode_version(33620284) == "v2.1.1.60"

    def test_rejects_non_versions(self):
        assert decode_version(0) == ""
        assert decode_version(-1) == ""
        assert decode_version(0x1_0000_0000) == ""
        assert decode_version("1.2.3") == ""
        assert decode_version(None) == ""
        assert decode_version(1.5) == ""

    def test_rejects_bool(self):
        # bool is an int subclass; True would otherwise decode to v0.0.0.1.
        assert decode_version(True) == ""


class TestExtractFirmwareVersions:
    """Which quota keys count as a revision."""

    def test_extracts_delta2_subsystems(self):
        raw = {
            "pd.sysVer": 16975450,
            "inv.sysVer": 33554523,
            "mppt.swVer": 83886173,
            "bms_bmsStatus.sysVer": 33620284,
            "pd.soc": 100,
        }
        result = extract_firmware_versions(raw)
        assert set(result) == {
            "pd.sysVer",
            "inv.sysVer",
            "mppt.swVer",
            "bms_bmsStatus.sysVer",
        }
        assert result["pd.sysVer"] == {"raw": 16975450, "decoded": "v1.3.6.90"}

    def test_ignores_keys_that_merely_contain_ver(self):
        # PowerOcean has 347 quota keys; several carry "Ver" inside a longer
        # word and none of them is a revision.
        raw = {
            "ems_change_report.pcsOverVolDeratingDaleyTime": 0.0,
            "ems_change_report.pcs10minOverVol": 253.0,
            "ems_change_report.bpReverseFlag": 0,
        }
        assert extract_firmware_versions(raw) == {}

    def test_powerocean_quota_reports_no_firmware(self):
        raw = {"bpSoc": 55.0, "mpptPwr": 1200.0, "pcsAPhase.vol": 231.4}
        assert extract_firmware_versions(raw) == {}

    def test_drops_unparsable_values(self):
        # hwVersion arrives as a byte list on Delta 2 Max, not a packed int.
        raw = {"bms_bmsStatus.hwVersion": [86, 48, 46, 49, 46, 50], "pd.wifiVer": 0}
        assert extract_firmware_versions(raw) == {}

    def test_empty_quota(self):
        assert extract_firmware_versions({}) == {}
