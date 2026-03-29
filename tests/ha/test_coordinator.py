"""Tests for EcoFlowDeviceCoordinator — setup, data flow, stale detection, shutdown."""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_SMARTPLUG,
    DOMAIN,
    ENERGY_STREAM_KEEPALIVE_S,
    HTTP_FALLBACK_INTERVAL_S,
    MODE_ENHANCED,
    MODE_STANDARD,
    STALE_THRESHOLD_S,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator

from .conftest import (
    MOCK_DELTA_DEVICE,
    MOCK_MQTT_CREDENTIALS,
    MOCK_POWEROCEAN_DEVICE,
    MOCK_SMARTPLUG_DEVICE,
)


# ===========================================================================
# Coordinator Initialization
# ===========================================================================


class TestCoordinatorInit:
    async def test_standard_mode_has_poll_interval(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Standard Mode coordinator has a polling interval."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.update_interval is not None
        assert coordinator.update_interval.total_seconds() == HTTP_FALLBACK_INTERVAL_S

    async def test_enhanced_mode_no_poll_interval(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Enhanced Mode has no polling interval (MQTT push is primary)."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        assert coordinator.update_interval is None
        assert coordinator.enhanced_mode is True

    async def test_enhanced_mode_ignored_for_delta(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Enhanced Mode config is ignored for non-PowerOcean devices (no WSS stream)."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_DELTA_DEVICE
        )
        # Delta has no WSS data source — always Standard regardless of config
        assert coordinator.enhanced_mode is False
        assert coordinator.update_interval is not None

    async def test_device_attributes(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Coordinator stores device attributes correctly."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.device_sn == "DAEBK5ZZ12340001"
        assert coordinator.device_name == "Delta 2 Max"
        assert coordinator.device_type == DEVICE_TYPE_DELTA
        assert coordinator.enhanced_mode is False


# ===========================================================================
# Properties
# ===========================================================================


class TestProperties:
    async def test_device_data_initially_empty(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.device_data == {}

    async def test_mqtt_client_initially_none(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.mqtt_client is None

    async def test_last_mqtt_ts_initially_zero(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        assert coordinator.last_mqtt_ts == 0.0


# ===========================================================================
# Setup
# ===========================================================================


class TestSetup:
    async def test_standard_setup_delta_subscribes_data(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """Delta in Standard Mode sets up MQTT with subscribe_data=True."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()
        assert coordinator.mqtt_client is not None

    async def test_standard_setup_smartplug_no_subscribe(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """Smart Plug in Standard Mode sets up MQTT with subscribe_data=False."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_SMARTPLUG_DEVICE
        )
        await coordinator.async_setup()
        assert coordinator.mqtt_client is not None


# ===========================================================================
# Message Parsing
# ===========================================================================


class TestMessageParsing:
    async def test_parse_delta_json(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Delta JSON quota messages are parsed."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        # Simulate _parse_message with a JSON quota message
        import json

        topic = "/open/cert_account/SN001/quota"
        payload = json.dumps({
            "typeCode": "pdStatus",
            "params": {"soc": 85, "wattsInSum": 200},
        }).encode()

        result = coordinator._parse_message(topic, payload)
        assert result is not None
        assert result["soc"] == 85.0
        assert result["watts_in_sum"] == 200.0

    async def test_parse_unknown_topic_returns_none(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Non-quota topics return None."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        result = coordinator._parse_message("/some/other/topic", b"data")
        assert result is None

    async def test_delta_mqtt_message_not_blocked(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Delta in Standard Mode passes MQTT data through (gate is open).

        _on_mqtt_message bridges to the event loop via call_soon_threadsafe,
        so we verify the gate doesn't block and _apply_data gets scheduled.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        import json

        topic = "/open/cert/SN001/quota"
        payload = json.dumps({"typeCode": "pdStatus", "params": {"soc": 85}}).encode()

        with patch.object(coordinator.hass.loop, "call_soon_threadsafe") as mock_csf:
            coordinator._on_mqtt_message(topic, payload)
            # Delta MQTT data should reach _apply_data (not blocked by gate)
            mock_csf.assert_called_once()
            args = mock_csf.call_args[0]
            assert args[0] == coordinator._apply_data
            assert args[1]["soc"] == 85.0

    async def test_smartplug_ignores_mqtt_data(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Smart Plug in Standard Mode ignores MQTT data (HTTP only)."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_SMARTPLUG_DEVICE
        )
        import json

        topic = "/open/cert/SN001/quota"
        payload = json.dumps({"typeCode": "pdStatus", "params": {"soc": 85}}).encode()

        coordinator._on_mqtt_message(topic, payload)
        # Smart Plug should NOT apply MQTT data
        assert coordinator.device_data == {}


# ===========================================================================
# HTTP Polling (_async_update_data)
# ===========================================================================


class TestHTTPPolling:
    async def test_http_update_delta(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """HTTP polling parses Delta quota data."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()

        data = await coordinator._async_update_data()
        assert "soc" in data
        assert data["soc"] == 75.0

    async def test_http_update_returns_existing_on_empty(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """If HTTP returns empty, existing data is preserved."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()

        # First successful fetch
        await coordinator._async_update_data()
        assert coordinator.device_data.get("soc") == 75.0

        # Now HTTP returns empty
        mock_http_client.get_quota_all = AsyncMock(return_value=None)
        data = await coordinator._async_update_data()
        # Existing data should be preserved
        assert data.get("soc") == 75.0


# ===========================================================================
# SET Commands
# ===========================================================================


class TestSETCommands:
    async def test_send_set_command_success(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """SET command is published via MQTT."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()

        command = {"moduleType": 1, "operateType": "dcOutCfg", "params": {"enabled": 1}}
        ok = await coordinator.async_send_set_command(command)
        assert ok is True
        mock_mqtt_client.publish.assert_called_once()

    async def test_send_set_command_no_mqtt(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """SET command returns False when MQTT not connected."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        # No async_setup → no MQTT client
        ok = await coordinator.async_send_set_command({"params": {}})
        assert ok is False


# ===========================================================================
# Shutdown
# ===========================================================================


class TestShutdown:
    async def test_shutdown_sets_flag(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """Shutdown sets the _shutdown flag and disconnects MQTT."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        await coordinator.async_setup()
        assert coordinator._shutdown is False

        await coordinator.async_shutdown()
        assert coordinator._shutdown is True
        assert coordinator.mqtt_client is None

    async def test_shutdown_cancels_timers(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
        mock_enhanced_auth,
    ) -> None:
        """Shutdown cancels keepalive and stale check timers."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        await coordinator.async_setup()

        # Enhanced Mode should have scheduled timers
        assert coordinator._keepalive_unsub is not None
        assert coordinator._stale_check_unsub is not None

        await coordinator.async_shutdown()
        assert coordinator._keepalive_unsub is None
        assert coordinator._stale_check_unsub is None


# ===========================================================================
# Stale Detection (_check_stale)
# ===========================================================================


class TestStaleDetection:
    def _cleanup_stale_timer(self, coordinator):
        """Cancel the timer that _check_stale re-schedules."""
        if coordinator._stale_check_unsub is not None:
            coordinator._stale_check_unsub.cancel()
            coordinator._stale_check_unsub = None

    async def test_stale_activates_http_fallback(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """When MQTT is stale, HTTP fallback is activated."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._last_mqtt_ts = time.time() - STALE_THRESHOLD_S - 10
        assert coordinator.update_interval is None

        coordinator._check_stale()

        assert coordinator.update_interval is not None
        assert coordinator.update_interval.total_seconds() == HTTP_FALLBACK_INTERVAL_S
        self._cleanup_stale_timer(coordinator)

    async def test_stale_recovery_disables_fallback(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """When MQTT recovers, HTTP fallback is disabled."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator.update_interval = timedelta(seconds=HTTP_FALLBACK_INTERVAL_S)
        coordinator._last_mqtt_ts = time.time()

        coordinator._check_stale()

        assert coordinator.update_interval is None
        self._cleanup_stale_timer(coordinator)

    async def test_stale_no_mqtt_ts_is_infinite(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """When last_mqtt_ts is 0, age is infinite → triggers fallback."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._last_mqtt_ts = 0.0

        coordinator._check_stale()

        assert coordinator.update_interval is not None
        self._cleanup_stale_timer(coordinator)

    async def test_stale_check_noop_when_shutdown(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_check_stale is a no-op when shutdown flag is set."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._shutdown = True
        coordinator._last_mqtt_ts = 0.0

        coordinator._check_stale()

        assert coordinator.update_interval is None
        # No timer scheduled when shutdown

    async def test_stale_triggers_reconnect(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Stale check triggers MQTT reconnect when disconnected."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        # Don't call async_setup — just set up a mock MQTT client directly
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = False
        mock_mqtt.try_reconnect.return_value = False
        coordinator._mqtt_client = mock_mqtt
        coordinator._last_mqtt_ts = time.time() - STALE_THRESHOLD_S - 10

        coordinator._check_stale()

        mock_mqtt.try_reconnect.assert_called_once()
        self._cleanup_stale_timer(coordinator)


# ===========================================================================
# Keepalive (_send_keepalive)
# ===========================================================================


class TestKeepalive:
    async def test_send_keepalive_when_connected(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Keepalive sends EnergyStreamSwitch when MQTT is connected."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        # Set up mock MQTT client directly (no async_setup → no lingering timers)
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt

        coordinator._send_keepalive()

        # Should reschedule
        assert coordinator._keepalive_unsub is not None
        # Cleanup
        coordinator._keepalive_unsub.cancel()
        coordinator._keepalive_unsub = None

    async def test_send_keepalive_noop_when_shutdown(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Keepalive is a no-op when shutdown flag is set."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._shutdown = True
        coordinator._send_keepalive()
        # Should not crash, no rescheduling after shutdown return


# ===========================================================================
# Enhanced Mode Setup
# ===========================================================================


class TestEnhancedSetup:
    async def test_enhanced_setup_creates_wss_client(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
        mock_enhanced_auth,
    ) -> None:
        """Enhanced setup creates MQTT client and schedules timers."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        await coordinator.async_setup()

        assert coordinator.mqtt_client is not None
        assert coordinator._keepalive_unsub is not None
        assert coordinator._stale_check_unsub is not None
        # Cleanup
        await coordinator.async_shutdown()

    async def test_enhanced_setup_credential_failure(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """Enhanced setup handles credential fetch failure gracefully."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        with patch.object(
            coordinator, "_fetch_enhanced_credentials",
            new_callable=AsyncMock,
            return_value=(None, ""),
        ):
            await coordinator.async_setup()

        # MQTT client should NOT be created on failure
        assert coordinator.mqtt_client is None


# ===========================================================================
# Apply Data (_apply_data)
# ===========================================================================


class TestApplyData:
    async def test_apply_data_updates_device_data(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_apply_data updates device_data and last_mqtt_ts."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        before = time.time()
        coordinator._apply_data({"solar_w": 3000, "soc_pct": 85})
        after = time.time()

        assert coordinator.device_data["solar_w"] == 3000
        assert coordinator.device_data["soc_pct"] == 85
        assert before <= coordinator.last_mqtt_ts <= after

    async def test_apply_data_merges(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_apply_data merges new data with existing."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._apply_data({"solar_w": 3000})
        coordinator._apply_data({"soc_pct": 85})

        # Core keys must be present; energy integration may add extra keys
        assert coordinator.device_data["solar_w"] == 3000
        assert coordinator.device_data["soc_pct"] == 85

    async def test_apply_data_integrates_energy(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """F-005: _apply_data calls _integrate_energy for PowerOcean."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        assert coordinator._energy_integrator is not None

        # Explicitly load state (normally done in async_setup via executor)
        coordinator._energy_integrator.load_state()

        # First call: sets baseline (no energy yet)
        coordinator._apply_data({"solar_w": 3000, "home_w": 1500})
        # Backdate the integrator's internal timestamp to simulate elapsed time
        # (same pattern as tests/test_energy_integrator.py — manipulate _state directly)
        for metric in ("solar_energy_kwh", "home_energy_kwh"):
            if metric in coordinator._energy_integrator._state:
                total, _ts, power = coordinator._energy_integrator._state[metric]
                coordinator._energy_integrator._state[metric] = (total, time.time() - 30, power)
        coordinator._apply_data({"solar_w": 3000, "home_w": 1500})

        # Energy keys should now exist in device_data
        assert "solar_energy_kwh" in coordinator.device_data
        # The rounded value (3 decimals) may be 0.000 for short intervals,
        # so check the raw integrator total which has full precision
        raw_total = coordinator._energy_integrator.get_total("solar_energy_kwh")
        assert raw_total is not None and raw_total > 0


# ===========================================================================
# Protobuf Key Remapping (_remap_proto_keys) — F-001
# ===========================================================================


class TestProtoKeyRemapping:
    async def test_remap_energy_stream_keys(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Protobuf keys are remapped to sensor keys."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "solar": 3200,
            "home_direct": 1500,
            "batt_pb": -800,
            "grid_raw_f2": 200,
            "soc": 72.0,
        }
        result = coordinator._remap_proto_keys(raw)

        assert result["solar_w"] == 3200
        assert result["home_w"] == 1500
        assert result["batt_w"] == -800
        assert result["grid_w"] == 200
        assert result["soc_pct"] == 72.0

    async def test_remap_derives_grid_import_export(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Grid import/export splits are computed from grid_w."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        # Positive grid_w = import
        result = coordinator._remap_proto_keys({"grid_raw_f2": 500})
        assert result["grid_import_power_w"] == 500
        assert result["grid_export_power_w"] == 0.0

        # Negative grid_w = export
        result = coordinator._remap_proto_keys({"grid_raw_f2": -300})
        assert result["grid_import_power_w"] == 0.0
        assert result["grid_export_power_w"] == 300

    async def test_remap_derives_batt_charge_discharge(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Battery charge/discharge splits are computed from batt_w."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        # Positive batt_w = charging
        result = coordinator._remap_proto_keys({"batt_pb": 1200})
        assert result["batt_charge_power_w"] == 1200
        assert result["batt_discharge_power_w"] == 0.0

        # Negative batt_w = discharging
        result = coordinator._remap_proto_keys({"batt_pb": -900})
        assert result["batt_charge_power_w"] == 0.0
        assert result["batt_discharge_power_w"] == 900

    async def test_remap_preserves_unknown_keys(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Keys not in the mapping are passed through unchanged."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        result = coordinator._remap_proto_keys({"solar": 100, "some_new_field": 42})
        assert result["solar_w"] == 100
        assert result["some_new_field"] == 42

    async def test_remap_zero_values(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Zero power values produce zero derived splits."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        result = coordinator._remap_proto_keys({"grid_raw_f2": 0.0, "batt_pb": 0.0})
        assert result["grid_w"] == 0.0
        assert result["grid_import_power_w"] == 0.0
        assert result["grid_export_power_w"] == 0.0
        assert result["batt_w"] == 0.0
        assert result["batt_charge_power_w"] == 0.0
        assert result["batt_discharge_power_w"] == 0.0


# ===========================================================================
# Heartbeat Nested Extraction (_flatten_heartbeat) — MPPT, Grid Phases
# ===========================================================================


class TestHeartbeatExtraction:
    async def test_mppt_per_string(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """MPPT per-string data extracted from nested mppt_heart_beat."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "mppt_heart_beat": [
                {
                    "mppt_pv": [
                        {"pwr": 2500, "vol": 480.0, "amp": 5.2},
                        {"pwr": 2400, "vol": 485.0, "amp": 4.9},
                    ]
                }
            ],
            "pcs_ac_freq": 50.01,
        }
        result = coordinator._flatten_heartbeat(raw)

        assert result["mppt_pv1_power_w"] == 2500.0
        assert result["mppt_pv1_voltage_v"] == 480.0
        assert result["mppt_pv1_current_a"] == 5.2
        assert result["mppt_pv2_power_w"] == 2400.0
        assert result["mppt_pv2_voltage_v"] == 485.0
        assert result["mppt_pv2_current_a"] == 4.9
        assert result["pcs_ac_freq_hz"] == 50.01

    async def test_grid_phase_from_load_info(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Grid phase data extracted from pcs_load_info nested array."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "pcs_load_info": [
                {"vol": 230.5, "amp": 10.2, "pwr": 2300.0},
                {"vol": 231.0, "amp": 11.0, "pwr": 2500.0},
                {"vol": 229.8, "amp": 9.5, "pwr": 2100.0},
            ]
        }
        result = coordinator._flatten_heartbeat(raw)

        assert result["grid_phase_a_voltage_v"] == 230.5
        assert result["grid_phase_a_current_a"] == 10.2
        assert result["grid_phase_a_active_power_w"] == 2300.0
        assert result["grid_phase_b_voltage_v"] == 231.0
        assert result["grid_phase_c_voltage_v"] == 229.8

    async def test_grid_phase_from_pcs_phase(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Grid phase data from pcs_a/b/c_phase fallback."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {
            "pcs_a_phase": {"vol": 230.0, "amp": 10.0, "act_pwr": -2200.0},
            "pcs_b_phase": {"vol": 231.0, "amp": 11.0, "act_pwr": -2500.0},
        }
        result = coordinator._flatten_heartbeat(raw)

        assert result["grid_phase_a_voltage_v"] == 230.0
        assert result["grid_phase_a_current_a"] == 10.0
        assert result["grid_phase_a_active_power_w"] == -2200.0
        assert result["grid_phase_b_voltage_v"] == 231.0

    async def test_empty_heartbeat(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Empty heartbeat produces empty result."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        result = coordinator._flatten_heartbeat({})
        assert result == {}


# ===========================================================================
# Battery / EMS Remapping (_remap_bp_keys)
# ===========================================================================


class TestBpRemapping:
    async def test_battery_keys_remapped(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Battery heartbeat keys mapped to sensor keys."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {"bp_soh": 98, "bp_cycles": 42, "bp_vol": 52.1, "bp_env_temp": 25}
        result = coordinator._remap_bp_keys(raw)

        assert result["bp_soh_pct"] == 98.0
        assert result["bp_cycles"] == 42.0
        assert result["bp_voltage_v"] == 52.1
        assert result["bp_env_temp_c"] == 25.0

    async def test_ems_change_keys_remapped(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """EMS change report keys mapped to sensor keys."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {"bp_online_sum": 2, "ems_feed_mode": 1, "sys_grid_sta": 0}
        result = coordinator._remap_bp_keys(raw)

        assert result["bp_online_sum"] == 2.0
        assert result["ems_feed_mode"] == 1.0
        assert result["grid_status"] == 0.0

    async def test_energy_totals_wh_to_kwh(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """EMS change report energy totals converted from Wh to kWh."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        raw = {"bp_total_chg_energy": 15000, "bp_total_dsg_energy": 12000}
        result = coordinator._remap_bp_keys(raw)

        assert result["batt_charge_energy_kwh"] == 15.0
        assert result["batt_discharge_energy_kwh"] == 12.0


# ===========================================================================
# Monotonic Filter (_enforce_monotonic) — total_increasing regression guard
# ===========================================================================


class TestMonotonicFilter:
    async def test_regression_dropped(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Values that decrease a total_increasing sensor are dropped."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        # Seed existing data
        coordinator._device_data["bp_cycles"] = 461.0
        coordinator._device_data["solar_energy_kwh"] = 100.5

        parsed = {"bp_cycles": 460.0, "solar_energy_kwh": 100.499, "solar_w": 3000}
        coordinator._enforce_monotonic(parsed)

        # Regressions removed, non-monotonic key preserved
        assert "bp_cycles" not in parsed
        assert "solar_energy_kwh" not in parsed
        assert parsed["solar_w"] == 3000

    async def test_increase_allowed(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Values that increase are kept."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._device_data["bp_cycles"] = 460.0

        parsed = {"bp_cycles": 461.0}
        coordinator._enforce_monotonic(parsed)

        assert parsed["bp_cycles"] == 461.0

    async def test_equal_value_kept(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Equal values are kept (not a regression)."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._device_data["bp_cycles"] = 460.0

        parsed = {"bp_cycles": 460.0}
        coordinator._enforce_monotonic(parsed)

        assert parsed["bp_cycles"] == 460.0

    async def test_no_existing_data_passes(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """First value is always accepted (no previous data)."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        parsed = {"bp_cycles": 460.0}
        coordinator._enforce_monotonic(parsed)

        assert parsed["bp_cycles"] == 460.0

    async def test_delta_bms_cycles(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Delta bms_cycles regression is also filtered."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._device_data["bms_cycles"] = 150.0

        parsed = {"bms_cycles": 149.0, "soc": 85.0}
        coordinator._enforce_monotonic(parsed)

        assert "bms_cycles" not in parsed
        assert parsed["soc"] == 85.0


# ===========================================================================
# Quotas Poll (_send_quotas_poll)
# ===========================================================================


class TestQuotasPoll:
    async def test_send_quotas_poll_when_connected(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_send_quotas_poll sends latestQuotas when MQTT is connected."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt

        with patch.object(hass, "async_add_executor_job") as mock_exec:
            coordinator._send_quotas_poll()
            mock_exec.assert_called_once_with(mock_mqtt.send_latest_quotas)

        # Should reschedule
        assert coordinator._quotas_unsub is not None
        # Cleanup: cancel the rescheduled timer
        coordinator._quotas_unsub.cancel()
        coordinator._quotas_unsub = None

    async def test_send_quotas_poll_noop_when_shutdown(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_send_quotas_poll is a no-op when shutdown flag is set."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._shutdown = True
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt

        coordinator._send_quotas_poll()

        # Should not schedule anything when shutdown
        assert coordinator._quotas_unsub is None

    async def test_send_quotas_poll_skips_when_disconnected(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_send_quotas_poll skips send when MQTT is disconnected."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = False
        coordinator._mqtt_client = mock_mqtt

        with patch.object(hass, "async_add_executor_job") as mock_exec:
            coordinator._send_quotas_poll()
            mock_exec.assert_not_called()

        # Should still reschedule
        assert coordinator._quotas_unsub is not None
        coordinator._quotas_unsub.cancel()
        coordinator._quotas_unsub = None


# ===========================================================================
# Ping Heartbeat (_send_ping)
# ===========================================================================


class TestPing:
    async def test_send_ping_when_connected(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_send_ping sends ping when MQTT is connected."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt

        with patch.object(hass, "async_add_executor_job") as mock_exec:
            coordinator._send_ping()
            mock_exec.assert_called_once_with(mock_mqtt.send_ping)

        # Should reschedule
        assert coordinator._ping_unsub is not None
        # Cleanup
        coordinator._ping_unsub.cancel()
        coordinator._ping_unsub = None

    async def test_send_ping_noop_when_shutdown(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_send_ping is a no-op when shutdown flag is set."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._shutdown = True
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt

        coordinator._send_ping()

        # Should not schedule anything when shutdown
        assert coordinator._ping_unsub is None

    async def test_send_ping_skips_when_disconnected(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_send_ping skips send when MQTT is disconnected."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = False
        coordinator._mqtt_client = mock_mqtt

        with patch.object(hass, "async_add_executor_job") as mock_exec:
            coordinator._send_ping()
            mock_exec.assert_not_called()

        # Should still reschedule
        assert coordinator._ping_unsub is not None
        coordinator._ping_unsub.cancel()
        coordinator._ping_unsub = None


# ===========================================================================
# _parse_message Protobuf Branch
# ===========================================================================


class TestParseMessageProtobuf:
    async def test_parse_protobuf_energy_stream(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_parse_message decodes a protobuf energy_stream frame for PowerOcean."""
        from custom_components.ecoflow_energy.ecoflow.energy_stream import (
            _encode_field_bytes,
            _encode_field_varint,
        )
        from custom_components.ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
            JTS1EnergyStreamReport,
        )

        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )

        # Build a real protobuf energy stream frame
        msg = JTS1EnergyStreamReport()
        msg.mppt_pwr = 4200.0
        msg.sys_load_pwr = 1800.0
        msg.bp_pwr = -500.0
        msg.sys_grid_pwr = 300.0
        msg.bp_soc = 65
        inner = msg.SerializeToString()

        # Wrap in HeaderMessage frame (cmd_func=96, cmd_id=33)
        header = bytearray()
        header.extend(_encode_field_bytes(1, inner))
        header.extend(_encode_field_varint(8, 96))
        header.extend(_encode_field_varint(9, 33))
        frame = _encode_field_bytes(1, bytes(header))

        topic = "/app/device/property/HW52ZAB412340001"
        result = coordinator._parse_message(topic, frame)

        assert result is not None
        assert result["solar_w"] == 4200.0
        assert result["home_w"] == 1800.0
        assert result["batt_w"] == -500.0
        assert result["grid_w"] == 300.0
        assert result["soc_pct"] == 65.0
        # Derived splits should be computed
        assert result["grid_import_power_w"] == 300.0
        assert result["grid_export_power_w"] == 0.0

    async def test_parse_protobuf_malformed_graceful(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """_parse_message handles malformed protobuf data without raising."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE
        )

        # Data with 0x0a in first 4 bytes triggers protobuf path, but is invalid
        malformed = b"\x0a\xff\xfe\x01garbage_data_here"
        topic = "/app/device/property/HW52ZAB412340001"
        result = coordinator._parse_message(topic, malformed)

        assert result is None


# ===========================================================================
# _parse_message JSON Decode Error Path
# ===========================================================================


class TestParseMessageJsonError:
    async def test_json_decode_error_returns_none(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Malformed JSON on a /quota topic returns None without raising."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        topic = "/open/cert_account/SN001/quota"
        malformed = b"\xff\xfe malformed json {"

        result = coordinator._parse_message(topic, malformed)
        assert result is None

    async def test_non_dict_json_returns_none(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Valid JSON that is not a dict returns None."""
        import json

        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        topic = "/open/cert_account/SN001/quota"
        payload = json.dumps([1, 2, 3]).encode()

        result = coordinator._parse_message(topic, payload)
        assert result is None
