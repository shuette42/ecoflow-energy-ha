"""Tests for the Riemann sum energy integrator."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ecoflow_energy.ecoflow.energy_integrator import EnergyIntegrator


@pytest.fixture
def state_file(tmp_path):
    return str(tmp_path / "energy_state.json")


@pytest.fixture
def integrator(state_file):
    return EnergyIntegrator(state_file)


class TestStateSnapshot:
    """The read-only view diagnostics reports from."""

    def test_snapshot_reports_the_running_state_not_the_file(self, integrator, state_file):
        """Nothing has been flushed, so the file does not exist yet."""
        integrator._state["solar"] = (5.0, 1000.0, 250.0)
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=1030.0,
        ):
            integrator.integrate("solar", 250.0)

        snapshot = integrator.state_snapshot()

        assert not Path(state_file).exists()
        assert snapshot["solar"][0] > 5.0
        assert snapshot["solar"][1] == 1030.0
        assert snapshot["solar"][2] == 250.0

    def test_snapshot_includes_a_metric_that_never_produced_a_total(self, integrator):
        """A seeded-once metric is the case a diagnostics reader is after."""
        assert integrator.integrate("solar", 400.0) is None

        snapshot = integrator.state_snapshot()

        assert snapshot["solar"][0] == 0.0
        assert snapshot["solar"][2] == 400.0

    def test_snapshot_is_a_copy(self, integrator):
        """A reader must not be able to disturb the running totals."""
        integrator._state["solar"] = (5.0, 1000.0, 250.0)

        snapshot = integrator.state_snapshot()
        snapshot["solar"] = (999.0, 0.0, 0.0)
        snapshot["invented"] = (1.0, 0.0, 0.0)

        assert integrator.get_total("solar") == 5.0
        assert "invented" not in integrator._state

    def test_snapshot_does_not_read_from_disk(self, integrator, state_file):
        """Diagnostics runs on the event loop - it must not touch the file."""
        Path(state_file).write_text(json.dumps({"solar": [999.0, 1.0, 1.0]}))

        assert integrator.state_snapshot() == {}


class TestBasicIntegration:
    def test_first_call_reports_no_total(self, integrator):
        """First reading seeds state and reports no total.

        It must not report 0.0: the caller would publish that as the sensor
        value, and on a total_increasing sensor a zero reads as a meter reset.
        """
        result = integrator.integrate("solar", 1000.0)
        assert result is None
        # State is seeded even though nothing is reported.
        assert integrator._state["solar"][0] == 0.0

    def test_second_call_integrates(self, integrator):
        """Second call after time delta produces energy."""
        integrator.integrate("solar", 1000.0)
        # Simulate time passing
        integrator._state["solar"] = (0.0, time.monotonic() - 30, 1000.0)
        result = integrator.integrate("solar", 1000.0)
        # 1000W * 30s / 3600 / 1000 = 0.00833 kWh
        assert result is not None
        assert result == pytest.approx(0.00833, abs=0.001)

    def test_zero_power_no_increase(self, integrator):
        """Zero power should not increase the total."""
        integrator.integrate("solar", 0.0)
        integrator._state["solar"] = (0.0, time.monotonic() - 30, 0.0)
        result = integrator.integrate("solar", 0.0)
        assert result == 0.0

    def test_monotonic_never_decreases(self, integrator):
        """Total should never decrease."""
        integrator._state["solar"] = (10.0, time.monotonic() - 30, 500.0)
        result = integrator.integrate("solar", 500.0)
        assert result >= 10.0


class TestGapDetection:
    def test_large_gap_preserves_total(self, integrator):
        """Gap >7min should skip integration but keep total."""
        integrator._state["solar"] = (5.0, time.monotonic() - 500, 1000.0)
        result = integrator.integrate("solar", 2000.0)
        assert result == 5.0  # Total unchanged

    def test_small_delta_skipped(self, integrator):
        """Updates faster than 100ms are skipped."""
        integrator._state["solar"] = (5.0, time.monotonic(), 1000.0)
        result = integrator.integrate("solar", 1000.0)
        assert result == 5.0  # No change


class TestJumpDetection:
    def test_large_jump_uses_minimum(self, integrator):
        """When power changes >50%, use min(last, current)."""
        # 100W → 1000W is a >50% jump
        integrator._state["solar"] = (0.0, time.monotonic() - 30, 100.0)
        result = integrator.integrate("solar", 1000.0)
        # Should use min(100, 1000) = 100W, not average
        # 100W * 30s / 3600000 ≈ 0.000833 kWh
        assert result is not None
        assert result < 0.005  # Much less than if it used average


class TestSetTotal:
    def test_set_total_monotonic(self, integrator):
        """set_total only accepts higher values."""
        integrator._state["batt"] = (100.0, time.monotonic(), 0.0)
        integrator.set_total("batt", 50.0)  # Lower → ignored
        assert integrator.get_total("batt") == 100.0

    def test_set_total_higher_accepted(self, integrator):
        """set_total accepts higher values."""
        integrator._state["batt"] = (100.0, time.monotonic(), 0.0)
        integrator.set_total("batt", 150.0)
        assert integrator.get_total("batt") == 150.0

    def test_set_total_new_metric(self, integrator):
        """set_total creates new metric."""
        integrator.set_total("new", 42.0)
        assert integrator.get_total("new") == 42.0


class TestPersistence:
    def test_save_and_load(self, state_file):
        """State survives across instances (explicit load_state call)."""
        i1 = EnergyIntegrator(state_file)
        i1._state["solar"] = (123.456, time.monotonic(), 500.0)
        i1._save_state()

        i2 = EnergyIntegrator(state_file)
        i2.load_state()
        assert i2.get_total("solar") == pytest.approx(123.456)

    def test_missing_file_starts_empty(self, tmp_path):
        """Missing state file starts with empty state."""
        i = EnergyIntegrator(str(tmp_path / "nonexistent.json"))
        i.load_state()
        assert i.get_total("solar") is None

    def test_corrupt_file_starts_empty(self, tmp_path):
        """Corrupt state file starts with empty state."""
        f = tmp_path / "corrupt.json"
        f.write_text("not json{{{")
        i = EnergyIntegrator(str(f))
        i.load_state()
        assert i.get_total("solar") is None

    def test_epoch_timestamp_migration(self, state_file):
        """State files with epoch timestamps (pre-v1.5.1) are migrated on load."""
        import json
        from pathlib import Path

        epoch_ts = 1748000000.0  # Unix epoch from 2025
        data = {"solar": [123.456, epoch_ts, 500.0]}
        Path(state_file).write_text(json.dumps(data))

        integrator = EnergyIntegrator(state_file)
        integrator.load_state()

        # Total must be preserved
        assert integrator.get_total("solar") == pytest.approx(123.456)

        # Timestamp must be migrated to monotonic range (not epoch)
        _, ts, _ = integrator._state["solar"]
        assert ts < 1e9  # monotonic timestamps are never in epoch range

        # Integration must work normally after migration
        integrator._state["solar"] = (123.456, time.monotonic() - 30, 500.0)
        result = integrator.integrate("solar", 1000.0)
        assert result is not None
        assert result > 123.456

    def test_monotonic_clock_reset_after_reboot(self, state_file):
        """State file from before host reboot has last_ts > current monotonic.

        After a host reboot, time.monotonic() restarts near zero while the
        state file retains the old (higher) timestamp. Without migration,
        delta_t_s becomes negative and the integrator is stuck forever.
        """
        from pathlib import Path

        old_uptime_ts = 86400.0  # 24h of uptime before reboot
        data = {"solar": [50.0, old_uptime_ts, 1000.0]}
        Path(state_file).write_text(json.dumps(data))

        # Simulate post-reboot: monotonic is ~10s (fresh boot)
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=10.0,
        ):
            integrator = EnergyIntegrator(state_file)
            integrator.load_state()

        # Total must be preserved
        assert integrator.get_total("solar") == pytest.approx(50.0)

        # Timestamp must be reset to current monotonic, not the stale value
        _, ts, _ = integrator._state["solar"]
        assert ts == pytest.approx(10.0)

    def test_integration_works_after_monotonic_reset(self, state_file):
        """After monotonic reset migration, integration resumes normally."""
        from pathlib import Path

        old_uptime_ts = 86400.0
        data = {"solar": [50.0, old_uptime_ts, 1000.0]}
        Path(state_file).write_text(json.dumps(data))

        # Load with post-reboot monotonic
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=100.0,
        ):
            integrator = EnergyIntegrator(state_file)
            integrator.load_state()

        # First integrate call after load: 30s later at 1000W
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=130.0,
        ):
            result = integrator.integrate("solar", 1000.0)

        # 1000W * 30s / 3_600_000 = 0.00833 kWh added to 50.0
        assert result is not None
        assert result == pytest.approx(50.00833, abs=0.001)

    def test_normal_monotonic_not_affected_by_reset_check(self, state_file):
        """Normal case: last_ts < now is not touched by the reboot migration."""
        from pathlib import Path

        data = {"solar": [50.0, 100.0, 1000.0]}
        Path(state_file).write_text(json.dumps(data))

        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=200.0,
        ):
            integrator = EnergyIntegrator(state_file)
            integrator.load_state()

        # Timestamp preserved as-is (100.0), not reset to now (200.0)
        _, ts, _ = integrator._state["solar"]
        assert ts == pytest.approx(100.0)


class TestPlausibilityBounds:
    """A single implausible reading must not freeze a counter forever (#88).

    Totals are monotonic, so a value that is wrong by orders of magnitude
    becomes a floor no correct reading can ever pass again. The bounds catch
    that class of fault at every entry point.
    """

    # The value a PowerOcean Plus actually reported against a real 2,638.98 kWh.
    POISONED_KWH = 54_501_280.65

    def test_set_total_rejects_implausible_value(self, integrator):
        integrator.set_total("solar", 2_638.98)
        integrator.set_total("solar", self.POISONED_KWH)
        assert integrator.get_total("solar") == pytest.approx(2_638.98)

    def test_set_total_rejects_infinity_and_nan(self, integrator):
        integrator.set_total("solar", 100.0)
        integrator.set_total("solar", float("inf"))
        integrator.set_total("solar", float("nan"))
        assert integrator.get_total("solar") == pytest.approx(100.0)

    def test_set_total_rejects_negative(self, integrator):
        integrator.set_total("solar", 100.0)
        integrator.set_total("solar", -5.0)
        assert integrator.get_total("solar") == pytest.approx(100.0)

    def test_set_total_accepts_a_genuinely_large_lifetime_counter(self, integrator):
        """500 MWh is a plausible decade of production and must pass."""
        integrator.set_total("solar", 500_000.0)
        assert integrator.get_total("solar") == pytest.approx(500_000.0)

    def test_integrate_rejects_implausible_power(self, integrator):
        """A 1e28 W reading was observed while the Plus payload was misdecoded."""
        integrator._state["solar"] = (12.5, 1000.0, 500.0)
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=1030.0,
        ):
            result = integrator.integrate("solar", 1e28)
        assert result == pytest.approx(12.5)
        assert integrator.get_total("solar") == pytest.approx(12.5)

    def test_integrate_rejects_infinite_power(self, integrator):
        integrator._state["solar"] = (12.5, 1000.0, 500.0)
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=1030.0,
        ):
            result = integrator.integrate("solar", float("inf"))
        assert result == pytest.approx(12.5)

    def test_integrate_still_accepts_a_normal_reading(self, integrator):
        """The guard must not disturb ordinary integration."""
        integrator._state["solar"] = (0.0, 1000.0, 1000.0)
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=1030.0,
        ):
            result = integrator.integrate("solar", 1000.0)
        assert result == pytest.approx(0.00833, abs=0.001)

    def test_load_state_discards_a_poisoned_total(self, state_file):
        from pathlib import Path

        Path(state_file).write_text(
            json.dumps({"solar": [self.POISONED_KWH, 100.0, 0.0], "home": [42.0, 100.0, 0.0]})
        )
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=200.0,
        ):
            integrator = EnergyIntegrator(state_file)
            integrator.load_state()

        assert integrator.get_total("solar") is None
        assert integrator.get_total("home") == pytest.approx(42.0)

    def test_poisoned_installation_recovers_after_restart(self, state_file):
        """The end-to-end case from #88, without hand-editing .storage.

        An installation carrying the bad total restarts, the value is dropped,
        and the next correct reading is accepted instead of being discarded
        for being lower.
        """
        from pathlib import Path

        Path(state_file).write_text(
            json.dumps({"solar_energy_kwh": [self.POISONED_KWH, 100.0, 0.0]})
        )
        with patch(
            "ecoflow_energy.ecoflow.energy_integrator.time.monotonic",
            return_value=200.0,
        ):
            integrator = EnergyIntegrator(state_file)
            integrator.load_state()
            integrator.set_total("solar_energy_kwh", 2_638.98)

        assert integrator.get_total("solar_energy_kwh") == pytest.approx(2_638.98)

    def test_repeated_rejection_warns_once(self, integrator, caplog):
        """A device stuck on a bad reading must not fill the log."""
        import logging

        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                integrator.set_total("solar", self.POISONED_KWH)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
