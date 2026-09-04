"""Tests for PowerOcean number entities - SoC limit SET via Enhanced Mode."""

from __future__ import annotations

import asyncio
import logging
import threading
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    DEVICE_TYPE_POWEROCEAN,
    POWEROCEAN_NUMBERS,
    SCHEDULE_MAX_INDEX,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.number import (
    EcoFlowNumber,
    _get_number_defs,
)

from .conftest import (
    MOCK_POWEROCEAN_DEVICE,
)


# ===========================================================================
# Number definition routing
# ===========================================================================


class TestGetNumberDefs:
    def test_powerocean_returns_powerocean_numbers(self):
        defs = _get_number_defs(DEVICE_TYPE_POWEROCEAN)
        assert defs is POWEROCEAN_NUMBERS

    def test_powerocean_number_keys(self):
        """Two sliders every PowerOcean has, plus one charge power per
        scheduled task slot. The schedule ones are accessory-gated, so a
        device without a schedule never sees them."""
        defs = _get_number_defs(DEVICE_TYPE_POWEROCEAN)
        keys = {d.key for d in defs}

        assert keys == {"backup_reserve", "solar_surplus_threshold"} | {
            f"schedule_{index}_power_w"
            for index in range(1, SCHEDULE_MAX_INDEX + 1)
        }
        assert not any(
            d.accessory for d in defs if not d.key.startswith("schedule_")
        )

    def test_powerocean_numbers_are_enhanced_only(self):
        defs = _get_number_defs(DEVICE_TYPE_POWEROCEAN)
        assert all(d.enhanced_only for d in defs)

    def test_powerocean_backup_reserve_range(self):
        defs = _get_number_defs(DEVICE_TYPE_POWEROCEAN)
        br = next(d for d in defs if d.key == "backup_reserve")
        assert br.min_value == 0
        assert br.max_value == 100
        assert br.step == 5

    def test_powerocean_solar_surplus_range(self):
        defs = _get_number_defs(DEVICE_TYPE_POWEROCEAN)
        ss = next(d for d in defs if d.key == "solar_surplus_threshold")
        assert ss.min_value == 0
        assert ss.max_value == 100
        assert ss.step == 5


# ===========================================================================
# Coordinator async_set_soc_limits
# ===========================================================================


class TestAsyncSetSocLimits:
    async def test_set_soc_limits_success(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Enhanced Mode coordinator sends SoC limits via proto SET."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE,
        )
        assert coordinator.enhanced_mode is True

        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        mock_mqtt.send_proto_set.return_value = True
        coordinator._mqtt_client = mock_mqtt

        result = await coordinator.async_set_soc_limits(100, 10)

        assert result is True
        mock_mqtt.send_proto_set.assert_called_once()
        payload = mock_mqtt.send_proto_set.call_args[0][0]
        assert isinstance(payload, bytes)

    async def test_set_soc_limits_fails_standard_mode(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        """Standard Mode coordinator rejects SoC limit SET."""
        standard_config_entry.add_to_hass(hass)

        from .conftest import MOCK_DELTA_DEVICE
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE,
        )
        assert coordinator.enhanced_mode is False

        result = await coordinator.async_set_soc_limits(100, 10)
        assert result is False

    async def test_set_soc_limits_fails_mqtt_disconnected(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Enhanced Mode with disconnected MQTT rejects SoC limit SET."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE,
        )

        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = False
        coordinator._mqtt_client = mock_mqtt

        result = await coordinator.async_set_soc_limits(100, 10)
        assert result is False

    async def test_set_soc_limits_fails_no_mqtt(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Enhanced Mode with no MQTT client rejects SoC limit SET."""
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE,
        )
        coordinator._mqtt_client = None

        result = await coordinator.async_set_soc_limits(100, 10)
        assert result is False


# ===========================================================================
# Number entity SET value routing
# ===========================================================================


class TestPowerOceanNumberBasic:
    """Basic native_value and failure-mode tests using backup_reserve."""

    def _make_number_entity(
        self, hass, entry,
    ) -> tuple[EcoFlowNumber, EcoFlowDeviceCoordinator]:
        """Create a PowerOcean backup_reserve entity with mocked coordinator."""
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE,
        )
        # Seed device data with current limits. ems_app_surplus_pct is the
        # source the backup_reserve write reads (ADR-011) - without it the
        # write is refused before it ever reaches
        # async_set_powerocean_soc_debounced, which is a different test.
        # A distinctive value (90, not the old missing-key default of 100)
        # makes test_set_failed_no_optimistic_update prove it read this key
        # rather than a default that happened to match it (review F8).
        coordinator._device_data = {
            "ems_charge_upper_limit_pct": 100,
            "ems_discharge_lower_limit_pct": 0,
            "ems_backup_ratio_pct": 100,
            "ems_app_surplus_pct": 90,
        }
        coordinator.async_set_updated_data(dict(coordinator._device_data))

        defn = next(d for d in POWEROCEAN_NUMBERS if d.key == "backup_reserve")
        entity = EcoFlowNumber(coordinator, defn)
        return entity, coordinator

    async def test_set_failed_no_optimistic_update(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Failed SET does not optimistically update coordinator data."""
        entity, coordinator = self._make_number_entity(
            hass, enhanced_config_entry,
        )
        coordinator.async_set_powerocean_soc_debounced = AsyncMock(return_value=False)

        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(50.0)

        coordinator.async_set_powerocean_soc_debounced.assert_called_once_with(50, 90)
        # No optimistic update - original value retained
        assert coordinator.data["ems_discharge_lower_limit_pct"] == 0

    async def test_native_value_reads_state_key(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Number entity reads current value from coordinator data via state_key."""
        entity, coordinator = self._make_number_entity(
            hass, enhanced_config_entry,
        )
        assert entity.native_value == 0.0

    async def test_native_value_none_when_no_data(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Number entity returns None when coordinator has no data."""
        entity, coordinator = self._make_number_entity(
            hass, enhanced_config_entry,
        )
        coordinator.async_set_updated_data(None)
        assert entity.native_value is None


# ===========================================================================
# 3-field PowerOcean SoC SET (verified against live app traffic 2026-05-06)
# ===========================================================================


class TestAsyncSetPowerOceanSoc:
    async def test_3field_set_success(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE,
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        mock_mqtt.send_proto_set.return_value = True
        coordinator._mqtt_client = mock_mqtt

        result = await coordinator.async_set_powerocean_soc(25, 80)

        assert result is True
        mock_mqtt.send_proto_set.assert_called_once()
        payload = mock_mqtt.send_proto_set.call_args[0][0]
        # Inner pdata contains 4 fields: 1=100, 2=25, 3=80, 4=80
        # Field 3 (0x18 tag) is sys_bat_backup_ratio (EMS state).
        # Field 4 (0x20 tag) is dev_soc / socDev (App-UI state, cloud quota).
        # Both must be present so HA, the device EMS, and the EcoFlow app
        # stay synchronized; writing only one desynchronizes them.
        assert b"\x08\x64\x10\x19\x18\x50\x20\x50" in payload

    async def test_3field_set_rejects_backup_above_solar(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE,
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        coordinator._mqtt_client = mock_mqtt

        result = await coordinator.async_set_powerocean_soc(80, 25)
        assert result is False
        mock_mqtt.send_proto_set.assert_not_called()

    async def test_3field_rejects_standard_mode(
        self,
        hass: HomeAssistant,
        standard_config_entry: MockConfigEntry,
    ) -> None:
        standard_config_entry.add_to_hass(hass)
        from .conftest import MOCK_DELTA_DEVICE
        coordinator = EcoFlowDeviceCoordinator(
            hass, standard_config_entry, MOCK_DELTA_DEVICE,
        )
        result = await coordinator.async_set_powerocean_soc(0, 100)
        assert result is False


class TestAsyncSetWorkMode:
    async def test_work_mode_self_use(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE,
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        mock_mqtt.send_proto_set.return_value = True
        coordinator._mqtt_client = mock_mqtt

        result = await coordinator.async_set_powerocean_work_mode(0)

        assert result is True
        payload = mock_mqtt.send_proto_set.call_args[0][0]
        # cmd_id=98 (0x62), inner field 1 = 0
        assert b"\x48\x62" in payload  # cmd_id=98

    async def test_work_mode_ai_schedule(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE,
        )
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        mock_mqtt.send_proto_set.return_value = True
        coordinator._mqtt_client = mock_mqtt

        result = await coordinator.async_set_powerocean_work_mode(12)

        assert result is True
        payload = mock_mqtt.send_proto_set.call_args[0][0]
        # cmd_id=98 (0x62), inner field 1 = 12 (0x0c)
        assert b"\x08\x0c" in payload


class TestPowerOceanNumberSet3Field:
    def _make_entity(self, hass, entry, key: str):
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE,
        )
        coordinator._device_data = {
            "ems_charge_upper_limit_pct": 100,
            "ems_discharge_lower_limit_pct": 30,
            "ems_app_surplus_pct": 80,  # user-side surplus mirror (dev_soc)
            "ems_backup_ratio_pct": 80,  # EMS-side derived value
        }
        coordinator.async_set_updated_data(dict(coordinator._device_data))
        defn = next(d for d in POWEROCEAN_NUMBERS if d.key == key)
        entity = EcoFlowNumber(coordinator, defn)
        return entity, coordinator

    async def test_set_backup_reserve_holds_solar(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Setting backup_reserve sends backup=value, solar=current_solar."""
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "backup_reserve",
        )
        coordinator.async_set_powerocean_soc_debounced = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        await entity.async_set_native_value(50.0)

        coordinator.async_set_powerocean_soc_debounced.assert_called_once_with(50, 80)

    async def test_set_backup_reserve_clamps_solar_when_higher(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """If new backup > current solar, raise solar to backup."""
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "backup_reserve",
        )
        coordinator._device_data["ems_app_surplus_pct"] = 40  # source for backup constraint
        coordinator._device_data["ems_backup_ratio_pct"] = 40  # EMS mirror
        coordinator.async_set_updated_data(dict(coordinator._device_data))
        coordinator.async_set_powerocean_soc_debounced = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        await entity.async_set_native_value(60.0)

        coordinator.async_set_powerocean_soc_debounced.assert_called_once_with(60, 60)

    async def test_set_solar_surplus_holds_backup(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Setting solar_surplus_threshold sends backup=current, solar=value."""
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "solar_surplus_threshold",
        )
        coordinator.async_set_powerocean_soc_debounced = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        await entity.async_set_native_value(90.0)

        coordinator.async_set_powerocean_soc_debounced.assert_called_once_with(30, 90)

    async def test_set_solar_clamps_backup_when_lower(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """If new solar < current backup, lower backup to solar."""
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "solar_surplus_threshold",
        )
        coordinator.async_set_powerocean_soc_debounced = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        # current backup = 30, set solar to 20 -> backup must clamp to 20
        await entity.async_set_native_value(20.0)

        coordinator.async_set_powerocean_soc_debounced.assert_called_once_with(20, 20)

    async def test_set_backup_reserve_rejects_missing_surplus(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """A backup write refuses rather than guessing a pair (ADR-011 addendum).

        coordinator.data holds nothing for ems_app_surplus_pct: neither the
        key nor a prior report exists. Guessing a default here would either
        send the device a value the user never chose or write a placeholder
        back to it, so the write is refused instead.
        """
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "backup_reserve",
        )
        coordinator._device_data = {}
        coordinator.async_set_updated_data({})
        coordinator.async_set_powerocean_soc_debounced = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(50.0)

        coordinator.async_set_powerocean_soc_debounced.assert_not_awaited()

    async def test_set_backup_reserve_rejects_explicit_none_surplus(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """A placeholder-guarded surplus (explicit None) also refuses the write."""
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "backup_reserve",
        )
        coordinator._device_data["ems_app_surplus_pct"] = None
        coordinator.async_set_updated_data(dict(coordinator._device_data))
        coordinator.async_set_powerocean_soc_debounced = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(50.0)

        coordinator.async_set_powerocean_soc_debounced.assert_not_awaited()

    async def test_set_solar_surplus_rejects_missing_backup(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """A solar write refuses rather than guessing a pair (review F6).

        coordinator.data holds nothing for ems_discharge_lower_limit_pct.
        Guessing 0 here would move the discharge lower limit to the value
        that lets the battery discharge fully, unchosen, so the write is
        refused the same way the backup_reserve branch already is.
        """
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "solar_surplus_threshold",
        )
        coordinator._device_data = {}
        coordinator.async_set_updated_data({})
        coordinator.async_set_powerocean_soc_debounced = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(90.0)

        coordinator.async_set_powerocean_soc_debounced.assert_not_awaited()

    async def test_set_solar_surplus_rejects_explicit_none_backup(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """An explicit None discharge lower limit also refuses the write."""
        entity, coordinator = self._make_entity(
            hass, enhanced_config_entry, "solar_surplus_threshold",
        )
        coordinator._device_data["ems_discharge_lower_limit_pct"] = None
        coordinator.async_set_updated_data(dict(coordinator._device_data))
        coordinator.async_set_powerocean_soc_debounced = AsyncMock(return_value=True)
        entity.async_write_ha_state = MagicMock()

        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(90.0)

        coordinator.async_set_powerocean_soc_debounced.assert_not_awaited()


_MISSING = object()  # parametrize marker: pop the key rather than set a value

_SURPLUS_SYNC_CLOCK = "custom_components.ecoflow_energy.coordinator.time.monotonic"


class TestPowerOceanAppSurplusAutoSync:
    """Auto-sync the EMS-side sysBatBackupRatio with the app-side dev_soc.

    The EcoFlow app writes only proto wire field 4 (`dev_soc`) via cmd_id=112,
    so the EMS keeps its previous threshold. The device mirrors the app's
    value back via cmd_id=13 (`EmsParamChangeReport.dev_soc`, surfaced as
    `ems_app_surplus_pct`). When that diverges from `ems_backup_ratio_pct`,
    the coordinator schedules a corrective both-field SET.
    """

    def _make_coordinator(self, hass, entry):
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE,
        )
        coordinator._enhanced_mode = True
        coordinator._device_data = {
            "ems_charge_upper_limit_pct": 100,
            "ems_discharge_lower_limit_pct": 0,
            "ems_backup_ratio_pct": 90,
            "ems_app_surplus_pct": 47,
        }
        # Fresh ParamChange frame in the coordinator's view by default. The
        # auto-sync only acts on frames newer than the last user SET; tests
        # that set _last_user_surplus_set_ts higher must override this too.
        coordinator._last_ems_param_change_ts = 1500.0
        coordinator.async_set_powerocean_soc = AsyncMock(return_value=True)
        return coordinator

    def _make_divergent_coordinator(self, hass, entry, app=13, ems=20):
        """A coordinator parked on a pair the EMS never adopts (#247)."""
        coordinator = self._make_coordinator(hass, entry)
        coordinator._device_data["ems_app_surplus_pct"] = app
        coordinator._device_data["ems_backup_ratio_pct"] = ems
        return coordinator

    async def _run_evaluations(self, hass, coordinator, n, start=2010.0, step=31.0):
        """Run ``n`` evaluations, each timestamped 31s past the previous one.

        A fixed ``return_value`` per evaluation (not a shared counter the
        mock consumes) so the spacing holds regardless of how many
        evaluations are suppressed before reaching the one call site that
        reads the clock. Each evaluation is drained with
        ``async_block_till_done`` because a scheduled write only reaches the
        mocked ``async_set_powerocean_soc`` once its tracked task runs.
        """
        for i in range(n):
            with patch(_SURPLUS_SYNC_CLOCK, return_value=start + i * step):
                coordinator._maybe_schedule_surplus_sync()
            await hass.async_block_till_done()

    async def test_discrepancy_triggers_corrective_set(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=1000.0,
        ):
            coordinator._maybe_schedule_surplus_sync()
            await hass.async_block_till_done()
        coordinator.async_set_powerocean_soc.assert_called_once_with(0, 47)

    async def test_no_sync_when_app_and_ems_equal(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator._device_data["ems_backup_ratio_pct"] = 47
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=1000.0,
        ):
            coordinator._maybe_schedule_surplus_sync()
        coordinator.async_set_powerocean_soc.assert_not_called()

    async def test_no_sync_when_app_value_missing(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator._device_data.pop("ems_app_surplus_pct")
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=1000.0,
        ):
            coordinator._maybe_schedule_surplus_sync()
        coordinator.async_set_powerocean_soc.assert_not_called()

    async def test_throttle_blocks_rapid_resync(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator._last_app_surplus_sync_ts = 1000.0
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=1010.0,  # 10s after last sync, throttle = 30s
        ):
            coordinator._maybe_schedule_surplus_sync()
        coordinator.async_set_powerocean_soc.assert_not_called()

    async def test_throttle_releases_after_interval(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator._last_app_surplus_sync_ts = 1000.0
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=1031.0,
        ):
            coordinator._maybe_schedule_surplus_sync()
            await hass.async_block_till_done()
        coordinator.async_set_powerocean_soc.assert_called_once_with(0, 47)

    async def test_user_grace_suppresses_auto_sync(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator._last_user_surplus_set_ts = 1000.0
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=1002.0,
        ):
            coordinator._maybe_schedule_surplus_sync()
        coordinator.async_set_powerocean_soc.assert_not_called()

    async def test_user_grace_releases_after_interval(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator._last_user_surplus_set_ts = 1000.0
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=1006.0,
        ):
            coordinator._maybe_schedule_surplus_sync()
            await hass.async_block_till_done()
        coordinator.async_set_powerocean_soc.assert_called_once_with(0, 47)

    async def test_no_sync_when_param_change_frame_is_stale(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """If the most recent EmsParamChangeReport arrived BEFORE the user
        pushed a new value in HA, the auto-sync must not fire - the
        ParamChange's dev_soc value is the obsolete app-side mirror that
        the user has already superseded."""
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator._last_user_surplus_set_ts = 2000.0
        coordinator._last_ems_param_change_ts = 1500.0  # frame older than user SET
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=2010.0,  # past user-grace and throttle
        ):
            coordinator._maybe_schedule_surplus_sync()
        coordinator.async_set_powerocean_soc.assert_not_called()

    async def test_sync_when_param_change_arrives_after_user_set(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """A genuine app-side change after the user's HA SET produces a
        fresh ParamChange frame; the auto-sync should pick it up and
        align the EMS to the new app value."""
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator._last_user_surplus_set_ts = 1000.0
        coordinator._last_ems_param_change_ts = 2000.0  # frame newer than user SET
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=2010.0,
        ):
            coordinator._maybe_schedule_surplus_sync()
            await hass.async_block_till_done()
        coordinator.async_set_powerocean_soc.assert_called_once_with(0, 47)

    async def test_non_numeric_app_value_aborts_silently(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """A non-numeric ems_app_surplus_pct aborts the sync without a SET."""
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator._device_data["ems_app_surplus_pct"] = "abc"
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=1000.0,
        ):
            coordinator._maybe_schedule_surplus_sync()
        coordinator.async_set_powerocean_soc.assert_not_called()
        assert not any(
            e["type"] == "surplus_auto_sync" for e in coordinator.event_log
        )

    async def test_non_numeric_ems_value_aborts_silently(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """A non-numeric ems_backup_ratio_pct aborts the sync without a SET."""
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator._device_data["ems_backup_ratio_pct"] = "n/a"
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=1000.0,
        ):
            coordinator._maybe_schedule_surplus_sync()
        coordinator.async_set_powerocean_soc.assert_not_called()

    @pytest.mark.parametrize(
        "backup_value",
        ["junk", _MISSING, None],
        ids=["non-numeric", "missing-key", "explicit-none"],
    )
    async def test_an_unknown_backup_limit_refuses_the_sync(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        backup_value: object,
    ) -> None:
        """ADR-013 decision 5 (ADR-011 decision 4 applied to this consumer).

        Guessing 0 for an unreadable discharge-lower-limit would move it to
        the value that lets the battery discharge fully - a setting nobody
        chose. The write is refused instead, silently, like the other
        guards on an unreadable app or ems value.
        """
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        if backup_value is _MISSING:
            coordinator._device_data.pop("ems_discharge_lower_limit_pct")
        else:
            coordinator._device_data["ems_discharge_lower_limit_pct"] = backup_value
        with patch(_SURPLUS_SYNC_CLOCK, return_value=1000.0):
            coordinator._maybe_schedule_surplus_sync()
        coordinator.async_set_powerocean_soc.assert_not_called()
        assert not any(
            e["type"] == "surplus_auto_sync" for e in coordinator.event_log
        )
        assert coordinator.surplus_auto_sync_diagnostics is None

    async def test_a_pair_the_ems_never_adopts_gets_two_writes_then_silence(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """PLAN-117: Xygen's app=13 ems=20 pair (#247), replayed six times.

        Before the bound this scheduled a write on every evaluation,
        forever. It must stop at two.
        """
        coordinator = self._make_divergent_coordinator(hass, enhanced_config_entry)
        await self._run_evaluations(hass, coordinator, 6)
        assert coordinator.async_set_powerocean_soc.call_count == 2
        coordinator.async_set_powerocean_soc.assert_has_calls(
            [call(0, 13), call(0, 13)]
        )

    async def test_the_stop_is_logged_once_and_reported(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        coordinator = self._make_divergent_coordinator(hass, enhanced_config_entry)
        with caplog.at_level(logging.INFO):
            await self._run_evaluations(hass, coordinator, 6)

        stop_events = [
            e for e in coordinator.event_log
            if e["type"] == "surplus_auto_sync_stopped"
        ]
        assert len(stop_events) == 1
        assert stop_events[0]["detail"] == "app=13 ems=20 writes=2"

        stop_logs = [
            r for r in caplog.records if "no further writes" in r.message
        ]
        assert len(stop_logs) == 1

        assert coordinator.surplus_auto_sync_diagnostics == {
            "app": 13, "ems": 20, "writes": 2, "stopped": True,
        }

    async def test_a_different_ems_value_reopens_the_sync(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """The device moved: a new ems value is new information (2c)."""
        coordinator = self._make_divergent_coordinator(hass, enhanced_config_entry)
        await self._run_evaluations(hass, coordinator, 6)
        coordinator._device_data["ems_backup_ratio_pct"] = 21
        with patch(_SURPLUS_SYNC_CLOCK, return_value=2010.0 + 6 * 31.0):
            coordinator._maybe_schedule_surplus_sync()
        await hass.async_block_till_done()
        assert coordinator.async_set_powerocean_soc.call_count == 3

    async def test_a_changed_app_value_reopens_the_sync(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """A move to the value the EMS already holds clears the record (2b),

        even though that report has converged and writes nothing itself.
        The next divergence is then treated as new intent.
        """
        coordinator = self._make_divergent_coordinator(hass, enhanced_config_entry)
        await self._run_evaluations(hass, coordinator, 6)

        coordinator._device_data["ems_app_surplus_pct"] = 20  # now equals ems
        with patch(_SURPLUS_SYNC_CLOCK, return_value=2010.0 + 6 * 31.0):
            coordinator._maybe_schedule_surplus_sync()
        await hass.async_block_till_done()
        assert coordinator.async_set_powerocean_soc.call_count == 2
        assert coordinator.surplus_auto_sync_diagnostics is None

        coordinator._device_data["ems_app_surplus_pct"] = 13
        with patch(_SURPLUS_SYNC_CLOCK, return_value=2010.0 + 7 * 31.0):
            coordinator._maybe_schedule_surplus_sync()
        await hass.async_block_till_done()
        assert coordinator.async_set_powerocean_soc.call_count == 3

    async def test_an_equal_reading_does_not_reopen_the_sync(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """An EMS that echoes the written value once and reverts (2d) must
        not re-arm the loop at double the rate."""
        coordinator = self._make_divergent_coordinator(hass, enhanced_config_entry)
        await self._run_evaluations(hass, coordinator, 6)

        coordinator._device_data["ems_backup_ratio_pct"] = 13  # equals app
        with patch(_SURPLUS_SYNC_CLOCK, return_value=2010.0 + 6 * 31.0):
            coordinator._maybe_schedule_surplus_sync()
        await hass.async_block_till_done()
        coordinator._device_data["ems_backup_ratio_pct"] = 20  # reverts
        with patch(_SURPLUS_SYNC_CLOCK, return_value=2010.0 + 7 * 31.0):
            coordinator._maybe_schedule_surplus_sync()
        await hass.async_block_till_done()

        assert coordinator.async_set_powerocean_soc.call_count == 2

    async def test_a_user_set_reopens_the_sync(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """A user SET from Home Assistant always clears the record (2a)."""
        coordinator = self._make_divergent_coordinator(hass, enhanced_config_entry)
        await self._run_evaluations(hass, coordinator, 6)

        t = 2010.0 + 6 * 31.0
        with patch(_SURPLUS_SYNC_CLOCK, return_value=t):
            coordinator.mark_user_surplus_set()
        coordinator._last_ems_param_change_ts = t + 1
        with patch(_SURPLUS_SYNC_CLOCK, return_value=t + 6):  # past the 5s grace
            coordinator._maybe_schedule_surplus_sync()
        await hass.async_block_till_done()

        assert coordinator.async_set_powerocean_soc.call_count == 3

    async def test_a_refused_schedule_does_not_count(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """A shutdown-refused schedule must not advance the write count."""
        coordinator = self._make_divergent_coordinator(hass, enhanced_config_entry)
        with (
            patch.object(coordinator, "_schedule_powerocean_soc_write", return_value=None),
            patch(_SURPLUS_SYNC_CLOCK, return_value=2010.0),
        ):
            coordinator._maybe_schedule_surplus_sync()
        assert coordinator.surplus_auto_sync_diagnostics is None

    async def test_apply_data_updates_param_change_ts(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Receiving a new EmsParamChangeReport with `ems_app_surplus_pct`
        must record the current monotonic time so the auto-sync can
        recognise the frame as fresh."""
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator._last_ems_param_change_ts = 0.0
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=3000.0,
        ):
            coordinator._apply_data({"ems_app_surplus_pct": 47})
        assert coordinator._last_ems_param_change_ts == 3000.0

    @pytest.mark.parametrize("app_value,ems_value", [(100, 90), (0, 10)])
    async def test_a_boundary_value_gets_two_writes_then_silence(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        app_value: int,
        ems_value: int,
    ) -> None:
        """The (0, 100) special case is gone (ADR-013 decision 4).

        At app_int == 100 the EMS clamps sys_bat_backup_ratio to ~90 by
        design; at app_int == 0 it diverges the same way. Neither is
        special-cased any more - the general bound covers it: two writes,
        then silence, exactly like any other pair the device never adopts.
        """
        coordinator = self._make_divergent_coordinator(
            hass, enhanced_config_entry, app=app_value, ems=ems_value,
        )
        coordinator._last_user_surplus_set_ts = 1000.0
        coordinator._last_ems_param_change_ts = 2000.0
        await self._run_evaluations(hass, coordinator, 6)
        assert coordinator.async_set_powerocean_soc.call_count == 2
        coordinator.async_set_powerocean_soc.assert_has_calls(
            [call(0, app_value), call(0, app_value)]
        )

    async def test_apply_data_does_not_touch_param_change_ts_for_other_fields(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Frames that do not carry `ems_app_surplus_pct` (e.g. a regular
        EmsChangeReport for sysBatBackupRatio) must leave the timestamp
        alone - only the ParamChange path proves the app-side value is
        fresh."""
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator._last_ems_param_change_ts = 1234.0
        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=3000.0,
        ):
            coordinator._apply_data({"ems_backup_ratio_pct": 80})
        assert coordinator._last_ems_param_change_ts == 1234.0

    async def test_user_set_records_timestamp_via_number(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        # When the user pushes a value via the surplus-threshold number entity,
        # the coordinator's `_last_user_surplus_set_ts` is updated so the
        # next auto-sync waits for the device echo before firing.
        enhanced_config_entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, enhanced_config_entry, MOCK_POWEROCEAN_DEVICE,
        )
        coordinator._device_data = {
            "ems_charge_upper_limit_pct": 100,
            "ems_discharge_lower_limit_pct": 0,
            "ems_backup_ratio_pct": 90,
        }
        coordinator.async_set_updated_data(dict(coordinator._device_data))
        coordinator.async_set_powerocean_soc_debounced = AsyncMock(return_value=True)
        defn = next(d for d in POWEROCEAN_NUMBERS if d.key == "solar_surplus_threshold")
        entity = EcoFlowNumber(coordinator, defn)
        entity.async_write_ha_state = MagicMock()
        with patch(
            "custom_components.ecoflow_energy.number.time.monotonic",
            return_value=2000.0,
        ):
            await entity.async_set_native_value(50.0)
        assert coordinator._last_user_surplus_set_ts == 2000.0


class TestPowerOceanSocSetDebounce:
    """Coalesce slider-drag SETs into one frame.

    HA's Number-Entity emits one async_set_native_value call per 5%-step
    while the user drags the slider. The device cannot keep wire field 3
    (EMS) and field 4 (App-Layer) in sync at that cadence, so the two
    fields desync. The debouncer collects calls inside the configured
    window and forwards only the most recent (backup, solar) pair.
    """

    def _make_coordinator(self, hass, entry):
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE,
        )
        coordinator._enhanced_mode = True
        coordinator.async_set_powerocean_soc = AsyncMock(return_value=True)
        return coordinator

    async def test_single_call_schedules_set(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        ok = await coordinator.async_set_powerocean_soc_debounced(50, 80)
        assert ok is True
        # Pending state recorded
        assert coordinator._powerocean_soc_pending == (50, 80)
        # Underlying SET not yet called - it is debounced
        coordinator.async_set_powerocean_soc.assert_not_called()
        # Timer still armed; flush to keep cleanup tidy
        assert coordinator._powerocean_soc_debounce_unsub is not None
        await coordinator._flush_powerocean_soc()

    async def test_drag_burst_only_sends_last_value(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        # Simulate a slider drag: 5 SETs in quick succession
        for solar in (60, 70, 80, 90, 100):
            ok = await coordinator.async_set_powerocean_soc_debounced(0, solar)
            assert ok is True
        # Only the last (backup, solar) is pending
        assert coordinator._powerocean_soc_pending == (0, 100)
        # No underlying SET yet
        coordinator.async_set_powerocean_soc.assert_not_called()
        # Manually flush to skip the timer
        await coordinator._flush_powerocean_soc()
        coordinator.async_set_powerocean_soc.assert_called_once_with(0, 100)

    async def test_flush_clears_pending(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        await coordinator.async_set_powerocean_soc_debounced(0, 75)
        await coordinator._flush_powerocean_soc()
        assert coordinator._powerocean_soc_pending is None

    async def test_rejects_invalid_constraint(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        # backup > solar must be rejected without scheduling anything
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        ok = await coordinator.async_set_powerocean_soc_debounced(80, 25)
        assert ok is False
        assert coordinator._powerocean_soc_pending is None

    async def test_rejects_outside_enhanced_mode(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator._enhanced_mode = False
        ok = await coordinator.async_set_powerocean_soc_debounced(0, 50)
        assert ok is False
        assert coordinator._powerocean_soc_pending is None

    async def test_flush_with_no_pending_is_noop(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
    ) -> None:
        # If the timer fires after some other path cleared the pending value,
        # the flush must not call the underlying SET with stale or empty data.
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        await coordinator._flush_powerocean_soc()
        coordinator.async_set_powerocean_soc.assert_not_called()


class TestPowerOceanSocFlushLifecycle:
    """Keep debounced SET work inside the coordinator unload boundary."""

    def _make_coordinator(self, hass, entry):
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(
            hass, entry, MOCK_POWEROCEAN_DEVICE
        )
        coordinator._enhanced_mode = True
        return coordinator

    @staticmethod
    def _fire_current_timer(coordinator) -> None:
        handle = coordinator._powerocean_soc_debounce_unsub
        assert handle is not None
        # We invoke the callback deterministically instead of waiting for the
        # real debounce delay. Cancelling keeps the loop from invoking it a
        # second time later in the test.
        handle.cancel()
        coordinator._powerocean_soc_debounce_fired(handle)

    async def test_shutdown_waits_for_executor_send_before_disconnect(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The HA test fixture normally executes executor jobs inline. Restore
        # the real contract for this test so the broker call keeps running in
        # a worker thread while shutdown progresses on the event loop.
        monkeypatch.setattr(
            hass,
            "async_add_executor_job",
            lambda target, *args: hass.loop.run_in_executor(
                None, target, *args
            ),
        )
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        caplog.set_level(logging.DEBUG)
        send_started = threading.Event()
        release_send = threading.Event()
        sequence: list[str] = []
        mqtt = MagicMock()
        mqtt.is_connected.return_value = True

        def _send(*_args, **_kwargs) -> bool:
            send_started.set()
            release_send.wait()
            sequence.append("send_return")
            return False

        mqtt.send_proto_set.side_effect = _send
        mqtt.disconnect.side_effect = lambda: sequence.append("disconnect")
        coordinator._mqtt_client = mqtt
        coordinator._device_data.update(
            ems_discharge_lower_limit_pct=20,
            ems_app_surplus_pct=80,
        )
        coordinator.data = dict(coordinator._device_data)
        coordinator.async_update_listeners = MagicMock()

        assert await coordinator.async_set_powerocean_soc_debounced(50, 90)
        self._fire_current_timer(coordinator)
        async with asyncio.timeout(1):
            while not send_started.is_set():
                await asyncio.sleep(0)

        first_caller = hass.async_create_task(
            coordinator.async_shutdown(), eager_start=False
        )
        second_caller: asyncio.Task[None] | None = None
        try:
            await asyncio.sleep(0)
            assert coordinator._shutdown is True
            shutdown_state = (
                coordinator._powerocean_soc_generation,
                coordinator._powerocean_soc_cycle_open,
                dict(coordinator._powerocean_soc_before),
                coordinator._powerocean_soc_rollback_generation,
                dict(coordinator._device_data),
                list(coordinator.event_log),
            )
            first_caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_caller
            assert coordinator._shutdown_task is not None
            assert not coordinator._shutdown_task.done()
            mqtt.disconnect.assert_not_called()

            second_caller = hass.async_create_task(
                coordinator.async_shutdown(), eager_start=False
            )
            await asyncio.sleep(0)
            assert not second_caller.done()
        finally:
            release_send.set()
            if second_caller is not None:
                await second_caller
            else:
                cleanup = coordinator._shutdown_task
                assert cleanup is not None
                await asyncio.shield(cleanup)

        assert sequence == ["send_return", "disconnect"]
        assert (
            coordinator._powerocean_soc_generation,
            coordinator._powerocean_soc_cycle_open,
            dict(coordinator._powerocean_soc_before),
            coordinator._powerocean_soc_rollback_generation,
            dict(coordinator._device_data),
            list(coordinator.event_log),
        ) == shutdown_state
        coordinator.async_update_listeners.assert_not_called()
        assert not any(
            "PowerOcean SoC sent" in record.message
            or "PowerOcean SoC SET failed" in record.message
            for record in caplog.records
        )

    async def test_shutdown_waits_for_blocked_surplus_auto_sync(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            hass,
            "async_add_executor_job",
            lambda target, *args: hass.loop.run_in_executor(
                None, target, *args
            ),
        )
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        caplog.set_level(logging.DEBUG)
        coordinator._device_data.update(
            ems_discharge_lower_limit_pct=20,
            ems_backup_ratio_pct=80,
            ems_app_surplus_pct=70,
        )
        coordinator.data = dict(coordinator._device_data)
        coordinator._last_ems_param_change_ts = 900.0
        coordinator.async_update_listeners = MagicMock()
        send_started = threading.Event()
        release_send = threading.Event()
        sequence: list[str] = []
        mqtt = MagicMock()
        mqtt.is_connected.return_value = True

        def _send(*_args, **_kwargs) -> bool:
            send_started.set()
            release_send.wait()
            sequence.append("send_return")
            return False

        mqtt.send_proto_set.side_effect = _send
        mqtt.disconnect.side_effect = lambda: sequence.append("disconnect")
        coordinator._mqtt_client = mqtt

        with patch(
            "custom_components.ecoflow_energy.coordinator.time.monotonic",
            return_value=1000.0,
        ):
            coordinator._maybe_schedule_surplus_sync()
        async with asyncio.timeout(1):
            while not send_started.is_set():
                await asyncio.sleep(0)
        # The coordinator owns both the auto-sync coroutine (start boundary)
        # and its shielded executor future (actual broker-write boundary).
        assert len(coordinator._powerocean_soc_write_tasks) == 2

        shutdown = hass.async_create_task(
            coordinator.async_shutdown(), eager_start=False
        )
        try:
            await asyncio.sleep(0)
            assert coordinator._shutdown is True
            after_shutdown = (
                dict(coordinator._device_data),
                dict(coordinator.data),
                list(coordinator.event_log),
                coordinator._powerocean_soc_rollback_generation,
            )
            assert not shutdown.done()
            mqtt.disconnect.assert_not_called()
        finally:
            release_send.set()
            await shutdown

        assert sequence == ["send_return", "disconnect"]
        assert (
            dict(coordinator._device_data),
            dict(coordinator.data),
            list(coordinator.event_log),
            coordinator._powerocean_soc_rollback_generation,
        ) == after_shutdown
        assert not coordinator._powerocean_soc_write_tasks
        coordinator.async_update_listeners.assert_not_called()
        assert not any(
            "PowerOcean SoC sent" in record.message
            or "PowerOcean SoC SET failed" in record.message
            for record in caplog.records
        )

    async def test_shutdown_awaits_two_concurrent_flush_tasks(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        started = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def _send(_backup: int, _solar: int) -> bool:
            nonlocal calls
            index = calls
            calls += 1
            started[index].set()
            await release[index].wait()
            return True

        coordinator.async_set_powerocean_soc = AsyncMock(side_effect=_send)
        mqtt = MagicMock()
        coordinator._mqtt_client = mqtt

        assert await coordinator.async_set_powerocean_soc_debounced(20, 80)
        self._fire_current_timer(coordinator)
        await started[0].wait()
        assert await coordinator.async_set_powerocean_soc_debounced(30, 90)
        self._fire_current_timer(coordinator)
        await started[1].wait()
        assert len(coordinator._powerocean_soc_flush_tasks) == 2

        shutdown = asyncio.create_task(coordinator.async_shutdown())
        await asyncio.sleep(0)
        release[0].set()
        await asyncio.sleep(0)
        assert not shutdown.done()
        mqtt.disconnect.assert_not_called()

        release[1].set()
        await shutdown
        mqtt.disconnect.assert_called_once_with()
        assert not coordinator._powerocean_soc_flush_tasks

    async def test_stale_callback_cannot_claim_newer_handle_or_pending(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator.async_set_powerocean_soc = AsyncMock(return_value=True)

        assert await coordinator.async_set_powerocean_soc_debounced(20, 80)
        stale_handle = coordinator._powerocean_soc_debounce_unsub
        assert stale_handle is not None
        assert await coordinator.async_set_powerocean_soc_debounced(30, 90)
        current_handle = coordinator._powerocean_soc_debounce_unsub

        coordinator._powerocean_soc_debounce_fired(stale_handle)

        assert coordinator._powerocean_soc_debounce_unsub is current_handle
        assert coordinator._powerocean_soc_pending == (30, 90)
        assert not coordinator._powerocean_soc_flush_tasks
        coordinator.async_set_powerocean_soc.assert_not_called()
        await coordinator.async_shutdown()

    async def test_pending_timer_shutdown_and_second_shutdown_are_idempotent(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator.async_set_powerocean_soc = AsyncMock(return_value=True)
        mqtt = MagicMock()
        coordinator._mqtt_client = mqtt

        assert await coordinator.async_set_powerocean_soc_debounced(20, 80)
        handle = coordinator._powerocean_soc_debounce_unsub
        assert handle is not None

        await coordinator.async_shutdown()
        await coordinator.async_shutdown()
        coordinator._powerocean_soc_debounce_fired(handle)

        assert handle.cancelled()
        assert coordinator._powerocean_soc_debounce_unsub is None
        assert coordinator._powerocean_soc_pending is None
        assert coordinator._powerocean_soc_cycle_open is False
        assert coordinator._powerocean_soc_before == {}
        assert not coordinator._powerocean_soc_flush_tasks
        assert coordinator._shutdown_complete.is_set()
        coordinator.async_set_powerocean_soc.assert_not_called()
        mqtt.disconnect.assert_called_once_with()
        assert not await coordinator.async_set_powerocean_soc_debounced(20, 80)

    @pytest.mark.parametrize("failure_stage", ["disconnect", "force_flush", "super"])
    async def test_shutdown_failure_is_shared_by_all_callers(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        failure_stage: str,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        error = RuntimeError(f"{failure_stage} failed")
        mqtt = MagicMock()
        if failure_stage == "disconnect":
            mqtt.disconnect.side_effect = error
        coordinator._mqtt_client = mqtt
        force_flush = MagicMock(
            side_effect=error if failure_stage == "force_flush" else None
        )
        coordinator._energy_integrator.force_flush = force_flush
        base_shutdown = AsyncMock(
            side_effect=error if failure_stage == "super" else None
        )

        with patch(
            "homeassistant.helpers.update_coordinator."
            "DataUpdateCoordinator.async_shutdown",
            base_shutdown,
        ):
            first = hass.async_create_task(
                coordinator.async_shutdown(), eager_start=False
            )
            second = hass.async_create_task(
                coordinator.async_shutdown(), eager_start=False
            )
            outcomes = await asyncio.gather(
                first, second, return_exceptions=True
            )

            assert outcomes == [error, error]
            assert coordinator._shutdown_task is not None
            assert coordinator._shutdown_task.done()
            assert not coordinator._shutdown_complete.is_set()
            with pytest.raises(RuntimeError) as later:
                await coordinator.async_shutdown()
            assert later.value is error

        mqtt.disconnect.assert_called_once_with()
        force_flush.assert_called_once_with()
        base_shutdown.assert_awaited_once_with()

    async def test_shutdown_collects_task_and_all_stage_failures(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        task_error = ValueError("write failed")
        disconnect_error = RuntimeError("disconnect failed")
        flush_error = OSError("flush failed")
        super_error = LookupError("super failed")

        async def _failed_write() -> None:
            raise task_error

        write = hass.async_create_task(_failed_write(), eager_start=False)
        coordinator._powerocean_soc_write_tasks.add(write)
        mqtt = MagicMock()
        mqtt.disconnect.side_effect = disconnect_error
        coordinator._mqtt_client = mqtt
        force_flush = MagicMock(side_effect=flush_error)
        coordinator._energy_integrator.force_flush = force_flush
        base_shutdown = AsyncMock(side_effect=super_error)

        with patch(
            "homeassistant.helpers.update_coordinator."
            "DataUpdateCoordinator.async_shutdown",
            base_shutdown,
        ):
            outcomes = await asyncio.gather(
                coordinator.async_shutdown(),
                coordinator.async_shutdown(),
                return_exceptions=True,
            )

        assert outcomes == [task_error, task_error]
        mqtt.disconnect.assert_called_once_with()
        force_flush.assert_called_once_with()
        base_shutdown.assert_awaited_once_with()

    async def test_shutdown_propagates_cancelled_task_after_all_stages(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)

        async def _cancelled_write() -> None:
            raise asyncio.CancelledError

        write = hass.async_create_task(_cancelled_write(), eager_start=False)
        coordinator._powerocean_soc_write_tasks.add(write)
        mqtt = MagicMock()
        coordinator._mqtt_client = mqtt
        force_flush = MagicMock()
        coordinator._energy_integrator.force_flush = force_flush
        base_shutdown = AsyncMock()

        with patch(
            "homeassistant.helpers.update_coordinator."
            "DataUpdateCoordinator.async_shutdown",
            base_shutdown,
        ):
            first = hass.async_create_task(
                coordinator.async_shutdown(), eager_start=False
            )
            second = hass.async_create_task(
                coordinator.async_shutdown(), eager_start=False
            )
            outcomes = await asyncio.gather(
                first, second, return_exceptions=True
            )

        assert all(isinstance(result, asyncio.CancelledError) for result in outcomes)
        mqtt.disconnect.assert_called_once_with()
        force_flush.assert_called_once_with()
        base_shutdown.assert_awaited_once_with()


class TestPowerOceanSocRollback:
    """The rollback that shipped in v1.16.0-beta.10 and did nothing.

    The flush took its "value before the write" snapshot from _device_data,
    0.3 s after the entity had already written its optimistic value there.
    Restoring that snapshot restored the value the device had refused, which
    is the exact #185 symptom the change was meant to end. No test caught it
    because every flush test mocked the SET to succeed.
    """

    def _make_coordinator(self, hass, entry, *, set_ok: bool):
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, MOCK_POWEROCEAN_DEVICE)
        coordinator._enhanced_mode = True
        coordinator.async_set_powerocean_soc = AsyncMock(return_value=set_ok)
        # The device reported these before the user touched anything.
        coordinator._device_data["ems_discharge_lower_limit_pct"] = 20
        coordinator._device_data["ems_app_surplus_pct"] = 80
        coordinator.data = dict(coordinator._device_data)
        return coordinator

    async def _drag(self, coordinator, backup: int, solar: int) -> None:
        """Request a value the way the entity does, optimistic write included."""
        assert await coordinator.async_set_powerocean_soc_debounced(backup, solar)
        for key, value in (
            ("ems_discharge_lower_limit_pct", backup),
            ("ems_app_surplus_pct", solar),
        ):
            coordinator.set_device_value(key, value)
            coordinator.data[key] = value

    async def test_a_failed_write_restores_the_device_value(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry, set_ok=False)

        await self._drag(coordinator, 50, 90)
        await coordinator._flush_powerocean_soc()

        assert coordinator._device_data["ems_discharge_lower_limit_pct"] == 20
        assert coordinator._device_data["ems_app_surplus_pct"] == 80

    async def test_a_drag_burst_rolls_back_to_the_value_before_the_drag(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Every step of a drag writes optimistically; only the first is real."""
        coordinator = self._make_coordinator(hass, enhanced_config_entry, set_ok=False)

        for solar in (85, 90, 95, 100):
            await self._drag(coordinator, 20, solar)
        await coordinator._flush_powerocean_soc()

        assert coordinator._device_data["ems_app_surplus_pct"] == 80

    async def test_a_successful_write_is_not_rolled_back(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry, set_ok=True)

        await self._drag(coordinator, 50, 90)
        await coordinator._flush_powerocean_soc()

        assert coordinator._device_data["ems_discharge_lower_limit_pct"] == 50
        assert coordinator._device_data["ems_app_surplus_pct"] == 90

    async def test_an_older_failure_cannot_undo_a_newer_pending_request(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """An older failure must leave a newer optimistic request untouched.

        The cycle remains open until both in-flight writes and the newer
        pending request settle. This regression also owns and cleans up the
        third debounce timer that originally made the test flaky.
        """
        coordinator = self._make_coordinator(hass, enhanced_config_entry, set_ok=False)
        release_first = asyncio.Event()
        writes: list[tuple[int, int]] = []

        async def _first_hangs_and_fails(backup: int, solar: int) -> bool:
            writes.append((backup, solar))
            if len(writes) == 1:
                await release_first.wait()
                return False
            return True

        coordinator.async_set_powerocean_soc = AsyncMock(
            side_effect=_first_hangs_and_fails
        )

        stale: asyncio.Task[None] | None = None
        try:
            await self._drag(coordinator, 50, 90)
            stale = asyncio.create_task(coordinator._flush_powerocean_soc())
            await asyncio.sleep(0)  # let it reach the hanging write

            # A second write succeeds, but the older publish is unresolved so
            # the cycle deliberately remains open.
            await self._drag(coordinator, 30, 70)
            await coordinator._flush_powerocean_soc()

            # A still-newer request applies its own optimistic value.
            await self._drag(coordinator, 10, 20)

            release_first.set()
            await stale

            assert coordinator._device_data["ems_app_surplus_pct"] == 20
            assert coordinator._device_data["ems_discharge_lower_limit_pct"] == 10
        finally:
            release_first.set()
            if stale is not None:
                await stale
            await coordinator.async_shutdown()

    async def test_rollback_releases_the_optimistic_lock(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Without this the slider shows the refused value for a further 5 s."""
        coordinator = self._make_coordinator(hass, enhanced_config_entry, set_ok=False)
        before = coordinator._powerocean_soc_rollback_generation

        await self._drag(coordinator, 50, 90)
        await coordinator._flush_powerocean_soc()

        assert coordinator._powerocean_soc_rollback_generation > before


class TestPowerOceanSocRollbackUnderOverlap:
    """The hole the first rollback fix still had, found by adversarial review.

    A cycle was treated as new whenever no pending value existed - but the
    flush clears the pending value when it starts, not when its write comes
    back. With a five second publish timeout, a user whose slider does not
    respond drags again inside exactly that gap, and the second window then
    snapshotted the optimistic values of the write that was still failing.
    Rolling back restored the value the device had refused: the #185 symptom
    for the second time, in the fix meant to end it.
    """

    def _make_coordinator(self, hass, entry):
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, MOCK_POWEROCEAN_DEVICE)
        coordinator._enhanced_mode = True
        coordinator._device_data["ems_discharge_lower_limit_pct"] = 20
        coordinator._device_data["ems_app_surplus_pct"] = 80
        coordinator.data = dict(coordinator._device_data)
        return coordinator

    async def _drag(self, coordinator, backup: int, solar: int) -> None:
        assert await coordinator.async_set_powerocean_soc_debounced(backup, solar)
        for key, value in (
            ("ems_discharge_lower_limit_pct", backup),
            ("ems_app_surplus_pct", solar),
        ):
            coordinator.set_device_value(key, value)
            coordinator.data[key] = value

    @pytest.mark.parametrize("older_succeeded", [False, True])
    @pytest.mark.parametrize("latest_succeeded", [False, True])
    @pytest.mark.parametrize("completion_order", [(0, 1), (1, 0)])
    async def test_two_request_outcome_and_completion_matrix(
        self,
        hass: HomeAssistant,
        enhanced_config_entry: MockConfigEntry,
        older_succeeded: bool,
        latest_succeeded: bool,
        completion_order: tuple[int, int],
    ) -> None:
        """All eight result/order combinations resolve from settled writes."""
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator.async_update_listeners = MagicMock()
        started = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        results = [older_succeeded, latest_succeeded]
        calls = 0

        async def _ordered_send(_backup: int, _solar: int) -> bool:
            nonlocal calls
            index = calls
            calls += 1
            started[index].set()
            await release[index].wait()
            return results[index]

        coordinator.async_set_powerocean_soc = AsyncMock(
            side_effect=_ordered_send
        )
        tasks: list[asyncio.Task[None]] = []
        try:
            await self._drag(coordinator, 50, 90)
            tasks.append(asyncio.create_task(coordinator._flush_powerocean_soc()))
            await started[0].wait()

            await self._drag(coordinator, 30, 70)
            tasks.append(asyncio.create_task(coordinator._flush_powerocean_soc()))
            await started[1].wait()

            first, second = completion_order
            release[first].set()
            await tasks[first]

            # A partial result never closes or rolls back the cycle. This is
            # especially important when the latest failure arrives first and
            # the older publish later becomes the confirmed device value.
            assert coordinator._powerocean_soc_cycle_open is True
            assert coordinator._device_data["ems_discharge_lower_limit_pct"] == 30
            assert coordinator._device_data["ems_app_surplus_pct"] == 70
            coordinator.async_update_listeners.assert_not_called()

            release[second].set()
            await tasks[second]

            if latest_succeeded:
                expected = (30, 70)
                expected_revision = 2
            elif older_succeeded:
                expected = (50, 90)
                expected_revision = 1
            else:
                expected = (20, 80)
                expected_revision = 0
            assert (
                coordinator._device_data["ems_discharge_lower_limit_pct"],
                coordinator._device_data["ems_app_surplus_pct"],
            ) == expected
            assert coordinator._powerocean_soc_confirmed_revision == expected_revision
            assert coordinator._powerocean_soc_cycle_open is False
            assert not coordinator._powerocean_soc_active_revisions
            if latest_succeeded:
                coordinator.async_update_listeners.assert_not_called()
            else:
                coordinator.async_update_listeners.assert_called_once_with()
        finally:
            for event in release:
                event.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await coordinator.async_shutdown()

    async def test_late_older_success_only_advances_pending_request_baseline(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        started = asyncio.Event()
        release = asyncio.Event()

        async def _older_send(_backup: int, _solar: int) -> bool:
            started.set()
            await release.wait()
            return True

        coordinator.async_set_powerocean_soc = AsyncMock(side_effect=_older_send)
        older: asyncio.Task[None] | None = None
        try:
            await self._drag(coordinator, 50, 90)
            older = asyncio.create_task(coordinator._flush_powerocean_soc())
            await started.wait()

            # The newer request is still optimistic and waiting on its timer.
            await self._drag(coordinator, 30, 70)
            release.set()
            await older

            assert coordinator._powerocean_soc_before == {
                "ems_discharge_lower_limit_pct": 50,
                "ems_app_surplus_pct": 90,
            }
            assert coordinator._powerocean_soc_confirmed_revision == 1
            assert coordinator._powerocean_soc_cycle_open is True
            assert coordinator._powerocean_soc_pending == (30, 70)
            assert coordinator._device_data["ems_discharge_lower_limit_pct"] == 30
            assert coordinator._device_data["ems_app_surplus_pct"] == 70
        finally:
            release.set()
            if older is not None:
                await older
            await coordinator.async_shutdown()

    async def test_two_failing_writes_still_land_on_the_device_value(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        release = asyncio.Event()

        async def _first_hangs_then_fails(backup: int, solar: int) -> bool:
            await release.wait()
            return False

        coordinator.async_set_powerocean_soc = AsyncMock(
            side_effect=_first_hangs_then_fails
        )

        await self._drag(coordinator, 50, 90)
        first = asyncio.create_task(coordinator._flush_powerocean_soc())
        await asyncio.sleep(0)

        # The slider has not moved, so the user drags again while the first
        # write is still hanging.
        await self._drag(coordinator, 30, 70)

        release.set()
        await first
        await coordinator._flush_powerocean_soc()

        assert coordinator._device_data["ems_discharge_lower_limit_pct"] == 20
        assert coordinator._device_data["ems_app_surplus_pct"] == 80

    async def test_a_failure_after_a_success_falls_back_to_what_was_written(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """The device holds the value of the successful write, not the old one."""
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator.async_set_powerocean_soc = AsyncMock(return_value=True)

        await self._drag(coordinator, 50, 90)
        await coordinator._flush_powerocean_soc()

        coordinator.async_set_powerocean_soc = AsyncMock(return_value=False)
        await self._drag(coordinator, 30, 70)
        await coordinator._flush_powerocean_soc()

        assert coordinator._device_data["ems_discharge_lower_limit_pct"] == 50
        assert coordinator._device_data["ems_app_surplus_pct"] == 90

    async def test_a_failure_before_the_device_ever_reported_clears_the_value(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        """Nothing to fall back to means unknown, not the refused value."""
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator._device_data.clear()
        coordinator.data = {}
        coordinator.async_set_powerocean_soc = AsyncMock(return_value=False)

        await self._drag(coordinator, 50, 90)
        await coordinator._flush_powerocean_soc()

        assert "ems_discharge_lower_limit_pct" not in coordinator._device_data
        assert "ems_app_surplus_pct" not in coordinator._device_data

    async def test_a_raising_write_still_rolls_back(
        self, hass: HomeAssistant, enhanced_config_entry: MockConfigEntry,
    ) -> None:
        coordinator = self._make_coordinator(hass, enhanced_config_entry)
        coordinator.async_set_powerocean_soc = AsyncMock(
            side_effect=RuntimeError("connection torn down")
        )

        await self._drag(coordinator, 50, 90)
        await coordinator._flush_powerocean_soc()

        assert coordinator._device_data["ems_app_surplus_pct"] == 80
