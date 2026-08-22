"""Functional tests for diagnostics - runtime output verification."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    CONF_ACCESS_KEY,
    CONF_DEVICES,
    CONF_MODE,
    CONF_RAW_CAPTURE,
    CONF_RAW_CAPTURE_UNTIL,
    CONF_SECRET_KEY,
    DATA_SKIPPED_DEVICES,
    DOMAIN,
    MODE_STANDARD,
    RAW_FRAME_BUNDLE_MAX_BYTES,
    RAW_FRAME_LOG_PER_KEY_MAX,
    RAW_FRAME_MAX_BYTES,
    RAW_FRAME_PER_KEY_MAX,
    UNKNOWN_FIELD_CMDS_MAX,
    UNKNOWN_FIELD_NUMBERS_MAX,
)
from custom_components.ecoflow_energy.diagnostics import (
    REDACTED,
    _device_diagnostics,
    _redact_serials,
    _skipped_devices_diagnostics,
    async_get_config_entry_diagnostics,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.frame_capture import build_frame_entry

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
        # Don't set up the integration - no coordinators in hass.data
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
        """Standard Mode is not a fallback - http_fallback_active is False."""
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


class TestFirmwareDiagnostics:
    """The firmware section is what tells a bug report which revision ran."""

    async def test_firmware_section_present_when_quota_reports_one(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._firmware = {
            "pd.sysVer": {"raw": 16975450, "decoded": "v1.3.6.90"},
        }

        result = _device_diagnostics(coordinator)

        assert result["firmware"]["pd.sysVer"]["decoded"] == "v1.3.6.90"
        assert result["firmware"]["pd.sysVer"]["raw"] == 16975450

    async def test_firmware_section_empty_when_device_reports_none(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """An empty section is the honest answer for PowerOcean.

        Its quota carries no revision under any key, so the section must be
        present and empty rather than absent - a reader has to be able to tell
        "device reports none" from "we forgot to collect it".
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_POWEROCEAN_DEVICE
        )

        result = _device_diagnostics(coordinator)

        assert result["firmware"] == {}

    async def test_firmware_keys_redact_serials(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """A revision addressed by battery pack serial must not leak it.

        The PowerOcean quota puts pack serials into the key itself, and users
        are asked to attach diagnostics to public issues. Asserted against the
        full download, because that is where the single redaction pass runs.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._firmware = {
            "bp_addr.HJ31TESTSERIAL02.sysVer": {
                "raw": 16975450,
                "decoded": "v1.3.6.90",
            },
        }
        hass.data.setdefault(DOMAIN, {})[standard_config_entry.entry_id] = {
            coordinator.device_sn: coordinator
        }

        result = await async_get_config_entry_diagnostics(hass, standard_config_entry)

        firmware = result["devices"][0]["firmware"]
        key = next(iter(firmware))
        assert "HJ31TESTSERIAL02" not in key
        assert firmware[key]["decoded"] == "v1.3.6.90"


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
        dump users are asked to attach to a public issue. Asserted against
        the full download, because that is where the single redaction pass
        runs.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._raw_quota = {"bp_addr.HJ31TESTSERIAL01": {"bpSoc": 74}}
        coordinator._raw_quota_captured_at = 1000.0
        hass.data.setdefault(DOMAIN, {})[standard_config_entry.entry_id] = {
            coordinator.device_sn: coordinator
        }

        result = await async_get_config_entry_diagnostics(hass, standard_config_entry)

        keys = list(result["devices"][0]["raw_quota"]["values"])
        assert keys == ["bp_addr.**REDACTED**"]
        assert "HJ31TESTSERIAL01" not in json.dumps(result)

    async def test_powerocean_two_pack_serial_keys_stay_distinct(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Both packs must survive the dump, under distinct placeholder keys.

        Redacting inside the section handed every key its own fresh alias
        map, so two `bp_addr.<sn>` keys collapsed onto the same placeholder
        and the dict comprehension kept only the last pack - a two-pack
        system read as a one-pack system in the artefact used to answer
        exactly that question.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._raw_quota = {
            "bp_addr.HJ31TESTSERIAL01": {"bpSoc": 74},
            "bp_addr.HJ31TESTSERIAL02": {"bpSoc": 71},
        }
        coordinator._raw_quota_captured_at = 1000.0
        hass.data.setdefault(DOMAIN, {})[standard_config_entry.entry_id] = {
            coordinator.device_sn: coordinator
        }

        result = await async_get_config_entry_diagnostics(hass, standard_config_entry)

        values = result["devices"][0]["raw_quota"]["values"]
        assert values["bp_addr.**REDACTED**"] == {"bpSoc": 74}
        assert values["bp_addr.**REDACTED-2**"] == {"bpSoc": 71}
        serialized = json.dumps(result)
        assert "HJ31TESTSERIAL01" not in serialized
        assert "HJ31TESTSERIAL02" not in serialized

    async def test_delta3_raw_quota_redacts_serials(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Serial-looking raw quota values are redacted in the download."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA3_DEVICE
        )
        coordinator._raw_quota = {
            "sn": "D3M1TEST00000001",
            "bpSoc": 80,
        }
        coordinator._raw_quota_captured_at = 1000.0
        hass.data.setdefault(DOMAIN, {})[standard_config_entry.entry_id] = {
            coordinator.device_sn: coordinator
        }

        result = await async_get_config_entry_diagnostics(hass, standard_config_entry)

        values = result["devices"][0]["raw_quota"]["values"]
        assert values["sn"] == REDACTED
        assert values["bpSoc"] == 80

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
        hass.data.setdefault(DOMAIN, {})[standard_config_entry.entry_id] = {
            coordinator.device_sn: coordinator
        }

        result = await async_get_config_entry_diagnostics(hass, standard_config_entry)

        values = result["devices"][0]["raw_quota"]["values"]
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


class TestEnergyIntegratorDiagnostics:
    """Why a kWh counter sits near zero, answerable from a download (#177).

    Three numbers per counter settle it: whether the metric exists at all,
    what power was last integrated into it, and how long ago. Before this,
    the only copy lived in a JSON file inside the configuration folder and a
    reporter had to be talked through finding it.
    """

    async def test_metrics_report_total_power_and_age(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._energy_integrator._state = {
            "solar_energy_kwh": (18.696950613807584, 1000.0, 150.0),
            "batt_charge_energy_kwh": (2.5, 600.0, 0.0),
        }

        with patch(
            "custom_components.ecoflow_energy.diagnostics.time.monotonic",
            return_value=1004.2,
        ):
            section = _device_diagnostics(coordinator)["energy_integrator"]

        solar = section["metrics"]["solar_energy_kwh"]
        assert solar["total_kwh"] == 18.696951
        assert solar["last_power_w"] == 150.0
        assert solar["age_s"] == 4.2

        # The second counter has not been touched in over six minutes, and
        # that has to be readable next to the first one without arithmetic.
        assert section["metrics"]["batt_charge_energy_kwh"]["age_s"] == 404.2
        assert "note" in section

    async def test_stored_monotonic_timestamp_never_reaches_the_output(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """784722 seconds since an arbitrary epoch tells a reader nothing.

        Worse, it reads like a wall clock and is not one. The age replaces
        it rather than sitting beside it.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._energy_integrator._state = {
            "solar_energy_kwh": (18.7, 784722.222677657, 150.0),
        }

        with patch(
            "custom_components.ecoflow_energy.diagnostics.time.monotonic",
            return_value=784726.0,
        ):
            section = _device_diagnostics(coordinator)["energy_integrator"]

        assert "784722" not in json.dumps(section)
        assert section["metrics"]["solar_energy_kwh"]["age_s"] == 3.8

    async def test_metric_seeded_once_and_never_integrated_again(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """The whole point of the section, in one test.

        A counter that took one reading and then never another is the case
        somebody opens the download to ask about, and it is invisible from
        the sensor: the entity shows a total of zero either way. It has to
        appear here, and its age has to be the real time since that one
        reading rather than a number that resets when the download is taken.
        A section listing only counters above zero would hide it entirely.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        # What integrate() leaves behind on a first reading: state seeded at
        # the current monotonic time, no total reported. Marked loaded so the
        # seeding does not go near the state file, since diagnostics runs on
        # the event loop.
        integrator = coordinator._energy_integrator
        integrator._loaded = True
        with patch(
            "custom_components.ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=1000.0,
        ):
            assert integrator.integrate("solar_energy_kwh", 150.0) is None

        # Nine hours later, with no further reading in between.
        with patch(
            "custom_components.ecoflow_energy.diagnostics.time.monotonic",
            return_value=1000.0 + 9 * 3600,
        ):
            section = _device_diagnostics(coordinator)["energy_integrator"]

        metric = section["metrics"]["solar_energy_kwh"]
        assert metric["total_kwh"] == 0.0
        assert metric["last_power_w"] == 150.0
        # Real elapsed time since the one reading, not the moment of the dump.
        assert metric["age_s"] == 32400.0

    async def test_a_creeping_total_is_not_rounded_down_to_zero(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Zero and almost-zero are different answers to the same question.

        One integration step at 150 W over three seconds is 0.000125 kWh.
        Rounded to the two places used elsewhere in this integration, a
        counter that is slowly moving is indistinguishable from one that has
        never moved at all.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._energy_integrator._state = {
            "solar_energy_kwh": (0.000125, 1000.0, 150.0),
        }

        with patch(
            "custom_components.ecoflow_energy.diagnostics.time.monotonic",
            return_value=1003.0,
        ):
            section = _device_diagnostics(coordinator)["energy_integrator"]

        assert section["metrics"]["solar_energy_kwh"]["total_kwh"] == 0.000125

    async def test_section_present_and_empty_when_nothing_integrated(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """"This device integrates nothing" is an answer, not a missing section.

        An absent section cannot be told apart from a download taken with a
        version that never had one.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )

        section = _device_diagnostics(coordinator)["energy_integrator"]

        assert section["metrics"] == {}
        assert section["note"]

    async def test_malformed_state_entry_does_not_break_the_download(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """One unreadable counter must not cost the whole file."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._energy_integrator._state = {
            "broken_energy_kwh": ("not", "a", "number"),
            "short_energy_kwh": (1.0,),
            "solar_energy_kwh": (18.7, 1000.0, 150.0),
        }

        with patch(
            "custom_components.ecoflow_energy.diagnostics.time.monotonic",
            return_value=1002.0,
        ):
            section = _device_diagnostics(coordinator)["energy_integrator"]

        # Named, not dropped: an absent metric would read as "no such counter".
        assert "error" in section["metrics"]["broken_energy_kwh"]
        assert "error" in section["metrics"]["short_energy_kwh"]
        assert section["metrics"]["solar_energy_kwh"]["total_kwh"] == 18.7

    async def test_missing_integrator_leaves_an_empty_section(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Diagnostics never raises, whatever state the coordinator is in."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        del coordinator._energy_integrator

        result = _device_diagnostics(coordinator)

        assert result["energy_integrator"]["metrics"] == {}
        assert result["device_name"] == "Delta 2 Max"

    async def test_metric_names_go_through_the_redaction_pass(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """The section is inside the single pass, not built after it.

        Energy metric names are static and carry no serial today. This is
        here so the section keeps that guarantee if a per-device metric key
        is ever introduced - and so nobody moves the section outside the pass
        without something failing.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._energy_integrator._state = {
            "pack_HJ31TESTSERIAL01_energy_kwh": (3.0, 1000.0, 20.0),
        }
        hass.data.setdefault(DOMAIN, {})[standard_config_entry.entry_id] = {
            coordinator.device_sn: coordinator
        }

        result = await async_get_config_entry_diagnostics(hass, standard_config_entry)

        metrics = result["devices"][0]["energy_integrator"]["metrics"]
        assert list(metrics) == ["pack_**REDACTED**_energy_kwh"]
        assert "HJ31TESTSERIAL01" not in json.dumps(result)


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

    def test_two_serials_do_not_collapse_onto_one_key(self) -> None:
        """A second battery pack must survive redaction.

        One shared placeholder made both `bp_addr.<sn>` keys identical, and
        the dict comprehension kept only the last - a two-pack system read as
        a one-pack system in the dump used to answer exactly that question.
        """
        out = _redact_serials(
            {
                "bp_addr.HJ31TESTSERIAL01": {"bpSoc": 74},
                "bp_addr.HJ31TESTSERIAL02": {"bpSoc": 71},
            }
        )
        assert len(out) == 2
        assert out["bp_addr.**REDACTED**"] == {"bpSoc": 74}
        assert out["bp_addr.**REDACTED-2**"] == {"bpSoc": 71}

    def test_same_serial_reads_the_same_throughout_one_pass(self) -> None:
        out = _redact_serials(
            {
                "a": "HJ31TESTSERIAL01",
                "b": {"c": "HJ31TESTSERIAL02"},
                "d": ["HJ31TESTSERIAL01"],
            }
        )
        assert out["a"] == REDACTED
        assert out["b"]["c"] == "**REDACTED-2**"
        assert out["d"] == [REDACTED]

    def test_placeholders_do_not_leak_the_serial(self) -> None:
        out = _redact_serials(["HJ31TESTSERIAL01", "HJ31TESTSERIAL02"])
        assert "HJ31TESTSERIAL01" not in json.dumps(out)
        assert "HJ31TESTSERIAL02" not in json.dumps(out)

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

        # get_quota_all() returns the already-unwrapped flat quota dict -
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
        coordinator._raw_frames.add("property:proto/96.33", {
            "ts": 1784973604.0,
            "topic": "property",
            "size": 42,
            "parsed_keys": 5,
            "cmds": [{"cmd_func": 96, "cmd_id": 33}],
            "hex": "0a02ffff",
        })

        result = _device_diagnostics(coordinator)

        assert result["raw_frames"]["count"] == 1
        # A pair, not a flat number: one figure would describe neither kind
        # of frame, and a silent revert to the flat cap has to fail here.
        assert result["raw_frames"]["truncated_at_bytes"] == {
            "message": RAW_FRAME_MAX_BYTES,
            "bundle": RAW_FRAME_BUNDLE_MAX_BYTES,
        }
        frame = result["raw_frames"]["frames"][0]
        assert frame["hex"] == "0a02ffff"
        assert frame["cmds"] == [{"cmd_func": 96, "cmd_id": 33}]
        assert frame["ts_iso"].startswith("2026-")

    async def test_a_capture_says_whether_app_writes_were_watched(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Absence of a write frame has two causes and they look identical.

        Either the vendor app sent none, or this version never subscribed to
        the topic they arrive on. The first is a finding about the device,
        the second about the reporter's version, and #284 spent a round trip
        on the difference. The flag makes the download answer it.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._raw_frames.add("property:proto/96.33", {
            "ts": 1784973604.0,
            "topic": "property",
            "size": 42,
            "hex": "0a02ffff",
        })

        # No client at all is the honest "not watching", not an omission.
        assert _device_diagnostics(coordinator)["raw_frames"][
            "app_writes_watched"
        ] is False

        coordinator._mqtt_client = self._watching_client()

        assert _device_diagnostics(coordinator)["raw_frames"][
            "app_writes_watched"
        ] is True

    @staticmethod
    def _watching_client() -> Mock:
        return Mock(
            capture_writes=True,
            last_connect_time=0.0,
            reconnect_attempts=0,
            **{"is_connected.return_value": False},
        )

    async def test_an_empty_capture_still_says_writes_were_watched(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """The case where the question is sharpest has no frame list at all.

        A recording that kept nothing is exactly when "was anything being
        watched?" needs answering, and the frame list it would have been
        attached to does not exist. So the flag stands on its own.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        coordinator._mqtt_client = self._watching_client()

        diag = _device_diagnostics(coordinator)

        assert "raw_frames" not in diag
        assert diag["app_writes_watched"] is True

    async def test_sampling_counts_are_exposed(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """The frame list is a sample, and a reader has to be able to tell."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        for index in range(30):
            coordinator._raw_frames.add("property:proto/96.33", {
                "ts": 1784973604.0 + index * 3,
                "topic": "property",
                "size": 42,
                "hex": "0a02ffff",
            })

        section = _device_diagnostics(coordinator)["raw_frames"]

        sampling = section["sampling"]
        assert sampling["frames_seen"] == 30
        assert sampling["frames_kept"] < 30
        # The counts only help if they describe the same state of the buffer
        # as the frame list next to them. Read separately, the capture thread
        # can add a frame in between and the two disagree.
        assert sampling["frames_kept"] == section["count"]
        assert sampling["frames_kept"] == len(section["frames"])

    @pytest.mark.parametrize(
        ("capture", "expected"),
        [
            (False, RAW_FRAME_LOG_PER_KEY_MAX),
            (True, RAW_FRAME_PER_KEY_MAX),
        ],
        ids=["always_on_depth", "volunteer_depth"],
    )
    async def test_sampling_names_the_depth_that_produced_the_download(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        capture: bool,
        expected: int,
    ) -> None:
        """A reader has to be able to tell the two depths apart.

        A supported device records either way, so a short frame list has one
        more possible cause than before: the opt-in was off. Without this
        figure in the download nothing distinguishes that from a quiet device.
        """
        now = 1_800_000_000.0
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="EcoFlow Energy",
            data={
                **enhanced_config_entry.data,
                CONF_RAW_CAPTURE: capture,
                CONF_RAW_CAPTURE_UNTIL: now + 3600,
            },
            unique_id="depth@example.com",
        )
        entry.add_to_hass(hass)

        with patch(
            "custom_components.ecoflow_energy.const.time.time", return_value=now
        ):
            coordinator = EcoFlowDeviceCoordinator(
                hass, entry, MOCK_POWEROCEAN_DEVICE
            )
        coordinator._raw_frames.add("property:proto/96.33", {
            "ts": 1784973604.0,
            "topic": "property",
            "size": 42,
            "hex": "0a02ffff",
        })

        section = _device_diagnostics(coordinator)["raw_frames"]

        assert section["sampling"]["per_key_max"] == expected


class TestUnroutedDeviceCapture:
    """A skipped device's captured frames are the evidence for adding support."""

    def _probe(self, sn: str = "RE11TEST00000001"):
        probe = MagicMock()
        probe.device_sn = sn
        # Mirrors the real probe's connection report. The export spreads it
        # into the section, so a bare MagicMock here would not survive the
        # spread at all.
        probe.connection = {
            "connected": True,
            "ever_connected": True,
            "connect_attempts": 1,
            "sessions": 1,
            "disconnects": 0,
            "capture_age_s": 21600,
            "verdict": "listening",
        }
        probe.topics = ["/app/{sn}/thing/property/get_reply"]
        probe.frames = [{
            "ts": 1784973604.0,
            "topic": "get_reply",
            "size": 120,
            "format": "proto",
            "cmds": [{"cmd_func": 96, "cmd_id": 33}],
            "hex": "0a02ffff",
        }]
        # Mirrors the real probe's sampling report. A bare MagicMock here
        # would satisfy the export and then fail to serialise, which is not
        # what the leak assertion below is meant to be testing.
        probe.sampling = {
            "frames_seen": 4212,
            "frames_kept": 1,
            "span_s": 21598.0,
            "keys_tracked": 1,
            "keys_max": 12,
            "per_key_max": 10,
            "frames_dropped_key_budget": 0,
            "per_key": {"get_reply:proto/96.33": {"seen": 4212, "kept": 1}},
        }
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
        assert capture["verdict"] == "listening"
        assert capture["frame_count"] == 1
        # Same pair as the routed-device section: both sections describe the
        # same budget split, and a reader of either has to see both figures.
        assert capture["truncated_at_bytes"] == {
            "message": RAW_FRAME_MAX_BYTES,
            "bundle": RAW_FRAME_BUNDLE_MAX_BYTES,
        }
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


class TestUnknownProtoFieldDiagnostics:
    """The field numbers a device sends that the binding does not declare.

    This is the evidence path for "does this hardware report the value a
    control would read back", which the polled quota cannot answer for a
    field that only travels on the protobuf push path.
    """

    async def test_no_unknown_fields_omits_section(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """A device whose fields are all declared has no section."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA3_DEVICE
        )
        assert "unknown_proto_fields" not in _device_diagnostics(coordinator)

    async def test_recorded_fields_exposed_sorted(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Field numbers reach diagnostics keyed by command, in order."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA3_DEVICE
        )
        coordinator.record_unknown_proto_fields("254/21", {5064: 1000, 96: "8 bytes"})
        coordinator.record_unknown_proto_fields("254/21", {6396: 1})

        section = _device_diagnostics(coordinator)["unknown_proto_fields"]

        assert list(section["commands"]["254/21"]) == ["96", "5064", "6396"]
        assert section["commands"]["254/21"]["5064"] == 1000

    async def test_newest_sample_wins(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """A value that changes is tracked, not frozen at first sight.

        A reporter asked to move a setting in the app and dump diagnostics
        again must see the number move too, otherwise the dump cannot tie a
        field to a setting.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA3_DEVICE
        )
        coordinator.record_unknown_proto_fields("254/21", {5064: 1000})
        coordinator.record_unknown_proto_fields("254/21", {5064: 2400})

        section = _device_diagnostics(coordinator)["unknown_proto_fields"]
        assert section["commands"]["254/21"]["5064"] == 2400

    async def test_command_count_is_bounded(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """A dict fed from the network does not grow without a bound."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA3_DEVICE
        )
        for cmd in range(UNKNOWN_FIELD_CMDS_MAX + 5):
            coordinator.record_unknown_proto_fields(f"254/{cmd}", {1: 1})

        section = _device_diagnostics(coordinator)["unknown_proto_fields"]
        assert len(section["commands"]) == UNKNOWN_FIELD_CMDS_MAX

    @staticmethod
    def _frame(cmd_func: int, cmd_id: int, inner: bytes) -> bytes:
        """Wrap a message in the header envelope the ingest path expects."""
        from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
            encode_field_bytes,
            encode_field_varint,
        )

        header = bytearray()
        header.extend(encode_field_bytes(1, inner))
        header.extend(encode_field_varint(8, cmd_func))
        header.extend(encode_field_varint(9, cmd_id))
        return encode_field_bytes(1, bytes(header))

    async def test_delta3_frame_records_through_the_ingest_path(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """A real frame through `_parse_message` reaches the summary.

        Asserting against the recording method directly would let both sides
        pass while the two are not wired together at all - deleting the calls
        in `_parse_message` has to break something.
        """
        from custom_components.ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
            Delta3DisplayProperty,
        )
        from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
            encode_field_varint,
        )

        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA3_DEVICE
        )

        msg = Delta3DisplayProperty()
        msg.cms_batt_soc = 82.0
        # 5064 is the AC charge power cap read-back the binding does not
        # declare - the field this whole path exists to find.
        inner = msg.SerializeToString() + encode_field_varint(5064, 1000)

        topic = f"/app/device/property/{MOCK_DELTA3_DEVICE['sn']}"
        parsed = coordinator._parse_message(topic, self._frame(254, 21, inner))

        assert coordinator.unknown_proto_fields == {"254/21": {"5064": 1000}}
        # The private keys must not travel on into the sensor data - one of
        # them holds a dict, and a dict as a sensor state is a broken entity.
        assert parsed is not None
        assert "_unknown_fields" not in parsed
        assert "_cmd_key" not in parsed

    async def test_powerocean_bundle_path_records_too(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """PowerOcean decodes through its own bundle path, which also records."""
        from custom_components.ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
            JTS1EnergyStreamReport,
        )
        from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
            encode_field_varint,
        )

        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_POWEROCEAN_DEVICE
        )

        msg = JTS1EnergyStreamReport()
        msg.bp_soc = 55
        inner = msg.SerializeToString() + encode_field_varint(4242, 7)

        topic = f"/app/device/property/{MOCK_POWEROCEAN_DEVICE['sn']}"
        parsed = coordinator._parse_message(topic, self._frame(96, 33, inner))

        assert coordinator.unknown_proto_fields == {"96/33": {"4242": 7}}
        assert parsed is not None
        assert "_unknown_fields" not in parsed

    async def test_field_count_per_command_is_bounded(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """The summary stops taking new numbers instead of growing forever.

        This is the axis that actually bounds memory: the decoder caps one
        message, but the summary accumulates across every message a device
        sends for as long as the integration is loaded.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA3_DEVICE
        )
        for base in range(0, UNKNOWN_FIELD_NUMBERS_MAX + 100, 10):
            coordinator.record_unknown_proto_fields(
                "254/21", {base + offset: 1 for offset in range(10)}
            )

        commands = coordinator.unknown_proto_fields["254/21"]
        assert len(commands) == UNKNOWN_FIELD_NUMBERS_MAX

    async def test_known_field_still_updates_at_the_cap(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """A full summary keeps tracking the values it already holds.

        Otherwise the "change a setting and dump again" workflow silently
        stops working once the cap is reached.
        """
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA3_DEVICE
        )
        coordinator.record_unknown_proto_fields(
            "254/21", {number: 1 for number in range(UNKNOWN_FIELD_NUMBERS_MAX)}
        )
        coordinator.record_unknown_proto_fields("254/21", {5: 2400, 99999: 1})

        commands = coordinator.unknown_proto_fields["254/21"]
        assert len(commands) == UNKNOWN_FIELD_NUMBERS_MAX
        assert commands["5"] == 2400
        assert "99999" not in commands

    async def test_malformed_mapping_is_ignored(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Capture never costs a message - a bad shape is dropped, not raised."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA3_DEVICE
        )
        coordinator._record_unknown_fields({"_unknown_fields": "nonsense"})
        coordinator._record_unknown_fields({})
        assert coordinator.unknown_proto_fields == {}


class TestEventLogSerialLeak:
    """The third serial leak of the 1.16.0 cycle, and the pass that ends the class.

    The set_reply topic is /open/<cert_account>/<sn>/set_reply, so every
    device write used to put a full serial and the account identifier into
    the event log - a section of a diagnostics download that users are asked
    to attach to public issues, and one that no redaction pass touched.
    """

    async def test_set_reply_event_records_no_topic_identifiers(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """The point fix: the serial never enters the log in the first place."""
        standard_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE
        )
        sn = MOCK_DELTA_DEVICE["sn"]

        coordinator._on_mqtt_message(f"/open/9876543210123456/{sn}/set_reply", b"{}")

        assert coordinator.event_log, "the acknowledgement must still be recorded"
        detail = coordinator.event_log[-1]["detail"]
        assert sn not in detail
        assert "9876543210123456" not in detail

    async def test_diagnostics_redact_an_event_log_serial(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
        mock_iot_api,
        mock_mqtt_client,
        mock_http_client,
    ) -> None:
        """The structural fix: a serial planted anywhere is caught on the way out.

        Written against the section rather than the caller on purpose. The
        point fix above closes the one path that is known today; this one has
        to keep holding for a path somebody adds next year without thinking
        about redaction at all.
        """
        standard_config_entry.add_to_hass(hass)
        with patch(
            "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator.async_config_entry_first_refresh",
            new_callable=AsyncMock,
        ):
            await hass.config_entries.async_setup(standard_config_entry.entry_id)
            await hass.async_block_till_done()

        coordinators = hass.data[DOMAIN][standard_config_entry.entry_id]
        coordinator = next(iter(coordinators.values()))
        sn = MOCK_DELTA_DEVICE["sn"]
        coordinator._log_event("set_reply", f"topic=/open/acct/{sn}/set_reply")

        result = await async_get_config_entry_diagnostics(hass, standard_config_entry)

        assert sn not in json.dumps(result)


class TestRedactionDoesNotEatTheEvidence:
    """The redaction pass must not destroy what diagnostics exist for.

    Running the whole payload through serial redaction looked safe: hex dumps
    are lowercase and the pattern wants uppercase. But hex digits 0-9 are
    inside [A-Z0-9], so any run of fifteen or more hex characters carrying no
    a-f matches it. Against this repo's own fixtures that corrupted 25 of 25
    captured frames - the artefact every device added this year was built
    from, quietly replaced by REDACTED.
    """

    def test_a_captured_frame_survives_the_pass(self) -> None:
        # Long enough to contain digit-only runs, which is what triggered it.
        frame_hex = "0a370a12c7768a219733bf641062af33b754414113301002182020012801"
        payload = {"raw_frames": {"frames": [{"hex": frame_hex, "topic": "property"}]}}

        result = _redact_serials(payload)

        assert result["raw_frames"]["frames"][0]["hex"] == frame_hex

    def test_a_frame_carrying_a_masked_serial_stays_decodable(self) -> None:
        """The guaranteed case, not the statistical one.

        sanitize_frame masks a serial with a run of 0x58 bytes, which is "58"
        per byte in hex: a 16 character serial becomes 32 consecutive digits
        and matches the pattern every single time. Nearly every PowerOcean
        frame carries the serial, so this was not an edge case for them. What
        came out was not even valid hex, and the byte offsets that
        sanitize_frame deliberately preserves were gone with it.
        """
        sn = "HJ31TEST00000001"
        payload = b"\x0a\x30\x12\x10" + sn.encode() + b"\x1a\x08\x08\x01\x10\x02"
        entry = build_frame_entry(
            "/app/device/property/x", payload, [sn], 512, parsed_keys=3
        )
        original = entry["hex"]

        result = _redact_serials({"raw_frames": {"frames": [entry]}})
        kept = result["raw_frames"]["frames"][0]["hex"]

        assert kept == original
        assert len(kept) == len(original)
        bytes.fromhex(kept)  # raises if the pass broke the encoding
        # The serial is gone because capture masked it, not because of this pass.
        assert sn not in json.dumps(result)

    def test_a_serial_beside_a_frame_is_still_redacted(self) -> None:
        """The exemption is for the frame value, not for its neighbours."""
        payload = {
            "raw_frames": {
                "frames": [
                    {"hex": "0a370a12c7768a2197331002182020012801", "sn": "HJ31TEST00000001"}
                ]
            }
        }

        result = _redact_serials(payload)

        assert "HJ31TEST00000001" not in json.dumps(result)
