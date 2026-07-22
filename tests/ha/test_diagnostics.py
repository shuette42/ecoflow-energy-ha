"""Functional tests for diagnostics — runtime output verification."""

from __future__ import annotations

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
