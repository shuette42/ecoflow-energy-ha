"""Functional tests for diagnostics — runtime output verification."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    CONF_ACCESS_KEY,
    CONF_DEVICES,
    CONF_MODE,
    CONF_SECRET_KEY,
    DATA_SKIPPED_DEVICES,
    DOMAIN,
    MODE_STANDARD,
)
from custom_components.ecoflow_energy.diagnostics import (
    REDACTED,
    _device_diagnostics,
    _redact_serials,
    _skipped_devices_diagnostics,
    async_get_config_entry_diagnostics,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator

from .conftest import (
    MOCK_DELTA_DEVICE,
    MOCK_DELTA3_DEVICE,
    MOCK_MQTT_CREDENTIALS,
    MOCK_POWEROCEAN_DEVICE,
)


# ===========================================================================
# async_get_config_entry_diagnostics
# ===========================================================================


class TestConfigEntryDiagnostics:
    async def test_credentials_redacted(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """All credentials in config_entry must be REDACTED."""
        standard_config_entry.add_to_hass(hass)
        with patch(
            "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator.async_config_entry_first_refresh",
            new_callable=AsyncMock,
        ):
            await hass.config_entries.async_setup(standard_config_entry.entry_id)
            await hass.async_block_till_done()

        result = await async_get_config_entry_diagnostics(hass, standard_config_entry)

        assert result["config_entry"]["access_key"] == REDACTED
        assert result["config_entry"]["secret_key"] == REDACTED
        assert result["config_entry"]["email"] == REDACTED
        assert result["config_entry"]["password"] == REDACTED

    async def test_structure(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """Diagnostics output has expected top-level keys."""
        standard_config_entry.add_to_hass(hass)
        with patch(
            "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator.async_config_entry_first_refresh",
            new_callable=AsyncMock,
        ):
            await hass.config_entries.async_setup(standard_config_entry.entry_id)
            await hass.async_block_till_done()

        result = await async_get_config_entry_diagnostics(hass, standard_config_entry)

        assert "config_entry" in result
        assert "devices" in result
        assert result["config_entry"]["mode"] == "standard"
        assert result["config_entry"]["device_count"] == 1
        assert len(result["devices"]) == 1

    async def test_no_coordinators(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Diagnostics handles missing coordinators gracefully."""
        standard_config_entry.add_to_hass(hass)
        # Don't set up the integration — no coordinators in hass.data
        result = await async_get_config_entry_diagnostics(hass, standard_config_entry)
        assert result["devices"] == []
        assert result["skipped_devices"] == []

    async def test_skipped_devices_in_diagnostics(
        self,
        hass: HomeAssistant,
        mock_mqtt_client,
    ) -> None:
        """Diagnostics expose the skipped_devices list for the entry."""
        unsupported_device = {
            "sn": "BK21TEST00000001",
            "name": "Smart Meter",
            "product_name": "Smart Meter",
            "device_type": "unknown",
            "online": 1,
        }
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_ACCESS_KEY: "test_access_key",
                CONF_SECRET_KEY: "test_secret_key",
                CONF_MODE: MODE_STANDARD,
                CONF_DEVICES: [unsupported_device],
            },
            unique_id="test_access_key",
        )
        entry.add_to_hass(hass)

        with patch(
            "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator.async_config_entry_first_refresh",
            new_callable=AsyncMock,
        ):
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        result = await async_get_config_entry_diagnostics(hass, entry)
        assert len(result["skipped_devices"]) == 1
        assert result["skipped_devices"][0]["sn_prefix"] == "BK21"
        assert result["skipped_devices"][0]["product_name"] == "Smart Meter"


# ===========================================================================
# _device_diagnostics
# ===========================================================================


class TestDeviceDiagnostics:
    async def test_device_diagnostics_basic(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Device diagnostics includes core fields."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        result = _device_diagnostics(coordinator)

        # SN prefix only - a diagnostics dump must not leak the full serial
        assert result["device_sn"] == "DAEB..."
        assert "DAEBK5ZZ12340001" not in str(result["device_sn"])
        assert result["device_name"] == "Delta 2 Max"
        assert result["product_name"] == "Delta 2 Max"
        assert result["enhanced_mode"] is False

    async def test_mqtt_status_disconnected(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """MQTT status shows disconnected when no client."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        result = _device_diagnostics(coordinator)

        assert result["mqtt_status"]["connected"] is False
        assert result["mqtt_status"]["uptime_s"] is None
        assert result["mqtt_status"]["wss_mode"] is False

    async def test_mqtt_status_connected(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """MQTT status shows connected with uptime."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()

        mock_mqtt_client.last_connect_time = 1000.0
        with patch(
            "custom_components.ecoflow_energy.diagnostics.time.monotonic",
            return_value=1120.0,
        ):
            result = _device_diagnostics(coordinator)

        assert result["mqtt_status"]["connected"] is True
        assert result["mqtt_status"]["uptime_s"] == 120.0

    async def test_data_freshness(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Data freshness reports last MQTT age."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._last_mqtt_ts = 1000.0
        with patch(
            "custom_components.ecoflow_energy.diagnostics.time.monotonic",
            return_value=1010.0,
        ):
            result = _device_diagnostics(coordinator)

        assert result["data_freshness"]["last_mqtt_age_s"] == 10.0

    async def test_data_keys_enumerated(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Device diagnostics includes sorted data keys."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._device_data = {"soc": 85, "solar_w": 3000, "batt_w": -200}
        result = _device_diagnostics(coordinator)

        assert result["data_keys"] == ["batt_w", "soc", "solar_w"]
        assert result["data_key_count"] == 3

    async def test_http_fallback_flag_standard_mode(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Standard Mode is not a fallback — http_fallback_active is False."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        result = _device_diagnostics(coordinator)
        # Standard Mode: HTTP polling is primary, not a fallback
        assert result["data_freshness"]["http_fallback_active"] is False

    async def test_http_fallback_flag_enhanced_with_fallback(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Enhanced Mode with stale MQTT shows http_fallback_active=True."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        # Simulate stale MQTT → HTTP fallback activated
        from datetime import timedelta
        coordinator.update_interval = timedelta(seconds=30)
        result = _device_diagnostics(coordinator)
        assert result["data_freshness"]["http_fallback_active"] is True

    async def test_event_log_in_diagnostics(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Device diagnostics includes event_log."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._log_event("http_ok", "keys=42")
        coordinator._log_event("mqtt_connect", "TCP Standard")
        result = _device_diagnostics(coordinator)

        assert "event_log" in result
        assert len(result["event_log"]) == 2
        assert result["event_log"][0]["type"] == "http_ok"
        assert result["event_log"][1]["type"] == "mqtt_connect"

    async def test_event_log_empty_by_default(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Event log is empty when no events recorded."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        result = _device_diagnostics(coordinator)
        assert result["event_log"] == []

    async def test_mqtt_status_includes_3state(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Diagnostics mqtt_status includes 'status' and 'data_receiving' fields."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        result = _device_diagnostics(coordinator)

        assert "status" in result["mqtt_status"]
        assert "data_receiving" in result["mqtt_status"]
        assert result["mqtt_status"]["status"] == "not_configured"
        assert result["mqtt_status"]["data_receiving"] is False

    async def test_event_log_has_iso_timestamps(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Event log entries include ISO-formatted timestamps."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._log_event("test", "entry_1")
        result = _device_diagnostics(coordinator)

        assert len(result["event_log"]) == 1
        entry = result["event_log"][0]
        assert "ts_iso" in entry
        assert entry["ts_iso"].endswith("+00:00")


class TestDeltaThreeRawQuotaDiagnostics:
    async def test_non_delta3_has_no_raw_quota_section(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Only delta3 devices expose the raw_quota diagnostics section."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        result = _device_diagnostics(coordinator)
        assert "raw_quota" not in result

    async def test_delta3_raw_quota_exposed(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Delta 3 diagnostics expose the raw quota key/value snapshot."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA3_DEVICE
        )
        coordinator._raw_quota = {"bpSoc": 80, "meterTotalPower": 1234.5}
        coordinator._raw_quota_captured_at = 1000.0

        with patch(
            "custom_components.ecoflow_energy.diagnostics.time.monotonic",
            return_value=1005.0,
        ):
            result = _device_diagnostics(coordinator)

        assert "raw_quota" in result
        assert result["raw_quota"]["captured"] is True
        assert result["raw_quota"]["key_count"] == 2
        assert result["raw_quota"]["age_s"] == 5.0
        assert result["raw_quota"]["values"]["bpSoc"] == 80
        assert result["raw_quota"]["values"]["meterTotalPower"] == 1234.5

    async def test_powerocean_raw_quota_exposed(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """PowerOcean exposes the raw quota so accessory keys become visible.

        Accessories such as the PowerGlow heating rod report through the
        PowerOcean quota rather than as devices of their own, and their key
        names are documented nowhere. Without this section an owner cannot
        report which keys their accessory contributes.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._raw_quota = {
            "bpSoc": 74,
            "ems_heating_rod.heatingPower": 1750,
        }
        coordinator._raw_quota_captured_at = 1000.0

        with patch(
            "custom_components.ecoflow_energy.diagnostics.time.monotonic",
            return_value=1002.0,
        ):
            result = _device_diagnostics(coordinator)

        assert "raw_quota" in result
        assert result["raw_quota"]["captured"] is True
        assert (
            result["raw_quota"]["values"]["ems_heating_rod.heatingPower"] == 1750
        )
        assert result["raw_quota"]["values"]["bpSoc"] == 74

    async def test_powerocean_raw_quota_redacts_serials_in_keys(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """A serial in the key name must not survive into the dump.

        The PowerOcean quota addresses battery packs by serial in the key
        itself. Redacting values alone would still publish that serial in a
        dump users are asked to attach to a public issue.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._raw_quota = {"bp_addr.HJ31TESTSERIAL01": {"bpSoc": 74}}
        coordinator._raw_quota_captured_at = 1000.0

        with patch(
            "custom_components.ecoflow_energy.diagnostics.time.monotonic",
            return_value=1001.0,
        ):
            result = _device_diagnostics(coordinator)

        keys = list(result["raw_quota"]["values"])
        assert keys == ["bp_addr.**REDACTED**"]
        assert "HJ31TESTSERIAL01" not in json.dumps(result)

    async def test_delta3_raw_quota_redacts_serials(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Serial-looking raw quota values are redacted."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA3_DEVICE
        )
        coordinator._raw_quota = {
            "sn": "D3M1TEST00000001",
            "bpSoc": 80,
        }
        coordinator._raw_quota_captured_at = 1000.0
        result = _device_diagnostics(coordinator)

        assert result["raw_quota"]["values"]["sn"] == REDACTED
        assert result["raw_quota"]["values"]["bpSoc"] == 80

    async def test_delta3_raw_quota_redacts_nested_and_embedded_serials(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Redaction recurses into nested containers and matches embedded serials."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA3_DEVICE
        )
        coordinator._raw_quota = {
            # Serial nested inside a dict value
            "nested": {"deviceSn": "D3M1TESTAAAABBBB"},
            # Serial embedded in a longer string
            "meta": "sn=D3M1TESTAAAABBBB;x=1",
            # Serial inside a list value
            "list": ["D3M1TESTAAAABBBB", 42],
            "bpSoc": 80,
        }
        coordinator._raw_quota_captured_at = 1000.0
        result = _device_diagnostics(coordinator)

        values = result["raw_quota"]["values"]
        assert values["nested"]["deviceSn"] == REDACTED
        assert REDACTED in values["meta"]
        assert "D3M1TESTAAAABBBB" not in values["meta"]
        assert values["list"][0] == REDACTED
        assert values["list"][1] == 42
        assert values["bpSoc"] == 80

    async def test_delta3_raw_quota_empty_before_capture(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Before the first quota poll the section reports not-captured."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA3_DEVICE
        )
        result = _device_diagnostics(coordinator)

        assert result["raw_quota"]["captured"] is False
        assert result["raw_quota"]["key_count"] == 0
        assert result["raw_quota"]["age_s"] is None


class TestRedactSerials:
    """Unit coverage for the recursive, unanchored serial redactor."""

    def test_bare_serial_redacted(self) -> None:
        assert _redact_serials("D3M1TESTAAAABBBB") == REDACTED

    def test_embedded_serial_redacted(self) -> None:
        out = _redact_serials("sn=D3M1TESTAAAABBBB;x=1")
        assert REDACTED in out
        assert "D3M1TESTAAAABBBB" not in out

    def test_nested_dict_recurses(self) -> None:
        out = _redact_serials({"a": {"b": "D3M1TESTAAAABBBB"}})
        assert out == {"a": {"b": REDACTED}}

    def test_list_recurses(self) -> None:
        out = _redact_serials(["D3M1TESTAAAABBBB", 42, "ok"])
        assert out == [REDACTED, 42, "ok"]

    def test_short_values_untouched(self) -> None:
        assert _redact_serials("bpSoc") == "bpSoc"
        assert _redact_serials(80) == 80


class TestDeltaThreeRawQuotaCapture:
    async def test_http_update_captures_raw_quota(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """The HTTP update stores the raw quota snapshot for delta3 devices."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA3_DEVICE
        )

        raw_quota = {"bpSoc": 80, "meterTotalPower": 1234.5, "sysWorkSta": 3}
        coordinator._http_client = MagicMock()
        coordinator._http_client.get_quota_all = AsyncMock(return_value=raw_quota)

        result = await coordinator._async_update_data()

        assert coordinator.raw_quota == raw_quota
        assert coordinator.raw_quota_captured_at > 0
        # Field map still empty → no mapped keys leak into device data
        assert result == {}


class TestSkippedDeviceRawQuotaDiagnostics:
    """Raw quota capture for unsupported/skipped devices (issue #135)."""

    DIAG_QUOTA_PATH = (
        "custom_components.ecoflow_energy.diagnostics.EcoFlowHTTPQuota"
    )
    # Fictional Smart Meter serial: a device we do not yet parse.
    SKIPPED_SN = "SM3ATEST00000001"
    # A 16-char alphanumeric quota value that looks like a serial and must
    # be redacted, alongside numeric fields that must be preserved.
    FAKE_SERIAL_FIELD = "ABCDEFGH12345678"

    def _register_skipped(self, hass: HomeAssistant, entry: MockConfigEntry) -> None:
        hass.data.setdefault(DATA_SKIPPED_DEVICES, {})[entry.entry_id] = [
            {
                "sn_prefix": self.SKIPPED_SN[:4],
                "sn": self.SKIPPED_SN,
                "product_name": "Smart Meter",
                "reason": "no parser available for this device type",
            }
        ]

    def _standard_entry(self) -> MockConfigEntry:
        return MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_ACCESS_KEY: "test_access_key",
                CONF_SECRET_KEY: "test_secret_key",
                CONF_MODE: MODE_STANDARD,
                CONF_DEVICES: [],
            },
            unique_id="test_access_key",
        )

    async def test_with_dev_keys_captures_redacted_quota(
        self,
        hass: HomeAssistant,
    ) -> None:
        """With developer keys: quota captured, serial redacted, SN hidden."""
        entry = self._standard_entry()
        entry.add_to_hass(hass)
        self._register_skipped(hass, entry)

        # get_quota_all() returns the already-unwrapped flat quota dict —
        # no code/data envelope. Match the real client contract.
        quota_response = {
            "meterSn": self.FAKE_SERIAL_FIELD,
            "gridWatts": 1234,
            "gridVol": 230.5,
        }

        with patch(self.DIAG_QUOTA_PATH) as quota_cls:
            quota_cls.return_value.get_quota_all = AsyncMock(
                return_value=quota_response
            )
            result = await async_get_config_entry_diagnostics(hass, entry)

        device = result["skipped_devices"][0]
        assert device["sn_prefix"] == "SM3A"
        assert "sn" not in device
        assert "raw_quota" in device

        raw = device["raw_quota"]
        assert raw["meterSn"] == REDACTED
        assert raw["gridWatts"] == 1234
        assert raw["gridVol"] == 230.5

        # Full SN and credential values must never appear in the output.
        serialized = json.dumps(result)
        assert self.SKIPPED_SN not in serialized
        assert "SM3A" in serialized
        assert "test_access_key" not in serialized
        assert "test_secret_key" not in serialized

    async def test_without_dev_keys_omits_quota(
        self,
        hass: HomeAssistant,
    ) -> None:
        """App-auth mode (no dev keys): quota omitted with a note, no crash."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                CONF_MODE: MODE_STANDARD,
                CONF_DEVICES: [],
            },
            unique_id="app_auth_entry",
        )
        entry.add_to_hass(hass)
        self._register_skipped(hass, entry)

        result = await async_get_config_entry_diagnostics(hass, entry)

        device = result["skipped_devices"][0]
        assert device["sn_prefix"] == "SM3A"
        assert "raw_quota" not in device
        assert "developer credentials required" in device["quota_note"]

    async def test_fetch_exception_is_swallowed(
        self,
        hass: HomeAssistant,
    ) -> None:
        """A quota fetch raising yields a note, no crash, no leaked detail."""
        entry = self._standard_entry()
        entry.add_to_hass(hass)
        self._register_skipped(hass, entry)

        secret_detail = "boom-secret-detail"

        with patch(self.DIAG_QUOTA_PATH) as quota_cls:
            quota_cls.return_value.get_quota_all = AsyncMock(
                side_effect=RuntimeError(secret_detail)
            )
            result = await async_get_config_entry_diagnostics(hass, entry)

        device = result["skipped_devices"][0]
        assert "raw_quota" not in device
        assert device["quota_note"] == "quota fetch unavailable"

        serialized = json.dumps(result)
        assert secret_detail not in serialized
        assert self.SKIPPED_SN not in serialized


class TestRawFrameDiagnostics:
    async def test_no_frames_omits_section(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """A device without captured frames has no raw_frames section."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert "raw_frames" not in _device_diagnostics(coordinator)

    async def test_captured_frames_exposed_with_iso_timestamps(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Captured frames reach diagnostics with readable timestamps."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._raw_frames.append({
            "ts": 1784973604.0,
            "topic": "property",
            "size": 42,
            "parsed_keys": 5,
            "cmds": [{"cmd_func": 96, "cmd_id": 33}],
            "hex": "0a02ffff",
        })

        result = _device_diagnostics(coordinator)

        assert result["raw_frames"]["count"] == 1
        frame = result["raw_frames"]["frames"][0]
        assert frame["hex"] == "0a02ffff"
        assert frame["cmds"] == [{"cmd_func": 96, "cmd_id": 33}]
        assert frame["ts_iso"].startswith("2026-")


class TestUnroutedDeviceCapture:
    """A skipped device's captured frames are the evidence for adding support."""

    def _probe(self, sn: str = "RE11TEST00000001"):
        probe = MagicMock()
        probe.device_sn = sn
        probe.connected = True
        probe.topics = ["/app/{sn}/thing/property/get_reply"]
        probe.frames = [{
            "ts": 1784973604.0,
            "topic": "get_reply",
            "size": 120,
            "format": "proto",
            "cmds": [{"cmd_func": 96, "cmd_id": 33}],
            "hex": "0a02ffff",
        }]
        return probe

    async def test_capture_is_attached_to_the_matching_device(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        skipped = [{
            "sn_prefix": "RE11",
            "sn": "RE11TEST00000001",
            "product_name": "",
            "reason": "no parser available for this device type",
        }]

        result = await _skipped_devices_diagnostics(
            hass, enhanced_config_entry, skipped, [self._probe()]
        )

        capture = result[0]["raw_capture"]
        assert capture["connected"] is True
        assert capture["frame_count"] == 1
        assert capture["topics"] == ["/app/{sn}/thing/property/get_reply"]
        assert capture["frames"][0]["hex"] == "0a02ffff"
        assert capture["frames"][0]["ts_iso"].startswith("2026-")

    async def test_full_serial_never_reaches_the_output(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        skipped = [{
            "sn_prefix": "RE11",
            "sn": "RE11TEST00000001",
            "product_name": "",
            "reason": "no parser available for this device type",
        }]

        result = await _skipped_devices_diagnostics(
            hass, enhanced_config_entry, skipped, [self._probe()]
        )

        assert "RE11TEST00000001" not in json.dumps(result)

    async def test_no_probe_says_so_instead_of_staying_silent(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """A failed probe start must be visible, not just absent.

        Omitting the section makes a failed login indistinguishable from a
        version that has no capture at all, and the reader has no way to
        tell which one they are looking at.
        """
        skipped = [{
            "sn_prefix": "RE11",
            "sn": "RE11TEST00000001",
            "product_name": "",
            "reason": "no parser available for this device type",
        }]

        result = await _skipped_devices_diagnostics(
            hass, enhanced_config_entry, skipped, []
        )

        assert result[0]["raw_capture"]["status"] == "no probe running for this device"
        assert "frames" not in result[0]["raw_capture"]

    async def test_probe_for_another_device_is_not_attached(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        skipped = [{
            "sn_prefix": "RE11",
            "sn": "RE11TEST00000001",
            "product_name": "",
            "reason": "no parser available for this device type",
        }]

        result = await _skipped_devices_diagnostics(
            hass, enhanced_config_entry, skipped,
            [self._probe("SM3ATEST00000001")],
        )

        # Another device's capture must not be attached here, and the entry
        # must still state that this device has none of its own.
        assert result[0]["raw_capture"]["status"] == "no probe running for this device"
        assert "frames" not in result[0]["raw_capture"]
